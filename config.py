import json
import logging
import os
import platform
import secrets
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── .env 文件加载 ──
# 必须在实例目录派生之前：PIXIV_INSTANCE_DIR 与其它键一样允许写进 .env。
_dotenv = os.path.join(BASE_DIR, '.env')
if os.path.exists(_dotenv):
    with open(_dotenv) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith('#'):
                continue
            if '=' in _line:
                _k, _v = _line.split('=', 1)
                _k = _k.strip()
                _v = _v.strip().strip('"').strip("'")
                if _k and _v:
                    os.environ.setdefault(_k, _v)

# 实例数据目录：数据库 / 密钥 / settings.json / 图片缓存全部由它派生，只此一处定义。
# PIXIV_INSTANCE_DIR 用于重定向实例目录（测试隔离、多实例部署），必须在 import config
# 之前设置；未设置时仍是仓库内的 instance/，生产默认行为不变。
# 故意不做"目录不可用时回落到默认目录"的兜底 —— 覆盖值写错就该在 import 时炸掉，
# 否则测试/部署会静默读写真实实例数据。
_instance_dir = os.path.abspath(os.path.expanduser(
    os.environ.get('PIXIV_INSTANCE_DIR') or os.path.join(BASE_DIR, 'instance')))

# 游标签名密钥
_cursor_secret_path = os.path.join(_instance_dir, '.cursor_secret')
if os.path.exists(_cursor_secret_path):
    with open(_cursor_secret_path) as _f:
        CURSOR_SECRET = _f.read().strip()
else:
    CURSOR_SECRET = secrets.token_hex(32)
    os.makedirs(_instance_dir, exist_ok=True)
    with open(_cursor_secret_path, 'w') as _f:
        _f.write(CURSOR_SECRET)

# Cookie 文件路径（根据环境自动切换）
if platform.system() == 'Linux' and os.path.exists('/etc/pixiv-viewer/cookies.txt'):
    COOKIE_PATH = '/etc/pixiv-viewer/cookies.txt'
else:
    COOKIE_PATH = os.path.join(BASE_DIR, 'cookies.txt')

# 数据库
DATABASE_PATH = os.path.join(_instance_dir, 'pixiv.db')

# 下载目录
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')

# Pixiv API 设置
PIXIV_BASE_URL = 'https://www.pixiv.net'  # 可改为代理/镜像地址
SEARCH_PAGES = 10          # 每次搜索最多抓取页数
PER_PAGE = 60              # Pixiv 每页作品数
DETAIL_TIMEOUT = (10, 30)   # 详情 API 超时（连接, 读取）
DETAIL_MAX_RETRIES = 2     # 详情 API 最大重试次数
FETCH_DETAIL_WORKERS = 5   # 详情 API 并行获取线程数

# 搜索预取设置
PREFETCH_INTERVAL = 3600        # 预取间隔（秒），0 禁用
PREFETCH_PAGES = 3              # 每标签预取页数
PREFETCH_MAX_ILLUSTS = 10000    # 预取来源作品最大数量
# 预取"最终收藏数"刷新失败退避（秒）：失败后该时长内不再尝试该作品，
# 防止永久失败的死作品每轮占满 100 个名额（head-of-line blocking）。
PREFETCH_REFRESH_BACKOFF = 86400
# 刷新失败强制完成阈值（秒）：连续失败超过该时长仍未成功 → 视为处理完毕，
# 退出刷新队列交由容量清理按低收藏优先淘汰（防"未刷新"积压顶破容量上限）。
PREFETCH_REFRESH_FORCE_DONE = 14 * 86400
# 连续多少条详情请求遭遇"全局性失败"（403/429 限流、连接错误）即中止本轮刷新。
# 限流是账户级状态，不该按单作品记退避；熔断避免把整队列刷上 24h 退避并白烧
# 3s+9s 的退避时间。
PREFETCH_REFRESH_ABORT_STREAK = 3
# 每轮"最终收藏数刷新"最多处理多少条。提高它用的是**已有的** 20 条/分钟后台桶
# 预算（300 条 ≈ 15 分钟，仍在 1 小时间隔内），不碰 403 红线；作用是让"已刷新"
# 池更快变大，容量清理才有更多可信的淘汰对象。
PREFETCH_REFRESH_BATCH = 300
# 未刷新的作品在什么年龄之后可以被容量清理淘汰（秒）。配合"失败过"判定：
# 刷新队列证明推不动的作品不该继续占容量，但太新的作品仍给刷新一次机会。
PREFETCH_EVICT_UNREFRESHED_AFTER = 3 * 86400

