# ── 认证 / CSRF / 限流 / 安全头中间件 ──
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from functools import wraps
from typing import Callable

from flask import Blueprint, Response, jsonify, redirect, request, session, url_for

from . import config as config_module
from .runtime import _rate_limit_store

bp = Blueprint('middleware', __name__)  # 不承载路由，仅用于挂 app 级钩子；无循环导入问题（不 import app）


# ── 简单内存限流器 ──
# _rate_limit 内 `global _rate_limit_cleanup_counter` 指向本模块命名空间，
# 计数器因此随函数迁到本模块（单一归属）；runtime.py 只保留被原地修改的
# _rate_limit_store（from-import 共享同一 dict 对象）。
_rate_limit_cleanup_counter = 0
# 限流是安全控制：整个"读窗口 → 判定 → 记录 → 清理"必须在锁内一次完成。
# 拆成多步时并发请求会各自读到未计入对方的 records，从而同时通过判定
# （gunicorn --threads 下并发登录请求各在一个线程），限流被绕过。
# 顺带保护 _rate_limit_store 的遍历：容器在遍历中被改动会抛
# RuntimeError: dictionary changed size during iteration。
_rate_limit_lock = threading.Lock()


def _check_rate_limit(ip: str, max_attempts: int, window: int) -> bool:
    """记录一次请求，返回 True 表示已超限（调用方应返回 429）。

    整个"读窗口 → 判定 → 记录 → 清理"必须在同一把锁内一次完成：拆成多步时
    并发请求会各自读到未计入对方的 records，从而同时通过判定（gunicorn
    --threads 下每次登录请求都在不同线程）。锁同时保护 _rate_limit_store 的
    遍历——容器在遍历中被改动会抛 RuntimeError: dictionary changed size
    during iteration。
    """
    global _rate_limit_cleanup_counter
    now = time.time()
    with _rate_limit_lock:
        records = _rate_limit_store.setdefault(ip, [])
        # 移除过期的记录
        records[:] = [t for t in records if now - t < window]
        if len(records) >= max_attempts:
            return True
        records.append(now)
        # 定期清理过期的 IP 记录
        _rate_limit_cleanup_counter += 1
        if _rate_limit_cleanup_counter >= 100:
            _rate_limit_cleanup_counter = 0
            cutoff = now - window
            stale = [k for k, v in _rate_limit_store.items() if v and max(v) < cutoff]
            for k in stale:
                del _rate_limit_store[k]
    return False


def _direct_peer() -> str:
    """直连对端地址（ProxyFix 改写前的 REMOTE_ADDR）。"""
    orig = request.environ.get('werkzeug.proxy_fix.orig') or {}
    return str(orig.get('REMOTE_ADDR') or request.environ.get('REMOTE_ADDR') or '')


def _client_ip() -> str:
    """限流/免认证守卫用的客户端地址：只对**显式受信**的反代信任 XFF 还原值。

    ProxyFix 无条件信任 `X-Forwarded-For`：公网直连部署时，客户端每个请求换一个
    XFF 就能换出一个新的 remote_addr，5 次/分钟的登录限流形同虚设（审计 P1-4）。
    这里把"真实直连对端"作为信任边界：只有对端落在 `config.TRUSTED_PROXIES`
    （默认仅 loopback）时才使用 ProxyFix 还原出的客户端地址；其他对端（公网、
    Docker 网桥、普通内网）一律用对端本身，忽略它自带的 XFF。这样：
      - 公网直连伪造 XFF 无效；
      - Docker `-p` 的网桥来源不会被当成反代；
      - 反代在别的私网地址时，必须显式把它的 CIDR 加进 TRUSTED_PROXIES。
    """
    peer = _direct_peer()
    candidate = request.remote_addr or peer or 'unknown'
    if not peer:
        return candidate
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        # 非 IP 字面量（测试/异常 WSGI 环境）：用直连对端本身兜底，不看 XFF 还原值。
        return peer or candidate
    if any(addr in network for network in config_module.TRUSTED_PROXIES):
        return candidate
    return peer


def _is_local_request() -> bool:
    """当前请求是否来自免认证守卫认可的"本机"范围。

    默认 `LOCAL_NETWORKS` 只有 loopback：Docker 网桥（172.17.x）和普通内网
    （192.168/10.x）都不再自动放行，避免公网端口映射或反代漏配 XFF 时绕过守卫。
    需要无密码访问内网时显式设 `LOCAL_NETWORKS`；反代场景还要同时配置
    `TRUSTED_PROXIES`，否则 XFF 不会被采信。无法解析时按"非本地"处理（fail closed）。
    """
    try:
        addr = ipaddress.ip_address(_client_ip())
    except ValueError:
        return False
    return any(addr in network for network in config_module.LOCAL_NETWORKS)


