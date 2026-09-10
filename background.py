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
from sqlalchemy import or_, text, update

from config import (PAGE_DOWNLOAD_INTERVAL, PREFETCH_EVICT_UNREFRESHED_AFTER,
                    PREFETCH_REFRESH_ABORT_STREAK, PREFETCH_REFRESH_BACKOFF,
                    PREFETCH_REFRESH_BATCH, PREFETCH_REFRESH_FORCE_DONE)
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


def _is_user_owned(db, pixiv_id: int) -> bool:
    """用户"拥有"这件作品：在收藏夹里，或有用户操作类下载日志。

    这类作品不能当缓存垃圾清掉：收藏=明确意图；下载日志（含失败/取消待重试）
    =用户点过下载。逐条查询而非整轮快照，避免"循环中新增收藏仍可能被删"的窗口。
    只认用户操作类 action —— `prefetch_deleted` 是缓存清理自己写的，不算。
    """
    if db.query(CollectionItem).filter(CollectionItem.pixiv_id == pixiv_id).first():
        return True
    return db.query(DownloadLog).filter(
        DownloadLog.pixiv_id == pixiv_id,
        DownloadLog.action.in_(('start', 'failed', 'cancelled', 'done', 'deleted')),
    ).first() is not None


def _prefetch_refresh_bookmarks(max_items: int = PREFETCH_REFRESH_BATCH) -> None:
    """入口：跑一轮最终收藏数刷新，并把结构化统计写入 `_prefetch_state['refresh_stats']`。

    统计无论正常结束、无候选提前返回还是中途中止都要落盘（供 /api/prefetch/status
    与设置页展示"这轮刷新了什么"），故用 try/finally 包裹；具体逻辑见
    `_refresh_bookmarks_pass`。
    """
    stats = {
        'processed': 0,          # 本轮实际发起的详情请求数
        'ok': 0,                 # 成功拿到详情并写入收藏数
        'deleted_low': 0,        # 其中因最终收藏数 < 10 被删除
        'deleted_dead': 0,       # 永久失败（404/删除类）被删除
        'kept_dead': 0,          # 永久失败但已下载/已收藏 → 保留并标记完成
        'failed_transient': 0,   # 暂时性失败 → 写退避标记
        'failed_global': 0,      # 限流/连接错误（全局性，不写标记）
        'force_done': 0,         # 失败超期被强制标记完成
        'aborted': '',           # '' | 'rate_limit' | 'auth' | 'cookie_missing'
        'at': None,
    }
    try:
        _refresh_bookmarks_pass(max_items, stats)
    finally:
        stats['at'] = datetime.now(timezone.utc).isoformat()
        _prefetch_state['refresh_stats'] = dict(stats)


