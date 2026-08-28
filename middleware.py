# ── 认证 / CSRF / 限流 / 安全头中间件 ──
from __future__ import annotations

import hmac
import secrets
import time
from functools import wraps
from typing import Callable

from flask import Blueprint, Response, jsonify, redirect, request, session, url_for

from runtime import _rate_limit_store

bp = Blueprint('middleware', __name__)  # 不承载路由，仅用于 app 级钩子

# _rate_limit 内 `global _rate_limit_cleanup_counter` 指向本模块命名空间，
# 计数器因此随函数迁到本模块（单一归属）；runtime.py 只保留被原地修改的
# _rate_limit_store（from-import 共享同一 dict 对象）。
_rate_limit_cleanup_counter = 0


# ── 简单内存限流器 ──
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
    # 延迟导入读取 app.ACCESS_PASSWORD：middleware 被 app 顶部 import（循环导入
    # 禁止模块级引用 app）；且 tests monkeypatch('app.ACCESS_PASSWORD')，
    # from config import 得到的独立绑定看不到该补丁，必须经 app 模块取最新值。
    import app
    return not app.ACCESS_PASSWORD or bool(session.get('authed'))


@bp.before_app_request
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


@bp.after_app_request
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