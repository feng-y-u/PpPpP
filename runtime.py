# ── 进程内存状态（-w 1 单进程语义，勿多 worker 部署）──
# 所有后台任务与内存状态都依赖单进程常驻，这是单用户自用场景的有意设计。
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from config import (
    DOWNLOAD_MAX_WORKERS, AUTO_FOLLOW_INTERVAL, AUTO_FOLLOW_DOWNLOAD,
    PREFETCH_INTERVAL, PREFETCH_PAGES, PREFETCH_MAX_ILLUSTS,
    THUMB_CONCURRENCY,
)

logger = logging.getLogger(__name__)

_scan_cache: dict = {'ts': 0.0, 'data': {}}
_SCAN_CACHE_TTL = 30.0  # 图库目录扫描缓存（秒）：避免每页请求全量重扫磁盘（省 IOPS）

# 缩略图代理实时拉取：限制并发数（而非按时间节流——节流会把批量缩略图
# 压成串行队列，刷新页面时肉眼可见变慢）+ 失败 URL 冷却防放大
_thumb_sem = threading.Semaphore(THUMB_CONCURRENCY)
# 等槽位的上限（秒，审计 S11）：图库一屏几十张缩略图同时未命中缓存时会全部来抢
# 槽位，若图床变慢（每张几秒），后来者会被**无限期**挂住 —— 攒到 gunicorn 的
# --timeout 300 就整个 worker 被杀，整批请求一起失败。宁可让超时的那张快速失败
# （前端本来就有占位图），也不要拖垮整页与 worker。15s 远大于正常取图耗时（单张
# 通常 < 2s），只在图床确实卡住时才触发。
THUMB_SEM_TIMEOUT = 15.0
_thumb_failed: dict[str, float] = {}
# 遍历清理与并发写入必须互斥：容器的清理是 Python 层推导式（每条之间有
# 字节码边界，可被其他线程抢入），期间被改动会抛
# RuntimeError: dictionary changed size during iteration。
_thumb_failed_lock = threading.Lock()
_THUMB_FAIL_COOLDOWN = 30.0

# ── /thumb 越界重定向：自动发现表 + 拒绝计数（仅观测用途）──
# 跨域重定向跟随成功后记录目标主机，用于回答"Pixiv 图床是不是换域名了"。这里
# **不做自动提升白名单**：B 级（无凭据）本身就能正常取图，白名单只决定"是否携带
# 凭据"，所以不需要为了可用性放宽信任 —— 人工确认是官方 CDN 后往
# config.IMAGE_HOST_ALLOWLIST 加一行即可。
_thumb_redirect_lock = threading.Lock()
_thumb_redirect_hosts: dict[str, dict] = {}
_thumb_redirect_rejected: dict[str, int] = {}


def thumb_redirect_state_path() -> str:
    """发现表落盘路径：跟随 config 的实例目录（S8 引入 PIXIV_INSTANCE_DIR 后自动生效）。"""
    import config
    instance_dir = (getattr(config, '_instance_dir', '')
                    or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance'))
    return os.path.join(instance_dir, 'thumb_redirect_hosts.json')


def _save_thumb_redirect_hosts_locked() -> None:
    """原子落盘（tmp + os.replace）：进程被杀不会留下半截 JSON。须在锁内调用。"""
    path = thumb_redirect_state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'hosts': _thumb_redirect_hosts}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        # 观测数据写不进去不该影响取图；下次变更会重试
        logger.warning(f'越界重定向发现表落盘失败: {e}')


def load_thumb_redirect_hosts() -> int:
    """启动时恢复发现表（重启不丢）。容忍文件缺失/损坏。"""
    path = thumb_redirect_state_path()
    try:
        with open(path, encoding='utf-8') as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0
    hosts = payload.get('hosts') if isinstance(payload, dict) else None
    if not isinstance(hosts, dict):
        return 0
    loaded = {h: e for h, e in hosts.items() if isinstance(h, str) and isinstance(e, dict)}
    with _thumb_redirect_lock:
        _thumb_redirect_hosts.update(loaded)
    return len(loaded)


def note_thumb_redirect_host(host: str, *, url: str, content_type: str) -> bool:
    """记录一次跨域重定向发现；返回是否**首次**发现（调用方据此只告警一次）。"""
    now = datetime.now(timezone.utc).isoformat()
    with _thumb_redirect_lock:
        entry = _thumb_redirect_hosts.get(host)
        first = entry is None
        if first:
            entry = {'host': host, 'count': 0, 'first_seen': now, 'last_seen': now,
                     'sample_url': url, 'last_content_type': content_type}
            _thumb_redirect_hosts[host] = entry
        entry['count'] += 1
        entry['last_seen'] = now
        entry['sample_url'] = url
        entry['last_content_type'] = content_type
        _save_thumb_redirect_hosts_locked()
    return first


def note_thumb_redirect_rejected(host: str) -> None:
    """记录一次被拒的越界重定向（按目标主机计数，便于发现有人在扫内网）。"""
    with _thumb_redirect_lock:
        _thumb_redirect_rejected[host] = _thumb_redirect_rejected.get(host, 0) + 1


def thumb_redirect_snapshot() -> dict:
    with _thumb_redirect_lock:
        return {'discovered': [dict(entry) for entry in _thumb_redirect_hosts.values()],
                'rejected': dict(_thumb_redirect_rejected)}


def clear_thumb_redirect_hosts() -> None:
    with _thumb_redirect_lock:
        _thumb_redirect_hosts.clear()
        _thumb_redirect_rejected.clear()
        _save_thumb_redirect_hosts_locked()

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
    # 最近一轮的出错信息（审计 S20 遗留）：只在**成功处理完一轮**时清空，所以非空
    # 即代表"最近一轮就失败了"，而不是"历史上某轮出过问题"。补这个字段的原因与预取
    # 那套（_prefetch_state['last_error']）相同：last_check 陈旧既可能是"本来没有新
    # 作品"、也可能是"每轮都在失败"，光看时间戳分不开。注意"拉不到任何作品"**不算**
    # 出错（Cookie 失效时 Pixiv 也静默返回空结果，写进去就是假告警），所以那种轮次
    # 既不改这个字段、也不清它。
    # 这是"键集合固定、只改值"的 dict（见并发约定），新字段必须写在这个字面量里。
    'last_error': None,
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
    # 最近一轮预取出错信息（审计 S12）：整轮干净收尾时清空，非空即代表"最近一轮
    # 有错"。线程活着不等于在干活，线程死掉时 last_check 只会越来越旧 —— 需要
    # 一个直接说"这轮报了什么"的字段配合 /api/prefetch/status 观察。
    'last_error': None,
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