def _refresh_bookmarks_pass(max_items: int, stats: dict) -> None:
    """最终收藏数刷新：入库满 1 天、尚未最终刷新的预取作品，拉详情更新收藏数一次。

    规则（用户需求）：
    - 拉取的作品给足一天时间涨收藏，之后只刷新这一次（prefetch_refresh_at 标记）；
    - 刷新后的最终收藏数 < 10 且未下载未收藏的，直接从缓存删除；
    - 已下载 / 已收藏的不删（保护）。

    失败处理（2026-09-08 持久化状态机，见 docs/superpowers/specs/
    2026-09-08-prefetch-refresh-retry-backoff-design.md）：
    - 暂时性失败（详情返回 None）→ 写 refresh_failed_at，退避期内不再入选，
      防止永久失败的死作品每轮占满名额（head-of-line blocking）；
    - 永久失败（DEAD_DETAIL：404 / 删除类报错）→ 未下载未收藏的当场删除出清，
      已下载 / 已收藏的标记刷新完成、保留作品；
    - 失败超过 PREFETCH_REFRESH_FORCE_DONE → 强制标记完成、退出刷新队列，
      交由容量清理按低收藏优先淘汰（防"未刷新"积压单独顶破容量上限）；
    - 全局性失败（限流/连接错误，`RETRYABLE_GLOBAL_DETAIL`）不写退避标记，
      连续 PREFETCH_REFRESH_ABORT_STREAK 条即中止本轮（限流是账户级状态）；
    - 认证失效（`PixivAuthError`）/ Cookie 缺失只中止本轮、不写标记，且**不冒泡**
      —— 否则 `_prefetch_loop` 的容量清理会被跳过、上限失效。
    每轮预取执行一次，最多处理 max_items 条，避免单轮耗时过长。
    """
    import app  # 延迟导入读取 app.build_pixiv_session：tests monkeypatch('app.build_pixiv_session')
    now = datetime.now(timezone.utc)
    deadline = now - timedelta(days=1)
    backoff_before = now - timedelta(seconds=PREFETCH_REFRESH_BACKOFF)
    force_done_before = now - timedelta(seconds=PREFETCH_REFRESH_FORCE_DONE)

    fav_ids: set[int] = set()
    pids: list[int] = []
    with get_session() as db:
        # 长期失败兜底先行：失败超过阈值仍未成功 → 强制标记完成、退出刷新队列
        #（先处理再查候选，否则本轮还会把 aged 行选进候选白跑一次详情请求）
        aged = db.query(Illust).filter(
            Illust.prefetch_source == 1,
            Illust.prefetch_refresh_at.is_(None),
            Illust.refresh_failed_at.isnot(None),
            Illust.refresh_failed_at < force_done_before,
        ).order_by(Illust.created_at.asc()).limit(max_items).all()
        for illust in aged:
            illust.prefetch_refresh_at = now
            illust.refresh_failed_at = None
            stats['force_done'] += 1
            logger.info(
                f'[prefetch] 刷新失败超过 {PREFETCH_REFRESH_FORCE_DONE // 86400} 天，'
                f'强制标记完成 {illust.pixiv_id}')
        if aged:
            safe_commit(db)

        candidates = db.query(Illust).filter(
            Illust.prefetch_source == 1,
            Illust.prefetch_refresh_at.is_(None),
            Illust.created_at < deadline,
            or_(Illust.refresh_failed_at.is_(None),
                Illust.refresh_failed_at < backoff_before),
        ).order_by(Illust.created_at.asc()).limit(max_items).all()
        if candidates:
            pids = [c.pixiv_id for c in candidates]
        if not candidates:
            return

    # 网络请求放在 DB session 外
    session = None
    try:
        try:
            session = app.build_pixiv_session()
            global_fail_streak = 0
            for pid in pids:
                stats['processed'] += 1
                detail = fetcher._get_illust_detail(
                    session, pid, limiter=fetcher._fill_limiter, return_dead=True)
                if detail is fetcher.RETRYABLE_GLOBAL_DETAIL:
                    # 限流（403/429）或连接错误是账户级/环境级状态，不是该作品的
                    # 问题：不写退避标记（否则 Cookie/网络恢复后还要白等 24h），
                    # 连续达到阈值即中止本轮，避免把整队列刷上失败标记。
                    stats['failed_global'] += 1
                    global_fail_streak += 1
                    if global_fail_streak >= PREFETCH_REFRESH_ABORT_STREAK:
                        stats['aborted'] = 'rate_limit'
                        logger.warning(
                            f'[prefetch] 连续 {global_fail_streak} 条详情请求遭遇限流/连接失败，'
                            f'中止本轮最终收藏数刷新（不写退避标记，下轮重试）')
                        break
                    continue
                global_fail_streak = 0
                now = datetime.now(timezone.utc)
                with get_session() as db:
                    illust = db.query(Illust).filter(Illust.pixiv_id == pid).first()
                    if not illust or illust.prefetch_refresh_at is not None:
                        continue
                    protected = (illust.download_status in ('done', 'downloading')
                                 or illust.local_paths_list
                                 or _is_user_owned(db, pid))
                    if detail is fetcher.DEAD_DETAIL:
                        if not protected:
                            _remove_pids_from_search_caches(db, [pid])
                            db.add(DownloadLog(
                                pixiv_id=pid, action='prefetch_deleted',
                                message='预取永久失败清理（已删除/非公開）'))
                            db.delete(illust)
                            stats['deleted_dead'] += 1
                            logger.info(f'[prefetch] 永久失败（已删除/非公開），删除缓存作品 {pid}')
                            safe_commit(db)
                            continue
                        # 已下载/已收藏：保留作品，标记完成退出刷新队列
                        illust.prefetch_refresh_at = now
                        illust.refresh_failed_at = None
                        stats['kept_dead'] += 1
                        safe_commit(db)
                        logger.info(f'[prefetch] 永久失败但已下载/收藏，保留并标记完成 {pid}')
                        continue
                    if detail is None:
                        # 暂时性失败：写退避时间戳，backoff 期内不再尝试
                        illust.refresh_failed_at = now
                        stats['failed_transient'] += 1
                        safe_commit(db)
                        continue
                    bookmark_count = detail.get('bookmark_count', 0)
                    illust.bookmark_count = bookmark_count
                    illust.bookmark_updated_at = now
                    illust.prefetch_refresh_at = now
                    illust.refresh_failed_at = None
                    stats['ok'] += 1
                    if bookmark_count < 10 and not protected:
                        _remove_pids_from_search_caches(db, [pid])
                        db.delete(illust)
                        stats['deleted_low'] += 1
                        logger.info(f'[prefetch] 最终收藏数 {bookmark_count} < 10，删除缓存作品 {pid}')
                    safe_commit(db)
        except fetcher.PixivAuthError as e:
            # 认证失效是全局状态：中止本轮、不写退避标记（Cookie 修好后自动继续）。
            # 关键是别让异常冒泡 —— 否则 _prefetch_loop 里的容量清理会被跳过，上限失效。
            stats['aborted'] = 'auth'
            logger.error(f'[prefetch] 认证失效，本轮最终收藏数刷新中止（未写退避标记）: {e}')
        except FileNotFoundError as e:
            stats['aborted'] = 'cookie_missing'
            logger.error(f'[prefetch] Cookie 文件缺失，本轮最终收藏数刷新中止: {e}')
    finally:
        if session is not None:
            session.close()


