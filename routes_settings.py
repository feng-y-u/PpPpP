# ── 登录 / 设置 / 屏蔽标签 / 自动关注控制 ──
# /login(GET/POST)、/settings、/api/settings、/api/settings/unlock、
# /api/blocked-tags、/api/auto-follow/* 路由 + 私有辅助
# _SETTINGS_PATH/_SETTINGS_DEFAULTS/_load_settings/_settings_locked。
# ACCESS_PASSWORD/SETTINGS_PASSWORD 均经 app 命名空间延迟读取：
# tests monkeypatch('app.ACCESS_PASSWORD'/'app.SETTINGS_PASSWORD')，
# 本模块 from config import 的独立绑定看不到补丁。
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time

from flask import (Blueprint, Response, jsonify, redirect, render_template,
                   request, session)

import config as config_module
import fetcher
from fetcher import clear_search_cache
from middleware import (_csrf_required, _get_csrf_token, _get_json_body,
                        _is_authed, _rate_limit, _safe_next)
from models import BlockedTag, get_session, safe_commit
from runtime import _auto_follow_state, _prefetch_state

logger = logging.getLogger(__name__)

bp = Blueprint('settings', __name__)


@bp.route('/login', methods=['GET'])
def login_page():
    if _is_authed():
        return redirect(_safe_next(request.args.get('next', '')))
    return render_template('login.html', csrf_token=_get_csrf_token())


@bp.route('/login', methods=['POST'])
@_csrf_required
@_rate_limit(max_attempts=5, window=60)
def login_submit():
    import app  # 延迟导入读取 app.ACCESS_PASSWORD：tests monkeypatch('app.ACCESS_PASSWORD')
    #             （test_auth.py auth_enabled 夹具），from config import 绑定看不到补丁
    body = _get_json_body()
    password = str(body.get('password', ''))
    if app.ACCESS_PASSWORD and hmac.compare_digest(password.encode(), app.ACCESS_PASSWORD.encode()):
        session['authed'] = True
        session.permanent = True
        return jsonify({'ok': True, 'next': _safe_next(str(body.get('next', '')))})
    time.sleep(1)  # 失败延迟，减缓爆破
    return jsonify({'error': '密码错误'}), 403


# ── 自动关注控制 ──

@bp.route('/api/auto-follow/status')
def auto_follow_status() -> Response:
    return jsonify(_auto_follow_state)

@bp.route('/api/auto-follow/config', methods=['POST'])
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


# ── 屏蔽标签 ──

@bp.route('/api/blocked-tags', methods=['GET'])
def list_blocked_tags() -> Response:
    with get_session() as db:
        tags = db.query(BlockedTag).order_by(BlockedTag.created_at.desc()).all()
        return jsonify([t.tag for t in tags])


@bp.route('/api/blocked-tags', methods=['POST'])
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


@bp.route('/api/blocked-tags/<path:tag>', methods=['DELETE'])
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

_SETTINGS_PATH = os.path.join(config_module._instance_dir, 'settings.json')

# 设置页可编辑键与默认值：由 config.SETTINGS_KEYS（唯一来源）派生，
# 排除密码类与 cookie_secure（这些只通过 settings.json/环境变量管理）。
_SETTINGS_DEFAULTS = {
    k: v for k, (_, v) in config_module.SETTINGS_KEYS.items()
    if not k.endswith('_password') and k != 'cookie_secure'
}


def _load_settings() -> dict:
    import app  # 延迟导入读取 app._SETTINGS_PATH：
    #             tests monkeypatch('app._SETTINGS_PATH')（test_prefetch_api.py
    #             _isolate_settings 夹具），本模块模块级绑定看不到补丁
    if os.path.exists(app._SETTINGS_PATH):
        try:
            with open(app._SETTINGS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            result = dict(_SETTINGS_DEFAULTS)
            result.update(data)
            return result
        except Exception:
            logger.warning('设置文件读取失败，使用默认设置')
    return dict(_SETTINGS_DEFAULTS)


def _settings_locked() -> bool:
    """设置页门禁：已全局登录则直通；否则按旧 SETTINGS_PASSWORD 流程。"""
    import app  # 延迟导入读取 app.SETTINGS_PASSWORD：tests monkeypatch('app.SETTINGS_PASSWORD')
    #             （test_auth.py TestSettingsCompat），from config import 绑定看不到补丁
    if session.get('authed'):
        return False
    return bool(app.SETTINGS_PASSWORD) and not session.get('settings_unlocked')


@bp.route('/settings')
def settings_page() -> str:
    if _settings_locked():
        return render_template('settings_unlock.html', csrf_token=_get_csrf_token())
    return render_template('settings.html', csrf_token=_get_csrf_token())


@bp.route('/api/settings/unlock', methods=['POST'])
@_csrf_required
@_rate_limit(max_attempts=5, window=60)
def settings_unlock() -> Response:
    import app  # 延迟导入读取 app.SETTINGS_PASSWORD：tests monkeypatch('app.SETTINGS_PASSWORD')
    #             （test_auth.py TestSettingsCompat），from config import 绑定看不到补丁
    if session.get('authed') or not app.SETTINGS_PASSWORD:
        return jsonify({'ok': True})
    body = _get_json_body()
    if hmac.compare_digest(str(body.get('password', '')).encode(), app.SETTINGS_PASSWORD.encode()):
        session['settings_unlocked'] = True
        return jsonify({'ok': True})
    time.sleep(1)  # 失败延迟，减缓爆破（与 login_submit 对齐）
    return jsonify({'error': '密码错误'}), 403


@bp.route('/api/settings', methods=['GET'])
def api_settings_get() -> Response:
    if _settings_locked():
        return jsonify({'error': '需要密码访问'}), 403
    data = _load_settings()
    # 脱敏：密码类与 Cookie 字段不回传明文（纵深防御，前端 FIELD_MAP 不消费这些键）
    for k in list(data):
        if k.endswith('_password') or k == 'cookie':
            data[k] = ''
    return jsonify(data)


@bp.route('/api/settings', methods=['POST'])
@_csrf_required
def api_settings_post() -> Response:
    import app  # 延迟导入经 app 命名空间读写 _SETTINGS_PATH：本函数读侧经
    #             _load_settings()（app._SETTINGS_PATH）已走 app 命名空间，写侧若用
    #             模块级绑定，在 tests monkeypatch('app._SETTINGS_PATH') 下会读临时路径
    #             却写坏生产 instance/settings.json（test_prefetch_api._isolate_settings 夹具）
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
        os.makedirs(os.path.dirname(app._SETTINGS_PATH), exist_ok=True)
        with open(app._SETTINGS_PATH, 'w', encoding='utf-8') as f:
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