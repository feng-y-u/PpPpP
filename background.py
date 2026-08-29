# ── 后台线程与下载引擎 ──
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import fetcher
import helpers
import runtime
from sqlalchemy import text

from config import PAGE_DOWNLOAD_INTERVAL
from fetcher import build_pixiv_session, fetch_following
from helpers import _get_download_dir, _extract_ext
from models import (get_session, Illust, CollectionItem, SearchCache,
                    DownloadLog, safe_commit)
from runtime import (_auto_follow_state, _auto_follow_stop, _prefetch_state,
                     _queued_downloads, _download_progress,
                     download_cancellations, download_executor)

logger = logging.getLogger(__name__)


# ── 启动重置 ──
def _reset_stuck_downloads() -> None:
    """启动时重置上次崩溃/重启遗留下的 downloading 状态。"""
    with get_session() as db:
        stuck = db.query(Illust).filter(Illust.download_status == 'downloading').all()
        if not stuck:
            return
        for illust in stuck:
            work_dir = _get_download_dir(illust.pixiv_id)
            if os.path.isdir(work_dir):
                for f in os.listdir(work_dir):
                    try:
                        os.remove(os.path.join(work_dir, f))
                    except OSError:
                        pass
                try:
                    os.rmdir(work_dir)
                except OSError:
                    pass
            illust.download_status = None
            db.add(DownloadLog(pixiv_id=illust.pixiv_id, action='failed',
                               message='app 重启，下载任务自动重置'))
        safe_commit(db)
        logger.info(f'重置了上次会话留下的 {len(stuck)} 个卡死下载')


def _reset_stuck_prefetch() -> None:
    """启动时重置上次崩溃/重启遗留下的 fetching 状态。

    fetching 只能由本进程的预取线程设置，进程重启后必然是残留；
    否则 _prefetch_one_tag 的抢占逻辑（status='fetching' 时 rowcount=0）
    会让该标签被永远跳过，预取从此不再执行（缓存永不更新）。
    """
    with get_session() as db:
        stuck = db.query(SearchCache).filter(SearchCache.status == 'fetching').all()
        if not stuck:
            return
        for sc in stuck:
            sc.status = 'done'
            sc.error = '上次预取被中断，已重置'
        safe_commit(db)
        logger.info(f'[prefetch] 重置了 {len(stuck)} 个卡死的预取标签（fetching → done）')


# ── 自动关注后台任务 ──
def _auto_follow_worker() -> None:
    while not _auto_follow_stop.is_set():
        interval = _auto_follow_state['interval']
        if interval <= 0:
            _auto_follow_stop.wait(30)
            continue
        try:
            collected = []
            page = 1
            while page <= 10:
                results, has_more = fetch_following(page=page)
                if not results:
                    break
                collected.extend(results)
                if not has_more:
                    break
                page += 1
                time.sleep(1)

            if not collected:
                _auto_follow_stop.wait(interval)
                continue

            seen = set()
            unique = []
            for r in collected:
                pid = r['pixiv_id']
                if pid not in seen:
                    seen.add(pid)
                    unique.append(r)

            pixiv_ids = [r['pixiv_id'] for r in unique]
            with get_session() as db:
                existing_ids = {i.pixiv_id for i in db.query(Illust.pixiv_id).filter(Illust.pixiv_id.in_(pixiv_ids)).all()}

            new_illusts = []
            download_pids: list[int] = []
            for r in unique:
                if r['pixiv_id'] in existing_ids:
                    continue
                illust = Illust(
                    pixiv_id=r['pixiv_id'], title=r['title'],
                    user_id=r['user_id'], user_name=r['user_name'],
                    page_count=r['page_count'], bookmark_count=r['bookmark_count'],
                    thumb_url=r['thumb_url'], upload_date=r['upload_date'],
                )
                illust.tags_list = r.get('tags', [])
                illust.original_urls_list = r.get('original_urls', [])
                new_illusts.append(illust)
                if _auto_follow_state['auto_download'] and illust.original_urls_list:
                    download_pids.append(r['pixiv_id'])

            if new_illusts:
                with get_session() as db:
                    db.add_all(new_illusts)
                    safe_commit(db)
            # 先 commit 再提交下载任务：_download_illust 需要能查到已持久化的
            # Illust 行，否则会在"行不存在"时静默跳过下载（竞态）。
            for pid in download_pids:
                _queued_downloads.add(pid)
                download_executor.submit(_download_illust, pid)
            new_count = len(new_illusts)
            _auto_follow_state['last_check'] = datetime.now(timezone.utc).isoformat()
            _auto_follow_state['last_count'] = new_count
            if new_count:
                logger.info(f'自动关注：发现 {new_count} 件新作品')
        except Exception as e:
            logger.error(f'自动关注出错：{e}')
        _auto_follow_stop.wait(interval)