def reset_prefetch_refresh(tag: str | None = None, pixiv_id: int | None = None) -> int:
    """把预取作品重新放回"最终收藏数"刷新队列（清空刷新完成与失败退避标记）。

    用途：Cookie 权限修复后救回被 14 天强制完成或永久失败退避的作品；或让某个
    标签的收藏数重新拉一遍。返回受影响条数。必须指定范围（tag 或 pixiv_id），
    避免误伤全库；作品是否再次成功仍由下一轮刷新的真实请求决定。
    """
    with get_session() as db:
        if pixiv_id is not None:
            where, params = 'illusts.pixiv_id = :pid', {'pid': pixiv_id}
        elif tag:
            row = db.query(SearchCache).filter(SearchCache.tag == tag).first()
            if row is None:
                return 0
            try:
                ids = json.loads(row.illust_ids) if row.illust_ids else []
            except (json.JSONDecodeError, TypeError):
                ids = []
            ids = [i for i in ids if isinstance(i, int)]
            if not ids:
                return 0
            # json_each 下推整个 id 数组（单个绑定参数）：拼 IN 在万级 id 时
            # 会生成上万个绑定参数，超 SQLite 变量上限（同 helpers._pid_filter）
            where = 'illusts.pixiv_id IN (SELECT value FROM json_each(:ids))'
            params = {'ids': json.dumps(ids)}
        else:
            return 0
        result = db.execute(text(
            'UPDATE illusts SET prefetch_refresh_at = NULL, refresh_failed_at = NULL '
            f'WHERE prefetch_source = 1 AND {where} '
            'AND (prefetch_refresh_at IS NOT NULL OR refresh_failed_at IS NOT NULL)'
        ), params)
        count = result.rowcount or 0
        if count:
            safe_commit(db)
        return count


