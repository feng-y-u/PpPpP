# ── X（Twitter）标签抓图路由 ──
# /x 页面、标签增删、作品列表、图片代理、下载、同作品判定。
# 与图库/Pixiv 下载完全隔离：这边所有 id 都是 x_works.id（自增整数），
# tweet_id 只作为字符串在 JSON 里流转。
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import threading
import time
from base64 import urlsafe_b64decode
from urllib.parse import urlsplit

import requests
from flask import Blueprint, Response, abort, jsonify, render_template, request, send_file
from sqlalchemy import or_

from .. import config
from .. import runtime
from .. import x_client
from .. import x_dedupe
from ..helpers import _atomic_write_text, check_image_url, enforce_image_cache_limit
from ..middleware import _csrf_required, _get_csrf_token, _get_json_body
from ..models import ArtistLink, XTag, XWork, get_session, iso_utc, safe_commit

logger = logging.getLogger(__name__)

bp = Blueprint('xfeed', __name__)

# X 图片只允许这些主机（pbs.twimg.com 是官方图床）
_X_IMAGE_HOSTS = config.X_IMAGE_HOSTS
_X_IMAGE_CACHE_DIR = config.X_IMAGE_CACHE_DIR
_X_THUMB_FAIL_COOLDOWN = 30.0
_x_thumb_failed: dict[str, float] = {}
_x_thumb_failed_lock = threading.Lock()
_x_thumb_sem = threading.Semaphore(6)

_TAG_MAX_LEN = 64


def _clean_tag(raw) -> str:
    tag = ' '.join(str(raw or '').split())
    return tag[:_TAG_MAX_LEN]


def _tag_dict(row: XTag) -> dict:
    return {
        'tag': row.tag,
        'enabled': bool(row.enabled),
        'status': row.status,
        'error': row.error,
        'total': row.total,
        'last_fetch_at': iso_utc(row.last_fetch_at),
    }


# ── 页面 ───────────────────────────────────────────────────────────────

@bp.route('/x')
def x_page() -> str:
    return render_template('x.html', csrf_token=_get_csrf_token(), items_per_page=_page_size())


def _page_size() -> int:
    try:
        return max(1, min(60, int(config.ITEMS_PER_PAGE)))
    except (TypeError, ValueError):
        return 24


# ── 列表与标签 ─────────────────────────────────────────────────────────

@bp.route('/api/x/items')
def x_items() -> Response:
    tag = _clean_tag(request.args.get('tag'))
    dup = (request.args.get('dup') or 'all').lower()
    try:
        offset = max(0, int(request.args.get('offset', 0)))
        limit = max(1, min(60, int(request.args.get('limit', _page_size()))))
    except (TypeError, ValueError):
        return jsonify({'error': 'offset/limit 必须是整数'}), 400

    with get_session() as db:
        query = db.query(XWork)
        if tag:
            query = query.filter(XWork.tag == tag)
        if dup == 'hide':
            # 只藏"已确认"的；疑似必须仍然显示（人工确认的前提）
            query = query.filter(or_(XWork.dup_status.is_(None),
                                     XWork.dup_status == 'suspected'))
        elif dup == 'only':
            query = query.filter(XWork.dup_status.in_(('suspected', 'confirmed')))
        total = query.count()
        rows = (query.order_by(XWork.id.desc())
                .offset(offset).limit(limit).all())
        data = [row.to_dict() for row in rows]
    return jsonify({'data': data, 'total': total, 'offset': offset, 'limit': limit})


@bp.route('/api/x/tags')
def x_tags() -> Response:
    with get_session() as db:
        rows = db.query(XTag).order_by(XTag.tag).all()
        data = [_tag_dict(row) for row in rows]
    return jsonify(data)


@bp.route('/api/x/tags', methods=['POST'])
@_csrf_required
def x_tag_add() -> Response:
    body = _get_json_body()
    tag = _clean_tag(body.get('tag'))
    if not tag:
        return jsonify({'error': '标签不能为空'}), 400
    with get_session() as db:
        row = db.query(XTag).filter(XTag.tag == tag).first()
        if row is None:
            row = XTag(tag=tag, enabled=1, status='idle')
            db.add(row)
            safe_commit(db)
        data = _tag_dict(row)
    from ..background import trigger_x_fetch
    trigger_x_fetch()
    return jsonify({'ok': True, 'tag': data}), 201


@bp.route('/api/x/tags/<path:tag>', methods=['DELETE'])
@_csrf_required
def x_tag_delete(tag: str) -> Response:
    tag = _clean_tag(tag)
    with get_session() as db:
        row = db.query(XTag).filter(XTag.tag == tag).first()
        if row is None:
            return jsonify({'error': '标签不存在'}), 404
        removed_works = db.query(XWork).filter(XWork.tag == tag).delete()
        db.delete(row)
        safe_commit(db)
    return jsonify({'ok': True, 'removed_works': removed_works})


