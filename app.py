from __future__ import annotations

import hashlib
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
    send_file, abort, Response,
)
from sqlalchemy import text

from config import (
    DOWNLOAD_DIR, DOWNLOAD_MAX_WORKERS, PAGE_DOWNLOAD_INTERVAL,
    MAX_BOOKMARKS_DEFAULT, AUTO_FOLLOW_INTERVAL, AUTO_FOLLOW_DOWNLOAD,
    SETTINGS_PASSWORD,
    SSL_VERIFY,
)
from models import init_db, get_session, Illust, DownloadLog, BlockedTag, Collection, CollectionItem, safe_commit
from fetcher import search_by_tag, search_by_user, fetch_following, browse_discovery, _build_session, _get_illust_detail

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 过滤掉请求头日志，防止 Cookie 泄露
logging.getLogger('werkzeug').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

app = Flask(__name__)

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
            'description': '',
            'original_urls': [],
            'local_paths': paths,
            'file_count': len(paths),
            'local_urls': [f'/api/image/{pid}/{n}' for n in range(len(paths))],
            'local_dir': os.path.abspath(_get_download_dir(pid)),
            'download_status': 'done',
            'downloaded_at': None,
            'file_size': total_size,
            'is_favorite': False,
            'favorited_at': None,
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

def _auto_follow_worker() -> None:
    while not _auto_follow_stop.is_set():
        interval = _auto_follow_state['interval']
        if interval <= 0:
            _auto_follow_stop.wait(30)
            continue
        try:
            results, _ = fetch_following(page=1)
            if not results:
                _auto_follow_stop.wait(interval)
                continue
            pixiv_ids = [r['pixiv_id'] for r in results]
            with get_session() as db:
                existing_ids = {i.pixiv_id for i in db.query(Illust.pixiv_id).filter(Illust.pixiv_id.in_(pixiv_ids)).all()}

            new_illusts = []
            for r in results:
                if r['pixiv_id'] in existing_ids:
                    continue
                illust = Illust(
                    pixiv_id=r['pixiv_id'], title=r['title'],
                    user_id=r['user_id'], user_name=r['user_name'],
                    page_count=r['page_count'], bookmark_count=r['bookmark_count'],
                    thumb_url=r['thumb_url'], upload_date=r['upload_date'],
                    description=r.get('description', ''),
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

            session_obj = requests.Session()
            session_obj.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://www.pixiv.net/',
            })
            session_obj.verify = SSL_VERIFY

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
        if not token or token != session.get('_csrf_token', ''):
            return jsonify({'error': 'CSRF校验失败'}), 403
        return f(*args, **kwargs)
    return decorated


def _original_to_resized(url: str) -> str:
    """Pixiv 原图 URL → 标准中等尺寸（master1200，最长边 1200px）。"""
    m = re.match(r'(https://i\.pximg\.net/)img-original/img/(.+)\.(\w+)(\?.*)?$', url)
    if not m:
        return url
    return f'{m.group(1)}c/1200x1200/img-master/img/{m.group(2)}_master1200.{m.group(3)}'


def _proxy_thumb(url: str) -> str:
    if not url:
        return ''
    return '/thumb/' + urlsafe_b64encode(url.encode()).decode().rstrip('=').replace('+', '-').replace('/', '_')


def _fetch_original_urls(pixiv_id: int) -> list[str]:
    """按需拉取 Pixiv 详情，返回 original_urls。用于惰性详情场景。"""
    session = _build_session()
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
    return render_template('index.html', csrf_token=_get_csrf_token())