def _naive_utc(value):
    """统一成 naive-UTC 再比较：SQLAlchemy SQLite 的 DATETIME 取出来是 naive，
    而内存里新建的对象可能是 aware；直接比较会抛 offset-naive/aware 错误。"""
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _prefetch_capacity_cleanup() -> None:
    """容量清理：超出上限时按"已刷新优先、低收藏优先"淘汰，保证上限压得住。

    三层（都跳过已下载/下载中/用户拥有过的作品）：
    1. **已最终刷新**的作品——收藏数信号可信，优先淘汰；
    2. **未刷新但推不动**的作品（刷新失败过，或入库超过
       PREFETCH_EVICT_UNREFRESHED_AFTER）——刷新队列已证明它刷不出来，别占容量；
    3. **兜底**：其余未刷新作品——只有前两层不够时才动，保证"入库 > 刷新吞吐"
       时上限也不会被顶破（这是**不靠暂停入库**也能维持容量的关键）。

    层内排序：收藏数低优先，并列时更早上传的优先。层 2/3 用的是入库快照收藏数，
    偏保守的用法是"宁可删新入的低收藏作品，也不删老的已刷新作品"。
    """
    with get_session() as db:
        count = db.query(Illust).filter(Illust.prefetch_source == 1).count()
        max_illusts = _prefetch_state['max_illusts']
        if count <= max_illusts:
            return
        need_free = count - max_illusts

        fav_ids = {c.pixiv_id for c in db.query(CollectionItem.pixiv_id).all()}
        # 用户操作过下载的作品（含失败/取消待重试）不当缓存垃圾；缓存清理自己写的
        # prefetch_deleted 不算（否则同一 pid 再次入库后会获得"永久保护"）
        dl_pids = {
            p[0] for p in db.query(DownloadLog.pixiv_id).filter(
                DownloadLog.action.in_(('start', 'failed', 'cancelled', 'done', 'deleted')),
            ).distinct().all()
        }
        evict_cutoff = _naive_utc(datetime.now(timezone.utc)) - timedelta(
            seconds=PREFETCH_EVICT_UNREFRESHED_AFTER)

        tiers: list[list[Illust]] = [[], [], []]
        for i in db.query(Illust).filter(Illust.prefetch_source == 1).all():
            if i.download_status in ('done', 'downloading') or i.local_paths_list:
                continue
            if i.pixiv_id in fav_ids or i.pixiv_id in dl_pids:
                continue
            if i.prefetch_refresh_at is not None:
                tiers[0].append(i)
            elif i.refresh_failed_at is not None \
                    or (_naive_utc(i.created_at) or evict_cutoff) < evict_cutoff:
                tiers[1].append(i)
            else:
                tiers[2].append(i)

        # 收藏数低优先删除，并列时更早上传的优先
        ordered: list[Illust] = []
        for tier in tiers:
            if len(ordered) >= need_free:
                break
            tier.sort(key=lambda x: (x.bookmark_count, x.upload_date or datetime.min))
            ordered.extend(tier[:need_free - len(ordered)])
        to_delete = [c.pixiv_id for c in ordered]
        if not to_delete:
            return
        # 统计必须在删除前算：bulk delete 之后 ORM 对象已失效，再读属性会抛 ObjectDeletedError
        refreshed_count = sum(1 for c in ordered if c.prefetch_refresh_at is not None)

        _remove_pids_from_search_caches(db, to_delete)
        safe_commit(db)

        db.query(Illust).filter(Illust.pixiv_id.in_(to_delete)).delete(synchronize_session=False)
        safe_commit(db)
        logger.info(
            f'[prefetch] 容量清理: 删除 {len(to_delete)} 条低收藏预取作品'
            f'（已刷新 {refreshed_count} / 未刷新 {len(to_delete) - refreshed_count}）')


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
            # 入库永不停：容量由 _prefetch_capacity_cleanup 的三层淘汰压住
            #（"暂停入库"的方案已否决——用户要的是持续入库 + 更狠的清理）
            for tag in tags:
                _prefetch_one_tag(tag)
            # 先刷新最终收藏数（满 1 天的作品），再按最终收藏数做容量清理
            #（已刷新的优先淘汰，不够时才会动未刷新的）
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