# 显示设置
MEDIUM_IMAGE_SIZE = 600   # 详情页图片中图尺寸（长边 px），小站点建议 600 以下

# 缩略图代理并发上限：图片走 i.pximg.net 图床（比 Ajax API 宽松）。
# 调大 → 图库首屏 / 灯箱加载更快；调小 → 减轻对 Pixiv 的压力。需重启生效。
THUMB_CONCURRENCY = 12

# 缩略图磁盘缓存（instance/image_cache）容量上限与淘汰策略。
# 该目录此前只写不删，磁盘会无限增长（1 万条预取缓存的规模下可达 GB 级）。
# 淘汰的代价只是下次访问回源一次，所以上限可以设宽松些以减少回源。
IMAGE_CACHE_MAX_BYTES = 1024 * 1024 * 1024   # 1 GB
# 淘汰后回落到上限的这个比例：留出余量，避免每次只删一点点而反复扫描
IMAGE_CACHE_TARGET_RATIO = 0.9
# 扫描目录的最小间隔（秒）：扫描要遍历全部缓存文件，不能每次写入都做
IMAGE_CACHE_CLEANUP_INTERVAL = 300.0

# 下载设置
DOWNLOAD_MAX_WORKERS = 2   # 全局下载线程池并发数
PAGE_DOWNLOAD_INTERVAL = 3 # 多页作品页面间下载间隔（秒）
# /download_file 打包 zip 的内存阈值（字节）：总大小不超过它就用内存缓冲（快、无临时
# 文件），超过就改落临时文件流式发送。取 200 MB —— 单张原图通常 1~5 MB，一屏作品
# （多页作品几十张）极少超过，而超限时那份 zip 会与正在跑的下载/缩略图抢同一进程内存
# （gunicorn -w 1 单进程常驻），代价远大于一次临时文件写盘。需重启生效。
ZIP_MEMORY_THRESHOLD_BYTES = 200 * 1024 * 1024

# 搜索设置
MAX_BOOKMARKS_DEFAULT = 0  # 默认最低收藏数

# 翻页设置
ITEMS_PER_PAGE = 24            # 每页展示作品数 (1-60)

# 自动关注抓取
AUTO_FOLLOW_INTERVAL = 600   # 检查间隔（秒），0 禁用
AUTO_FOLLOW_DOWNLOAD = False # 是否自动下载新作品

# 网络代理
PROXY = ''                   # HTTP/SOCKS5 代理, 如 'http://127.0.0.1:7890', 留空禁用

# SSL 证书验证
# 默认**开启**：关闭校验意味着链路（代理、网关、公共 WiFi）上的任何中间人都能
# 读取并篡改流量。只有当"你的代理确实在做 TLS 拦截（用自签根证书解密）"时才设
# `SSL_VERIFY=false` —— 先跑 `python scripts/check_tls.py` 判定，规则见
# docs/maintenance.md「8. 公网部署检查清单」。
SSL_VERIFY = os.environ.get('SSL_VERIFY', 'true').lower() != 'false'

# 图片主机白名单：决定"访问该主机时是否允许携带 Pixiv 凭据"
# i.pximg.net 是 Pixiv 官方图床。若你自建图片镜像/反代，把它的域名加进来；
# 白名单外的主机仍可访问（下载引擎、缩略图重定向），但**不带 Cookie**。
IMAGE_HOST_ALLOWLIST = frozenset({'i.pximg.net'})

# 缩略图越界重定向自动发现（默认开启）：
# 白名单外的**公网 https** 目标用无凭据会话跟随一次（且要求响应确实是图片），
# 并记入发现表供观测（`GET /api/thumb/redirect-hosts`）。设 False → 跨域重定向
# 一律拒绝，回到"只允许白名单内重定向"的纯拒绝行为。
THUMB_REDIRECT_DISCOVERY = os.environ.get('THUMB_REDIRECT_DISCOVERY', 'true').lower() != 'false'

