from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import platform
import re
import secrets
import threading
import time
import zipfile
import atexit
from base64 import urlsafe_b64decode, urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Callable
from io import BytesIO

import requests
import urllib3
from flask import (
    Flask, jsonify, render_template, request, session,
    send_file, abort, Response, redirect, url_for,
)
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from config import (
    DOWNLOAD_DIR, DOWNLOAD_MAX_WORKERS, PAGE_DOWNLOAD_INTERVAL,
    MAX_BOOKMARKS_DEFAULT, AUTO_FOLLOW_INTERVAL, AUTO_FOLLOW_DOWNLOAD,
    PREFETCH_INTERVAL, PREFETCH_PAGES, PREFETCH_MAX_ILLUSTS,
    MEDIUM_IMAGE_SIZE,
    SETTINGS_PASSWORD, ACCESS_PASSWORD, COOKIE_SECURE,
    ITEMS_PER_PAGE,
)
import config as config_module
from models import init_db, get_session, get_favorite_pids, Illust, DownloadLog, BlockedTag, Collection, CollectionItem, SearchCache, safe_commit
import fetcher
from fetcher import search_by_tag, search_by_user, fetch_following, browse_discovery, build_pixiv_session, _get_illust_detail, _is_r18, PixivAuthError, encode_cursor, decode_cursor, paginated_search, clear_search_cache

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 过滤掉请求头日志，防止 Cookie 泄露
logging.getLogger('werkzeug').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

app = Flask(__name__)

# 反代后还原真实客户端 IP（限流/open-dir 本机判断依赖）；x_proto 供 HTTPS 判定
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

_secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', '.secret_key')
if os.path.exists(_secret_path):
    with open(_secret_path) as f:
        _secret = f.read().strip()
    if not _secret:
        # 空密钥文件（写入中断等残留）：重新生成，避免空 SECRET_KEY
        # 导致会话签名可预测。
        _secret = secrets.token_hex(32)
        with open(_secret_path, 'w') as f:
            f.write(_secret)
        logger.warning('.secret_key 内容为空，已重新生成')
    app.config['SECRET_KEY'] = _secret
else:
    app.config['SECRET_KEY'] = secrets.token_hex(32)
    os.makedirs(os.path.dirname(_secret_path), exist_ok=True)
    with open(_secret_path, 'w') as f:
        f.write(app.config['SECRET_KEY'])
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 最大上传 1MB

# ── Session 安全加固（公网部署基线）──
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'image_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

init_db()


def _get_download_dir(pixiv_id: int) -> str:
    return os.path.join(DOWNLOAD_DIR, str(pixiv_id))


def _page_sort_key(path: str) -> tuple[int, str]:
    """按文件名页号排序：'xxx_p10.png' 应排在 '_p2' 之后（字典序会把 p10 排在 p2 前）。"""
    m = re.search(r'_p(\d+)\.', os.path.basename(path))
    return (int(m.group(1)), path) if m else (10 ** 9, path)


_scan_cache: dict = {'ts': 0.0, 'data': {}}
_SCAN_CACHE_TTL = 10.0  # 图库目录扫描缓存（秒）：避免每页请求全量重扫磁盘


def _scan_local_downloads() -> dict[int, list[str]]:
    """扫描 downloads/ 目录，返回 {pixiv_id: [file_paths]}（带 TTL 缓存）。"""
    now = time.time()
    if now - _scan_cache['ts'] < _SCAN_CACHE_TTL:
        return _scan_cache['data']
    result: dict[int, list[str]] = {}
    if not os.path.isdir(DOWNLOAD_DIR):
        return result
    for entry in os.listdir(DOWNLOAD_DIR):
        subdir = os.path.join(DOWNLOAD_DIR, entry)
        if not os.path.isdir(subdir):
            continue
        try:
            pid = int(entry)
        except ValueError:
            continue
        files = sorted(
            (os.path.join(subdir, f) for f in os.listdir(subdir)
             if os.path.isfile(os.path.join(subdir, f))),
            key=_page_sort_key,
        )
        if files:
            result[pid] = files
    _scan_cache['ts'] = now
    _scan_cache['data'] = result
    return result


def _build_orphan_dicts(pixiv_ids: list[int], local_items: dict[int, list[str]]) -> list[dict]:
    """为不在 DB 的本地文件构建虚拟 illust 字典。"""
    results = []
    for pid in pixiv_ids:
        paths = local_items.get(pid, [])
        if not paths:
            continue
        total_size = sum(os.path.getsize(p) for p in paths if os.path.isfile(p))
        results.append({
            'id': 0,
            'pixiv_id': pid,
            'title': str(pid),
            'user_id': 0,
            'user_name': '',
            'tags': [],
            'page_count': len(paths),
            'bookmark_count': 0,
            'upload_date': None,
            'thumb_url': '',
            'original_urls': [],
            'local_paths': paths,
            'file_count': len(paths),
            'local_urls': [f'/api/image/{pid}/{n}' for n in range(len(paths))],
            'local_dir': os.path.abspath(_get_download_dir(pid)),
            'download_status': 'done',
            'downloaded_at': None,
            'file_size': total_size,
            'is_favorite': False,
            'created_at': None,
        })
    return results


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


_reset_stuck_downloads()


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


_reset_stuck_prefetch()

# ── ⚠ 多进程限制 ─────────────────────────────────────
# 以下状态变量（_auto_follow_state、download_locks、
# download_cancellations、_queued_downloads、_download_progress、
# _prefetch_state）存在于进程内存中。使用多个 gunicorn worker
# （或任何多进程部署）时，每个 worker 拥有自己的副本，
# 因此状态不在 worker 之间共享。worker A 启动的下载
# 对 worker B 不可见。
#
# 要正确支持多 worker，需要共享存储
#（Redis / SQLite KV 表）。在此之前，请使用单 worker 运行：
#   gunicorn -w 1 app:app
# ─────────────────────────────────────────────────────────────────────

# ── 自动关注后台任务 ──
_auto_follow_state = {
    'last_check': None,
    'last_count': 0,
    'interval': AUTO_FOLLOW_INTERVAL,
    'auto_download': AUTO_FOLLOW_DOWNLOAD,
}
_auto_follow_stop = threading.Event()

_prefetch_state = {
    'running': False,
    'last_check': None,
    'interval': PREFETCH_INTERVAL,
    'pages': PREFETCH_PAGES,
    'max_illusts': PREFETCH_MAX_ILLUSTS,
}

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

_auto_follow_thread = threading.Thread(target=_auto_follow_worker, daemon=True)
_auto_follow_thread.start()

