# ── X（Twitter）标签抓图路由 ──
# /x 页面、标签增删、作品列表、图片代理、下载、同作品判定。
# 与图库/Pixiv 下载完全隔离：这边所有 id 都是 x_works.id（自增整数），
# tweet_id 只作为字符串在 JSON 里流转。
from __future__ import annotations

import hashlib
import logging
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

# 缓存命中时的 Content-Type 按扩展名定死：mimetypes.guess_type 在
# Python ≤3.9 / 精简 MIME 数据库下认不出 .webp（返回 None → 退成
# image/jpeg → 被 nosniff 拒绝渲染，缓存反而比首取更糟，/thumb 的
# .meta 教训同款）。这里的扩展名集合就是入口白名单，map 永远完备。
_EXT_MIME = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
             '.png': 'image/png', '.webp': 'image/webp'}

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
    saved = (request.args.get('saved') or 'all').lower()
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
        if saved == 'done':
            # 「只看已下载」：X 页上直接浏览本地已保存的图片
            query = query.filter(XWork.saved_status == 'done')
        elif saved == 'undone':
            # SQL 三值逻辑：NULL != 'done' 不为真，未下载（NULL）必须显式并入
            query = query.filter(or_(XWork.saved_status.is_(None),
                                     XWork.saved_status != 'done'))
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
    # 让前端如实提示：总开关/间隔没开时，这轮不会真的抓（循环空转）
    triggered = _x_loop_active()
    from ..background import trigger_x_fetch
    trigger_x_fetch()
    return jsonify({'ok': True, 'tag': data, 'triggered': triggered}), 201


def _x_loop_active() -> bool:
    """抓取循环当前是否会真的发请求（enabled 且 interval > 0，与循环判定同口径）。"""
    state = runtime._x_prefetch_state
    return bool(state.get('enabled')) and int(state.get('interval') or 0) > 0


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
    # 用户点"刷新"的预期是"现在就抓"：循环没开时如实拒绝，而不是回一个
    # queued:True 然后让线程空转（那是在界面承诺一个不会发生的动作）
    if not _x_loop_active():
        return jsonify({'error': 'X 抓取未启用（设置页开启并设间隔 > 0）'}), 409
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
        'transaction_available': bool(getattr(x_client, 'TRANSACTION_AVAILABLE', False)),
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
        if work.saved_status in ('downloading', 'queued'):
            # 不只是挡重复点击：双击/页面重渲染复活按钮都会走到这里，放行
            # 就是两个线程写同一批文件（图片交错损坏）
            return jsonify({'error': '该作品正在下载中'}), 409
        work.saved_status = 'queued'
        safe_commit(db)
    from ..background import _download_x_work
    try:
        runtime.x_download_executor.submit(_download_x_work, work_id)
    except RuntimeError:
        # 执行器正在关闭（进程退出窗口）：把 queued 状态回滚，别让卡上的
        # "下载中" 挂到下次重启（启动重置只兜底进程真挂掉的情况）
        with get_session() as db:
            row = db.get(XWork, work_id)
            if row is not None and row.saved_status == 'queued':
                row.saved_status = 'failed'
                safe_commit(db)
        return jsonify({'error': '下载服务正在关闭，请稍后重试'}), 503
    return jsonify({'ok': True, 'status': 'queued'}), 202


# ── 本地看图（下载后的浏览入口；X 图片不在 Pixiv 图库里，这是唯一查看面）──

@bp.route('/x/view/<int:work_id>')
def x_work_view(work_id: int) -> Response:
    with get_session() as db:
        work = db.get(XWork, work_id)
        if work is None:
            abort(404)
        data = work.to_dict()
        # to_dict 与 Pixiv 同约定：不回传磁盘路径。渲染页面在服务端自己读
        # ORM 的 saved_paths_list（只有下载 worker 会写这一列）
        data['local_images'] = [
            {'index': i, 'name': os.path.basename(path),
             'size': (os.path.getsize(path) if os.path.isfile(path) else 0)}
            for i, path in enumerate(work.saved_paths_list or [])
        ]
    if data['saved_status'] != 'done':
        # 未下载的作品没有本地图可看：如实提示，别渲染一个空页
        return render_template('x_view.html', work=data,
                               csrf_token=_get_csrf_token(), not_downloaded=True)
    return render_template('x_view.html', work=data,
                           csrf_token=_get_csrf_token(), not_downloaded=False)


@bp.route('/api/x/image/<int:work_id>/<int:idx>')
def x_image_file(work_id: int, idx: int) -> Response:
    """把已下载的 X 图片文件送出去（下载目录不在任何静态服务下）。

    只信 `saved_paths`（下载 worker 自己写的路径），并复核目录归属
    `downloads/x_<tweet_id>`：列表/详情接口都回显路径，将来若有人把
    其它路径塞进这一列，这里不能成为任意文件读取口。
    """
    with get_session() as db:
        work = db.get(XWork, work_id)
        if work is None:
            return jsonify({'error': '作品不存在'}), 404
        paths = work.saved_paths_list or []
        tweet_id = work.tweet_id
    if idx < 0 or idx >= len(paths):
        return jsonify({'error': '图片序号越界'}), 404
    path = paths[idx]
    if not os.path.dirname(path).endswith(os.path.join('downloads', f'x_{tweet_id}')):
        return jsonify({'error': '非法图片路径'}), 403
    if not os.path.isfile(path):
        return jsonify({'error': '文件不存在'}), 404
    ext = os.path.splitext(path)[1].lower()
    return send_file(path, mimetype=_EXT_MIME.get(ext, 'image/jpeg'),
                     max_age=86400 * 7)


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


@bp.route('/api/x/artist-links')
def x_artist_links_get() -> Response:
    """H1 的前提数据：Pixiv uid ↔ X handle 映射清单（X 页管理用）。"""
    with get_session() as db:
        rows = db.query(ArtistLink).order_by(ArtistLink.pixiv_user_id).all()
        data = [{'pixiv_user_id': row.pixiv_user_id, 'x_screen_name': row.x_screen_name}
                for row in rows]
    return jsonify(data)


@bp.route('/api/x/artist-links/<int:pixiv_user_id>', methods=['DELETE'])
@_csrf_required
def x_artist_link_delete(pixiv_user_id: int) -> Response:
    with get_session() as db:
        row = db.get(ArtistLink, pixiv_user_id)
        if row is None:
            return jsonify({'error': '映射不存在'}), 404
        db.delete(row)
        safe_commit(db)
    return jsonify({'ok': True})


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
        return send_file(cache_path, mimetype=_EXT_MIME.get(ext.lower(), 'image/jpeg'),
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
            try:
                with open(tmp, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                os.replace(tmp, cache_path)
            except BaseException:
                # 半写的 tmp 不会被容量淘汰器认领（它只认 md5 命名），留着就是
                # 只进不出的垃圾；写一半失败必须当场清掉再让异常走 502/500 路径
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
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