@app.route('/search')
def search() -> Response:
    search_type = request.args.get('type', 'tag')
    query = request.args.get('query', '').strip()
    min_bookmarks = request.args.get('min_bookmarks', MAX_BOOKMARKS_DEFAULT)
    start_page = request.args.get('page', '1')
    sort_order = request.args.get('sort', 'date_d')

    if search_type == 'user' and not query:
        return jsonify({'error': '请输入画师ID'}), 400

    try:
        min_bookmarks = int(min_bookmarks)
    except (ValueError, TypeError):
        min_bookmarks = MAX_BOOKMARKS_DEFAULT

    try:
        start_page = int(start_page)
    except (ValueError, TypeError):
        start_page = 1
    start_page = max(1, start_page)

    tag_mode = request.args.get('tag_mode', 'or')
    if tag_mode not in ('or', 'and'):
        tag_mode = 'or'

    if sort_order not in ('popular_d', 'date_d'):
        sort_order = 'date_d'

    r18_mode = request.args.get('r18_mode', 'all')
    if r18_mode not in ('all', 'safe'):
        r18_mode = 'all'

    logger.info(f'搜索：type={search_type}, query={query!r}, min={min_bookmarks}, start={start_page}, sort={sort_order}, tag_mode={tag_mode}')

    all_results = []
    has_more = False
    try:
        if search_type == 'tag':
            if len(query) > 200:
                return jsonify({'error': '搜索关键词过长'}), 400
            if not query:
                all_results, has_more = browse_discovery(start_page, sort_order, min_bookmarks, r18_mode=r18_mode)
            else:
                all_results, has_more = search_by_tag(query, min_bookmarks, start_page, sort_order, 9999, tag_mode, r18_mode=r18_mode)
        else:
            if not query.isdigit():
                return jsonify({'error': '画师ID必须为数字'}), 400
            all_results, has_more = search_by_user(query, min_bookmarks, start_page, hide_r18=(r18_mode == 'safe'))
    except FileNotFoundError as e:
        logger.error(f'搜索失败 - 文件未找到：{e}')
        return jsonify({'error': f'缺少文件: {e}'}), 500
    except Exception as e:
        logger.error(f'搜索失败：{e}', exc_info=True)
        return jsonify({'error': f'搜索出错: {e}'}), 500

    return jsonify({'results': all_results, 'has_more': has_more})


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
    results, has_more = fetch_following(page, r18_mode=r18_mode)
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
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.pixiv.net/',
        })
        s.verify = SSL_VERIFY
        resp = s.get(url, timeout=(10, 30))
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

    # 扫描本地 downloads 目录
    local_items = _scan_local_downloads()
    local_pids = sorted(local_items.keys(), reverse=True)

    with get_session() as db:
        blocked = {t.tag for t in db.query(BlockedTag).all()}

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

        where_clause = ' AND '.join(wheres)

        # 合计总数 + 收藏数
        row = db.execute(
            text(f'SELECT COUNT(*) AS total, SUM(CASE WHEN is_favorite=1 THEN 1 ELSE 0 END) AS fav_total FROM illusts WHERE {where_clause}'),
            params
        ).one()
        total = row[0] or 0
        fav_total = row[1] or 0

        # 分页查询 ID
        order_col = 'downloaded_at DESC' if sort == 'downloaded' else 'created_at DESC'
        page_params = {**params, 'lim': limit, 'off': offset}
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
        for i in illusts:
            paths = local_items.get(i.pixiv_id) or i.local_paths_list or []
            if not i.file_size and paths:
                total_size = sum(os.path.getsize(p) for p in paths if os.path.isfile(p))
                if total_size:
                    i.file_size = total_size
            d = i.to_dict()
            d['local_paths'] = paths
            d['file_count'] = len(paths)
            d['local_urls'] = [f'/api/image/{i.pixiv_id}/{n}' for n in range(len(paths))]
            d['local_dir'] = os.path.abspath(_get_download_dir(i.pixiv_id)) if paths else None
            results.append(d)
            seen_pids.add(i.pixiv_id)

        # 补充不在 DB 的本地文件
        local_pid_set = set(local_pids)
        orphan_pids = sorted(local_pid_set - seen_pids, reverse=True)
        orphan_results = _build_orphan_dicts(orphan_pids, local_items)
        total += len(orphan_results)
        results.extend(orphan_results[:max(0, limit - len(results))])

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


# ── 批量下载 ──

_bulk_tasks: dict[str, dict] = {}

def _bulk_worker(task_id: str, tag: str, min_bookmarks: int, sort_order: str, max_pages: int, r18_mode: str = 'all') -> None:
    task = _bulk_tasks[task_id]
    page = 1
    while page <= max_pages and not task['cancelled']:
        task['current_page'] = page
        task['log'].append((datetime.now(timezone.utc).isoformat(), f'搜索第 {page} 页...'))
        try:
            results, has_more = search_by_tag(tag, min_bookmarks, page, sort_order, 9999, 'or', r18_mode=r18_mode)
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
                    description=r.get('description', ''),
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


