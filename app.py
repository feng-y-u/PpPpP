import hashlib
import logging
import os
import re
import secrets
import threading
import time
import zipfile
from base64 import urlsafe_b64decode, urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import wraps
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
    SSL_VERIFY,
)
from models import init_db, get_session, Illust, DownloadLog, BlockedTag, safe_commit
from fetcher import search_by_tag, search_by_user, fetch_following, browse_discovery

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
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1MB max upload

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

init_db()


def _get_download_dir(pixiv_id: int) -> str:
    return os.path.join(DOWNLOAD_DIR, str(pixiv_id))


def _reset_stuck_downloads():
    """Startup: reset any 'downloading' status left over from a previous crash/restart."""
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
        logger.info(f'Reset {len(stuck)} stuck downloads from previous session')


_reset_stuck_downloads()

# ── Auto-Follow Worker ──
_auto_follow_state = {
    'last_check': None,
    'last_count': 0,
    'interval': AUTO_FOLLOW_INTERVAL,
    'auto_download': AUTO_FOLLOW_DOWNLOAD,
}
_auto_follow_stop = threading.Event()
_auto_follow_stop.set()  # Active by default

def _auto_follow_worker():
    while _auto_follow_stop.is_set():
        interval = _auto_follow_state['interval']
        if interval <= 0:
            if _auto_follow_stop.wait(30):
                return
            continue
        try:
            results, _ = fetch_following(page=1)
            if not results:
                _auto_follow_stop.wait(interval)
                continue
            pixiv_ids = [r['pixiv_id'] for r in results]
            with get_session() as db:
                existing_ids = {i.pixiv_id for i in db.query(Illust.pixiv_id).filter(Illust.pixiv_id.in_(pixiv_ids)).all()}

            new_count = 0
            for r in results:
                if r['pixiv_id'] in existing_ids:
                    continue
                with get_session() as db:
                    illust = Illust(
                        pixiv_id=r['pixiv_id'], title=r['title'],
                        user_id=r['user_id'], user_name=r['user_name'],
                        page_count=r['page_count'], bookmark_count=r['bookmark_count'],
                        thumb_url=r['thumb_url'], upload_date=r['upload_date'],
                    )
                    illust.tags_list = r.get('tags', [])
                    illust.original_urls_list = r.get('original_urls', [])
                    db.add(illust)
                    safe_commit(db)
                    new_count += 1
                    if _auto_follow_state['auto_download'] and illust.original_urls_list:
                        _queued_downloads.add(r['pixiv_id'])
                        download_executor.submit(_download_illust, r['pixiv_id'])
            _auto_follow_state['last_check'] = datetime.now(timezone.utc).isoformat()
            _auto_follow_state['last_count'] = new_count
            if new_count:
                logger.info(f'Auto-follow: found {new_count} new works')
        except Exception as e:
            logger.error(f'Auto-follow error: {e}')
        if _auto_follow_stop.wait(interval):
            return

_auto_follow_thread = threading.Thread(target=_auto_follow_worker, daemon=True)
_auto_follow_thread.start()

download_executor = ThreadPoolExecutor(max_workers=DOWNLOAD_MAX_WORKERS)
download_locks: dict[int, threading.Lock] = {}
download_cancellations: set[int] = set()
_queued_downloads: set[int] = set()
_download_progress: dict[int, dict] = {}


def _enrich_with_download_status(results: list[dict]) -> list[dict]:
    """Re-fetch results from DB to include current download_status/local_paths."""
    if not results:
        return results
    pixiv_ids = [r['pixiv_id'] for r in results]
    with get_session() as db:
        illusts = db.query(Illust).filter(Illust.pixiv_id.in_(pixiv_ids)).all()
        illust_map = {i.pixiv_id: i.to_dict() for i in illusts}
    return [illust_map.get(r['pixiv_id'], r) for r in results]