def _rate_limit(max_attempts: int = 5, window: int = 60) -> Callable:
    """装饰器：限制同一 IP 在 window 秒内最多 max_attempts 次请求。"""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            ip = _client_ip()
            if _check_rate_limit(ip, max_attempts, window):
                return jsonify({'error': '请求过于频繁，请稍后再试'}), 429
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


def _credential_version() -> str:
    """派生当前全站口令代次；口令轮换后旧 session 即不再匹配。"""
    import app
    return hmac.new(
        app.app.config['SECRET_KEY'].encode(),
        (app.ACCESS_PASSWORD or '').encode(),
        hashlib.sha256,
    ).hexdigest()


def _session_authed() -> bool:
    """仅接受绑定到当前凭据代次的全站认证 session。"""
    return (
        bool(session.get('authed'))
        and session.get('credential_version') == _credential_version()
    )


def _is_authed() -> bool:
    # 延迟导入读取 app.ACCESS_PASSWORD：middleware 被 app 顶部 import（循环导入
    # 禁止模块级引用 app）；且 tests monkeypatch('app.ACCESS_PASSWORD')，
    # from config import 得到的独立绑定看不到该补丁，必须经 app 模块取最新值。
    import app
    return not app.ACCESS_PASSWORD or _session_authed()


@bp.before_app_request
def _require_login():
    import app
    path = request.path
    # 先放行显式豁免路径（/login、/csrf-token、/favicon、/static）：静态资源
    # 与登录页必须能在未认证/公网未放行时加载，否则登录页和 403 页会连样式都拿不到。
    if path in _AUTH_EXEMPT_PATHS or any(
        path == p or path.startswith(p + '/') for p in _AUTH_EXEMPT_PREFIXES
    ):
        return None
    # 免认证只对"本机/可信反代还原后的本机"成立：ACCESS_PASSWORD 留空是单人本机
    # 部署的既定用法，但直接挂公网时设置页（可写 cookies.txt）、删除接口、全部
    # API 都会裸奔。需要显式接受风险时设 ALLOW_UNAUTHENTICATED_PUBLIC=1（审计 P1-5）。
    if (not app.ACCESS_PASSWORD
            and not app.ALLOW_UNAUTHENTICATED_PUBLIC
            and not _is_local_request()):
        if path.startswith('/api/') or path == '/search' or request.method != 'GET':
            return jsonify({
                'error': '未设置访问密码，已拒绝来自非本机/LOCAL_NETWORKS 的访问',
                'error_code': 'PUBLIC_ACCESS_DENIED',
            }), 403
        return Response(
            '<!doctype html><meta charset="utf-8">'
            '<h1>403 Forbidden</h1>'
            '<p>本服务未设置访问密码，只允许本机或 LOCAL_NETWORKS 配置的网段访问。'
            '请设置 ACCESS_PASSWORD 后重试。</p>',
            status=403, mimetype='text/html')
    if _is_authed():
        return None
    # 免认证路径已在函数开头处理；到这里说明需要登录。
    # 前缀豁免必须以 '/' 为边界：裸 startswith('/static') 会让 '/static../.git/config'
    # 这类路径也跳过登录墙（2026-09-21 在真实部署的 nginx 日志里看到扫描器正是这么打的：
    # 该请求绕过认证墙后落到 Flask 静态路由、因无匹配文件返回 404，故不可利用，但匹配范围
    # 比必要的宽）。注意 /static 免认证本身是设计使然——CSS/JS 必须能在未登录时加载——
    # 推论是 static/ 目录下的任何文件都是公开的，别往里面放密钥或配置。
    if path in _AUTH_EXEMPT_PATHS or any(
        path == p or path.startswith(p + '/') for p in _AUTH_EXEMPT_PREFIXES
    ):
        return None
    if path.startswith('/api/') or path == '/search' or request.method != 'GET':
        return jsonify({'error': '未登录', 'error_code': 'AUTH_REQUIRED'}), 401
    # /login 路由已随设置 Blueprint 迁移（Task 5）：端点经 blueprint 命名空间
    # 变为 settings.login_page（Flask 对显式 endpoint 也会加 bp 前缀）。
    return redirect(url_for('settings.login_page', next=path))


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