@app.route('/settings')
def settings_page() -> str:
    if SETTINGS_PASSWORD and not session.get('settings_unlocked'):
        return render_template('settings_unlock.html', csrf_token=_get_csrf_token())
    return render_template('settings.html', csrf_token=_get_csrf_token())


@app.route('/api/settings/unlock', methods=['POST'])
@_rate_limit(max_attempts=5, window=60)
@_csrf_required
def settings_unlock() -> Response:
    body = request.get_json(silent=True) or {}
    if body.get('password') == SETTINGS_PASSWORD:
        session['settings_unlocked'] = True
        return jsonify({'ok': True})
    return jsonify({'error': '密码错误'}), 403


@app.route('/api/settings', methods=['GET'])
def api_settings_get() -> Response:
    if SETTINGS_PASSWORD and not session.get('settings_unlocked'):
        return jsonify({'error': '需要密码访问'}), 403
    return jsonify(_load_settings())


@app.route('/api/settings', methods=['POST'])
@_csrf_required
def api_settings_post() -> Response:
    if SETTINGS_PASSWORD and not session.get('settings_unlocked'):
        return jsonify({'error': '需要密码访问'}), 403

    body = request.get_json(silent=True) or {}
    current = _load_settings()
    # 仅合并已知的配置键
    for key in _SETTINGS_DEFAULTS:
        if key in body:
            val = body[key]
            if key in ('auto_follow_download',):
                val = bool(val)
            elif key in ('download_max_workers', 'per_page', 'search_pages',
                         'max_bookmarks_default', 'auto_follow_interval'):
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


def _sync_is_favorite(pixiv_id: int) -> None:
    """根据收藏夹归属重新计算 is_favorite。"""
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return
        count = db.query(CollectionItem).filter(CollectionItem.pixiv_id == pixiv_id).count()
        illust.is_favorite = count > 0
        illust.favorited_at = datetime.now(timezone.utc) if count > 0 else None
        safe_commit(db)


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
def delete_collection(collection_id: int) -> Response:
    with get_session() as db:
        c = db.query(Collection).filter(Collection.id == collection_id).first()
        if not c:
            return jsonify({'error': '收藏夹不存在'}), 404
        affected = [item.pixiv_id for item in db.query(CollectionItem).filter(CollectionItem.collection_id == collection_id).all()]
        db.query(CollectionItem).filter(CollectionItem.collection_id == collection_id).delete()
        db.delete(c)
        safe_commit(db)
        for pid in affected:
            _sync_is_favorite(pid)
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
        ).order_by(CollectionItem.created_at.desc()).offset(offset).limit(limit).all()
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
        item = CollectionItem(collection_id=collection_id, pixiv_id=pixiv_id)
        db.add(item)
        safe_commit(db)
        data = item.to_dict()
    _sync_is_favorite(pixiv_id)
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
    _sync_is_favorite(pixiv_id)
    return jsonify({'status': 'deleted'})


@app.route('/api/illust/<int:pixiv_id>/collections')
def illust_collections(pixiv_id: int) -> Response:
    with get_session() as db:
        items = db.query(CollectionItem).filter(CollectionItem.pixiv_id == pixiv_id).all()
        return jsonify([item.collection_id for item in items])


# ── 收藏 ──


@app.route('/api/open-dir', methods=['POST'])
@_csrf_required
def api_open_dir() -> Response:
    """打开本地文件夹（仅限本机浏览器访问时有效）。"""
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
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        return jsonify({'is_favorite': illust.is_favorite if illust else False})


@app.route('/api/favorite/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def api_favorite_post(pixiv_id: int) -> Response:
    """切换'我的收藏'收藏夹中的归属（向后兼容）。"""
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
            _sync_is_favorite(pixiv_id)
        else:
            db.add(CollectionItem(collection_id=default.id, pixiv_id=pixiv_id))
            safe_commit(db)
            _sync_is_favorite(pixiv_id)
        # 重新读取以获取更新后的状态
        db.refresh(illust)
        return jsonify({'is_favorite': illust.is_favorite})


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