# ── 搜索预取后台任务 ──
def _prefetch_one_tag(tag: str) -> None:
    """预取单个标签：搜索并缓存作品 ID，将 Illust 标记为预取来源。"""
    import app  # 延迟导入读取 app.search_by_tag：tests monkeypatch('app.search_by_tag')，
    #             from fetcher import 的独立绑定看不到该补丁（与 middleware._is_authed 同款先例）
    all_ids: list[int] = []
    try:
        with get_session() as db:
            row = db.query(SearchCache).filter(SearchCache.tag == tag).first()
            if row is None:
                row = SearchCache(tag=tag, status='fetching')
                db.add(row)
                safe_commit(db)
            else:
                # 原子抢占 fetching 状态，避免并发重复预取同一标签
                updated = db.execute(
                    text('UPDATE search_cache SET status = :s WHERE tag = :t AND status != :s'),
                    {'s': 'fetching', 't': tag},
                ).rowcount
                safe_commit(db)
                if updated == 0:
                    # 已被其他线程抢占，正在预取中
                    return

        for page in range(1, _prefetch_state['pages'] + 1):
            results, has_more = app.search_by_tag(
                tag, min_bookmarks=1, page=page,
                sort_order='date_d', r18_mode='all', tag_mode='or',
            )
            page_ids = [r.get('pixiv_id') for r in results if r.get('pixiv_id')]
            all_ids.extend(page_ids)
            if page_ids:
                # search_by_tag 已写入 Illust 行，这里只翻转 prefetch_source 标记；
                # 已下载的作品不标记，避免计入容量却永远无法清理
                with get_session() as db:
                    existing = db.query(Illust).filter(Illust.pixiv_id.in_(page_ids)).all()
                    for illust in existing:
                        if not illust.download_status:
                            illust.prefetch_source = 1
                    safe_commit(db)
            if not has_more:
                break

        with get_session() as db:
            row = db.query(SearchCache).filter(SearchCache.tag == tag).first()
            if row:
                # 累积合并：新结果在前，旧作品去重保留在后（条数只增不减，由容量清理兜底）
                old_ids = json.loads(row.illust_ids) if row.illust_ids else []
                seen = set(all_ids)
                merged = list(all_ids) + [pid for pid in old_ids if pid not in seen]
                row.illust_ids = json.dumps(merged, ensure_ascii=False)
                row.cached_at = datetime.now(timezone.utc)
                row.status = 'done'
                row.total = len(merged)
                row.error = ''
                safe_commit(db)
    except Exception as e:
        logger.error(f'[prefetch] 标签 {tag} 预取失败: {e}')
        with get_session() as db:
            row = db.query(SearchCache).filter(SearchCache.tag == tag).first()
            if row:
                row.status = 'error'
                row.error = str(e)
                safe_commit(db)


def _collect_other_tag_pids(db, exclude_tag: str) -> set[int]:
    """收集除 exclude_tag 外所有 SearchCache 标签引用的 pixiv_id 集合。"""
    result: set[int] = set()
    for other in db.query(SearchCache).filter(SearchCache.tag != exclude_tag).all():
        try:
            ids = json.loads(other.illust_ids or '[]')
        except (json.JSONDecodeError, TypeError):
            continue
        for pid in ids:
            if isinstance(pid, int):
                result.add(pid)
    return result


