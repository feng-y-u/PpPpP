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
from base64 import urlsafe_b64decode, urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor, as_completed
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

from config import (
    DOWNLOAD_DIR, DOWNLOAD_MAX_WORKERS, PAGE_DOWNLOAD_INTERVAL,
    MAX_BOOKMARKS_DEFAULT, AUTO_FOLLOW_INTERVAL, AUTO_FOLLOW_DOWNLOAD,
    PREFETCH_INTERVAL, PREFETCH_PAGES, PREFETCH_MAX_ILLUSTS,
    MEDIUM_IMAGE_SIZE,
    SETTINGS_PASSWORD, ACCESS_PASSWORD, COOKIE_SECURE,
    ITEMS_PER_PAGE,
)
from models import init_db, get_session, Illust, DownloadLog, BlockedTag, Collection, CollectionItem, SearchCache, safe_commit
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
        app.config['SECRET_KEY'] = f.read().strip()
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


def _scan_local_downloads() -> dict[int, list[str]]:
    """扫描 downloads/ 目录，返回 {pixiv_id: [file_paths]}。"""
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
            os.path.join(subdir, f) for f in os.listdir(subdir)
            if os.path.isfile(os.path.join(subdir, f))
        )
        if files:
            result[pid] = files
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

# ── ⚠ 多进程限制 ─────────────────────────────────────
# 以下状态变量（_auto_follow_state、download_locks、
# download_cancellations、_queued_downloads、_download_progress、
# _bulk_tasks）存在于进程内存中。使用多个 gunicorn worker
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
                    _queued_downloads.add(r['pixiv_id'])
                    download_executor.submit(_download_illust, r['pixiv_id'])

            if new_illusts:
                with get_session() as db:
                    db.add_all(new_illusts)
                    safe_commit(db)
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
                row.illust_ids = json.dumps(all_ids, ensure_ascii=False)
                row.cached_at = datetime.now(timezone.utc)
                row.status = 'done'
                row.total = len(all_ids)
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


def _prefetch_capacity_cleanup() -> None:
    """容量清理：超出上限时从最旧标签末尾删除未下载未收藏的预取作品。"""
    with get_session() as db:
        count = db.query(Illust).filter(Illust.prefetch_source == 1).count()
        max_illusts = _prefetch_state['max_illusts']
        if count <= max_illusts:
            return
        need_free = count - max_illusts

        to_delete: list[int] = []
        old_tags = db.query(SearchCache).order_by(SearchCache.cached_at.asc()).all()
        for sc in old_tags:
            if need_free <= 0:
                break
            try:
                ids = json.loads(sc.illust_ids) if sc.illust_ids else []
            except (json.JSONDecodeError, TypeError):
                continue
            # 从尾部（最旧条目）找可删作品；受保护的作品保留在标签列表中
            delete_from_tag: list[int] = []
            other_pids = _collect_other_tag_pids(db, sc.tag)
            for pid in reversed(ids):
                if need_free <= 0:
                    break
                if not isinstance(pid, int):
                    continue
                # 仍被其他 SearchCache 引用时跳过（防止破坏其他标签的缓存）
                if pid in other_pids:
                    continue
                illust = db.query(Illust).filter(Illust.pixiv_id == pid).first()
                if illust is None or illust.download_status == 'done' or illust.local_paths_list:
                    continue
                if db.query(CollectionItem).filter(CollectionItem.pixiv_id == pid).first():
                    continue
                delete_from_tag.append(pid)
                need_free -= 1

            if delete_from_tag:
                # 只移除真正删除的 pid，受保护的作品留在列表中
                new_ids = [p for p in ids if p not in delete_from_tag]
                sc.illust_ids = json.dumps(new_ids, ensure_ascii=False)
                to_delete.extend(delete_from_tag)
                safe_commit(db)

        if to_delete:
            db.query(Illust).filter(Illust.pixiv_id.in_(to_delete)).delete(synchronize_session=False)
            safe_commit(db)
            logger.info(f'[prefetch] 容量清理: 删除 {len(to_delete)} 条最旧预取作品')


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
                     limit: int = 24) -> tuple[list[dict], bool, int]:
    """从 SearchCache + Illust 表查询预取结果，支持库内过滤排序分页。

    Returns:
        (results_dicts, has_more, next_offset)
    """
    with get_session() as db:
        sc = db.query(SearchCache).filter(
            SearchCache.tag == tag,
            SearchCache.status == 'done',
        ).first()
        if not sc:
            return [], False, 0

        try:
            all_ids = json.loads(sc.illust_ids) if sc.illust_ids else []
        except (json.JSONDecodeError, TypeError):
            all_ids = []
        if not all_ids:
            return [], False, 0

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

    return [i.to_dict() for i in page], has_more, next_offset


