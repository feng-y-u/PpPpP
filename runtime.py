# ── 进程内存状态（-w 1 单进程语义，勿多 worker 部署）──
# 所有后台任务与内存状态都依赖单进程常驻，这是单用户自用场景的有意设计。
import threading
from concurrent.futures import ThreadPoolExecutor

from config import (
    DOWNLOAD_MAX_WORKERS, AUTO_FOLLOW_INTERVAL, AUTO_FOLLOW_DOWNLOAD,
    PREFETCH_INTERVAL, PREFETCH_PAGES, PREFETCH_MAX_ILLUSTS,
)

_scan_cache: dict = {'ts': 0.0, 'data': {}}
_SCAN_CACHE_TTL = 30.0  # 图库目录扫描缓存（秒）：避免每页请求全量重扫磁盘（省 IOPS）

# 缩略图代理实时拉取：限制并发数（而非按时间节流——节流会把批量缩略图
# 压成串行队列，刷新页面时肉眼可见变慢）+ 失败 URL 冷却防放大
_thumb_sem = threading.Semaphore(6)
_thumb_failed: dict[str, float] = {}
_THUMB_FAIL_COOLDOWN = 30.0

# 图库孤儿判定用的"全部 DB pixiv_id 集合"缓存：避免每次图库请求全表加载
#（illusts 表可能远大于已下载数，含搜索/预取记录）
_db_pids_cache: dict = {'ts': 0.0, 'data': set()}
_DB_PIDS_CACHE_TTL = 30.0

# ── ⚠ 多进程限制 ─────────────────────────────────────
# 以下状态变量（_auto_follow_state、download_locks、
# download_cancellations、_queued_downloads、_download_progress、
# _prefetch_state）存在于进程内存中。使用多个 gunicorn worker
# （或任何多进程部署）时，每个 worker 拥有自己的副本，
# 因此状态不在 worker 之间共享。worker A 启动的下载
# 对 worker B 不可见。
#
# 要正确支持多 worker，需要共享存储
#（Redis / SQLite KV 表）。在此之前，请使用单 worker 运行：
#   gunicorn -w 1 app:app
# 注：download_locks 已随下载引擎迁移至 background.py（模块级全局，与 _download_illust 同居）
# ─────────────────────────────────────────────────────────────────────

# ── 自动关注后台任务 ──
_auto_follow_state = {
    'last_check': None,
    'last_count': 0,
    'interval': AUTO_FOLLOW_INTERVAL,
    'auto_download': AUTO_FOLLOW_DOWNLOAD,
}
_auto_follow_stop = threading.Event()

_prefetch_state = {
    'running': False,
    'last_check': None,
    'interval': PREFETCH_INTERVAL,
    'pages': PREFETCH_PAGES,
    'max_illusts': PREFETCH_MAX_ILLUSTS,
}

# ── 下载队列/进度/取消/线程池 ──
download_executor = ThreadPoolExecutor(max_workers=DOWNLOAD_MAX_WORKERS)
download_cancellations: set[int] = set()
_queued_downloads: set[int] = set()
_download_progress: dict[int, dict] = {}

# ── 简单内存限流器 ──
# 清理计数器 _rate_limit_cleanup_counter 已随 _rate_limit 迁至 middleware.py
#（函数内 global 声明指向其定义模块，单一归属；此处仅保留原地修改的 store）。
_rate_limit_store: dict[str, list[float]] = {}

# ── 异步搜索任务 ──
# 搜索（含限速拉取详情）在后台线程执行，/search 立即返回 task_id，
# 前端轮询 /api/search/status/<id>。gunicorn -w 1 下搜索不再阻塞其他请求。
_search_tasks: dict[str, dict] = {}
_search_tasks_lock = threading.Lock()
SEARCH_TASK_TTL = 600.0  # 完成 10 分钟后清理