@bp.route('/api/x/tags/<path:tag>/refresh', methods=['POST'])
@_csrf_required
def x_tag_refresh(tag: str) -> Response:
    tag = _clean_tag(tag)
    with get_session() as db:
        if db.query(XTag.tag).filter(XTag.tag == tag).first() is None:
            return jsonify({'error': '标签不存在'}), 404
    from ..background import trigger_x_fetch
    trigger_x_fetch()
    return jsonify({'ok': True, 'queued': True})


@bp.route('/api/x/status')
def x_status() -> Response:
    with get_session() as db:
        works = db.query(XWork).count()
        tags = db.query(XTag).count()
        error_tags = (db.query(XTag)
                      .filter(XTag.status == 'error')
                      .order_by(XTag.tag).all())
        tag_errors = [{'tag': row.tag, 'error': row.error} for row in error_tags]
    configured = bool(getattr(config, 'X_SEARCH_QUERY_ID', '') or _api_query_id())
    state = dict(runtime._x_prefetch_state)
    return jsonify({
        'state': state,
        'works': works,
        'tags': tags,
        'tag_errors': tag_errors,
        'cookie_configured': _cookie_configured(),
        'query_id_configured': configured,
        'hash_available': x_dedupe.PIL_AVAILABLE,
    })


def _api_query_id() -> str:
    try:
        api = x_client._load_api_config()
    except Exception:
        return ''
    return str(api.get('query_id') or '')


def _cookie_configured() -> bool:
    try:
        x_client.load_cookie()
        return True
    except x_client.XAuthError:
        return False


# ── Cookie ─────────────────────────────────────────────────────────────

@bp.route('/api/x/cookie', methods=['GET'])
def x_cookie_get() -> Response:
    return jsonify({
        'configured': _cookie_configured(),
        'path': config.X_COOKIE_PATH,
    })


@bp.route('/api/x/cookie', methods=['POST'])
@_csrf_required
def x_cookie_post() -> Response:
    body = _get_json_body()
    raw = str(body.get('cookie') or '').strip()
    if not raw:
        return jsonify({'error': 'Cookie 不能为空'}), 400
    values: dict[str, str] = {}
    for part in raw.replace('\n', ';').split(';'):
        if '=' in part:
            key, _, value = part.partition('=')
            values[key.strip()] = value.strip()
    missing = [k for k in ('auth_token', 'ct0') if not values.get(k)]
    if missing:
        return jsonify({'error': f'缺少 {", ".join(missing)}'}), 400
    normalized = f'auth_token={values["auth_token"]}; ct0={values["ct0"]}'
    try:
        _atomic_write_text(config.X_COOKIE_PATH, normalized)
        if os.name == 'posix':
            os.chmod(config.X_COOKIE_PATH, 0o600)
    except OSError as e:
        return jsonify({'error': f'写入失败：{e}', 'path': config.X_COOKIE_PATH}), 500
    x_client.clear_cookie_cache()
    return jsonify({'ok': True})


@bp.route('/api/x/probe', methods=['POST'])
@_csrf_required
def x_probe() -> Response:
    result = x_client.probe_credentials()
    return jsonify(result), (200 if result.get('ok') else 400)


# ── 作品操作 ───────────────────────────────────────────────────────────

@bp.route('/api/x/works/<int:work_id>')
def x_work_get(work_id: int) -> Response:
    with get_session() as db:
        work = db.get(XWork, work_id)
        if work is None:
            return jsonify({'error': '作品不存在'}), 404
        data = work.to_dict()
    return jsonify(data)


@bp.route('/api/x/works/<int:work_id>/download', methods=['POST'])
@_csrf_required
def x_work_download(work_id: int) -> Response:
    with get_session() as db:
        work = db.get(XWork, work_id)
        if work is None:
            return jsonify({'error': '作品不存在'}), 404
        if work.saved_status == 'done':
            return jsonify({'ok': True, 'status': 'done'})
        work.saved_status = 'queued'
        safe_commit(db)
    from ..background import _download_x_work
    runtime.x_download_executor.submit(_download_x_work, work_id)
    return jsonify({'ok': True, 'status': 'queued'}), 202


@bp.route('/api/x/works/<int:work_id>', methods=['DELETE'])
@_csrf_required
def x_work_delete(work_id: int) -> Response:
    with get_session() as db:
        work = db.get(XWork, work_id)
        if work is None:
            return jsonify({'error': '作品不存在'}), 404
        tweet_id = work.tweet_id
        for path in work.saved_paths_list:
            # 只删本模块自己的目录，避免路径被篡改时误删
            if os.path.dirname(path).endswith(os.path.join('downloads', f'x_{tweet_id}')):
                try:
                    os.remove(path)
                except OSError:
                    pass
        db.delete(work)
        safe_commit(db)
    return jsonify({'ok': True})