def _download_illust(pixiv_id: int):
    """Background task: download all original images for an illust."""
    lock = download_locks.setdefault(pixiv_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return  # Already being downloaded
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
                    logger.error(f'Download failed for {pixiv_id} page {i}: {e}')
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
    """Extract file extension from image URL."""
    match = re.search(r'\.(jpg|jpeg|png|gif|webp)(?:\?|$)', url, re.IGNORECASE)
    return match.group(1) if match else 'jpg'


def _get_csrf_token() -> str:
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']


def _csrf_required(f):
    """Decorator: require valid X-CSRF-Token header for POST endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-CSRF-Token', '')
        if not token or token != session.get('_csrf_token', ''):
            return jsonify({'error': 'CSRF校验失败'}), 403
        return f(*args, **kwargs)
    return decorated


def _proxy_thumb(url):
    if not url:
        return ''
    return '/thumb/' + urlsafe_b64encode(url.encode()).decode().rstrip('=').replace('+', '-').replace('/', '_')


def _fmt_num(n):
    if not n:
        return '0'
    n = int(n)
    return f'{n/10000:.1f}w' if n >= 10000 else str(n)


@app.route('/')
def index():
    return render_template('index.html', csrf_token=_get_csrf_token())


@app.route('/search')
def search():
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

    logger.info(f'Search: type={search_type}, query={query!r}, min={min_bookmarks}, start={start_page}, sort={sort_order}, tag_mode={tag_mode}')

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
        logger.error(f'Search failed - file not found: {e}')
        return jsonify({'error': f'缺少文件: {e}'}), 500
    except Exception as e:
        logger.error(f'Search failed: {e}', exc_info=True)
        return jsonify({'error': f'搜索出错: {e}'}), 500

    all_results = _enrich_with_download_status(all_results)
    return jsonify({'results': all_results, 'has_more': has_more})


@app.route('/api/following')
def api_following():
    page = request.args.get('page', '1')
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        page = 1
    r18_mode = request.args.get('r18_mode', 'all')
    if r18_mode not in ('all', 'safe'):
        r18_mode = 'all'
    results, has_more = fetch_following(page, r18_mode=r18_mode)
    results = _enrich_with_download_status(results)
    return jsonify({'results': results, 'has_more': has_more})


@app.route('/download/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def trigger_download(pixiv_id):
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404

        if illust.download_status == 'done':
            return jsonify({'status': 'done', 'message': '已下载'})

        if illust.download_status == 'downloading':
            return jsonify({'status': 'downloading', 'message': '下载中'})

        if not illust.original_urls_list:
            return jsonify({'error': '无原图链接'}), 400

    _queued_downloads.add(pixiv_id)
    download_executor.submit(_download_illust, pixiv_id)
    return jsonify({'status': 'accepted', 'message': '已加入下载队列'})


@app.route('/api/download/batch', methods=['POST'])
@_csrf_required
def batch_download():
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


def _cancel_download_internal(pixiv_id, reset=False):
    """Mark a download for cancellation, optionally cleaning up partial files."""
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
            return jsonify({'status': 'reset', 'message': '已重置'})

        return jsonify({'status': 'cancelling', 'message': '正在取消...'})


@app.route('/download/cancel/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def cancel_download(pixiv_id):
    return _cancel_download_internal(pixiv_id, reset=False)


@app.route('/download/reset/<int:pixiv_id>', methods=['POST'])
@_csrf_required
def reset_download(pixiv_id):
    return _cancel_download_internal(pixiv_id, reset=True)


@app.route('/download_status/<int:pixiv_id>')
def download_status(pixiv_id):
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404
        return jsonify({
            'status': illust.download_status or 'none',
            'local_paths': illust.local_paths_list,
        })


@app.route('/download_file/<int:pixiv_id>')
def download_file(pixiv_id):
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
def csrf_token():
    return jsonify({'token': _get_csrf_token()})


@app.route('/thumb/<path:url_b64>')
def thumb_proxy(url_b64):
    """代理 Pixiv 缩略图，绕过 Referer 检查。url_b64 为 base64(urlencode) 编码的原始 URL。"""
    try:
        padding = 4 - len(url_b64) % 4
        if padding != 4:
            url_b64 += '=' * padding
        url = urlsafe_b64decode(url_b64.encode()).decode()
    except Exception:
        return abort(400)

    # 仅允许 Pixiv CDN
    if not url.startswith('https://i.pximg.net/'):
        return abort(403)

    try:
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.pixiv.net/',
        })
        s.verify = SSL_VERIFY
        resp = s.get(url, timeout=(5, 15))
        resp.raise_for_status()
    except requests.RequestException:
        return abort(502)

    cache_timeout = timedelta(hours=6)
    return Response(
        resp.content,
        mimetype=resp.headers.get('Content-Type', 'image/jpeg'),
        headers={
            'Cache-Control': f'public, max-age={int(cache_timeout.total_seconds())}',
            'ETag': hashlib.md5(url.encode()).hexdigest(),
        },
    )


@app.route('/api/image/<int:pixiv_id>/<int:index>')
def serve_image(pixiv_id, index):
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust or illust.download_status != 'done' or not illust.local_paths_list:
            abort(404)
        paths = illust.local_paths_list
        if index < 0 or index >= len(paths):
            abort(404)
        filepath = paths[index]
        if not os.path.isfile(filepath):
            abort(404)
        return send_file(filepath)


@app.route('/detail/<int:pixiv_id>')
def detail_page(pixiv_id):
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            abort(404)

        data = illust.to_dict()
        paths = illust.local_paths_list or []
        local_urls = [f'/api/image/{pixiv_id}/{n}' for n in range(len(paths))]

        file_size = illust.file_size or None

        # Related: same user, exclude self
        related = db.query(Illust).filter(
            Illust.user_id == illust.user_id,
            Illust.pixiv_id != pixiv_id,
            Illust.download_status == 'done',
        ).order_by(Illust.created_at.desc()).limit(6).all()
        related = [r.to_dict() for r in related]

        return render_template(
            'detail.html',
            illust=data,
            local_urls=local_urls,
            file_size=file_size,
            related=related,
            proxy_thumb=_proxy_thumb,
            fmt_num=_fmt_num,
            csrf_token=_get_csrf_token(),
        )


@app.route('/api/detail/<int:pixiv_id>')
def detail_api(pixiv_id):
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
def gallery():
    return render_template('gallery.html', csrf_token=_get_csrf_token())


@app.route('/api/gallery')
def api_gallery():
    tag_filter = request.args.get('tag', '').strip()
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    favorites_only = request.args.get('favorites', '').lower() == 'true'
    limit = max(1, min(200, limit))
    offset = max(0, offset)

    with get_session() as db:
        blocked = {t.tag for t in db.query(BlockedTag).all()}

        base_q = db.query(Illust).filter(Illust.download_status == 'done')
        if favorites_only:
            base_q = base_q.filter(Illust.is_favorite == True)
        total = base_q.count()
        illusts = base_q.order_by(Illust.created_at.desc()).limit(limit).offset(offset).all()

        results = []
        for i in illusts:
            if blocked and set(i.tags_list) & blocked:
                continue
            if tag_filter and tag_filter not in i.tags_list:
                continue
            paths = i.local_paths_list or []
            if not i.file_size and paths:
                total_size = sum(os.path.getsize(p) for p in paths if os.path.isfile(p))
                if total_size:
                    i.file_size = total_size
            d = i.to_dict()
            d['file_count'] = len(paths)
            d['local_urls'] = [f'/api/image/{i.pixiv_id}/{n}' for n in range(len(paths))]
            results.append(d)
        safe_commit(db)

        # 计算收藏总数
        fav_total = db.query(Illust).filter(
            Illust.download_status == 'done',
            Illust.is_favorite == True,
        ).count()

        return jsonify({
            'data': results,
            'total': total,
            'favorite_total': fav_total,
            'has_more': offset + limit < total,
        })


@app.route('/api/gallery/tags')
def api_gallery_tags():
    with get_session() as db:
        rows = db.execute(text("""
            SELECT DISTINCT j.value AS tag
            FROM illusts, json_each(illusts.tags) AS j
            WHERE illusts.download_status = 'done'
            ORDER BY tag
        """)).all()
        return jsonify([row[0] for row in rows])


def _delete_illust_files(illust):
    """Remove downloaded files and directory for an illust. Returns file count."""
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
def delete_gallery(pixiv_id):
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
def batch_delete_gallery():
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
            total_files += _delete_illust_files(illust)
            db.add(DownloadLog(pixiv_id=illust.pixiv_id, action='deleted', message=f'已删除 {len(illust.local_paths_list or [])} 个文件'))
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



# ── Auto-Follow Control ──

@app.route('/api/auto-follow/status')
def auto_follow_status():
    return jsonify(_auto_follow_state)

@app.route('/api/auto-follow/config', methods=['POST'])
@_csrf_required
def auto_follow_config():
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


# ── Bulk Download ──

_bulk_tasks: dict[str, dict] = {}

def _bulk_worker(task_id: str, tag: str, min_bookmarks: int, sort_order: str, max_pages: int, r18_mode: str = 'all'):
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
                )
                illust.tags_list = r.get('tags', [])
                illust.original_urls_list = r.get('original_urls', [])
                db.add(illust)
            safe_commit(db)

        for r in results:
            if task['cancelled']:
                break
            pixiv_id = r['pixiv_id']
            if r.get('download_status') != 'done':
                download_executor.submit(_download_illust, pixiv_id).result()
            with get_session() as db:
                illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
                if illust and illust.download_status == 'done':
                    task['downloaded'] += 1
                    task['log'].append((datetime.now(timezone.utc).isoformat(), f'✓ #{pixiv_id} {r.get("title","")[:30]}'))
                else:
                    task['failed'] += 1
                    task['log'].append((datetime.now(timezone.utc).isoformat(), f'✗ #{pixiv_id} 下载失败'))
            if task['cancelled']:
                break
            time.sleep(1.5)
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
def bulk_start():
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
def bulk_status(task_id):
    task = _bulk_tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({k: v for k, v in task.items() if k != 'cancelled'})


@app.route('/api/bulk/running')
def bulk_running():
    """Return the currently running task if any."""
    for task_id, task in _bulk_tasks.items():
        if task['status'] == 'running':
            return jsonify({'task_id': task_id, **{k: v for k, v in task.items() if k != 'cancelled'}})
    return jsonify({'task_id': None})


@app.route('/api/bulk/stop/<task_id>', methods=['POST'])
@_csrf_required
def bulk_stop(task_id):
    task = _bulk_tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    task['cancelled'] = True
    return jsonify({'status': 'stopping'})


# ── Download Management ──

@app.route('/bulk')
def bulk_page():
    return render_template('bulk.html', csrf_token=_get_csrf_token())


@app.route('/downloads')
def downloads_page():
    return render_template('downloads.html', csrf_token=_get_csrf_token())


@app.route('/api/downloads')
def api_downloads():
    with get_session() as db:
        active = db.query(Illust).filter(Illust.download_status == 'downloading').order_by(Illust.created_at.desc()).all()
        queued_ids = list(_queued_downloads)
        queued = db.query(Illust).filter(Illust.pixiv_id.in_(queued_ids)).order_by(Illust.created_at.desc()).all() if queued_ids else []
        completed = db.query(Illust).filter(Illust.download_status == 'done').order_by(Illust.created_at.desc()).limit(30).all()
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

        return jsonify({
            'active': [_with_progress(i) for i in active],
            'queued': [i.to_dict() for i in queued],
            'completed': [i.to_dict() for i in completed],
            'logs': [l.to_dict() for l in logs],
        })


# ── Blocked Tags ──

@app.route('/api/blocked-tags', methods=['GET'])
def list_blocked_tags():
    with get_session() as db:
        tags = db.query(BlockedTag).order_by(BlockedTag.created_at.desc()).all()
        return jsonify([t.tag for t in tags])


@app.route('/api/blocked-tags', methods=['POST'])
@_csrf_required
def add_blocked_tag():
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
def remove_blocked_tag(tag):
    with get_session() as db:
        entry = db.query(BlockedTag).filter(BlockedTag.tag == tag).first()
        if not entry:
            return jsonify({'error': '标签不存在'}), 404
        db.delete(entry)
        safe_commit(db)
        return jsonify({'status': 'deleted', 'tag': tag})


# ── Settings ──

import json as json_module

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


def _load_settings():
    if os.path.exists(_SETTINGS_PATH):
        try:
            with open(_SETTINGS_PATH, 'r', encoding='utf-8') as f:
                data = json_module.load(f)
            result = dict(_SETTINGS_DEFAULTS)
            result.update(data)
            return result
        except Exception:
            pass
    return dict(_SETTINGS_DEFAULTS)


@app.route('/settings')
def settings_page():
    return render_template('settings.html', csrf_token=_get_csrf_token())


@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'GET':
        return jsonify(_load_settings())

    # POST requires CSRF
    token = request.headers.get('X-CSRF-Token', '')
    if not token or token != session.get('_csrf_token', ''):
        return jsonify({'error': 'CSRF校验失败'}), 403

    body = request.get_json(silent=True) or {}
    current = _load_settings()
    # Merge only known keys
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
            json_module.dump(current, f, ensure_ascii=False, indent=2)
        return jsonify(current)
    except Exception as e:
        return jsonify({'error': f'保存失败: {e}'}), 500


# ── Favorites ──


@app.route('/api/favorite/<int:pixiv_id>', methods=['GET', 'POST'])
def api_favorite(pixiv_id):
    if request.method == 'GET':
        with get_session() as db:
            illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
            return jsonify({'is_favorite': illust.is_favorite if illust else False})

    # POST — 需要 CSRF
    token = request.headers.get('X-CSRF-Token', '')
    if not token or token != session.get('_csrf_token', ''):
        return jsonify({'error': 'CSRF校验失败'}), 403

    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust:
            return jsonify({'error': '作品不存在'}), 404
        illust.is_favorite = not illust.is_favorite
        illust.favorited_at = datetime.now(timezone.utc) if illust.is_favorite else None
        safe_commit(db)
        return jsonify({'is_favorite': illust.is_favorite})


if __name__ == '__main__':
    app.run(debug=False, host='127.0.0.1', port=5000)
