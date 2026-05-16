import io
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import zipfile
from base64 import urlsafe_b64decode
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import requests
from flask import (
    Flask, jsonify, render_template, request, session,
    send_file, abort, Response,
)

from config import (
    DOWNLOAD_DIR, DOWNLOAD_MAX_WORKERS, PAGE_DOWNLOAD_INTERVAL,
    MAX_BOOKMARKS_DEFAULT, COOKIE_PATH,
)
from models import init_db, get_session, Illust
from fetcher import search_by_tag, search_by_user

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
# 过滤掉请求头日志，防止 Cookie 泄露
logging.getLogger('werkzeug').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1MB max upload

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

init_db()

download_executor = ThreadPoolExecutor(max_workers=DOWNLOAD_MAX_WORKERS)
download_locks: dict[int, threading.Lock] = {}


def _get_download_dir(pixiv_id: int) -> str:
    return os.path.join(DOWNLOAD_DIR, str(pixiv_id))


def _download_illust(pixiv_id: int):
    """Background task: download all original images for an illust."""
    lock = download_locks.setdefault(pixiv_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return  # Already being downloaded
    try:
        db = get_session()
        try:
            illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
            if not illust:
                return

            illust.download_status = 'downloading'
            db.commit()

            urls = illust.original_urls_list
            work_dir = _get_download_dir(pixiv_id)
            os.makedirs(work_dir, exist_ok=True)

            session_obj = requests.Session()
            session_obj.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://www.pixiv.net/',
            })

            local_paths = []
            for i, url in enumerate(urls):
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

                    if i < len(urls) - 1:
                        time.sleep(PAGE_DOWNLOAD_INTERVAL)
                except Exception as e:
                    logger.error(f'Download failed for {pixiv_id} page {i}: {e}')
                    # 清理已下载的部分
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
                    db.commit()
                    return

            illust.local_paths_list = local_paths
            illust.download_status = 'done'
            db.commit()
        finally:
            db.close()
    finally:
        lock.release()
        download_locks.pop(pixiv_id, None)


def _extract_ext(url: str) -> str:
    """Extract file extension from image URL."""
    match = re.search(r'\.(jpg|jpeg|png|gif|webp)(?:\?|$)', url, re.IGNORECASE)
    return match.group(1) if match else 'jpg'


def _get_csrf_token() -> str:
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']


@app.route('/')
def index():
    return render_template('index.html', csrf_token=_get_csrf_token())


@app.route('/search')
def search():
    search_type = request.args.get('type', 'tag')
    query = request.args.get('query', '').strip()
    min_bookmarks = request.args.get('min_bookmarks', MAX_BOOKMARKS_DEFAULT)
    page = request.args.get('page', '1')

    if not query:
        return jsonify({'error': '请输入搜索关键词或画师ID'}), 400

    try:
        min_bookmarks = int(min_bookmarks)
    except (ValueError, TypeError):
        min_bookmarks = MAX_BOOKMARKS_DEFAULT

    try:
        page = int(page)
    except (ValueError, TypeError):
        page = 1
    page = max(1, page)

    if search_type == 'tag':
        if len(query) > 200:
            return jsonify({'error': '搜索关键词过长'}), 400
        results, has_more = search_by_tag(query, min_bookmarks, page)
    else:
        if not query.isdigit():
            return jsonify({'error': '画师ID必须为数字'}), 400
        results, has_more = search_by_user(query, min_bookmarks, page)

    # 获取最新数据（含下载状态）
    if results:
        pixiv_ids = [r['pixiv_id'] for r in results]
        with get_session() as db:
            illusts = db.query(Illust).filter(Illust.pixiv_id.in_(pixiv_ids)).all()
            illust_map = {i.pixiv_id: i.to_dict() for i in illusts}
        results = [illust_map.get(r['pixiv_id'], r) for r in results]

    return jsonify({'results': results, 'has_more': has_more})


@app.route('/download/<int:pixiv_id>', methods=['POST'])
def trigger_download(pixiv_id):
    # CSRF 校验
    token = request.headers.get('X-CSRF-Token', '')
    if not token or token != session.get('_csrf_token', ''):
        return jsonify({'error': 'CSRF校验失败'}), 403

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

    download_executor.submit(_download_illust, pixiv_id)
    return jsonify({'status': 'accepted', 'message': '已加入下载队列'})


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
            ext = os.path.splitext(valid_paths[0])[1]
            return send_file(
                valid_paths[0],
                mimetype=f'image/{ext[1:] if ext else "jpeg"}',
                as_attachment=True,
                download_name=f'{safe_title}{ext}',
            )

        # 多文件打包 zip（ZIP_STORED 不压缩）
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        try:
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_STORED) as zf:
                for i, p in enumerate(valid_paths):
                    ext = os.path.splitext(p)[1]
                    zf.write(p, f'{safe_title}_p{i}{ext}')
            tmp.close()
            return send_file(
                tmp.name,
                mimetype='application/zip',
                as_attachment=True,
                download_name=f'{safe_title}.zip',
            )
        except Exception:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise


@app.route('/csrf-token')
def csrf_token():
    return jsonify({'token': _get_csrf_token()})


@app.route('/thumb/<path:url_b64>')
def thumb_proxy(url_b64):
    """代理 Pixiv 缩略图，绕过 Referer 检查。url_b64 为 base64(urlencode) 编码的原始 URL。"""
    try:
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
            'ETag': str(hash(url)),
        },
    )


if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)