def _remove_pids_from_search_caches(db, pids: list[int]) -> None:
    """从所有 SearchCache 的 illust_ids 中移除指定作品（不 commit）。"""
    pid_set = set(pids)
    if not pid_set:
        return
    for sc in db.query(SearchCache).all():
        try:
            ids = json.loads(sc.illust_ids) if sc.illust_ids else []
        except (json.JSONDecodeError, TypeError):
            continue
        new_ids = [p for p in ids if p not in pid_set]
        if len(new_ids) != len(ids):
            sc.illust_ids = json.dumps(new_ids, ensure_ascii=False)


def _prefetch_refresh_bookmarks(max_items: int = 100) -> None:
    """最终收藏数刷新：入库满 1 天、尚未最终刷新的预取作品，拉详情更新收藏数一次。

    规则（用户需求）：
    - 拉取的作品给足一天时间涨收藏，之后只刷新这一次（prefetch_refresh_at 标记）；
    - 刷新后的最终收藏数 < 10 且未下载未收藏的，直接从缓存删除；
    - 详情拉取失败跳过，等下一轮重试。
    每轮预取执行一次，最多处理 max_items 条，避免单轮耗时过长。
    """
    import app  # 延迟导入读取 app.build_pixiv_session：tests monkeypatch('app.build_pixiv_session')
    deadline = datetime.now(timezone.utc) - timedelta(days=1)
    with get_session() as db:
        candidates = db.query(Illust).filter(
            Illust.prefetch_source == 1,
            Illust.prefetch_refresh_at.is_(None),
            Illust.created_at < deadline,
        ).order_by(Illust.created_at.asc()).limit(max_items).all()
        if not candidates:
            return
        pids = [c.pixiv_id for c in candidates]
        fav_ids = {c.pixiv_id for c in db.query(CollectionItem.pixiv_id).all()}

    # 网络请求放在 DB session 外
    session = app.build_pixiv_session()
    try:
        for pid in pids:
            detail = fetcher._get_illust_detail(session, pid, limiter=fetcher._fill_limiter)
            if detail is None:
                continue
            bookmark_count = detail.get('bookmark_count', 0)
            now = datetime.now(timezone.utc)
            with get_session() as db:
                illust = db.query(Illust).filter(Illust.pixiv_id == pid).first()
                if not illust or illust.prefetch_refresh_at is not None:
                    continue
                illust.bookmark_count = bookmark_count
                illust.bookmark_updated_at = now
                illust.prefetch_refresh_at = now
                protected = (illust.download_status in ('done', 'downloading')
                             or illust.local_paths_list or pid in fav_ids)
                if bookmark_count < 10 and not protected:
                    _remove_pids_from_search_caches(db, [pid])
                    db.delete(illust)
                    logger.info(f'[prefetch] 最终收藏数 {bookmark_count} < 10，删除缓存作品 {pid}')
                safe_commit(db)
    finally:
        session.close()


def _prefetch_capacity_cleanup() -> None:
    """容量清理：超出上限时优先删除最终收藏数最低的未下载未收藏预取作品。

    删除决策只看刷新后的最终收藏数：未完成最终刷新（prefetch_refresh_at 为空）的
    作品暂不参与淘汰，避免用抓取时的快照收藏数误删实际收藏很高的新作品。
    """
    with get_session() as db:
        count = db.query(Illust).filter(Illust.prefetch_source == 1).count()
        max_illusts = _prefetch_state['max_illusts']
        if count <= max_illusts:
            return
        need_free = count - max_illusts

        fav_ids = {c.pixiv_id for c in db.query(CollectionItem.pixiv_id).all()}
        candidates: list[Illust] = []
        for i in db.query(Illust).filter(
                Illust.prefetch_source == 1,
                Illust.prefetch_refresh_at.isnot(None),
        ).all():
            if i.download_status in ('done', 'downloading') or i.local_paths_list:
                continue
            if i.pixiv_id in fav_ids:
                continue
            candidates.append(i)

        # 最终收藏数低优先删除，并列时更早上传的优先
        candidates.sort(key=lambda x: (x.bookmark_count, x.upload_date or datetime.min))
        to_delete = [c.pixiv_id for c in candidates[:need_free]]
        if not to_delete:
            return

        _remove_pids_from_search_caches(db, to_delete)
        safe_commit(db)

        db.query(Illust).filter(Illust.pixiv_id.in_(to_delete)).delete(synchronize_session=False)
        safe_commit(db)
        logger.info(f'[prefetch] 容量清理: 删除 {len(to_delete)} 条低收藏预取作品')


