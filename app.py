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
from io import BytesIO

import requests
import urllib3
from flask import (
    Flask, jsonify, render_template, request, session,
    send_file, abort, Response, redirect,
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

import helpers
import runtime
from helpers import (query_cached_tag, _scan_local_downloads, _build_orphan_dicts,
                     _proxy_thumb, _original_to_resized, _fetch_original_urls,
                     _get_download_dir, _page_sort_key, _extract_ext, _fmt_num,
                     _safe_int, _pid_in_clause, _delete_illust_files,
                     _next_collection_position, _compute_move_position)
from runtime import (_scan_cache, _SCAN_CACHE_TTL, _thumb_sem, _thumb_failed,
                     _THUMB_FAIL_COOLDOWN, _db_pids_cache, _DB_PIDS_CACHE_TTL,
                     _auto_follow_state, _auto_follow_stop, _prefetch_state,
                     _queued_downloads, _download_progress, download_cancellations,
                     download_executor, _search_tasks, _search_tasks_lock,
                     SEARCH_TASK_TTL, _rate_limit_store)
# 中间件（认证/CSRF/限流/安全头）——app 级钩子经 middleware_bp 注册全局生效；
# _rate_limit_store 仍在 app 命名空间可见（tests/test_auth.py 清空同一共享 dict）
from middleware import (bp as middleware_bp, _get_csrf_token, _rate_limit,
                        _get_json_body, _csrf_required, _safe_next, _is_authed)

# 后台线程与下载引擎（auto_follow / 预取 / 下载 / 启动重置）已迁至 background.py：
# from-import 供路由/模块级调用使用，同时保持 tests 的 app.<符号> monkeypatch 契约。
import background
from background import (
    _download_illust, _shutdown_background_threads,
    _reset_stuck_downloads, _reset_stuck_prefetch,
    _prefetch_one_tag, _collect_other_tag_pids, _remove_pids_from_search_caches,
    _prefetch_refresh_bookmarks, _prefetch_capacity_cleanup, _prefetch_loop,
    _start_prefetch_thread,
)
# 路由 Blueprint（搜索/缓存/关注 + 图库/详情/图片/收藏）；
# _cleanup_search_tasks 经 from-import 保持在 app 命名空间（tests/test_app.py 直接调用 app._cleanup_search_tasks()）
from routes_search import bp as search_bp, _cleanup_search_tasks
from routes_gallery import bp as gallery_bp

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

# 中间件 Blueprint：不承载路由，仅注册 app 级钩子（认证拦截/安全头/限流等）
app.register_blueprint(middleware_bp)
app.register_blueprint(search_bp)
app.register_blueprint(gallery_bp)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'image_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

init_db()


# ── 后台任务组装（定义见 background.py）──
# 保持重构前 import 时序：init_db → _reset_stuck_*（清理残留状态）→ 后台线程启动 → atexit 注册。
_reset_stuck_downloads()
_reset_stuck_prefetch()

background.start_background_threads()


atexit.register(_shutdown_background_threads)


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


@app.route('/favicon.ico')
def favicon() -> Response:
    return Response(status=204)


@app.route('/')
def index() -> str:
    return render_template('index.html', csrf_token=_get_csrf_token(), max_bookmarks_default=MAX_BOOKMARKS_DEFAULT)


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


@app.route('/cache')
def cache_page() -> str:
    """缓存浏览页：查看预取标签的缓存结果。"""
    return render_template('cache.html', csrf_token=_get_csrf_token())


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


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