def _rescue_commit(db, pixiv_id: int, *, stage: str) -> bool:
    """提交下载状态变更；失败时不让作品卡在 downloading，返回是否提交成功。

    `safe_commit` 的语义是失败即 rollback + 原样抛出（不做内部重试）。若**终态**
    写入（done / failed / 无原图）提交失败，回滚会让作品永远停在 'downloading'：
    `_reset_stuck_downloads` 只在启动时跑，运行期没有任何自愈路径 —— 用户既不能
    重新触发下载（trigger 直接返回"下载中"），进度条也永远不动。

    失败后尽力把**仍是 downloading** 的行复位为 failed 并留痕；复位再失败只记
    日志、不冒泡（数据库确实不可写时任何写入都救不了，等重启自愈）。复位用条件
    更新，避免覆盖已由别的路径（如 reset 抢先置空）写好的状态。
    """
    try:
        safe_commit(db)
        return True
    except Exception as e:
        logger.error(f'下载状态提交失败 {pixiv_id}（{stage}）：{e}')
    try:
        cleared = db.execute(
            update(Illust)
            .where(Illust.pixiv_id == pixiv_id,
                   Illust.download_status == 'downloading')
            .values(download_status='failed')
        ).rowcount
        if not cleared:
            # 行已不在 downloading（例如 reset 抢先置空）：没有卡死状态需要救，
            # 不要覆盖别人的结果，也不写误导性的失败日志。
            db.rollback()
            return False
        db.add(DownloadLog(pixiv_id=pixiv_id, action='failed',
                           message=f'状态写入失败（{stage}），已复位为下载失败'))
        safe_commit(db)
    except Exception as e:
        logger.error(f'下载状态复位失败 {pixiv_id}（{stage}）：{e}')
        try:
            db.rollback()
        except Exception:
            pass
        return False
    return False


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
            if not _rescue_commit(db, pixiv_id, stage='开始下载'):
                # 起始状态没落库：不要继续下载。结尾的 done 写入要求行仍是
                # downloading（见下方 CAS），写不进去只会白下载一轮再把文件删掉。
                return

            urls = illust.original_urls_list or []
            if not urls:
                # 无原图来源（详情未拉取到）：不能把"空下载"固化为 done，
                # 否则该作品将永远不再重试且磁盘无文件。
                illust.download_status = None
                db.add(DownloadLog(pixiv_id=pixiv_id, action='failed',
                                   message='无原图地址，跳过下载（请刷新详情后重试）'))
                _rescue_commit(db, pixiv_id, stage='无原图地址')
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
                    _rescue_commit(db, pixiv_id, stage=f'下载失败（第 {i} 页）')
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
                total_size = sum(os.path.getsize(p) for p in local_paths if os.path.isfile(p))
                # 状态写入用条件更新（CAS）：只有仍是 downloading 的行才允许固化为
                # done。reset 会在"最后一次取消检查"之后、本次写入之前把状态置空并
                # 删掉文件；无条件赋值会留下 DB=done 而磁盘无文件的幻影状态 —— 图库
                # 显示已下载、点开全 404，trigger 被 done 挡回、reset 被状态守卫挡回，
                # 用户只剩"删稿重下"一条路。
                claimed = db.execute(
                    update(Illust)
                    .where(Illust.pixiv_id == pixiv_id,
                           Illust.download_status == 'downloading')
                    .values(download_status='done',
                            local_paths=json.dumps(local_paths, ensure_ascii=False),
                            downloaded_at=datetime.now(timezone.utc),
                            file_size=total_size)
                ).rowcount
                if claimed:
                    db.add(DownloadLog(pixiv_id=pixiv_id, action='done', message=f'下载完成: {len(local_paths)} 个文件, {total_size} 字节'))
                else:
                    # 重置已抢先接管（状态不再是 downloading）：不固化 done，清掉
                    # 本轮文件并留痕，避免把不存在的下载报成成功。
                    for p in local_paths:
                        try:
                            os.remove(p)
                        except OSError:
                            pass  # reset 可能已删除这些文件
                    try:
                        os.rmdir(work_dir)
                    except OSError:
                        pass
                    db.add(DownloadLog(pixiv_id=pixiv_id, action='cancelled',
                                       message=f'重置已抢先完成, 放弃本轮 {len(local_paths)} 个文件'))
            _rescue_commit(db, pixiv_id, stage='下载结束')
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