# 设置页访问密码（留空则不启用）
# 可通过环境变量 SETTINGS_PASSWORD 或 settings.json 的 settings_password 设置
SETTINGS_PASSWORD = os.environ.get('SETTINGS_PASSWORD', '')

# 全局访问密码（留空 = 免认证，本机使用无需设置；公网部署必须设置）
ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', '')

# Session Cookie 仅 HTTPS 传输（公网反代 HTTPS 时应为 True；本地 HTTP 调试可设 false）
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', 'true').lower() != 'false'

# ── ⚠ 从 settings.json 覆盖配置（运行时通过设置页面修改） ──────────
# 注意：这里在模块 import 时修改全局常量。因为运行在 import 时，
# settings.json 必须在模块首次被 import 前存在。import 之后修改
# settings.json 需要重启进程才能生效。
# 未来可改为 Config 类延迟加载，消除 import 时副作用。

# 统一设置键定义：settings.json 键 → (生效常量名, 默认值)。
# config.py（import 时覆盖常量）与 app.py（设置页白名单/默认值）
# 共用这一份，新增/改名设置键只改这里，避免两处失步。
SETTINGS_KEYS: dict[str, tuple[str, object]] = {
    'proxy': ('PROXY', ''),
    'settings_password': ('SETTINGS_PASSWORD', ''),
    'access_password': ('ACCESS_PASSWORD', ''),
    'cookie_secure': ('COOKIE_SECURE', True),
    'download_max_workers': ('DOWNLOAD_MAX_WORKERS', 2),
    'per_page': ('PER_PAGE', 60),
    'search_pages': ('SEARCH_PAGES', 10),
    'max_bookmarks_default': ('MAX_BOOKMARKS_DEFAULT', 0),
    'auto_follow_interval': ('AUTO_FOLLOW_INTERVAL', 600),
    'auto_follow_download': ('AUTO_FOLLOW_DOWNLOAD', False),
    'fetch_detail_workers': ('FETCH_DETAIL_WORKERS', 5),
    'medium_image_size': ('MEDIUM_IMAGE_SIZE', 600),
    'items_per_page': ('ITEMS_PER_PAGE', 24),
    'prefetch_interval': ('PREFETCH_INTERVAL', 3600),
    'prefetch_pages': ('PREFETCH_PAGES', 3),
    'prefetch_max_illusts': ('PREFETCH_MAX_ILLUSTS', 10000),
}

def _backup_corrupt_settings(path: str) -> None:
    """把读不出来的 settings.json 留一份副本（审计 S16），回退默认值的行为不变。

    为什么值得留：损坏时 `config.py` 只回退默认值、`_load_settings()` 也只回退默认值，
    而设置页下次保存会用默认值**整体覆盖**这个文件 —— 用户那份（可能只是手抖少了个
    括号、或磁盘写坏了一行）的配置就永久没了，事后无从查证。副本只在不存在时写一次，
    避免每次启动都覆盖掉第一份现场（首次损坏才有诊断价值）。

    任何失败都只记日志：备份是附加证据，不能让它影响启动。
    """
    backup = f'{path}.corrupt.bak'
    try:
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
    except OSError as e:
        logging.getLogger(__name__).warning(f'[config] 备份损坏的 settings.json 失败: {e!r}')


_settings_path = os.path.join(_instance_dir, 'settings.json')
if os.path.exists(_settings_path):
    try:
        with open(_settings_path, 'r', encoding='utf-8') as _f:
            _overrides = json.load(_f)
        for _json_key, (_const_name, _default) in SETTINGS_KEYS.items():
            if _json_key in _overrides and _overrides[_json_key] != '':
                _val = _overrides[_json_key]
                if _json_key == 'cookie_secure':
                    # 统一布尔化：手改 settings.json 为字符串 "false" 时不能变成
                    # 真值字符串（否则 Session Cookie 被标记 Secure，本地 HTTP 登录失效）
                    _val = str(_val).lower() in ('1', 'true', 'yes', 'on')
                globals()[_const_name] = _val
    except Exception as _e:
        logging.getLogger(__name__).warning(
            f'[config] settings.json 读取失败，已回退默认配置: {_e!r}')
        _backup_corrupt_settings(_settings_path)