@bp.route('/api/x/works/<int:work_id>/dup', methods=['POST'])
@_csrf_required
def x_work_dup(work_id: int) -> Response:
    body = _get_json_body()
    action = str(body.get('action') or '').lower()
    if action not in ('confirm', 'ignore'):
        return jsonify({'error': 'action 只能是 confirm / ignore'}), 400
    with get_session() as db:
        work = db.get(XWork, work_id)
        if work is None:
            return jsonify({'error': '作品不存在'}), 404
        if action == 'confirm':
            pixiv_id = body.get('pixiv_id')
            try:
                pixiv_id = int(pixiv_id) if pixiv_id not in (None, '') else None
            except (TypeError, ValueError):
                return jsonify({'error': 'pixiv_id 必须是整数'}), 400
            x_dedupe.confirm_pair(db, work, pixiv_id=pixiv_id, source='manual')
        else:
            x_dedupe.ignore_pair(db, work)
        safe_commit(db)
        data = {'dup_status': work.dup_status, 'dup_pixiv_id': work.dup_pixiv_id}
    return jsonify({'ok': True, **data})


@bp.route('/api/x/artist-links', methods=['POST'])
@_csrf_required
def x_artist_link() -> Response:
    body = _get_json_body()
    try:
        pixiv_user_id = int(body.get('pixiv_user_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'pixiv_user_id 必须是整数'}), 400
    screen_name = str(body.get('x_screen_name') or '').strip().lstrip('@').lower()
    if not screen_name or len(screen_name) > 64:
        return jsonify({'error': 'x_screen_name 不合法'}), 400
    with get_session() as db:
        row = db.get(ArtistLink, pixiv_user_id)
        if row is None:
            row = ArtistLink(pixiv_user_id=pixiv_user_id)
            db.add(row)
        row.x_screen_name = screen_name
        row.source = 'manual'
        safe_commit(db)
    return jsonify({'ok': True})


@bp.route('/api/x/dedupe/scan', methods=['POST'])
@_csrf_required
def x_dedupe_scan() -> Response:
    try:
        with get_session() as db:
            stats = x_dedupe.index_local_files(db, limit=200)
            safe_commit(db)
    except x_dedupe.HashUnavailable as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, **stats})


# ── 图片代理 ───────────────────────────────────────────────────────────

@bp.route('/ximg/<path:url_b64>')
def x_image_proxy(url_b64: str) -> Response:
    """只代理 pbs.twimg.com 的图片（磁盘缓存 + 失败冷却）。"""
    try:
        padding = 4 - len(url_b64) % 4
        if padding != 4:
            url_b64 += '=' * padding
        url = urlsafe_b64decode(url_b64.encode()).decode()
    except Exception:
        return abort(400)

    host, reject = check_image_url(url)
    if reject or host not in _X_IMAGE_HOSTS:
        return abort(403)
    path_part = urlsplit(url).path.lower()
    if not path_part.endswith(('.jpg', '.jpeg', '.png', '.webp')):
        return abort(403)

    os.makedirs(_X_IMAGE_CACHE_DIR, exist_ok=True)
    cache_key = hashlib.md5(url.encode()).hexdigest()
    ext = os.path.splitext(path_part)[1] or '.jpg'
    cache_path = os.path.join(_X_IMAGE_CACHE_DIR, f'{cache_key}{ext}')
    if os.path.isfile(cache_path):
        return send_file(cache_path, mimetype=mimetypes.guess_type(cache_path)[0] or 'image/jpeg',
                         max_age=86400 * 7)

    now = time.time()
    with _x_thumb_failed_lock:
        if now - _x_thumb_failed.get(url, 0.0) < _X_THUMB_FAIL_COOLDOWN:
            return abort(502)
    if not _x_thumb_sem.acquire(timeout=10):
        return abort(503)
    try:
        session = requests.Session()
        proxy = getattr(config, 'PROXY', '')
        if proxy:
            session.proxies = {'http': proxy, 'https': proxy}
        session.verify = bool(getattr(config, 'SSL_VERIFY', True))
        session.headers.update({
            'User-Agent': x_client._USER_AGENT,
            'Referer': 'https://x.com/',
            'Accept': 'image/*',
        })
        try:
            resp = session.get(url, timeout=(10, 30), stream=True, allow_redirects=False)
            if resp.is_redirect:
                raise requests.RequestException('图片地址发生重定向，已拒绝')
            resp.raise_for_status()
            mimetype = resp.headers.get('Content-Type', 'image/jpeg')
            if not mimetype.startswith('image/'):
                raise requests.RequestException(f'非图片响应：{mimetype}')
            tmp = f'{cache_path}.{os.getpid()}.{threading.get_ident()}.tmp'
            with open(tmp, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            os.replace(tmp, cache_path)
        finally:
            session.close()
    except requests.RequestException as e:
        logger.warning(f'/ximg 取图失败：{url} → {e}')
        with _x_thumb_failed_lock:
            _x_thumb_failed[url] = now
            for stale_url in [k for k, v in _x_thumb_failed.items()
                              if now - v >= _X_THUMB_FAIL_COOLDOWN]:
                _x_thumb_failed.pop(stale_url, None)
        return abort(502)
    finally:
        _x_thumb_sem.release()

    enforce_image_cache_limit(_X_IMAGE_CACHE_DIR)
    return send_file(cache_path, mimetype=mimetype, max_age=86400 * 7)