def _prefetch_loop() -> None:
    """后台预取循环：遍历所有 SearchCache 标签，串行预取。"""
    import app  # 延迟导入：tests monkeypatch('app.get_session'/'app._prefetch_capacity_cleanup')
    _prefetch_state['running'] = True
    try:
        try:
            with app.get_session() as db:
                tags = [t[0] for t in db.query(SearchCache.tag).all()]
        except Exception as e:
            # 标签列表查询失败不退出线程，等待下个 interval 重试
            logger.error(f'[prefetch] 读取标签列表失败: {e}')
            return
        if not tags:
            return
        logger.info(f'[prefetch] 开始预取 {len(tags)} 个标签')
        try:
            for tag in tags:
                _prefetch_one_tag(tag)
            # 先刷新最终收藏数（满 1 天的作品），再按最终收藏数做容量清理
            #（仅已完成最终刷新的作品参与淘汰，未刷新的等下一轮）
            _prefetch_refresh_bookmarks()
            app._prefetch_capacity_cleanup()
            _prefetch_state['last_check'] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            # 单轮异常不退出线程，等待下个 interval 重试
            logger.error(f'[prefetch] 循环异常: {e}')
    finally:
        _prefetch_state['running'] = False


def _start_prefetch_thread() -> None:
    """启动预取守护线程。首轮延迟 5 秒，之后按 interval 循环。"""
    import app  # 延迟导入：tests monkeypatch('app.time'/'app.threading'/'app._prefetch_loop')
    interval = _prefetch_state['interval']
    if interval <= 0:
        logger.info('[prefetch] 已禁用（interval=0）')
        return

    def _run() -> None:
        app.time.sleep(5)  # 等 app 完全启动
        while True:
            # 每次迭代读取最新 interval，支持运行时通过 /api/prefetch/config 调整
            interval = _prefetch_state.get('interval') or 0
            if interval <= 0:
                app.time.sleep(60)  # interval=0 时暂停（禁用），每分钟检查一次以便重新启用
                continue
            app._prefetch_loop()
            app.time.sleep(interval)

    app.threading.Thread(target=_run, daemon=True).start()
    logger.info(f'[prefetch] 后台线程已启动，interval={interval}s')


# ── 下载引擎与生命周期 ──
download_locks: dict[int, threading.Lock] = {}
# 保护 download_locks 自身的读写。setdefault 单次调用虽是原子的，但"取出锁"
# 与 finally 里的"删除锁"之间跨越了整个下载过程：若任务 A 在 release 之后、
# pop 之前被抢占，任务 B 会拿到 A 那把已释放的锁并开始下载，A 随后把它 pop
# 掉，任务 C 再进来就拿到一把全新锁 —— 同一作品被并发下载两次。
# 故删除时只删自己那把。
_download_locks_guard = threading.Lock()


def _release_download_lock(pixiv_id: int, lock: threading.Lock) -> None:
    """注销本任务的下载锁。只删自己那把——期间若已被新任务替换，删错会让
    后来者拿到一把不同的锁，同一作品被并发下载两次。"""
    with _download_locks_guard:
        if download_locks.get(pixiv_id) is lock:
            download_locks.pop(pixiv_id, None)