# ── 搜索预取后台任务 ──
def _prefetch_one_tag(tag: str) -> None:
    """预取单个标签：搜索并缓存作品 ID，将 Illust 标记为预取来源。"""
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
            results, has_more = search_by_tag(
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
    session = build_pixiv_session()
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
    """容量清理：超出上限时优先删除收藏数最低的未下载未收藏预取作品。"""
    with get_session() as db:
        count = db.query(Illust).filter(Illust.prefetch_source == 1).count()
        max_illusts = _prefetch_state['max_illusts']
        if count <= max_illusts:
            return
        need_free = count - max_illusts

        fav_ids = {c.pixiv_id for c in db.query(CollectionItem.pixiv_id).all()}
        candidates: list[Illust] = []
        for i in db.query(Illust).filter(Illust.prefetch_source == 1).all():
            if i.download_status in ('done', 'downloading') or i.local_paths_list:
                continue
            if i.pixiv_id in fav_ids:
                continue
            candidates.append(i)

        # 收藏数低优先删除，并列时更早上传的优先
        candidates.sort(key=lambda x: (x.bookmark_count, x.upload_date or datetime.min))
        to_delete = [c.pixiv_id for c in candidates[:need_free]]
        if not to_delete:
            return

        to_delete_set = set(to_delete)
        _remove_pids_from_search_caches(db, to_delete)
        safe_commit(db)

        db.query(Illust).filter(Illust.pixiv_id.in_(to_delete)).delete(synchronize_session=False)
        safe_commit(db)
        logger.info(f'[prefetch] 容量清理: 删除 {len(to_delete)} 条低收藏预取作品')


def _prefetch_loop() -> None:
    """后台预取循环：遍历所有 SearchCache 标签，串行预取。"""
    _prefetch_state['running'] = True
    try:
        try:
            with get_session() as db:
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
            # 先刷新最终收藏数（满 1 天的作品），再按新收藏数做容量清理
            _prefetch_refresh_bookmarks()
            _prefetch_capacity_cleanup()
            _prefetch_state['last_check'] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            # 单轮异常不退出线程，等待下个 interval 重试
            logger.error(f'[prefetch] 循环异常: {e}')
    finally:
        _prefetch_state['running'] = False


def _start_prefetch_thread() -> None:
    """启动预取守护线程。首轮延迟 5 秒，之后按 interval 循环。"""
    interval = _prefetch_state['interval']
    if interval <= 0:
        logger.info('[prefetch] 已禁用（interval=0）')
        return

    def _run() -> None:
        time.sleep(5)  # 等 app 完全启动
        while True:
            # 每次迭代读取最新 interval，支持运行时通过 /api/prefetch/config 调整
            interval = _prefetch_state.get('interval') or 0
            if interval <= 0:
                time.sleep(60)  # interval=0 时暂停（禁用），每分钟检查一次以便重新启用
                continue
            _prefetch_loop()
            time.sleep(interval)

    threading.Thread(target=_run, daemon=True).start()
    logger.info(f'[prefetch] 后台线程已启动，interval={interval}s')


_start_prefetch_thread()


def query_cached_tag(tag: str, min_bookmarks: int, sort_order: str,
                     tag_mode: str, r18_mode: str, offset: int = 0,
                     limit: int = 24, filter_tag: str = '') -> tuple[list[dict], bool, int, int]:
    """从 SearchCache + Illust 表查询预取结果，支持库内过滤排序分页。

    不限制 SearchCache.status（fetching/error 时也能查看已累积的缓存数据）。
    filter_tag: 按作品标签（Illust.tags）精确过滤，空串不过滤。
    全局屏蔽标签（BlockedTag）同搜索/图库一致生效。

    Returns:
        (results_dicts, has_more, next_offset, filtered_total)
    """
    with get_session() as db:
        blocked = {t.tag for t in db.query(BlockedTag).all()}
        sc = db.query(SearchCache).filter(
            SearchCache.tag == tag,
        ).first()
        if not sc:
            return [], False, 0, 0

        try:
            all_ids = json.loads(sc.illust_ids) if sc.illust_ids else []
        except (json.JSONDecodeError, TypeError):
            all_ids = []
        if not all_ids:
            return [], False, 0, 0

        id_map = {i.pixiv_id: i for i in db.query(Illust).filter(Illust.pixiv_id.in_(all_ids)).all()}

    # 按预取顺序过滤（缺失的 Illust 行跳过）
    filtered: list[Illust] = []
    for pid in all_ids:
        illust = id_map.get(pid)
        if not illust:
            continue
        if illust.bookmark_count < min_bookmarks:
            continue
        if r18_mode == 'safe' and _is_r18(illust.tags_list):
            continue
        if blocked and (set(illust.tags_list or []) & blocked):
            continue
        if filter_tag and filter_tag not in (illust.tags_list or []):
            continue
        # tag_mode：预取是单标签搜索，or/and 均命中该标签，无需额外过滤
        filtered.append(illust)

    # 排序
    if sort_order == 'popular_d':
        filtered.sort(key=lambda x: x.bookmark_count, reverse=True)
    else:  # date_d
        filtered.sort(key=lambda x: (x.upload_date is not None, x.upload_date or datetime.min), reverse=True)

    total = len(filtered)
    page = filtered[offset:offset + limit]
    has_more = (offset + limit) < total
    next_offset = offset + limit if has_more else 0

    page_dicts = [i.to_dict() for i in page]
    if page_dicts:
        with get_session() as fav_db:
            fav = get_favorite_pids(fav_db)
        for d in page_dicts:
            d['is_favorite'] = d.get('pixiv_id') in fav
    return page_dicts, has_more, next_offset, total


download_executor = ThreadPoolExecutor(max_workers=DOWNLOAD_MAX_WORKERS)
download_locks: dict[int, threading.Lock] = {}
download_cancellations: set[int] = set()
_queued_downloads: set[int] = set()
_download_progress: dict[int, dict] = {}


def _shutdown_background_threads() -> None:
    """进程退出时优雅停止后台线程（gunicorn worker 退出 / 测试进程结束）。"""
    _auto_follow_stop.set()
    download_executor.shutdown(wait=False)


atexit.register(_shutdown_background_threads)



def _download_illust(pixiv_id: int) -> None:
    """后台任务：下载作品的所有原图。"""
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
        download_locks.pop(pixiv_id, None)
        download_cancellations.discard(pixiv_id)
        _queued_downloads.discard(pixiv_id)


def _extract_ext(url: str) -> str:
    """从图片 URL 中提取文件扩展名。"""
    match = re.search(r'\.(jpg|jpeg|png|gif|webp)(?:\?|$)', url, re.IGNORECASE)
    return match.group(1) if match else 'jpg'


# ── 简单内存限流器 ──
_rate_limit_store: dict[str, list[float]] = {}
_rate_limit_cleanup_counter = 0

def _rate_limit(max_attempts: int = 5, window: int = 60) -> Callable:
    """装饰器：限制同一 IP 在 window 秒内最多 max_attempts 次请求。"""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            global _rate_limit_cleanup_counter
            ip = request.remote_addr or 'unknown'
            now = time.time()
            records = _rate_limit_store.setdefault(ip, [])
            # 移除过期的记录
            records[:] = [t for t in records if now - t < window]
            if len(records) >= max_attempts:
                return jsonify({'error': '请求过于频繁，请稍后再试'}), 429
            records.append(now)
            # 定期清理过期的 IP 记录
            _rate_limit_cleanup_counter += 1
            if _rate_limit_cleanup_counter >= 100:
                _rate_limit_cleanup_counter = 0
                cutoff = now - window
                stale = [k for k, v in _rate_limit_store.items() if v and max(v) < cutoff]
                for k in stale:
                    del _rate_limit_store[k]
            return f(*args, **kwargs)
        return decorated
    return decorator


def _get_csrf_token() -> str:
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']


def _get_json_body() -> dict:
    """安全解析请求 JSON：非法 JSON / 非对象（list、标量、null）一律返回空 dict。"""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _safe_int(value, default: int = 0) -> int:
    """安全整数解析：None / 空 / 非数字一律返回 default，不抛异常。"""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _csrf_required(f: Callable) -> Callable:
    """装饰器：POST 接口要求携带有效的 X-CSRF-Token 请求头。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-CSRF-Token', '')
        expected = session.get('_csrf_token', '')
        if not token or not expected or not hmac.compare_digest(token, expected):
            return jsonify({'error': 'CSRF校验失败'}), 403
        return f(*args, **kwargs)
    return decorated


# ── 全局认证 ──
_AUTH_EXEMPT_PATHS = {'/login', '/favicon.ico', '/csrf-token'}
_AUTH_EXEMPT_PREFIXES = ('/static',)


def _is_authed() -> bool:
    return not ACCESS_PASSWORD or bool(session.get('authed'))


@app.before_request
def _require_login():
    if _is_authed():
        return None
    path = request.path
    if path in _AUTH_EXEMPT_PATHS or any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return None
    if path.startswith('/api/') or path == '/search' or request.method != 'GET':
        return jsonify({'error': '未登录', 'error_code': 'AUTH_REQUIRED'}), 401
    return redirect(url_for('login_page', next=path))


def _safe_next(url: str) -> str:
    """防开放重定向：只允许站内相对路径。"""
    if not url or not url.startswith('/'):
        return '/'
    # 拒绝协议相对地址（//...）及其反斜杠变体：浏览器会把首字符 \ 规整为 /，
    # 使 "/\evil.com" 变成 "//evil.com" 协议相对 URL；控制字符一律拒绝。
    if url.startswith('//') or '\\' in url or any(ord(c) < 0x20 for c in url):
        return '/'
    return url


@app.route('/login', methods=['GET'])
def login_page():
    if _is_authed():
        return redirect(_safe_next(request.args.get('next', '')))
    return render_template('login.html', csrf_token=_get_csrf_token())


@app.route('/login', methods=['POST'])
@_csrf_required
@_rate_limit(max_attempts=5, window=60)
def login_submit():
    body = _get_json_body()
    password = str(body.get('password', ''))
    if ACCESS_PASSWORD and hmac.compare_digest(password.encode(), ACCESS_PASSWORD.encode()):
        session['authed'] = True
        session.permanent = True
        return jsonify({'ok': True, 'next': _safe_next(str(body.get('next', '')))})
    time.sleep(1)  # 失败延迟，减缓爆破
    return jsonify({'error': '密码错误'}), 403


@app.after_request
def _security_headers(resp: Response) -> Response:
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    # CSP：脚本已全部抽离到 static/，script-src 收紧为 'self'；
    # style 仍允许 unsafe-inline（模板大量 style 属性），img 放行 data:
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return resp


def _original_to_resized(url: str) -> str:
    """Pixiv 原图 URL → 中图（尺寸可配）。"""
    m = re.match(r'(https://i\.pximg\.net/)img-original/img/(.+)\.(\w+)(\?.*)?$', url)
    if not m:
        return url
    size = MEDIUM_IMAGE_SIZE
    return f'{m.group(1)}c/{size}x{size}/img-master/img/{m.group(2)}_master1200.{m.group(3)}'


def _proxy_thumb(url: str) -> str:
    if not url:
        return ''
    return '/thumb/' + urlsafe_b64encode(url.encode()).decode().rstrip('=').replace('+', '-').replace('/', '_')


def _fetch_original_urls(pixiv_id: int) -> list[str]:
    """按需拉取 Pixiv 详情，返回 original_urls。用于惰性详情场景。"""
    session = build_pixiv_session()
    try:
        detail = _get_illust_detail(session, pixiv_id)
    finally:
        session.close()
    return detail.get('original_urls', []) if detail else []


def _fmt_num(n: int | str) -> str:
    if not n:
        return '0'
    n = int(n)
    return f'{n/10000:.1f}w' if n >= 10000 else str(n)


@app.route('/favicon.ico')
def favicon() -> Response:
    return Response(status=204)


@app.route('/')
def index() -> str:
    return render_template('index.html', csrf_token=_get_csrf_token(), max_bookmarks_default=MAX_BOOKMARKS_DEFAULT)


# ── 异步搜索任务 ──
# 搜索（含限速拉取详情）在后台线程执行，/search 立即返回 task_id，
# 前端轮询 /api/search/status/<id>。gunicorn -w 1 下搜索不再阻塞其他请求。

_search_tasks: dict[str, dict] = {}
_search_tasks_lock = threading.Lock()
SEARCH_TASK_TTL = 600.0  # 完成 10 分钟后清理


def _cleanup_search_tasks() -> None:
    now = time.time()
    with _search_tasks_lock:
        for tid in [t for t, v in _search_tasks.items()
                    if v['status'] in ('done', 'error')
                    and v.get('finished_at') and now - v['finished_at'] > SEARCH_TASK_TTL]:
            del _search_tasks[tid]


def _submit_search_task(fn) -> str:
    """提交搜索任务到后台线程，返回 task_id。

    fn 返回元组 (results, cursor, has_more)。
    """
    _cleanup_search_tasks()
    task_id = secrets.token_hex(8)
    task: dict = {
        'status': 'running',
        'results': [],
        'cursor': None,
        'has_more': False,
        'error': None,
        'fetch_stats': {},
        'created_at': time.time(),
        'finished_at': None,
    }
    with _search_tasks_lock:
        _search_tasks[task_id] = task

    def _run() -> None:
        try:
            results, next_cursor, has_more = fn()
            task['results'] = results
            task['cursor'] = next_cursor
            task['has_more'] = has_more
            task['fetch_stats'] = fetcher.get_last_fetch_stats()
            task['status'] = 'done'
            logger.info(
                f'[search] 完成 task={task_id} results={len(task["results"])} has_more={task["has_more"]} '
                f'details={task["fetch_stats"].get("detail_fetched", 0)} '
                f'failed={task["fetch_stats"].get("detail_failed", 0)} '
                f'seconds={(time.time() - task["created_at"]):.1f}'
            )
        except PixivAuthError:
            logger.warning(f'搜索任务 {task_id} 认证失败')
            task['status'] = 'error'
            task['error'] = 'auth'
        except FileNotFoundError as e:
            logger.error(f'搜索任务 {task_id} 文件缺失：{e}')
            task['status'] = 'error'
            task['error'] = f'缺少文件: {e}'
        except Exception as e:
            logger.error(f'搜索任务 {task_id} 失败：{e}', exc_info=True)
            task['status'] = 'error'
            task['error'] = '搜索服务暂时不可用，请稍后重试'
        finally:
            task['finished_at'] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return task_id


@app.route('/search')
def search() -> Response:
    search_type = request.args.get('type', 'tag')
    query = request.args.get('query', '').strip()
    min_bookmarks = request.args.get('min_bookmarks', MAX_BOOKMARKS_DEFAULT)
    sort_order = request.args.get('sort', 'date_d')
    cursor_str = request.args.get('cursor', '')

    if search_type == 'user' and not cursor_str and not query:
        return jsonify({'error': '请输入画师ID'}), 400

    try:
        min_bookmarks = int(min_bookmarks)
    except (ValueError, TypeError):
        min_bookmarks = MAX_BOOKMARKS_DEFAULT

    tag_mode = request.args.get('tag_mode', 'or')
    if tag_mode not in ('or', 'and'):
        tag_mode = 'or'

    if sort_order not in ('popular_d', 'date_d'):
        sort_order = 'date_d'

    r18_mode = request.args.get('r18_mode', 'all')
    if r18_mode not in ('all', 'safe'):
        r18_mode = 'all'

    # 解析游标（同步快速校验）
    cursor_data = None
    if cursor_str:
        cursor_data = decode_cursor(cursor_str)
        if cursor_data is None:
            return jsonify({'error': '游标无效', 'error_code': 'CURSOR_INVALID'}), 400
        # 游标 24 小时过期。Pixiv 的 p 参数翻页长期有效（无服务端会话），
        # 过期保护只用于拦截极端陈旧参数；分页漂移由前端去重兜底
        if time.time() - cursor_data.get('created_at', 0) > 86400:
            return jsonify({'error': '搜索已过期，请重新搜索', 'error_code': 'CURSOR_EXPIRED'}), 400
        # 从游标恢复搜索参数
        search_type = cursor_data.get('type', search_type)
        query = cursor_data.get('query', query)
        sort_order = cursor_data.get('sort', sort_order)
        tag_mode = cursor_data.get('tag_mode', tag_mode)
        r18_mode = cursor_data.get('r18_mode', r18_mode)
        min_bookmarks = cursor_data.get('min_bookmarks', min_bookmarks)

    query_params = {
        'type': search_type,
        'query': query,
        'sort': sort_order,
        'tag_mode': tag_mode,
        'r18_mode': r18_mode,
        'min_bookmarks': min_bookmarks,
    }

    # 组装后台执行闭包
    if search_type == 'tag':
        if len(query) > 200:
            return jsonify({'error': '搜索关键词过长'}), 400
        if not query:
            def _browse_fn(page, remaining=None):
                return browse_discovery(page, sort_order, min_bookmarks, r18_mode=r18_mode,
                                        max_results=remaining or ITEMS_PER_PAGE)

            def _fn():
                return paginated_search(_browse_fn, query_params, ITEMS_PER_PAGE, cursor_data)
        else:
            def _tag_fn(page, remaining=None):
                return search_by_tag(query, min_bookmarks, page, sort_order, 9999, tag_mode, r18_mode=r18_mode,
                                     max_results=remaining or ITEMS_PER_PAGE)

            def _fn():
                return paginated_search(_tag_fn, query_params, ITEMS_PER_PAGE, cursor_data)
    else:
        if not cursor_str and not query.isdigit():
            return jsonify({'error': '画师ID必须为数字'}), 400

        def _user_fn(page, remaining=None):
            return search_by_user(query, min_bookmarks, page, hide_r18=(r18_mode == 'safe'),
                                  max_results=remaining or ITEMS_PER_PAGE)

        def _fn():
            return paginated_search(_user_fn, query_params, ITEMS_PER_PAGE, cursor_data)

    task_id = _submit_search_task(_fn)
    logger.info(
        f'[search] 已提交 task={task_id} type={search_type} query={query!r} min={min_bookmarks} '
        f'sort={sort_order} tag_mode={tag_mode} r18={r18_mode} cursor={"yes" if cursor_str else "no"}'
    )
    return jsonify({'task_id': task_id, 'status': 'running'})


@app.route('/api/search/status/<task_id>')
def search_status(task_id: str) -> Response:
    # 访问即清理过期任务（无需依赖下次提交），控制内存上限
    _cleanup_search_tasks()
    # 无锁读取：CPython 下 dict 读取原子，且写入方最后才置 status，
    # 读到 done/error 时其余字段必已写入完成，视图一致
    task = _search_tasks.get(task_id)
    if not task:
        return jsonify({'error': '搜索任务不存在或已过期，请重新搜索',
                        'error_code': 'TASK_LOST'}), 404
    resp = {
        'status': task['status'],
        'results': task['results'],
        'cursor': task['cursor'],
        'has_more': task['has_more'],
        'fetch_stats': task['fetch_stats'],
    }
    if task['status'] == 'error':
        resp['error'] = task['error']
        if task['error'] == 'auth':
            return jsonify(resp), 401
        return jsonify(resp), 502
    return jsonify(resp)


@app.route('/api/cache/items')
def cache_items() -> Response:
    """浏览预取缓存：库内过滤/排序/分页，不请求 Pixiv。"""
    tag = request.args.get('tag', '').strip()
    if not tag:
        return jsonify({'error': '缺少标签参数'}), 400

    min_bookmarks = max(0, _safe_int(request.args.get('min_bookmarks'), 0))

    sort_order = request.args.get('sort', 'date_d')
    if sort_order not in ('popular_d', 'date_d'):
        sort_order = 'date_d'

    offset = max(0, _safe_int(request.args.get('offset'), 0))
    filter_tag = request.args.get('filter_tag', '').strip()

    # R18 过滤：默认 safe（不含 R18）；显式 r18=all 才包含
    r18_mode = request.args.get('r18', 'safe')
    if r18_mode not in ('all', 'safe'):
        r18_mode = 'safe'

    with get_session() as db:
        sc = db.query(SearchCache).filter(SearchCache.tag == tag).first()
        if not sc:
            return jsonify({'error': '标签不存在'}), 404
        cached_at = sc.cached_at.isoformat() if sc.cached_at else None
        sc_status = sc.status
        sc_total = sc.total

    results, has_more, _next, filtered_total = query_cached_tag(
        tag, min_bookmarks, sort_order, 'or', r18_mode,
        offset=offset, limit=ITEMS_PER_PAGE, filter_tag=filter_tag,
    )
    return jsonify({
        'tag': tag,
        'cached_at': cached_at,
        'status': sc_status,
        'total': sc_total,
        'filtered_total': filtered_total,
        'offset': offset,
        'page_size': ITEMS_PER_PAGE,
        'results': results,
        'has_more': has_more,
    })


@app.route('/api/cache/tags')
def api_cache_tags() -> Response:
    """缓存作品（预取来源）中出现过的标签列表，供前端 datalist 提示。"""
    try:
        with get_session() as db:
            rows = db.execute(text("""
                SELECT DISTINCT j.value AS tag
                FROM illusts, json_each(illusts.tags) AS j
                WHERE illusts.prefetch_source = 1
                ORDER BY tag
                LIMIT 500
            """)).all()
            return jsonify([row[0] for row in rows])
    except OperationalError:
        # 单条损坏 tags JSON 会让 json_each 抛错：降级返回空列表
        logger.warning('缓存标签列表查询失败（可能含损坏 tags），返回空列表')
        return jsonify([])


@app.route('/api/cache/items/<int:pixiv_id>/delete', methods=['POST'])
@_csrf_required
def cache_item_delete(pixiv_id: int) -> Response:
    """从预取缓存删除单条作品（移除 SearchCache 引用 + Illust 行）。"""
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust or not illust.prefetch_source:
            return jsonify({'error': '作品不在预取缓存中'}), 404
        if illust.download_status in ('done', 'downloading') or illust.local_paths_list:
            return jsonify({'error': '已下载/下载中的作品请在图库中处理'}), 400
        if db.query(CollectionItem).filter(CollectionItem.pixiv_id == pixiv_id).first():
            return jsonify({'error': '已收藏的作品不能从缓存删除'}), 400
        _remove_pids_from_search_caches(db, [pixiv_id])
        db.delete(illust)
        safe_commit(db)
    return jsonify({'status': 'deleted'})


@app.route('/api/following')
def api_following() -> Response:
    page = request.args.get('page', '1')
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        page = 1
    r18_mode = request.args.get('r18_mode', 'all')
    if r18_mode not in ('all', 'safe'):
        r18_mode = 'all'
    try:
        results, has_more = fetch_following(page, r18_mode=r18_mode)
    except PixivAuthError as e:
        logger.warning(f'关注列表认证失败：{e}')
        return jsonify({'error': 'Cookie 已过期，请更新 cookies.txt 后重试'}), 401
    return jsonify({'results': results, 'has_more': has_more})


@app.route('/download/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def trigger_download(pixiv_id: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404

        if illust.download_status == 'done':
            return jsonify({'status': 'done', 'message': '已下载'})

        if illust.download_status == 'downloading':
            return jsonify({'status': 'downloading', 'message': '下载中'})

        if not illust.original_urls_list:
            urls = _fetch_original_urls(pixiv_id)
            if not urls:
                return jsonify({'error': '无法获取原图链接'}), 400
            illust.original_urls_list = urls
            safe_commit(db)

    _queued_downloads.add(pixiv_id)
    download_executor.submit(_download_illust, pixiv_id)
    return jsonify({'status': 'accepted', 'message': '已加入下载队列'})


@app.route('/api/download/batch', methods=['POST'])
@_csrf_required
def batch_download() -> Response:
    body = _get_json_body()
    pixiv_ids = body.get('ids', [])
    if not pixiv_ids or not isinstance(pixiv_ids, list):
        return jsonify({'error': '请提供作品ID列表'}), 400

    accepted, skipped = 0, 0
    with get_session() as db:
        ids = [int(pid) for pid in pixiv_ids if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit())]
        existing_list = db.query(Illust).filter(Illust.pixiv_id.in_(ids)).all()
        existing_map = {i.pixiv_id: i for i in existing_list}

        for pid in ids:
            illust = existing_map.get(pid)
            if not illust or not illust.original_urls_list:
                skipped += 1
                continue
            if illust.download_status in ('done', 'downloading'):
                skipped += 1
                continue
            _queued_downloads.add(pid)
            download_executor.submit(_download_illust, pid)
            accepted += 1

    return jsonify({'accepted': accepted, 'skipped': skipped, 'message': f'已加入 {accepted} 个下载任务'})


def _cancel_download_internal(pixiv_id: int, reset: bool = False) -> Response:
    """标记下载为取消状态，可选清理已下载的部分文件。"""
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404
        is_queued = pixiv_id in _queued_downloads
        if illust.download_status != 'downloading' and not is_queued:
            return jsonify({'error': '该作品未在下载中'}), 400

        _queued_downloads.discard(pixiv_id)
        download_cancellations.add(pixiv_id)

        if reset:
            work_dir = _get_download_dir(pixiv_id)
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
            db.add(DownloadLog(pixiv_id=pixiv_id, action='failed', message='下载已手动重置'))
            safe_commit(db)
            # 不在此处清除取消标记：若 worker 仍在下载，须让它感知取消并自行
            # 清理（_download_illust 的 finally 会 discard）；若任务仅 queued 未
            # 启动，worker 启动时的取消检查也会走 finally 清理。
            return jsonify({'status': 'reset', 'message': '已重置'}), 200

        return jsonify({'status': 'cancelling', 'message': '正在取消...'}), 200


@app.route('/download/cancel/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def cancel_download(pixiv_id: int) -> Response:
    return _cancel_download_internal(pixiv_id, reset=False)


@app.route('/download/reset/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def reset_download(pixiv_id: int) -> Response:
    return _cancel_download_internal(pixiv_id, reset=True)


@app.route('/download_status/<int:pixiv_id>')
def download_status(pixiv_id: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404
        return jsonify({
            'status': illust.download_status or 'none',
            'local_paths': illust.local_paths_list,
        })


@app.route('/api/download/status/batch')
def download_status_batch() -> Response:
    ids_str = request.args.get('ids', '')
    if not ids_str:
        return jsonify({'error': '请提供作品ID'}), 400
    pixiv_ids = [int(pid) for pid in ids_str.split(',') if pid.strip().isdigit()]
    if not pixiv_ids:
        return jsonify({'error': '无效的作品ID'}), 400
    with get_session() as db:
        illusts = db.query(Illust).filter(Illust.pixiv_id.in_(pixiv_ids)).all()
        statuses = {i.pixiv_id: i.download_status or 'none' for i in illusts}
        for pid in pixiv_ids:
            statuses.setdefault(pid, 'none')
        return jsonify({'statuses': statuses})


@app.route('/download_file/<int:pixiv_id>')
def download_file(pixiv_id: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust or illust.download_status != 'done' or not illust.local_paths_list:
            return jsonify({'error': '文件未下载'}), 404

        paths = illust.local_paths_list
        # 验证文件存在
        valid_paths = [p for p in paths if os.path.isfile(p)]
        if not valid_paths:
            return jsonify({'error': '文件已丢失，请重新下载'}), 404

        title = illust.title or str(pixiv_id)
        safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:50]

        # 单文件直接返回
        if len(valid_paths) == 1:
            return send_file(
                valid_paths[0],
                as_attachment=True,
                download_name=f'{safe_title}{os.path.splitext(valid_paths[0])[1]}',
            )

        # 多文件打包 zip（ZIP_STORED 不压缩），使用内存缓冲避免临时文件泄漏
        buf = BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
            for i, p in enumerate(valid_paths):
                ext = os.path.splitext(p)[1]
                zf.write(p, f'{safe_title}_p{i}{ext}')
        buf.seek(0)
        return send_file(
            buf,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'{safe_title}.zip',
        )


@app.route('/csrf-token')
def csrf_token() -> Response:
    return jsonify({'token': _get_csrf_token()})


@app.route('/thumb/<path:url_b64>')
def thumb_proxy(url_b64: str) -> Response:
    """代理 Pixiv 图片，绕过 Referer 检查。带磁盘缓存。"""
    try:
        padding = 4 - len(url_b64) % 4
        if padding != 4:
            url_b64 += '=' * padding
        url = urlsafe_b64decode(url_b64.encode()).decode()
    except Exception:
        return abort(400)

    if not url.startswith('https://i.pximg.net/'):
        return abort(403)

    cache_key = hashlib.md5(url.encode()).hexdigest()
    ext = _extract_ext(url)
    cache_path = os.path.join(CACHE_DIR, f'{cache_key}.{ext}')
    meta_path = cache_path + '.meta'
    if os.path.isfile(cache_path):
        mimetype = 'image/jpeg'
        if os.path.isfile(meta_path):
            with open(meta_path) as f:
                mimetype = f.read().strip()
        return send_file(cache_path, mimetype=mimetype, max_age=86400 * 7)

    try:
        session = build_pixiv_session()
        try:
            resp = session.get(url, timeout=(10, 30))
            resp.raise_for_status()
        finally:
            session.close()
    except requests.RequestException:
        return abort(502)

    mimetype = resp.headers.get('Content-Type', 'image/jpeg')
    try:
        # 原子写：先写唯一临时文件再 rename，避免并发/半程中断留下损坏缓存
        tmp_path = f'{cache_path}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        os.replace(tmp_path, cache_path)
        with open(meta_path, 'w') as f:
            f.write(mimetype)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return Response(resp.iter_content(chunk_size=8192), mimetype=mimetype)

    return send_file(cache_path, mimetype=mimetype, max_age=86400 * 7)


@app.route('/api/image/<int:pixiv_id>/<int:index>')
def serve_image(pixiv_id: int, index: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if illust and illust.download_status == 'done' and illust.local_paths_list:
            paths = illust.local_paths_list
            if 0 <= index < len(paths) and os.path.isfile(paths[index]):
                return send_file(paths[index])

    # 不在 DB（或状态不对）→ 直接从 downloads 目录读
    ddir = _get_download_dir(pixiv_id)
    if not os.path.isdir(ddir):
        abort(404)
    files = sorted(
        (os.path.join(ddir, f) for f in os.listdir(ddir)
         if os.path.isfile(os.path.join(ddir, f))),
        key=_page_sort_key,
    )
    if 0 <= index < len(files) and os.path.isfile(files[index]):
        return send_file(files[index])
    abort(404)


@app.route('/detail/<int:pixiv_id>')
def detail_page(pixiv_id: int) -> str:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            abort(404)

        data = illust.to_dict(favorite=(pixiv_id in get_favorite_pids(db)))
        paths = illust.local_paths_list or []
        local_urls = [f'/api/image/{pixiv_id}/{n}' for n in range(len(paths))]

        file_size = illust.file_size or None

        # 相关作品：同一画师，排除自身
        related = db.query(Illust).filter(
            Illust.user_id == illust.user_id,
            Illust.pixiv_id != pixiv_id,
            Illust.download_status == 'done',
        ).order_by(Illust.created_at.desc()).limit(6).all()
        related = [r.to_dict() for r in related]

        need_fetch_urls = not illust.original_urls_list

    # 网络请求放到 DB session 之外（避免事务随网络往返长时间占用连接）
    if need_fetch_urls:
        urls = _fetch_original_urls(pixiv_id)
        if urls:
            with get_session() as db:
                row = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
                if row:
                    row.original_urls_list = urls
                    safe_commit(db)
    else:
        urls = illust.original_urls_list or []

    medium_urls = []
    original_proxied = []
    for url in urls:
        medium_urls.append(_proxy_thumb(_original_to_resized(url)))
        original_proxied.append(_proxy_thumb(url))

    return render_template(
        'detail.html',
        illust=data,
        local_urls=local_urls,
        medium_urls=medium_urls,
        original_proxied=original_proxied,
        file_size=file_size,
        related=related,
        proxy_thumb=_proxy_thumb,
        fmt_num=_fmt_num,
        csrf_token=_get_csrf_token(),
    )


@app.route('/api/detail/<int:pixiv_id>')
def detail_api(pixiv_id: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404
        d = illust.to_dict()
        paths = illust.local_paths_list or []
        d['local_urls'] = [f'/api/image/{pixiv_id}/{n}' for n in range(len(paths))]
        d['file_count'] = len(paths)
        return jsonify(d)


@app.route('/gallery')
def gallery() -> str:
    return render_template('gallery.html', csrf_token=_get_csrf_token())


@app.route('/cache')
def cache_page() -> str:
    """缓存浏览页：查看预取标签的缓存结果。"""
    return render_template('cache.html', csrf_token=_get_csrf_token())


@app.route('/api/gallery')
def api_gallery() -> Response:
    tag_filter = request.args.get('tag', '').strip()
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    favorites_only = request.args.get('favorites', '').lower() == 'true'
    collection_id = request.args.get('collection_id', type=int)
    sort = request.args.get('sort', 'created')
    if sort not in ('created', 'downloaded'):
        sort = 'created'
    limit = max(1, min(200, limit))
    offset = max(0, offset)

    is_collection_view = collection_id is not None

    # 扫描本地 downloads 目录
    local_items = _scan_local_downloads()
    local_pids = sorted(local_items.keys(), reverse=True)

    with get_session() as db:
        blocked = {t.tag for t in db.query(BlockedTag).all()}

        default_cid = None
        default_fav_set: set[int] = set()
        if not collection_id:
            dc = db.query(Collection).filter(Collection.name == '我的收藏').first()
            if dc:
                default_cid = dc.id
                pids = db.query(CollectionItem.pixiv_id).filter(
                    CollectionItem.collection_id == dc.id
                ).all()
                default_fav_set = {p[0] for p in pids}

        if local_pids:
            # 本地 pid 可能数千：分片拼 IN，避免超过 SQLite 绑定变量上限
            #（旧版本默认 999，现代版本 32766；分片后对两者都安全）。
            or_clauses = ["illusts.download_status = 'done'"]
            params = {}
            for ci, chunk in enumerate(local_pids[i:i + 500] for i in range(0, len(local_pids), 500)):
                phs = ','.join(f':local_pid_{ci}_{j}' for j in range(len(chunk)))
                or_clauses.append(f'illusts.pixiv_id IN ({phs})')
                for j, pid in enumerate(chunk):
                    params[f'local_pid_{ci}_{j}'] = pid
            wheres = ['(' + ' OR '.join(or_clauses) + ')']
        else:
            wheres = ["illusts.download_status = 'done'"]
            params = {}
        if blocked:
            blk_list = list(blocked)
            phs = ','.join(f':blk_{i}' for i in range(len(blk_list)))
            wheres.append(f'NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) AS je WHERE je.value IN ({phs}))')
            for i, t in enumerate(blk_list):
                params[f'blk_{i}'] = t
        if tag_filter:
            wheres.append('EXISTS (SELECT 1 FROM json_each(illusts.tags) AS je WHERE je.value = :tag_filter)')
            params['tag_filter'] = tag_filter
        if favorites_only:
            if default_cid is not None:
                wheres.append('illusts.pixiv_id IN (SELECT pixiv_id FROM collection_items WHERE collection_id = :default_cid)')
                params['default_cid'] = default_cid
            else:
                wheres.append('0 = 1')

        where_clause = ' AND '.join(wheres)

        total: int = 0
        fav_total: int = 0
        pk_ids: list[int] = []

        # 查询执行闭包：COUNT + 本页 pk_ids。
        # json_each(illusts.tags) 遇到单条非法 JSON 会抛 OperationalError，
        # 外层捕获后降级去掉标签相关过滤重试（数据损坏兜底，不让整页 500）。
        def _run_gallery_queries(wc: str, p: dict) -> None:
            nonlocal total, fav_total, pk_ids
            page_params = {**p, 'lim': limit, 'off': offset}
            if is_collection_view:
                p['collection_id'] = collection_id
                page_params['collection_id'] = collection_id
                row = db.execute(
                    text(f'SELECT COUNT(*) FROM illusts '
                         f'JOIN collection_items ON collection_items.pixiv_id = illusts.pixiv_id '
                         f'WHERE collection_items.collection_id = :collection_id AND {wc}'),
                    p
                ).one()
                total = row[0] or 0
                fav_total = 0
                pk_ids = db.execute(
                    text(f'SELECT illusts.id FROM illusts '
                         f'JOIN collection_items ON collection_items.pixiv_id = illusts.pixiv_id '
                         f'WHERE collection_items.collection_id = :collection_id AND {wc} '
                         f'ORDER BY collection_items.position ASC '
                         f'LIMIT :lim OFFSET :off'),
                    page_params
                ).scalars().all()
            else:
                row = db.execute(
                    text(f'SELECT COUNT(*) AS total FROM illusts WHERE {wc}'),
                    p
                ).one()
                total = row[0] or 0
                fav_total = 0
                order_col = 'downloaded_at DESC' if sort == 'downloaded' else 'created_at DESC'
                pk_ids = db.execute(
                    text(f'SELECT id FROM illusts WHERE {wc} ORDER BY {order_col} LIMIT :lim OFFSET :off'),
                    page_params
                ).scalars().all()

        try:
            _run_gallery_queries(where_clause, params)
        except OperationalError:
            logger.warning('图库查询因 tags 数据异常失败，降级跳过标签过滤重试')
            wc = ' AND '.join(w for w in wheres if 'json_each' not in w)
            _run_gallery_queries(wc, {k: v for k, v in params.items()
                                      if not k.startswith('blk_') and k != 'tag_filter'})

        # 获取完整 ORM 对象并保持排序
        illusts = db.query(Illust).filter(Illust.id.in_(pk_ids)).all()
        id_order = {id_: i for i, id_ in enumerate(pk_ids)}
        illusts.sort(key=lambda x: id_order.get(x.id, 0))

        results = []
        fill_ids: list[int] = []
        for i in illusts:
            paths = local_items.get(i.pixiv_id) or i.local_paths_list or []
            if not i.file_size and paths:
                total_size = sum(os.path.getsize(p) for p in paths if os.path.isfile(p))
                if total_size:
                    i.file_size = total_size
            d = i.to_dict(favorite=(i.pixiv_id in default_fav_set))
            d['file_count'] = len(paths)
            d['local_urls'] = [f'/api/image/{i.pixiv_id}/{n}' for n in range(len(paths))]
            results.append(d)
            if i.bookmark_count == 0 and not i.original_urls_list:
                fill_ids.append(i.pixiv_id)

        if fill_ids:
            fetcher._kick_background_fill(fill_ids)

        # 补充真正不在 DB 的本地文件（孤儿：磁盘有文件但 DB 无记录）。
        # 注意：必须排除【全部】DB 记录而非仅当前页（seen_pids）——否则其他页
        # 或未通过过滤条件的 DB 作品会被误判为孤儿，生成"只有作品号"的简陋卡片，
        # 与正常卡片重复展示（同一作品两张卡片），且 total 被重复计算。
        if not collection_id and not favorites_only:
            db_pid_set = {r[0] for r in db.query(Illust.pixiv_id).all()}
            orphan_pids = sorted(set(local_pids) - db_pid_set, reverse=True)
            orphan_results = _build_orphan_dicts(orphan_pids, local_items)
            total += len(orphan_results)
            results.extend(orphan_results[:max(0, limit - len(results))])

        fav_total = total if favorites_only else sum(1 for r in results if r.get('pixiv_id') in default_fav_set)

        safe_commit(db)

        return jsonify({
            'data': results,
            'total': total,
            'favorite_total': fav_total,
            'has_more': offset + limit < total,
        })


@app.route('/api/gallery/tags')
def api_gallery_tags() -> Response:
    with get_session() as db:
        rows = db.execute(text("""
            SELECT DISTINCT j.value AS tag
            FROM illusts, json_each(illusts.tags) AS j
            WHERE illusts.download_status = 'done'
            ORDER BY tag
            LIMIT 1000
        """)).all()
        return jsonify([row[0] for row in rows])


def _delete_illust_files(illust: Illust) -> int:
    """删除作品的已下载文件及目录。返回删除的文件数。"""
    paths = illust.local_paths_list or []
    deleted = 0
    for p in paths:
        try:
            if os.path.isfile(p):
                os.remove(p)
                deleted += 1
        except OSError:
            pass
    if paths:
        work_dir = os.path.dirname(paths[0])
        try:
            if os.path.isdir(work_dir) and not os.listdir(work_dir):
                os.rmdir(work_dir)
        except OSError:
            pass
    illust.download_status = None
    illust.local_paths = None
    return deleted


@app.route('/api/gallery/<int:pixiv_id>', methods=['DELETE'])
@_csrf_required
def delete_gallery(pixiv_id: int) -> Response:
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404

        deleted = _delete_illust_files(illust)
        db.add(DownloadLog(pixiv_id=pixiv_id, action='deleted', message=f'已删除 {deleted} 个文件'))
        safe_commit(db)
        return jsonify({'status': 'deleted', 'message': f'已删除 {deleted} 个文件'})


@app.route('/api/gallery/batch-delete', methods=['POST'])
@_csrf_required
def batch_delete_gallery() -> Response:
    body = _get_json_body()
    ids = body.get('ids', [])
    if not ids or not isinstance(ids, list):
        return jsonify({'error': '请提供作品ID列表'}), 400

    with get_session() as db:
        pixiv_ids = [int(pid) for pid in ids if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit())]
        illusts = db.query(Illust).filter(Illust.pixiv_id.in_(pixiv_ids)).all()
        deleted_count = 0
        total_files = 0
        for illust in illusts:
            n = _delete_illust_files(illust)
            total_files += n
            db.add(DownloadLog(pixiv_id=illust.pixiv_id, action='deleted', message=f'已删除 {n} 个文件'))
            deleted_count += 1
        safe_commit(db)

    failed = len(pixiv_ids) - deleted_count
    return jsonify({
        'status': 'done',
        'deleted': deleted_count,
        'failed': failed,
        'total_files': total_files,
        'message': f'已删除 {deleted_count} 个作品 ({total_files} 个文件)' + (f', {failed} 个失败' if failed else ''),
    })



# ── 自动关注控制 ──

@app.route('/api/auto-follow/status')
def auto_follow_status() -> Response:
    return jsonify(_auto_follow_state)

@app.route('/api/auto-follow/config', methods=['POST'])
@_csrf_required
def auto_follow_config() -> Response:
    body = _get_json_body()
    if 'interval' in body:
        try:
            _auto_follow_state['interval'] = max(0, int(body['interval']))
        except (ValueError, TypeError):
            return jsonify({'error': 'interval must be integer seconds'}), 400
    if 'auto_download' in body:
        val = body['auto_download']
        _auto_follow_state['auto_download'] = val if isinstance(val, bool) else str(val).lower() == 'true'
    return jsonify(_auto_follow_state)


# ── 搜索预取管理 ──

_PREFETCH_SETTINGS_KEYS = {
    'interval': 'prefetch_interval',
    'pages': 'prefetch_pages',
    'max_illusts': 'prefetch_max_illusts',
}


@app.route('/api/prefetch/config', methods=['GET'])
def prefetch_config_get() -> Response:
    return jsonify({
        'interval': _prefetch_state['interval'],
        'pages': _prefetch_state['pages'],
        'max_illusts': _prefetch_state['max_illusts'],
    })


@app.route('/api/prefetch/config', methods=['POST'])
@_csrf_required
def prefetch_config_post() -> Response:
    body = _get_json_body()
    updates: dict[str, int] = {}
    for key in _PREFETCH_SETTINGS_KEYS:
        if key in body:
            try:
                updates[key] = max(0, int(body[key]))
            except (ValueError, TypeError):
                return jsonify({'error': f'{key} must be integer'}), 400

    # 写配置：先全部校验并持久化 settings.json，成功后一次性提交到内存，避免校验/写盘失败时状态漂移
    if updates:
        current = _load_settings()
        for key, val in updates.items():
            current[_PREFETCH_SETTINGS_KEYS[key]] = val
        try:
            os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
            with open(_SETTINGS_PATH, 'w', encoding='utf-8') as f:
                json.dump(current, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return jsonify({'error': f'保存失败: {e}'}), 500
        _prefetch_state.update(updates)

    return jsonify({
        'interval': _prefetch_state['interval'],
        'pages': _prefetch_state['pages'],
        'max_illusts': _prefetch_state['max_illusts'],
    })


@app.route('/api/prefetch/tags', methods=['GET'])
def prefetch_tags_get() -> Response:
    with get_session() as db:
        rows = db.query(SearchCache).order_by(SearchCache.cached_at.desc()).all()
        return jsonify([{
            'tag': r.tag,
            'cached_at': r.cached_at.isoformat() if r.cached_at else None,
            'status': r.status,
            'total': r.total,
            'error': r.error,
        } for r in rows])


@app.route('/api/prefetch/tags', methods=['POST'])
@_csrf_required
def prefetch_tags_post() -> Response:
    tag = _get_json_body().get('tag', '').strip()
    if not tag:
        return jsonify({'error': '标签不能为空'}), 400
    with get_session() as db:
        if db.query(SearchCache).filter(SearchCache.tag == tag).first():
            return jsonify({'error': '标签已存在'}), 409
        db.add(SearchCache(tag=tag))
        safe_commit(db)
        return jsonify({'tag': tag}), 201


@app.route('/api/prefetch/tags/<path:tag>', methods=['DELETE'])
@_csrf_required
def prefetch_tags_delete(tag: str) -> Response:
    with get_session() as db:
        row = db.query(SearchCache).filter(SearchCache.tag == tag).first()
        if not row:
            return jsonify({'error': '标签不存在'}), 404

        try:
            ids = json.loads(row.illust_ids) if row.illust_ids else []
        except (json.JSONDecodeError, TypeError):
            ids = []

        deletable: list[int] = []
        other_pids = _collect_other_tag_pids(db, tag)
        for pid in ids:
            if not isinstance(pid, int):
                continue
            # 仍被其他 SearchCache 引用时保留
            if pid in other_pids:
                continue
            illust = db.query(Illust).filter(Illust.pixiv_id == pid).first()
            if illust is None or not illust.prefetch_source:
                continue
            if illust.download_status in ('done', 'downloading') or illust.local_paths_list:
                continue
            if db.query(CollectionItem).filter(CollectionItem.pixiv_id == pid).first():
                continue
            deletable.append(pid)

        if deletable:
            db.query(Illust).filter(Illust.pixiv_id.in_(deletable)).delete(synchronize_session=False)
        db.delete(row)
        safe_commit(db)
        return jsonify({'tag': tag})


@app.route('/api/prefetch/status', methods=['GET'])
def prefetch_status_get() -> Response:
    return jsonify({
        'running': _prefetch_state['running'],
        'last_check': _prefetch_state['last_check'],
        'interval': _prefetch_state['interval'],
    })


@app.route('/api/prefetch/refresh', methods=['POST'])
@_csrf_required
def prefetch_refresh_post() -> Response:
    tag = _get_json_body().get('tag', '').strip()
    if not tag:
        return jsonify({'error': '标签不能为空'}), 400
    with get_session() as db:
        row = db.query(SearchCache).filter(SearchCache.tag == tag).first()
        if not row:
            return jsonify({'error': '标签不存在'}), 404
        if row.status == 'fetching':
            return jsonify({'error': '该标签正在刷新中'}), 409
    threading.Thread(target=_prefetch_one_tag, args=(tag,), daemon=True).start()
    return jsonify({'tag': tag, 'status': 'refreshing'})


# ── 下载管理 ──

@app.route('/downloads')
def downloads_page() -> str:
    return render_template('downloads.html', csrf_token=_get_csrf_token())


@app.route('/api/downloads')
def api_downloads() -> Response:
    with get_session() as db:
        active = db.query(Illust).filter(Illust.download_status == 'downloading').order_by(Illust.created_at.desc()).all()
        queued_ids = list(_queued_downloads)
        queued = db.query(Illust).filter(Illust.pixiv_id.in_(queued_ids)).order_by(Illust.created_at.desc()).all() if queued_ids else []
        completed = db.query(Illust).filter(Illust.download_status == 'done').order_by(Illust.downloaded_at.desc().nullslast()).limit(30).all()
        logs = (
            db.query(DownloadLog)
            .order_by(DownloadLog.created_at.desc())
            .limit(50).all()
        )
        def _with_progress(i):
            d = i.to_dict()
            p = _download_progress.get(i.pixiv_id)
            if p and p['total'] > 0:
                d['progress'] = {'current': p['current'], 'total': p['total']}
            return d

        def _with_dir(i):
            d = i.to_dict()
            paths = i.local_paths_list or []
            d['local_dir'] = os.path.abspath(_get_download_dir(i.pixiv_id)) if paths else None
            return d

        return jsonify({
            'active': [_with_progress(i) for i in active],
            'queued': [i.to_dict() for i in queued],
            'completed': [_with_dir(i) for i in completed],
            'logs': [l.to_dict() for l in logs],
        })


# ── 屏蔽标签 ──

@app.route('/api/blocked-tags', methods=['GET'])
def list_blocked_tags() -> Response:
    with get_session() as db:
        tags = db.query(BlockedTag).order_by(BlockedTag.created_at.desc()).all()
        return jsonify([t.tag for t in tags])


@app.route('/api/blocked-tags', methods=['POST'])
@_csrf_required
def add_blocked_tag() -> Response:
    tag = _get_json_body().get('tag', '').strip()
    if not tag:
        return jsonify({'error': '标签不能为空'}), 400
    with get_session() as db:
        if db.query(BlockedTag).filter(BlockedTag.tag == tag).first():
            return jsonify({'error': '标签已存在'}), 409
        db.add(BlockedTag(tag=tag))
        safe_commit(db)
        clear_search_cache()
        return jsonify({'status': 'added', 'tag': tag})


@app.route('/api/blocked-tags/<path:tag>', methods=['DELETE'])
@_csrf_required
def remove_blocked_tag(tag: str) -> Response:
    with get_session() as db:
        entry = db.query(BlockedTag).filter(BlockedTag.tag == tag).first()
        if not entry:
            return jsonify({'error': '标签不存在'}), 404
        db.delete(entry)
        safe_commit(db)
        clear_search_cache()
        return jsonify({'status': 'deleted', 'tag': tag})


# ── 设置 ──

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'settings.json')

# 设置页可编辑键与默认值：由 config.SETTINGS_KEYS（唯一来源）派生，
# 排除密码类与 cookie_secure（这些只通过 settings.json/环境变量管理）。
_SETTINGS_DEFAULTS = {
    k: v for k, (_, v) in config_module.SETTINGS_KEYS.items()
    if not k.endswith('_password') and k != 'cookie_secure'
}


def _load_settings() -> dict:
    if os.path.exists(_SETTINGS_PATH):
        try:
            with open(_SETTINGS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            result = dict(_SETTINGS_DEFAULTS)
            result.update(data)
            return result
        except Exception:
            logger.warning('设置文件读取失败，使用默认设置')
    return dict(_SETTINGS_DEFAULTS)


def _settings_locked() -> bool:
    """设置页门禁：已全局登录则直通；否则按旧 SETTINGS_PASSWORD 流程。"""
    if session.get('authed'):
        return False
    return bool(SETTINGS_PASSWORD) and not session.get('settings_unlocked')


@app.route('/settings')
def settings_page() -> str:
    if _settings_locked():
        return render_template('settings_unlock.html', csrf_token=_get_csrf_token())
    return render_template('settings.html', csrf_token=_get_csrf_token())


@app.route('/api/settings/unlock', methods=['POST'])
@_csrf_required
@_rate_limit(max_attempts=5, window=60)
def settings_unlock() -> Response:
    if session.get('authed') or not SETTINGS_PASSWORD:
        return jsonify({'ok': True})
    body = _get_json_body()
    if hmac.compare_digest(str(body.get('password', '')).encode(), SETTINGS_PASSWORD.encode()):
        session['settings_unlocked'] = True
        return jsonify({'ok': True})
    return jsonify({'error': '密码错误'}), 403


@app.route('/api/settings', methods=['GET'])
def api_settings_get() -> Response:
    if _settings_locked():
        return jsonify({'error': '需要密码访问'}), 403
    data = _load_settings()
    # 脱敏：密码类与 Cookie 字段不回传明文（纵深防御，前端 FIELD_MAP 不消费这些键）
    for k in list(data):
        if k.endswith('_password') or k == 'cookie':
            data[k] = ''
    return jsonify(data)


@app.route('/api/settings', methods=['POST'])
@_csrf_required
def api_settings_post() -> Response:
    if _settings_locked():
        return jsonify({'error': '需要密码访问'}), 403

    body = _get_json_body()
    current = _load_settings()

    # Cookie 字段特殊处理：写入项目根目录 cookies.txt，立即更新内存状态
    cookie_val = body.pop('cookie', '').strip()
    if cookie_val:
        # 剔除换行/控制字符，防止向 cookies.txt 注入多行破坏鉴权
        clean_val = re.sub(r'[\r\n\t\x00-\x1f\x7f]', '', cookie_val).strip()
        if not clean_val:
            return jsonify({'error': 'Cookie 内容无效'}), 400
        cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')
        try:
            with open(cookie_path, 'w') as f:
                f.write(f'PHPSESSID={clean_val}\n')
        except OSError as e:
            return jsonify({'error': f'cookies.txt 写入失败: {e}'}), 500
        fetcher._cookie_value = clean_val
        fetcher._cookie_mtime = os.path.getmtime(cookie_path)
        logger.info('cookies.txt 已通过设置页更新')

    # 仅合并已知的配置键
    for key in _SETTINGS_DEFAULTS:
        if key in body:
            val = body[key]
            if key in ('auto_follow_download',):
                val = bool(val)
            elif key in ('download_max_workers', 'per_page', 'search_pages',
                         'max_bookmarks_default', 'auto_follow_interval',
                         'fetch_detail_workers', 'medium_image_size',
                         'items_per_page', 'prefetch_interval',
                         'prefetch_pages', 'prefetch_max_illusts'):
                try:
                    val = max(0, int(val))
                except (ValueError, TypeError):
                    continue
            current[key] = val
    try:
        os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
        with open(_SETTINGS_PATH, 'w', encoding='utf-8') as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        # prefetch_* 键与 /api/prefetch/config 保持同构：保存成功后同步内存态
        #（interval 即时生效，不再需要重启）
        _prefetch_state.update({
            k: current[k] for k in ('prefetch_interval', 'prefetch_pages', 'prefetch_max_illusts')
            if k in current
        })
        return jsonify(current)
    except Exception as e:
        return jsonify({'error': f'保存失败: {e}'}), 500


# ── 收藏夹 ──


@app.route('/api/collections', methods=['GET'])
def list_collections() -> Response:
    with get_session() as db:
        collections = db.query(Collection).order_by(Collection.created_at).all()
        result = []
        for c in collections:
            d = c.to_dict()
            d['item_count'] = db.query(CollectionItem).filter(CollectionItem.collection_id == c.id).count()
            result.append(d)
        return jsonify(result)


@app.route('/api/collections', methods=['POST'])
@_csrf_required
def create_collection() -> Response:
    body = _get_json_body()
    name = body.get('name', '').strip()
    if not name or len(name) > 50:
        return jsonify({'error': '收藏夹名称不能为空且不超过50字'}), 400
    with get_session() as db:
        if db.query(Collection).filter(Collection.name == name).first():
            return jsonify({'error': '收藏夹名称已存在'}), 409
        c = Collection(name=name, description=body.get('description', ''))
        db.add(c)
        safe_commit(db)
        return jsonify(c.to_dict()), 201


@app.route('/api/collections/<int:collection_id>', methods=['PUT'])
@_csrf_required
def update_collection(collection_id: int) -> Response:
    body = _get_json_body()
    name = body.get('name', '').strip()
    if not name or len(name) > 50:
        return jsonify({'error': '收藏夹名称不能为空且不超过50字'}), 400
    with get_session() as db:
        c = db.query(Collection).filter(Collection.id == collection_id).first()
        if not c:
            return jsonify({'error': '收藏夹不存在'}), 404
        if c.name != name and db.query(Collection).filter(Collection.name == name).first():
            return jsonify({'error': '收藏夹名称已存在'}), 409
        c.name = name
        if 'description' in body:
            c.description = body.get('description', '')
        safe_commit(db)
        return jsonify(c.to_dict())


@app.route('/api/collections/<int:collection_id>', methods=['DELETE'])
@_csrf_required
def         delete_collection(collection_id: int) -> Response:
    with get_session() as db:
        c = db.query(Collection).filter(Collection.id == collection_id).first()
        if not c:
            return jsonify({'error': '收藏夹不存在'}), 404
        db.query(CollectionItem).filter(CollectionItem.collection_id == collection_id).delete()
        db.delete(c)
        safe_commit(db)
        return jsonify({'status': 'deleted'})


@app.route('/api/collections/<int:collection_id>/items', methods=['GET'])
def list_collection_items(collection_id: int) -> Response:
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    limit = max(1, min(200, limit))
    offset = max(0, offset)
    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        total = db.query(CollectionItem).filter(CollectionItem.collection_id == collection_id).count()
        items = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id
        ).order_by(CollectionItem.position.asc()).offset(offset).limit(limit).all()
        return jsonify({
            'data': [item.to_dict() for item in items],
            'total': total,
            'has_more': offset + limit < total,
        })


def _next_collection_position(db, collection_id: int) -> float:
    """计算收藏夹下一个可用位置：当前最大位置 + 1000（单语句，统一三处调用）。"""
    return float(db.execute(text(
        'SELECT COALESCE(MAX(position), 0) + 1000.0 FROM collection_items WHERE collection_id = :cid'
    ), {'cid': collection_id}).scalar() or 1000.0)


@app.route('/api/collections/<int:collection_id>/items', methods=['POST'])
@_csrf_required
def add_collection_item(collection_id: int) -> Response:
    body = _get_json_body()
    pixiv_id = body.get('pixiv_id')
    if not pixiv_id:
        return jsonify({'error': '请提供作品ID'}), 400
    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        existing = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id,
            CollectionItem.pixiv_id == pixiv_id,
        ).first()
        if existing:
            return jsonify({'error': '作品已在收藏夹中'}), 409
        item = CollectionItem(
            collection_id=collection_id, pixiv_id=pixiv_id,
            position=_next_collection_position(db, collection_id),
        )
        db.add(item)
        safe_commit(db)
        data = item.to_dict()
    return jsonify(data), 201


@app.route('/api/collections/<int:collection_id>/items/<int:pixiv_id>', methods=['DELETE'])
@_csrf_required
def remove_collection_item(collection_id: int, pixiv_id: int) -> Response:
    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        item = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id,
            CollectionItem.pixiv_id == pixiv_id,
        ).first()
        if not item:
            return jsonify({'error': '作品不在收藏夹中'}), 404
        db.delete(item)
        safe_commit(db)
    return jsonify({'status': 'deleted'})


@app.route('/api/illust/<int:pixiv_id>/collections')
def illust_collections(pixiv_id: int) -> Response:
    with get_session() as db:
        items = db.query(CollectionItem).filter(CollectionItem.pixiv_id == pixiv_id).all()
        return jsonify([item.collection_id for item in items])


@app.route('/api/collections/<int:collection_id>/items/batch', methods=['POST'])
@_csrf_required
def batch_add_collection_items(collection_id: int) -> Response:
    body = _get_json_body()
    pixiv_ids = body.get('pixiv_ids', [])
    if not pixiv_ids or not isinstance(pixiv_ids, list):
        return jsonify({'error': '请提供作品ID列表'}), 400
    pixiv_ids = [int(pid) for pid in pixiv_ids
                 if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit())]
    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        next_pos = _next_collection_position(db, collection_id)
        added = 0
        for pid in pixiv_ids:
            existing = db.query(CollectionItem).filter(
                CollectionItem.collection_id == collection_id,
                CollectionItem.pixiv_id == pid,
            ).first()
            if not existing:
                db.add(CollectionItem(collection_id=collection_id, pixiv_id=pid, position=next_pos))
                next_pos += 1000.0
                added += 1
        safe_commit(db)
    return jsonify({'added': added, 'total': len(pixiv_ids)})


@app.route('/api/collections/<int:collection_id>/items/batch', methods=['DELETE'])
@_csrf_required
def batch_remove_collection_items(collection_id: int) -> Response:
    body = _get_json_body()
    pixiv_ids = body.get('pixiv_ids', [])
    if not pixiv_ids or not isinstance(pixiv_ids, list):
        return jsonify({'error': '请提供作品ID列表'}), 400
    pixiv_ids = [int(pid) for pid in pixiv_ids
                 if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit())]
    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        removed = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id,
            CollectionItem.pixiv_id.in_(pixiv_ids),
        ).delete(synchronize_session='fetch')
        safe_commit(db)
    return jsonify({'removed': removed})


def _compute_move_position(items: list, idx: int, direction: str):
    """返回 (new_pos, needs_rebalance, error_code)。
    items 是 [(id, position), ...] 元组列表，按 position ASC 排序。
    error_code 为 None（成功）或 400（边界）。"""
    n = len(items)
    if direction == 'up':
        if idx == 0:
            return None, False, 400
        prev = items[idx - 1]
        prev_pos = prev[1]
        if idx == 1:
            return prev_pos - 1000.0, False, None
        prev_of_prev = items[idx - 2]
        pop_pos = prev_of_prev[1]
        if prev_pos - pop_pos < 1.0:
            return None, True, None
        return (pop_pos + prev_pos) / 2.0, False, None
    else:  # down
        if idx == n - 1:
            return None, False, 400
        nxt = items[idx + 1]
        nxt_pos = nxt[1]
        if idx + 1 == n - 1:
            return nxt_pos + 1000.0, False, None
        next_of_next = items[idx + 2]
        non_pos = next_of_next[1]
        if non_pos - nxt_pos < 1.0:
            return None, True, None
        return (nxt_pos + non_pos) / 2.0, False, None


@app.route('/api/collections/<int:collection_id>/items/<int:pixiv_id>/move', methods=['POST'])
@_csrf_required
def move_collection_item(collection_id: int, pixiv_id: int) -> Response:
    body = _get_json_body()
    direction = body.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction 必须是 up 或 down'}), 400

    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        current = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id,
            CollectionItem.pixiv_id == pixiv_id,
        ).first()
        if not current:
            return jsonify({'error': '作品不在收藏夹中'}), 404

        rows = db.execute(text(
            'SELECT id, position FROM collection_items WHERE collection_id = :cid ORDER BY position ASC, id ASC'
        ), {'cid': collection_id}).fetchall()
        items = [(r[0], r[1]) for r in rows]
        idx = next((i for i, it in enumerate(items) if it[0] == current.id), None)
        if idx is None:
            return jsonify({'error': '作品不在收藏夹中'}), 404

        new_pos, needs_rebalance, err = _compute_move_position(items, idx, direction)
        if err == 400:
            return jsonify({'error': '已在边界位置'}), 400

        rebalanced = False
        if needs_rebalance:
            rebalanced = True
            for i, it_tuple in enumerate(items):
                db.execute(text('UPDATE collection_items SET position=:p WHERE id=:id'),
                           {'p': (i + 1) * 1000.0, 'id': it_tuple[0]})
            safe_commit(db)
            rows = db.execute(text(
                'SELECT id, position FROM collection_items WHERE collection_id = :cid ORDER BY position ASC, id ASC'
            ), {'cid': collection_id}).fetchall()
            items = [(r[0], r[1]) for r in rows]
            idx = next((i for i, it in enumerate(items) if it[0] == current.id), None)
            new_pos, _, _ = _compute_move_position(items, idx, direction)

        # 用重查后的 items 里的 position 做乐观锁基准：rebalance 分支已重排
        # 并 commit，current 的 ORM 缓存仍是重排前的旧值（否则误报 409）。
        old_pos = items[idx][1]
        result = db.execute(text(
            'UPDATE collection_items SET position=:np '
            'WHERE collection_id=:cid AND pixiv_id=:pid AND position=:op'
        ), {'np': new_pos, 'cid': collection_id, 'pid': pixiv_id, 'op': old_pos})
        if result.rowcount == 0:
            db.rollback()
            return jsonify({'error': '位置已被修改，请刷新后重试'}), 409
        safe_commit(db)
        return jsonify({'position': new_pos, 'rebalanced': rebalanced})


# ── 收藏 ──


@app.route('/api/open-dir', methods=['POST'])
@_csrf_required
def api_open_dir() -> Response:
    """打开本地文件夹（仅限本机浏览器访问时有效）。"""
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'error': '该功能仅本机可用'}), 403
    body = _get_json_body()
    path = body.get('path', '')
    if not path or not os.path.isdir(path):
        return jsonify({'error': '目录不存在'}), 404
    try:
        if platform.system() == 'Windows':
            os.startfile(path)
        else:
            import subprocess
            subprocess.Popen(['xdg-open', path])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/favorite/<int:pixiv_id>', methods=['GET'])
def api_favorite_get(pixiv_id: int) -> Response:
    with get_session() as db:
        default = db.query(Collection).filter(Collection.name == '我的收藏').first()
        if not default:
            return jsonify({'is_favorite': False})
        exists = db.query(CollectionItem).filter(
            CollectionItem.collection_id == default.id,
            CollectionItem.pixiv_id == pixiv_id,
        ).first() is not None
        return jsonify({'is_favorite': exists})


@app.route('/api/favorite/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def api_favorite_post(pixiv_id: int) -> Response:
    """切换'我的收藏'收藏夹中的归属。"""
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404
        default = db.query(Collection).filter(Collection.name == '我的收藏').first()
        if not default:
            return jsonify({'error': '默认收藏夹不存在'}), 500
        existing = db.query(CollectionItem).filter(
            CollectionItem.collection_id == default.id,
            CollectionItem.pixiv_id == pixiv_id,
        ).first()
        if existing:
            db.delete(existing)
            safe_commit(db)
            return jsonify({'is_favorite': False})
        else:
            db.add(CollectionItem(
                collection_id=default.id, pixiv_id=pixiv_id,
                position=_next_collection_position(db, default.id),
            ))
            safe_commit(db)
            return jsonify({'is_favorite': True})


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
