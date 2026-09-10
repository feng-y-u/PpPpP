from __future__ import annotations

import logging
import os
import platform
import secrets
import threading
import time
import atexit
from datetime import timedelta

import urllib3
from flask import (
    Flask, jsonify, render_template, Response,
)

from config import (
    DOWNLOAD_DIR,
    MAX_BOOKMARKS_DEFAULT,
    SETTINGS_PASSWORD, ACCESS_PASSWORD, COOKIE_SECURE, SSL_VERIFY,
)
from models import init_db, get_session  # get_session：tests 补丁目标（test_prefetch.py setattr(app, 'get_session')）
import fetcher
# 以下为 app 命名空间测试补丁契约绑定，勿删（见 docs/architecture.md「测试契约」）
from fetcher import search_by_tag, search_by_user, browse_discovery, paginated_search, build_pixiv_session

# 以下为 app 命名空间测试补丁契约绑定，勿删（见 docs/architecture.md「测试契约」）
from helpers import enforce_image_cache_limit, query_cached_tag
from runtime import (_scan_cache, _db_pids_cache, _prefetch_state,
                     SEARCH_TASK_TTL, _rate_limit_store)
# 中间件（认证/CSRF/限流/安全头）——app 级钩子经 middleware_bp 注册全局生效；
# _rate_limit_store 仍在 app 命名空间可见（tests/test_auth.py 清空同一共享 dict）；
# _get_csrf_token/_safe_next 亦为 app 命名空间契约（模板渲染 / test_auth.py 直接调用）
from middleware import bp as middleware_bp, _get_csrf_token, _safe_next

# 后台线程与下载引擎（auto_follow / 预取 / 下载 / 启动重置）已迁至 background.py：
# 以下 app 命名空间绑定为测试补丁契约，勿删（见 docs/architecture.md「测试契约」）
import background
from background import (
    _shutdown_background_threads, _reset_stuck_downloads, _reset_stuck_prefetch,
    _prefetch_one_tag,
    _prefetch_refresh_bookmarks, _prefetch_capacity_cleanup, _prefetch_loop,
    _start_prefetch_thread, reset_prefetch_refresh,
)
# 路由 Blueprint（搜索/缓存/关注 + 图库/详情/图片/收藏 + 下载/预取/收藏夹/设置）；
# _cleanup_search_tasks 经 from-import 保持在 app 命名空间（tests/test_app.py 直接调用 app._cleanup_search_tasks()）；
# _SETTINGS_PATH/_load_settings 同样保持在 app 命名空间：test_prefetch_api.py 夹具
# monkeypatch('app._SETTINGS_PATH') 重定向 settings.json，routes_prefetch.prefetch_config_post
# 经 app 命名空间调用 app._load_settings()/app._SETTINGS_PATH 以看到该补丁。
from routes_search import bp as search_bp, _cleanup_search_tasks
from routes_gallery import CACHE_DIR, bp as gallery_bp
from routes_download import bp as download_bp
from routes_prefetch import bp as prefetch_bp
from routes_collections import bp as collections_bp
from routes_settings import bp as settings_bp, _SETTINGS_PATH, _load_settings

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
app.register_blueprint(download_bp)
app.register_blueprint(prefetch_bp)
app.register_blueprint(collections_bp)
app.register_blueprint(settings_bp)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
# 缓存目录路径由 routes_gallery 单点定义（此前两处各写一份，靠注释约定保持一致）
os.makedirs(CACHE_DIR, exist_ok=True)
# 启动时兜底清理：上次进程可能留下超限的缓存，不必等下一次写入触发
enforce_image_cache_limit(CACHE_DIR, force=True)

init_db()


# ── 启动自检 ──
def _warn_if_unprotected() -> None:
    """ACCESS_PASSWORD 为空即全站免认证，启动时明确告警（默认单人本机部署如此）。

    告警而不是阻断：单人本机自部署免认证是既定用法（见 AGENTS.md「认证」），
    这里只是让"忘了设密码就挂到公网"这件事在日志里可见。
    """
    if not ACCESS_PASSWORD:
        logger.warning('ACCESS_PASSWORD 未设置：全站免认证，仅限本机/可信内网使用；公网部署必须设置')


def _warn_if_tls_unverified() -> None:
    """TLS 校验被关闭时告警：那种状态下链路上的任何中间人都能读改流量。

    默认已是开启（config.SSL_VERIFY 默认 True）；只有为"代理确实做 TLS 拦截"而
    显式设了 `SSL_VERIFY=false` 才会走到这里 —— 让这个状态在日志里可见。
    """
    if not SSL_VERIFY:
        logger.warning('TLS 校验已关闭（SSL_VERIFY=false）：流量可被链路上任何中间人读取/篡改；'
                       '仅在代理做 TLS 拦截时使用，判定办法见 scripts/check_tls.py')


def _dev_server_bind() -> tuple[str, int]:
    """`python app.py` 的绑定地址：默认只监听 loopback。

    以前默认 `0.0.0.0`：本应用默认无访问密码，且 `/api/open-dir` 能在服务器上
    打开本地目录 —— 直跑等于把这些能力暴露给整个局域网。要对外开放请走
    gunicorn + 反代（见 docs/maintenance.md「公网部署检查清单」）。
    """
    return os.environ.get('HOST', '127.0.0.1'), int(os.environ.get('PORT', '5000'))


_warn_if_unprotected()
_warn_if_tls_unverified()


# ── 后台任务组装（定义见 background.py）──
# 保持重构前 import 时序：init_db → _reset_stuck_*（清理残留状态）→ 后台线程启动 → atexit 注册。
_reset_stuck_downloads()
_reset_stuck_prefetch()

background.start_background_threads()


atexit.register(_shutdown_background_threads)


@app.route('/favicon.ico')
def favicon() -> Response:
    return Response(status=204)


@app.route('/')
def index() -> str:
    return render_template('index.html', csrf_token=_get_csrf_token(), max_bookmarks_default=MAX_BOOKMARKS_DEFAULT)


@app.route('/csrf-token')
def csrf_token() -> Response:
    return jsonify({'token': _get_csrf_token()})


@app.route('/cache')
def cache_page() -> str:
    """缓存浏览页：查看预取标签的缓存结果。"""
    return render_template('cache.html', csrf_token=_get_csrf_token())


if __name__ == '__main__':
    _host, _port = _dev_server_bind()
    app.run(debug=False, host=_host, port=_port)