download_executor = ThreadPoolExecutor(max_workers=DOWNLOAD_MAX_WORKERS)
download_locks: dict[int, threading.Lock] = {}
download_cancellations: set[int] = set()
_queued_downloads: set[int] = set()
_download_progress: dict[int, dict] = {}



def _download_illust(pixiv_id: int) -> None:
    """后台任务：下载作品的所有原图。"""
    lock = download_locks.setdefault(pixiv_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return  # 正在下载中，跳过
    try:
        _download_progress[pixiv_id] = {'current': 0, 'total': 0}
        with get_session() as db:
            illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
            if not illust:
                return

            _queued_downloads.discard(pixiv_id)
            illust.download_status = 'downloading'
            db.add(DownloadLog(pixiv_id=pixiv_id, action='start', message=f'开始下载: {illust.title or pixiv_id}'))
            safe_commit(db)

            urls = illust.original_urls_list
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
                        pass
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
    if not url or not url.startswith('/') or url.startswith('//'):
        return '/'
    return url


@app.route('/login', methods=['GET'])
def login_page():
    if _is_authed():
        return redirect(_safe_next(request.args.get('next', '')))
    return render_template('login.html', csrf_token=_get_csrf_token())


@app.route('/login', methods=['POST'])
@_rate_limit(max_attempts=5, window=60)
@_csrf_required
def login_submit():
    body = request.get_json(silent=True) or {}
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
    # 宽松版 CSP：内联 script 抽离到 static/（批次 C）后收紧为 script-src 'self'
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:"
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
    detail = _get_illust_detail(session, pixiv_id)
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

    fn 返回两种形态之一：
    - 元组 (results, cursor, has_more)
    - 字典 {results, cursor, has_more, fetch_stats, source, cached_at}
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
            result = fn()
            if isinstance(result, dict):
                task['results'] = result.get('results', [])
                task['cursor'] = result.get('cursor')
                task['has_more'] = result.get('has_more', False)
                task['fetch_stats'] = result.get('fetch_stats') or fetcher.get_last_fetch_stats()
                task['source'] = result.get('source')
                task['cached_at'] = result.get('cached_at')
            else:
                results, next_cursor, has_more = result
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

    # 命中预取缓存：返回库内结果，不请求 Pixiv
    if search_type == 'tag' and query and not cursor_str:
        with get_session() as db:
            sc = db.query(SearchCache).filter(
                SearchCache.tag == query,
                SearchCache.status == 'done',
            ).first()
            if sc:
                cached_at = sc.cached_at.isoformat() if sc.cached_at else None

                def _cache_fn():
                    results, has_more, next_offset = query_cached_tag(
                        query, min_bookmarks, sort_order, tag_mode, r18_mode,
                        offset=0, limit=ITEMS_PER_PAGE,
                    )
                    next_cursor = None
                    if has_more:
                        next_cursor = encode_cursor({
                            'type': 'tag',
                            'query': query,
                            'sort': sort_order,
                            'tag_mode': tag_mode,
                            'r18_mode': r18_mode,
                            'min_bookmarks': min_bookmarks,
                            'cache_offset': next_offset,
                            'cached_at': cached_at,
                            'created_at': int(time.time()),
                        })
                    return {
                        'results': results,
                        'cursor': next_cursor,
                        'has_more': has_more,
                        'fetch_stats': {'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0},
                        'cached_at': cached_at,
                        'source': 'cache',
                    }

                task_id = _submit_search_task(_cache_fn)
                logger.info(
                    f'[search] 命中预取缓存 task={task_id} query={query!r} '
                    f'min={min_bookmarks} sort={sort_order} r18={r18_mode}'
                )
                return jsonify({'task_id': task_id, 'status': 'running'})

    # 缓存游标翻页
    if cursor_data and 'cache_offset' in cursor_data:
        cache_offset = cursor_data.get('cache_offset', 0)
        cache_tag = cursor_data.get('query', '')
        if cache_tag and search_type == 'tag':

            def _cache_page_fn():
                results, has_more, next_offset = query_cached_tag(
                    cache_tag, min_bookmarks, sort_order, tag_mode, r18_mode,
                    offset=cache_offset, limit=ITEMS_PER_PAGE,
                )
                next_cursor = None
                if has_more:
                    next_cursor = encode_cursor({
                        **cursor_data,
                        'cache_offset': next_offset,
                        'created_at': int(time.time()),
                    })
                return {
                    'results': results,
                    'cursor': next_cursor,
                    'has_more': has_more,
                    'fetch_stats': {'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0},
                    'cached_at': cursor_data.get('cached_at'),
                    'source': 'cache',
                }

            task_id = _submit_search_task(_cache_page_fn)
            return jsonify({'task_id': task_id, 'status': 'running'})

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
    if task.get('source'):
        resp['source'] = task['source']
    if task.get('cached_at'):
        resp['cached_at'] = task['cached_at']
    if task['status'] == 'error':
        resp['error'] = task['error']
        if task['error'] == 'auth':
            return jsonify(resp), 401
        return jsonify(resp), 502
    return jsonify(resp)


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
    body = request.get_json(silent=True) or {}
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
            download_cancellations.discard(pixiv_id)
            _queued_downloads.discard(pixiv_id)
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
        resp = build_pixiv_session().get(url, timeout=(10, 30))
        resp.raise_for_status()
    except requests.RequestException:
        return abort(502)

    mimetype = resp.headers.get('Content-Type', 'image/jpeg')
    try:
        with open(cache_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        with open(meta_path, 'w') as f:
            f.write(mimetype)
    except OSError:
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
        os.path.join(ddir, f) for f in os.listdir(ddir)
        if os.path.isfile(os.path.join(ddir, f))
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

        data = illust.to_dict()
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

        if not illust.original_urls_list:
            urls = _fetch_original_urls(pixiv_id)
            if urls:
                illust.original_urls_list = urls
                safe_commit(db)

        medium_urls = []
        original_proxied = []
        for url in illust.original_urls_list or []:
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
            pid_phs = ','.join(f':local_pid_{i}' for i in range(len(local_pids)))
            wheres = [f"(illusts.download_status = 'done' OR illusts.pixiv_id IN ({pid_phs}))"]
            params = {f'local_pid_{i}': pid for i, pid in enumerate(local_pids)}
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

        page_params = {**params, 'lim': limit, 'off': offset}
        if is_collection_view:
            params['collection_id'] = collection_id
            page_params['collection_id'] = collection_id
            row = db.execute(
                text(f'SELECT COUNT(*) FROM illusts '
                     f'JOIN collection_items ON collection_items.pixiv_id = illusts.pixiv_id '
                     f'WHERE collection_items.collection_id = :collection_id AND {where_clause}'),
                params
            ).one()
            total = row[0] or 0
            fav_total = 0
            pk_ids = db.execute(
                text(f'SELECT illusts.id FROM illusts '
                     f'JOIN collection_items ON collection_items.pixiv_id = illusts.pixiv_id '
                     f'WHERE collection_items.collection_id = :collection_id AND {where_clause} '
                     f'ORDER BY collection_items.position ASC '
                     f'LIMIT :lim OFFSET :off'),
                page_params
            ).scalars().all()
        else:
            row = db.execute(
                text(f'SELECT COUNT(*) AS total FROM illusts WHERE {where_clause}'),
                params
            ).one()
            total = row[0] or 0
            fav_total = 0
            order_col = 'downloaded_at DESC' if sort == 'downloaded' else 'created_at DESC'
            pk_ids = db.execute(
                text(f'SELECT id FROM illusts WHERE {where_clause} ORDER BY {order_col} LIMIT :lim OFFSET :off'),
                page_params
            ).scalars().all()

        # 获取完整 ORM 对象并保持排序
        illusts = db.query(Illust).filter(Illust.id.in_(pk_ids)).all()
        id_order = {id_: i for i, id_ in enumerate(pk_ids)}
        illusts.sort(key=lambda x: id_order.get(x.id, 0))

        results = []
        seen_pids = set()
        fill_ids: list[int] = []
        for i in illusts:
            paths = local_items.get(i.pixiv_id) or i.local_paths_list or []
            if not i.file_size and paths:
                total_size = sum(os.path.getsize(p) for p in paths if os.path.isfile(p))
                if total_size:
                    i.file_size = total_size
            d = i.to_dict(favorite=(i.pixiv_id in default_fav_set))
            d['local_paths'] = paths
            d['file_count'] = len(paths)
            d['local_urls'] = [f'/api/image/{i.pixiv_id}/{n}' for n in range(len(paths))]
            d['local_dir'] = os.path.abspath(_get_download_dir(i.pixiv_id)) if paths else None
            results.append(d)
            seen_pids.add(i.pixiv_id)
            if i.bookmark_count == 0 and not i.original_urls_list:
                fill_ids.append(i.pixiv_id)

        if fill_ids:
            fetcher._kick_background_fill(fill_ids)

        # 补充不在 DB 的本地文件（收藏夹/收藏筛选时不补孤儿，因其不在任何收藏夹中）
        if not collection_id and not favorites_only:
            local_pid_set = set(local_pids)
            orphan_pids = sorted(local_pid_set - seen_pids, reverse=True)
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
    body = request.get_json(silent=True) or {}
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
    body = request.get_json(silent=True) or {}
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
    body = request.get_json(silent=True) or {}
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
    tag = (request.get_json(silent=True) or {}).get('tag', '').strip()
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
            if illust.download_status == 'done' or illust.local_paths_list:
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
    tag = (request.get_json(silent=True) or {}).get('tag', '').strip()
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


# ── 批量下载 ──

_bulk_tasks: dict[str, dict] = {}

def _bulk_worker(task_id: str, tag: str, min_bookmarks: int, sort_order: str, max_pages: int, r18_mode: str = 'all') -> None:
    task = _bulk_tasks[task_id]
    page = 1
    while page <= max_pages and not task['cancelled']:
        task['current_page'] = page
        task['log'].append((datetime.now(timezone.utc).isoformat(), f'搜索第 {page} 页...'))
        try:
            results, has_more = search_by_tag(tag, min_bookmarks, page, sort_order, 9999, 'or',
                                              r18_mode=r18_mode, limiter=fetcher._bulk_limiter)
        except Exception as e:
            task['log'].append((datetime.now(timezone.utc).isoformat(), f'搜索失败: {e}'))
            break
        task['log'].append((datetime.now(timezone.utc).isoformat(), f'第 {page} 页找到 {len(results)} 件'))
        pixiv_ids = [r['pixiv_id'] for r in results]
        with get_session() as db:
            existing_ids = {i.pixiv_id for i in db.query(Illust.pixiv_id).filter(Illust.pixiv_id.in_(pixiv_ids)).all()}
            for r in results:
                pixiv_id = r['pixiv_id']
                if pixiv_id in existing_ids:
                    continue
                illust = Illust(
                    pixiv_id=pixiv_id, title=r['title'], user_id=r['user_id'],
                    user_name=r['user_name'], page_count=r['page_count'],
                    bookmark_count=r['bookmark_count'], thumb_url=r['thumb_url'],
                    upload_date=r['upload_date'],
                )
                illust.tags_list = r.get('tags', [])
                illust.original_urls_list = r.get('original_urls', [])
                db.add(illust)
            safe_commit(db)

        # 已完成的直接处理，剩余的提交并发下载
        futures = {}
        id_result_map = {}
        for r in results:
            if task['cancelled']:
                break
            pixiv_id = r['pixiv_id']
            id_result_map[pixiv_id] = r
            if r.get('download_status') == 'done':
                task['downloaded'] += 1
                task['log'].append((datetime.now(timezone.utc).isoformat(), f'✓ #{pixiv_id} {r.get("title","")[:30]}'))
            else:
                futures[download_executor.submit(_download_illust, pixiv_id)] = pixiv_id

        processed_ids = []
        for future in as_completed(futures):
            pixiv_id = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error(f'批量下载失败 #{pixiv_id}: {e}')
            processed_ids.append(pixiv_id)
            if task['cancelled']:
                break

        # 批量查询：一次往返获取所有已处理项目的状态
        if processed_ids:
            with get_session() as db:
                status_map = {
                    i.pixiv_id: i.download_status
                    for i in db.query(Illust).filter(Illust.pixiv_id.in_(processed_ids)).all()
                }
                for pixiv_id in processed_ids:
                    r = id_result_map.get(pixiv_id)
                    title = r.get('title', '')[:30] if r else ''
                    if status_map.get(pixiv_id) == 'done':
                        task['downloaded'] += 1
                        task['log'].append((datetime.now(timezone.utc).isoformat(), f'✓ #{pixiv_id} {title}'))
                    else:
                        task['failed'] += 1
                        task['log'].append((datetime.now(timezone.utc).isoformat(), f'✗ #{pixiv_id} 下载失败'))
        if not has_more:
            break
        page += 1
        time.sleep(2)
    task['status'] = 'stopped' if task['cancelled'] else 'done'
    task['log'].append((datetime.now(timezone.utc).isoformat(),
        f'完成: 下载 {task["downloaded"]} 件, 失败 {task["failed"]} 件'))
    # 5 分钟后清理任务记录，防止内存泄漏
    threading.Timer(300, lambda: _bulk_tasks.pop(task_id, None)).start()


@app.route('/api/bulk/start', methods=['POST'])
@_csrf_required
def bulk_start() -> Response:
    body = request.get_json(silent=True) or {}
    tag = body.get('tag', '').strip()
    if not tag:
        return jsonify({'error': '请输入标签'}), 400
    min_bookmarks = max(0, int(body.get('min_bookmarks', 0) or 0))
    sort_order = body.get('sort', 'date_d')
    if sort_order not in ('popular_d', 'date_d'):
        sort_order = 'date_d'
    r18_mode = body.get('r18_mode', 'all')
    if r18_mode not in ('all', 'safe'):
        r18_mode = 'all'
    max_pages = max(1, min(100, int(body.get('max_pages', 10) or 10)))
    task_id = secrets.token_hex(8)
    _bulk_tasks[task_id] = {
        'tag': tag, 'min_bookmarks': min_bookmarks, 'sort': sort_order,
        'max_pages': max_pages, 'current_page': 0, 'downloaded': 0, 'failed': 0,
        'status': 'running', 'cancelled': False, 'r18_mode': r18_mode, 'log': [],
    }
    _bulk_tasks[task_id]['log'].append((datetime.now(timezone.utc).isoformat(),
        f'开始: 标签={tag}, 收藏≥{min_bookmarks}, 排序={sort_order}, 最多{max_pages}页'))
    threading.Thread(target=_bulk_worker, args=(task_id, tag, min_bookmarks, sort_order, max_pages, r18_mode), daemon=True).start()
    return jsonify({'task_id': task_id})


@app.route('/api/bulk/status/<task_id>')
def bulk_status(task_id: str) -> Response:
    task = _bulk_tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({k: v for k, v in task.items() if k != 'cancelled'})


@app.route('/api/bulk/running')
def bulk_running() -> Response:
    """返回当前正在运行的任务（如果有）。"""
    for task_id, task in _bulk_tasks.items():
        if task['status'] == 'running':
            return jsonify({'task_id': task_id, **{k: v for k, v in task.items() if k != 'cancelled'}})
    return jsonify({'task_id': None})


@app.route('/api/bulk/stop/<task_id>', methods=['POST'])
@_csrf_required
def bulk_stop(task_id: str) -> Response:
    task = _bulk_tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    task['cancelled'] = True
    return jsonify({'status': 'stopping'})


# ── 下载管理 ──

@app.route('/bulk')
def bulk_page() -> str:
    return render_template('bulk.html', csrf_token=_get_csrf_token())


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
    tag = (request.get_json(silent=True) or {}).get('tag', '').strip()
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

_SETTINGS_DEFAULTS = {
    'proxy': '',
    'download_max_workers': 2,
    'per_page': 60,
    'search_pages': 10,
    'max_bookmarks_default': 0,
    'auto_follow_interval': 600,
    'auto_follow_download': False,
    'fetch_detail_workers': 2,
    'medium_image_size': 600,
    'items_per_page': 24,
    'prefetch_interval': 3600,
    'prefetch_pages': 3,
    'prefetch_max_illusts': 20000,
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
@_rate_limit(max_attempts=5, window=60)
@_csrf_required
def settings_unlock() -> Response:
    if session.get('authed') or not SETTINGS_PASSWORD:
        return jsonify({'ok': True})
    body = request.get_json(silent=True) or {}
    if hmac.compare_digest(str(body.get('password', '')).encode(), SETTINGS_PASSWORD.encode()):
        session['settings_unlocked'] = True
        return jsonify({'ok': True})
    return jsonify({'error': '密码错误'}), 403


@app.route('/api/settings', methods=['GET'])
def api_settings_get() -> Response:
    if _settings_locked():
        return jsonify({'error': '需要密码访问'}), 403
    return jsonify(_load_settings())


@app.route('/api/settings', methods=['POST'])
@_csrf_required
def api_settings_post() -> Response:
    if _settings_locked():
        return jsonify({'error': '需要密码访问'}), 403

    body = request.get_json(silent=True) or {}
    current = _load_settings()

    # Cookie 字段特殊处理：写入项目根目录 cookies.txt，立即更新内存状态
    cookie_val = body.pop('cookie', '').strip()
    if cookie_val:
        cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')
        with open(cookie_path, 'w') as f:
            f.write(f'PHPSESSID={cookie_val}\n')
        fetcher._cookie_value = cookie_val
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
    body = request.get_json(silent=True) or {}
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
    body = request.get_json(silent=True) or {}
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


@app.route('/api/collections/<int:collection_id>/items', methods=['POST'])
@_csrf_required
def add_collection_item(collection_id: int) -> Response:
    body = request.get_json(silent=True) or {}
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
        max_row = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id
        ).order_by(CollectionItem.position.desc()).first()
        next_pos = (max_row.position + 1000.0) if max_row else 1000.0
        item = CollectionItem(collection_id=collection_id, pixiv_id=pixiv_id, position=next_pos)
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
    body = request.get_json(silent=True) or {}
    pixiv_ids = body.get('pixiv_ids', [])
    if not pixiv_ids or not isinstance(pixiv_ids, list):
        return jsonify({'error': '请提供作品ID列表'}), 400
    pixiv_ids = [int(pid) for pid in pixiv_ids]
    with get_session() as db:
        if not db.query(Collection).filter(Collection.id == collection_id).first():
            return jsonify({'error': '收藏夹不存在'}), 404
        max_row = db.query(CollectionItem).filter(
            CollectionItem.collection_id == collection_id
        ).order_by(CollectionItem.position.desc()).first()
        next_pos = (max_row.position + 1000.0) if max_row else 1000.0
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
    body = request.get_json(silent=True) or {}
    pixiv_ids = body.get('pixiv_ids', [])
    if not pixiv_ids or not isinstance(pixiv_ids, list):
        return jsonify({'error': '请提供作品ID列表'}), 400
    pixiv_ids = [int(pid) for pid in pixiv_ids]
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
    body = request.get_json(silent=True) or {}
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

        old_pos = current.position
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
    body = request.get_json(silent=True) or {}
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
            max_row = db.query(CollectionItem).filter(
                CollectionItem.collection_id == default.id
            ).order_by(CollectionItem.position.desc()).first()
            next_pos = (max_row.position + 1000.0) if max_row else 1000.0
            db.add(CollectionItem(collection_id=default.id, pixiv_id=pixiv_id, position=next_pos))
            safe_commit(db)
            return jsonify({'is_favorite': True})


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