def _download_illust(pixiv_id: int) -> None:
    """后台任务：下载作品的所有原图。"""
    with _download_locks_guard:
        lock = download_locks.setdefault(pixiv_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return  # 正在下载中，跳过
    try:
        if pixiv_id in download_cancellations:
            # 任务被取消/重置后才轮到本线程启动（queued 场景）：不再开始下载。
            # 取消标记由 finally 清理。
            _queued_downloads.discard(pixiv_id)
            return
        _download_progress[pixiv_id] = {'current': 0, 'total': 0}
        session_obj = None
        with get_session() as db:
            illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
            if not illust:
                return

            _queued_downloads.discard(pixiv_id)
            illust.download_status = 'downloading'
            db.add(DownloadLog(pixiv_id=pixiv_id, action='start', message=f'开始下载: {illust.title or pixiv_id}'))
            safe_commit(db)

            urls = illust.original_urls_list or []
            if not urls:
                # 无原图来源（详情未拉取到）：不能把"空下载"固化为 done，
                # 否则该作品将永远不再重试且磁盘无文件。
                illust.download_status = None
                db.add(DownloadLog(pixiv_id=pixiv_id, action='failed',
                                   message='无原图地址，跳过下载（请刷新详情后重试）'))
                safe_commit(db)
                return
            _download_progress[pixiv_id]['total'] = len(urls)
            work_dir = _get_download_dir(pixiv_id)
            os.makedirs(work_dir, exist_ok=True)

            session_obj = build_pixiv_session()

            local_paths = []
            for i, url in enumerate(urls):
                if pixiv_id in download_cancellations:
                    break
                try:
                    ext = _extract_ext(url)
                    filename = f'{pixiv_id}_p{i}.{ext}'
                    filepath = os.path.join(work_dir, filename)

                    resp = session_obj.get(url, timeout=(10, 60), stream=True)
                    resp.raise_for_status()
                    with open(filepath, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            f.write(chunk)
                    local_paths.append(filepath)
                    _download_progress[pixiv_id]['current'] = i + 1

                    if i < len(urls) - 1:
                        time.sleep(PAGE_DOWNLOAD_INTERVAL)
                except Exception as e:
                    logger.error(f'下载失败 {pixiv_id} 第 {i} 页：{e}')
                    for p in local_paths:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                    try:
                        os.rmdir(work_dir)
                    except OSError:
                        pass
                    illust.download_status = 'failed'
                    db.add(DownloadLog(pixiv_id=pixiv_id, action='failed', message=f'下载失败: 第 {i} 页'))
                    safe_commit(db)
                    return

            if pixiv_id in download_cancellations:
                for p in local_paths:
                    try:
                        os.remove(p)
                    except OSError:
                        pass  # reset 可能已删除这些文件
                try:
                    os.rmdir(work_dir)
                except OSError:
                    pass
                illust.download_status = None
                db.add(DownloadLog(pixiv_id=pixiv_id, action='cancelled', message=f'已取消, 删除了 {len(local_paths)} 个已下载文件'))
            else:
                illust.local_paths_list = local_paths
                illust.download_status = 'done'
                illust.downloaded_at = datetime.now(timezone.utc)
                total_size = sum(os.path.getsize(p) for p in local_paths if os.path.isfile(p))
                illust.file_size = total_size
                db.add(DownloadLog(pixiv_id=pixiv_id, action='done', message=f'下载完成: {len(local_paths)} 个文件, {total_size} 字节'))
            safe_commit(db)
    finally:
        if session_obj is not None:
            session_obj.close()  # 释放连接池，防止长驻进程累积 socket
        _download_progress.pop(pixiv_id, None)
        lock.release()
        _release_download_lock(pixiv_id, lock)
        download_cancellations.discard(pixiv_id)
        _queued_downloads.discard(pixiv_id)


_auto_follow_thread: threading.Thread | None = None


def _shutdown_background_threads() -> None:
    """进程退出时优雅停止后台线程（gunicorn worker 退出 / 测试进程结束）。"""
    _auto_follow_stop.set()
    download_executor.shutdown(wait=False)


def start_background_threads() -> None:
    """启动所有后台线程（app.py import 时调用；-w 1 单进程常驻语义）。"""
    global _auto_follow_thread
    if _auto_follow_thread is not None:
        return  # 幂等：防止重复调用启动两个工作者
    _auto_follow_thread = threading.Thread(target=_auto_follow_worker, daemon=True)
    _auto_follow_thread.start()
    _start_prefetch_thread()