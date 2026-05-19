import json
import os
import platform

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Cookie 文件路径（根据环境自动切换）
if platform.system() == 'Linux' and os.path.exists('/etc/pixiv-viewer/cookies.txt'):
    COOKIE_PATH = '/etc/pixiv-viewer/cookies.txt'
else:
    COOKIE_PATH = os.path.join(BASE_DIR, 'cookies.txt')

# 数据库
DATABASE_PATH = os.path.join(BASE_DIR, 'instance', 'pixiv.db')

# 下载目录
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')

# Pixiv API 设置
PIXIV_BASE_URL = 'https://www.pixiv.net'  # 可改为代理/镜像地址
SEARCH_PAGES = 10          # 每次搜索最多抓取页数
PER_PAGE = 60              # Pixiv 每页作品数
DETAIL_TIMEOUT = (5, 15)   # 详情 API 超时（连接, 读取）
DETAIL_MAX_RETRIES = 2     # 详情 API 最大重试次数
FETCH_DETAIL_WORKERS = 3   # 详情 API 并行获取线程数

# 下载设置
DOWNLOAD_MAX_WORKERS = 2   # 全局下载线程池并发数
PAGE_DOWNLOAD_INTERVAL = 3 # 多页作品页面间下载间隔（秒）

# 搜索设置
MAX_BOOKMARKS_DEFAULT = 0  # 默认最低收藏数

# 自动关注抓取
AUTO_FOLLOW_INTERVAL = 600   # 检查间隔（秒），0 禁用
AUTO_FOLLOW_DOWNLOAD = False # 是否自动下载新作品

# 网络代理
PROXY = ''                   # HTTP/SOCKS5 代理, 如 'http://127.0.0.1:7890', 留空禁用

# SSL 证书验证
SSL_VERIFY = False           # 生产环境建议设为 True，需安装 CA 证书

# 设置页访问密码（留空则不启用）
# 可通过环境变量 SETTINGS_PASSWORD 或 settings.json 的 settings_password 设置
SETTINGS_PASSWORD = os.environ.get('SETTINGS_PASSWORD', '')

# Pixiv OAuth 自动登录（可选）
# 配置后自动通过 OAuth 维护会话，不再需要手动填写 cookies.txt
PIXIV_USERNAME = os.environ.get('PIXIV_USERNAME', '')
PIXIV_PASSWORD = os.environ.get('PIXIV_PASSWORD', '')

# ── 从 settings.json 覆盖配置（运行时通过设置页面修改） ──
_settings_path = os.path.join(BASE_DIR, 'instance', 'settings.json')
if os.path.exists(_settings_path):
    try:
        with open(_settings_path, 'r', encoding='utf-8') as _f:
            _overrides = json.load(_f)
        _key_map = {
            'proxy': 'PROXY',
            'settings_password': 'SETTINGS_PASSWORD',
            'download_max_workers': 'DOWNLOAD_MAX_WORKERS',
            'per_page': 'PER_PAGE',
            'search_pages': 'SEARCH_PAGES',
            'max_bookmarks_default': 'MAX_BOOKMARKS_DEFAULT',
            'auto_follow_interval': 'AUTO_FOLLOW_INTERVAL',
            'auto_follow_download': 'AUTO_FOLLOW_DOWNLOAD',
        }
        for _json_key, _const_name in _key_map.items():
            if _json_key in _overrides and _overrides[_json_key] != '':
                globals()[_const_name] = _overrides[_json_key]
    except Exception:
        pass  # settings.json invalid → silently use defaults
