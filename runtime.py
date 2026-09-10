# ── 进程内存状态（-w 1 单进程语义，勿多 worker 部署）──
# 所有后台任务与内存状态都依赖单进程常驻，这是单用户自用场景的有意设计。
import threading
from concurrent.futures import ThreadPoolExecutor

from config import (
    DOWNLOAD_MAX_WORKERS, AUTO_FOLLOW_INTERVAL, AUTO_FOLLOW_DOWNLOAD,
    PREFETCH_INTERVAL, PREFETCH_PAGES, PREFETCH_MAX_ILLUSTS,
    THUMB_CONCURRENCY,
)

_scan_cache: dict = {'ts': 0.0, 'data': {}}
_SCAN_CACHE_TTL = 30.0  # 图库目录扫描缓存（秒）：避免每页请求全量重扫磁盘（省 IOPS）

# 缩略图代理实时拉取：限制并发数（而非按时间节流——节流会把批量缩略图
# 压成串行队列，刷新页面时肉眼可见变慢）+ 失败 URL 冷却防放大
_thumb_sem = threading.Semaphore(THUMB_CONCURRENCY)
_thumb_failed: dict[str, float] = {}
# 遍历清理与并发写入必须互斥：容器的清理是 Python 层推导式（每条之间有
# 字节码边界，可被其他线程抢入），期间被改动会抛
# RuntimeError: dictionary changed size during iteration。
_thumb_failed_lock = threading.Lock()
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
    # 最近一轮"最终收藏数刷新"的结构化统计（background._prefetch_refresh_bookmarks
    # 每轮结束/中止时整份替换；键固定，供 /api/prefetch/status 暴露）
    'refresh_stats': None,
}

# ── 下载队列/进度/取消/线程池 ──
download_executor = ThreadPoolExecutor(max_workers=DOWNLOAD_MAX_WORKERS)
download_cancellations: set[int] = set()
_queued_downloads: set[int] = set()
_download_progress: dict[int, dict] = {}

# `_queued_downloads` 的并发纪律（2026-09-10 审计 S3）：请求线程（trigger /
# batch / cancel）、后台 worker（`_download_illust` 的 discard）、容量清理与最终
# 收藏数刷新都要碰它。裸 set 有两个问题：① `list(set)` 与并发 discard 交错会抛
# `RuntimeError: Set changed size during iteration`（`/api/downloads` 每次刷新都
# 遍历）；② 队列判定是"读 → 判定 → 写"序列，分开执行会各自读到未计入对方的中间
# 状态。约定：**写点一律 `with _download_queue_lock`，读点一律走下面两个 helper**
# （单元素判定 `is_queued_download`、遍历 `queued_download_snapshot`）。
_download_queue_lock = threading.Lock()


def queued_download_snapshot() -> set[int]:
    """`_queued_downloads` 的一致快照：所有遍历/批量判定都走这里。"""
    with _download_queue_lock:
        return set(_queued_downloads)


def is_queued_download(pixiv_id: int) -> bool:
    """单元素队列判定（加锁读，与其他读写点看到同一状态）。"""
    with _download_queue_lock:
        return pixiv_id in _queued_downloads

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
