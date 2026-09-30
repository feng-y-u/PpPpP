from __future__ import annotations

import logging
import time
import hmac
import json
from base64 import urlsafe_b64encode, urlsafe_b64decode
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from config import (
    SEARCH_PAGES, PER_PAGE, ITEMS_PER_PAGE,
    DETAIL_MAX_RETRIES,  # 测试断言 `fetcher.DETAIL_MAX_RETRIES`；值本身由配置单点定义
    FETCH_DETAIL_WORKERS, CURSOR_SECRET,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from models import Illust, BlockedTag, get_session, get_favorite_pids, safe_commit

import pixiv_client
# ── 适配层再导出（历史调用方 + 测试补丁 seam）──
# Pixiv 的 Ajax 路径/查询参数/响应字段/认证/限流全部归 `pixiv_client`（分层契约见
# 该模块顶部 docstring）。这里再导出，是为了让 `fetcher.X` 的老调用方
# （routes_* / background / helpers / tests）不必同时改两处。
#
# ⚠️ 有状态符号（`_cookie_value` / `_cookie_mtime` / `_total_limiter` / `_fill_limiter` /
# `_detail_error_samples` / `PROXY` / `SSL_VERIFY`）刻意**不**在这里再导出：它们归适配层
# 所有，读取与补丁都必须直接对 `pixiv_client`。再导出一份只会制造"补丁打了却没生效"的
# 静默失效（`_TokenBucket` 是无状态类，再导出供测试构造限速器，见 test_fetcher.py）。
from pixiv_client import (
    PixivAuthError,
    PixivRateLimitedError,
    R18_TAGS,
    DEAD_DETAIL, RETRYABLE_GLOBAL_DETAIL,
    _TokenBucket,
    build_pixiv_session, build_credentialless_session,
    get_pooled_session, reset_pooled_session,
    get_detail_error_samples,
    split_tags as _split_tags,
    parse_tags as _parse_tags,
    item_pixiv_id, item_bookmark_count, parse_illust_summary,
)

# 适配层入口的历史私有名：background/tests 仍按老名字调用与打补丁。
# `_fetch_details_parallel` 里的 `_get_illust_detail(...)` 是模块级查找，补丁可见。
_get_illust_detail = pixiv_client.fetch_illust_detail

logger = logging.getLogger(__name__)


def encode_cursor(data: dict) -> str:
    payload = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
    b64 = urlsafe_b64encode(payload.encode()).decode().rstrip('=')
    sig = hmac.new(CURSOR_SECRET.encode(), b64.encode(), 'sha256').hexdigest()
    return b64 + '.' + sig


def decode_cursor(cursor: str) -> dict | None:
    try:
        b64, sig = cursor.rsplit('.', 1)
    except ValueError:
        return None
    expected = hmac.new(CURSOR_SECRET.encode(), b64.encode(), 'sha256').hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = urlsafe_b64decode(b64 + '===').decode()
    except Exception:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


_MAX_SCAN_PAGES = 10

# ── 单次搜索的详情拉取预算 ──
#
# 背景：作者搜索的过滤条件（hide_r18 / min_bookmarks）必须拿到详情的 tags 才能
# 判定，而 `_process_items` 的 early_stop 只数"**通过过滤**"的条数。筛选严格时
# 一页可能一条都不通过，paginated_search 于是继续翻页，最坏扫满 _MAX_SCAN_PAGES
# 页 × 整页详情 —— 10 × 24 × 1.33s ≈ 5 分钟起步。
#
# 预算把"一次搜索最多拉多少条详情"变成硬上限，在翻下一页前检查额度，耗尽即停。
# 代价：筛选极严时返回不足一屏就结束（宁可少给，也不要卡几分钟）。
#
# 状态放 threading.local：搜索任务跑在自己的线程里，天然按搜索隔离，无需加锁；
# 且不会像模块级变量那样被并发的另一次搜索污染。
USER_SEARCH_DETAIL_BUDGET_PAGES = 2   # 作者搜索预算 = ITEMS_PER_PAGE × 该倍数
_detail_budget = threading.local()


def _budget_begin(total: int) -> None:
    if total > 0:
        _detail_budget.remaining = float(total)


def _budget_end() -> None:
    _detail_budget.remaining = None


def _budget_consume(n: int) -> None:
    remaining = getattr(_detail_budget, 'remaining', None)
    if remaining is not None:
        _detail_budget.remaining = max(0.0, remaining - n)


def budget_exhausted() -> bool:
    """当前搜索的详情预算是否已耗尽。未启用预算时恒为 False。"""
    remaining = getattr(_detail_budget, 'remaining', None)
    return remaining is not None and remaining <= 0


# ── 搜索任务取消 ──
#
# 用户在搜索中途改条件重搜：路由层提交新任务时把旧任务的取消事件置位
# （见 routes_search._submit_search_task），旧任务在下一个检查点中止。
# 检查点：paginated_search 翻页前后 + _fetch_details_parallel 每个 worker
# 发起请求前 —— 取消延迟 ≤ 最慢的在途详情请求（≈1.4s）。页内已拉到的详情
# 照常入库（下次同条件搜索命中 existing_map 免重拉），只是不再继续翻页。
#
# 与预算同款 threading.local：只有任务线程自己能看到自己的取消事件，
# 预取/后台补全线程不受影响。


class SearchCancelledError(Exception):
    """搜索任务被新搜索取代（用户改了条件重搜），调用链据此中止。"""


class SearchRateLimitedError(Exception):
    """本次搜索被 Pixiv 全局限流截断（详情熔断闸开路），结果不完整。

    与 `SearchCancelledError` 并列，但语义相反：取消是"这批结果已作废"，限流是
    "这批结果**只是部分**"。调用方不得把它降级成空页或"没有匹配"——那等于把
    受限作品说成不存在：既会写进成功缓存（下次命中还是这份残缺页），又会让
    游标以为本页已筛选完。
    `paginated_search` 显式把它抛出宽泛的分页 except，由路由层以可重试状态收尾。
    """


_cancel_state = threading.local()


def _cancel_begin(should_stop: Callable[[], bool] | None) -> None:
    _cancel_state.should_stop = should_stop


def _cancel_end() -> None:
    _cancel_state.should_stop = None


def _cancelled() -> bool:
    """当前搜索是否已被取消。非搜索线程（预取/补全/主线程）恒为 False。"""
    cb = getattr(_cancel_state, 'should_stop', None)
    return cb is not None and cb()


# _fetch_details_parallel 的 worker 返回"已取消"哨兵：不发起请求、不计入
# attempted。用哨兵而非抛异常，避免与 worker 内真正的请求异常（记失败）混淆
_CANCELLED_FETCH = object()


def paginated_search(search_fn, query_params: dict, items_per_page: int,
                     cursor_data: dict | None = None, *,
                     detail_budget: int = 0) -> tuple:
    """游标驱动的分页搜索。

    Args:
        search_fn: 搜索函数，签名为 (page: int, remaining: int) -> tuple[list[dict], bool]。
            remaining 为本页还需收集的条数，供流式过滤跨页累计提前终止。
        query_params: {type, query, sort, tag_mode, r18_mode, min_bookmarks}
        items_per_page: 每页件数
        cursor_data: 解码后的游标，None 表示新搜索
        detail_budget: 本次搜索允许拉取的详情总条数上限（0 = 不限）。
            仅作者搜索需要 —— 它的详情是"必须拉完才知道能不能要"的成本，
            其余路径要么不拉详情（defer），要么代价是常数级。

    本层**不得**接收 progress / publisher 形参：它只看页边界、看不到条目，收下也只能
    原样丢掉（"接线通过、测试全绿、端到端零事件"的静默陷阱）。publisher 一律注入
    `search_by_tag` / `search_by_user` / `browse_discovery`，由它们转交 `_process_items`。

    Returns:
        (results, next_cursor, has_more)
    """
    pixiv_page = cursor_data.get('pixiv_page', 1) if cursor_data else 1
    skip_count = cursor_data.get('skip_count', 0) if cursor_data else 0
    collected: list[dict] = []
    page_sizes: list[int] = []
    pages_scanned = 0
    pixiv_has_more = True
    effective_start = pixiv_page  # 实际开始收集的页号（跳过整页后会滞后）

    _budget_begin(detail_budget)
    try:
        while len(collected) < items_per_page and pages_scanned < _MAX_SCAN_PAGES:
            # 预算在**翻下一页之前**检查：本页已发起的详情不打断（early_stop 负责
            # 页内提前终止），耗尽后不再开新的一页。
            if budget_exhausted():
                logger.info(
                    f'paginated_search: 详情预算 {detail_budget} 条已耗尽，'
                    f'已收集 {len(collected)}/{items_per_page} 件，停止翻页')
                break
            # 取消同理：不再开新的一页。已收集的批次直接作废（结果永远不会被
            # 前端渲染 —— 取消者是新搜索，旧结果渲染了也是错的）
            if _cancelled():
                raise SearchCancelledError()
            try:
                remaining = items_per_page - len(collected)
                results, has_more = search_fn(page=pixiv_page, remaining=remaining)
            except PixivAuthError:
                raise
            except SearchCancelledError:
                raise
            except SearchRateLimitedError:
                # 限流不是"这页失败了"，而是"这页只拉到了一部分"：如果落进下面的
                # 宽泛 except，残缺页会被当成正常（甚至空）页完成，并且照常返回游标。
                raise
            except Exception as e:
                logger.error(f'paginated_search: page {pixiv_page} failed: {e}')
                # 失败页不可靠：结束分页，避免游标卡在失败页反复重试
                # （已收集的批次照常返回，前端显示已有数据、无下一页）
                pixiv_has_more = False
                break

            # 页内取消（_fetch_details_parallel 提前返回了部分结果）：当前页
            # 已在 search_fn 内提交入库，这里直接中止，不把残缺页当正常结果返回
            if _cancelled():
                raise SearchCancelledError()

            if not results and not has_more:
                pixiv_has_more = False
                break

            if skip_count > 0 and results:
                if len(results) <= skip_count:
                    skip_count -= len(results)
                    pages_scanned += 1
                    pixiv_page += 1
                    effective_start = pixiv_page
                    if not has_more:
                        pixiv_has_more = False
                        break
                    continue
                else:
                    results = results[skip_count:]
                    skip_count = 0

            collected.extend(results)
            page_sizes.append(len(results))
            pages_scanned += 1
            pixiv_page += 1

            if not has_more:
                pixiv_has_more = False
    finally:
        _budget_end()

    if pages_scanned == _MAX_SCAN_PAGES and len(collected) < items_per_page:
        logger.info(f'paginated_search: 扫描 {_MAX_SCAN_PAGES} 页未攒够 {items_per_page} 件')

    batch = collected[:items_per_page]

    # 计算下一页 cursor：遍历 page_sizes 找到 batch 结束位置
    cursor_pixiv_page = cursor_data.get('pixiv_page', 1) if cursor_data else 1
    cursor_skip = cursor_data.get('skip_count', 0) if cursor_data else 0
    next_pixiv_page = effective_start
    next_skip = 0
    cumulative = 0
    for sz in page_sizes:
        if cumulative + sz > len(batch):
            next_skip = len(batch) - cumulative
            # 如果还在游标的同一页内，累加之前的偏移
            if next_pixiv_page == cursor_pixiv_page:
                next_skip += cursor_skip
            break
        cumulative += sz
        next_pixiv_page += 1

    remaining = len(collected) - len(batch)
    has_more = remaining > 0 or pixiv_has_more

    next_cursor = None
    if has_more:
        next_cursor = encode_cursor({
            **query_params,
            'pixiv_page': next_pixiv_page if batch else pixiv_page,
            'skip_count': next_skip,
            'created_at': int(time.time()),
        })

    return batch, next_cursor, has_more


# ── Cookie / Session / 连接池 / 标签切分 ──
# 实现已迁至 `pixiv_client`（认证与传输的归属地）：`_load_cookie`、
# `build_pixiv_session`、`build_credentialless_session`、`get_pooled_session`、
# `reset_pooled_session` 由本模块顶部再导出；`_split_tags` 是适配层 `split_tags`
# 的别名（Pixiv 检索词的拼装见 `pixiv_client.build_search_query`）。
#
# 为什么整块搬走而不是"留在业务层也无所谓"：Cookie 缓存（`_cookie_value` /
# `_cookie_mtime`）与连接池失效戳必须与建 session 的代码同处一个模块，否则
# 设置页写盘后同步内存态、以及"mtime 变了就重建连接"这条链会跨模块断裂。


def _get_blocked_tags(db: Any) -> set[str]:
    return {t.tag for t in db.query(BlockedTag).all()}


# 收藏数刷新周期：距上次成功补全超过该天数的记录在搜索命中时重新拉取
BOOKMARK_STALE_DAYS = 7


def _is_bookmark_stale(illust: Illust) -> bool:
    """收藏数是否过期需要刷新。

    仅对"曾成功补全过"（bookmark_updated_at 非空）的记录生效——
    存量老数据该列为空，不触发批量刷新，避免首次部署时刷爆限流。
    """
    if not illust.bookmark_updated_at:
        return False
    updated = illust.bookmark_updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).days >= BOOKMARK_STALE_DAYS


def _is_blocked(tags: list[str], blocked: set[str]) -> bool:
    if not blocked:
        return False
    return bool(set(tags) & blocked)


# `R18_TAGS` / `parse_tags` / `extract_original_urls` 已迁至 `pixiv_client`
# （R18 词表是 Pixiv 标签词汇，tags 的多种历史形态与详情页原图地址的三条优先级
# 路径都是协议细节）。`_parse_tags` 是适配层 `parse_tags` 的别名。


def _is_r18(tags: list[str]) -> bool:
    return bool(set(tags) & R18_TAGS)


def _rejected(tags: list[str], bookmark_count: int, blocked: set[str],
              min_bookmarks: int, hide_r18: bool) -> bool:
    """记录是否被"屏蔽标签 / 收藏数 / R18"三者之一拒绝。

    入库前的过滤与逐条 progress 的"能否发布"必须是**同一份判定**，否则预览里
    会出现最终结果里没有的作品（或反之）。所以抽出来单点定义，以下各处共用。
    """
    return (
        _is_blocked(tags, blocked)
        or bookmark_count < min_bookmarks
        or (hide_r18 and _is_r18(tags))
    )

# 详情失败形态（`DEAD_DETAIL` / `RETRYABLE_GLOBAL_DETAIL`）、删除类报文关键词表与
# 未识别报文采样，全部随详情请求迁至 `pixiv_client` —— 它们是"这次请求的失败形态"，
# 不是业务概念。哨兵与 `get_detail_error_samples` 由本模块顶部再导出。

# 最近一次搜索的详情拉取统计（供前端展示"为什么慢"）
_last_fetch_stats: dict = {'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0}


def get_last_fetch_stats() -> dict:
    return dict(_last_fetch_stats)


# 详情请求本身（含 404/删除类判定、限流退避、连接错误 fail fast）在
# `pixiv_client.fetch_illust_detail`；`_get_illust_detail` 是它的历史私有别名，
# 由本模块顶部绑定（`_fetch_details_parallel` 按模块级名查找，测试补丁可见）。


@dataclass(frozen=True)
class _DetailFetchBatch:
    """一批详情请求的结果。

    `rate_limited=True` 表示这批被全局限流闸截断（详情请求被 `PixivRateLimitedError`
    拒绝）：`details` 只是**已完成的部分**，调用方不得当成完整结果（不许写成功缓存、
    不许把没拉到的作品当成"不匹配"）。
    单件失败仍用 `details` 里缺这个 pid / `None` 表示 —— 刻意不把两者混成同一个
    `None`：一个只影响这一件，另一个说明上游正在限流、继续发请求只会更糟。
    """

    details: dict[int, dict]
    attempted: int
    rate_limited: bool = False


def _fetch_details_parallel(pixiv_ids: list[int],
                            early_stop: Callable[[dict | None], bool] | None = None,
                            limiter: _TokenBucket | None = None,
                            on_detail: Callable[[int, dict | None], None] | None = None
                            ) -> _DetailFetchBatch:
    """并行拉取详情，支持提前终止，并可逐条回调已完成的结果。

    early_stop: 每完成一个详情后调用（参数为该详情或 None），返回 True 时
    取消未启动的拉取。**已启动的请求会全部处理完再返回**（不丢弃其结果，
    避免作品未入库导致分页漂移后跨页重复）；调用方需等待最慢的在途请求，
    代价受 DETAIL_TIMEOUT/退避上限约束。
    limiter: 请求限速器；不传时用适配层的前台高速桶（搜索），后台补全应传
        `pixiv_client._fill_limiter`。
    on_detail: 每个完成详情（含失败，即 detail=None）的回调，参数为 (pixiv_id, detail)。
        在**本函数的调用线程**（消费 as_completed 的 collector 线程）里按 future
        完成顺序调用 —— 调用方的进度回调要读 `_cancelled()`（threading.local）
        并复用调用线程的事务语义，所以不能下放到 worker 线程。
        被限流闸拒绝的请求不会回调：它根本没发出，不是"这件作品详情失败"。

    Returns: `_DetailFetchBatch`。`attempted` 只计**真正发出**的请求：early_stop /
    取消拦下的未启动请求、以及被限流闸拒绝的请求都不计入，避免统计把"未尝试"
    误报成"失败"。
    """
    if not pixiv_ids:
        return _DetailFetchBatch({}, 0)
    results = {}
    attempted = 0
    rate_limited = False
    auth_error: BaseException | None = None
    # 取消回调在**调用线程**（搜索任务线程）读取后闭包进 worker：threading.local
    # 在线程池线程里读不到任务线程的值。Event.is_set 线程安全，worker 里随时可查。
    cancel_cb = getattr(_cancel_state, 'should_stop', None)

    def _worker(pid: int) -> tuple[int, dict | None]:
        # 用线程内连接池：本线程处理的多个 pid 复用同一条连接（旧代码每个 pid
        # 都新建 Session，24~60 个作品就是 24~60 次 TCP + TLS 握手）
        if cancel_cb is not None and cancel_cb():
            return pid, _CANCELLED_FETCH
        session = get_pooled_session()
        return pid, _get_illust_detail(session, pid, limiter)

    executor = ThreadPoolExecutor(max_workers=FETCH_DETAIL_WORKERS)
    try:
        futures = {executor.submit(_worker, pid): pid for pid in pixiv_ids}
        for future in as_completed(futures):
            try:
                pid, detail = future.result()
            except CancelledError:
                continue  # 被 early_stop 取消的未发起请求：不计入 attempted/失败
            except PixivRateLimitedError as e:
                # 闸在**发出请求之前**拒绝：这条请求没发出，既不计 attempted、
                # 也不算详情失败（否则限流会被误报成"这些作品不匹配"）。
                # 未启动的任务立刻取消；已在途的继续收（成功详情照常保留并发布）。
                if not rate_limited:
                    logger.warning(f'详情批量拉取被限流闸截断（{e}），保留已完成的部分')
                rate_limited = True
                for f in futures:
                    f.cancel()
                continue
            except PixivAuthError as e:
                # 认证失效必须原样上报：宽泛 except 会把它吞成"这件详情失败"，
                # 整页静默变空（用户看到 0 结果，而不是"Cookie 已失效"）。
                # 先取消未启动的，收完在途的，最后再抛。
                if auth_error is None:
                    auth_error = e
                for f in futures:
                    f.cancel()
                continue
            except Exception as e:
                logger.error(f'Parallel fetch failed for {futures[future]}: {e}')
                detail = None
            if detail is _CANCELLED_FETCH:
                # 搜索被取消：与 early_stop 同款语义 —— 取消未启动的请求，已启动的
                # 照常处理完（其结果会入库，下次搜索免重拉）。取消的请求不计入
                # attempted，统计不误报"失败"。
                for f in futures:
                    f.cancel()
                continue
            attempted += 1
            if detail is not None:
                results[pid] = detail
            if on_detail is not None:
                on_detail(pid, detail)
            if early_stop is not None and early_stop(detail):
                # 触发早停：只取消未启动的请求；已启动的照常处理完再返回。
                # 若丢弃已启动的结果，这些作品不会写入 DB，Pixiv 分页漂移后
                # 会再次出现并被当作新作品 → 跨页重复（2026-08 bug 修复）。
                for f in futures:
                    f.cancel()
        if auth_error is not None:
            raise auth_error
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return _DetailFetchBatch(results, attempted, rate_limited)


# ── 后台详情补全 ──

_fill_lock = threading.Lock()
_filling_ids: set[int] = set()
_fill_last_attempt: dict[int, float] = {}
_FILL_ATTEMPT_INTERVAL = 300.0  # 同一作品两次补全尝试的最小间隔（秒），防 429 限流
# 节流表的条目上限：超过才触发清理（审计 S14）。见 _background_fill_details 的清理段。
_FILL_ATTEMPT_MAX_ENTRIES = 1000


def _background_fill_details(pixiv_ids: list[int]) -> None:
    """后台补拉详情并写入 DB（bookmark_count / original_urls）。

    使用 _filling_ids 集合去重，避免同一 pixiv_id 同时被多个补全任务拉取。
    同一作品距上次补全尝试不足 _FILL_ATTEMPT_INTERVAL 时跳过，防止反复失败刷限流。
    """
    if not pixiv_ids:
        return
    now = time.time()
    with _fill_lock:
        new_ids = [
            pid for pid in pixiv_ids
            if pid not in _filling_ids
            and now - _fill_last_attempt.get(pid, 0) >= _FILL_ATTEMPT_INTERVAL
        ]
        if not new_ids:
            return
        for pid in new_ids:
            _fill_last_attempt[pid] = now
        # 清理（审计 S14）：节流表按"见过的作品"只增不减，长期运行 + 大库（图库/
        # 缓存页每次翻页都会补全）会攒到几万条 int→float，永不释放。只在表变大时
        # 清理，且只清掉**已经远超节流窗口**（2 × _FILL_ATTEMPT_INTERVAL）的条目：
        # 这类条目即使留着，下一轮判定 `now - ts >= _FILL_ATTEMPT_INTERVAL` 也必然
        # 通过，所以删掉不改变任何节流行为。窗口内的一律保留。
        if len(_fill_last_attempt) > _FILL_ATTEMPT_MAX_ENTRIES:
            for pid, ts in list(_fill_last_attempt.items()):
                if now - ts >= _FILL_ATTEMPT_INTERVAL * 2:
                    del _fill_last_attempt[pid]
        _filling_ids.update(new_ids)
    try:
        batch = _fetch_details_parallel(new_ids, limiter=pixiv_client._fill_limiter)
        # 限流截断时照常写入已完成的部分（补全只是刷新收藏数/原图，拿到的就是
        # 有效数据）；`_fetch_details_parallel` 自己吞掉限流异常，不会打断补全循环。
        if batch.rate_limited:
            logger.info(
                f'后台详情补全被限流闸截断：只写入已完成的 {len(batch.details)}/{len(new_ids)} 条')
        details = batch.details
        if not details:
            return
        with get_session() as db:
            now_utc = datetime.now(timezone.utc)
            for pid, detail in details.items():
                existing = db.query(Illust).filter(Illust.pixiv_id == pid).first()
                if not existing:
                    continue
                if detail.get('original_urls'):
                    existing.original_urls_list = detail['original_urls']
                existing.bookmark_count = detail.get('bookmark_count', existing.bookmark_count)
                existing.bookmark_updated_at = now_utc
            safe_commit(db)
    except Exception as e:
        logger.error(f'Background fill details failed: {e}')
    finally:
        with _fill_lock:
            _filling_ids.difference_update(new_ids)


def _kick_background_fill(pixiv_ids: list[int]) -> None:
    """启动一个 daemon 线程异步补全详情。"""
    if not pixiv_ids:
        return
    t = threading.Thread(target=_background_fill_details, args=(list(pixiv_ids),), daemon=True)
    t.start()


# ── 短期搜索结果缓存 ──

_SEARCH_CACHE: 'OrderedDict[str, tuple[float, float, tuple[list[dict], bool]]]' = OrderedDict()
_SEARCH_CACHE_TTL = 30.0
_SEARCH_CACHE_MAX = 64
_search_cache_lock = threading.Lock()

# 作者搜索专用的、长得多的 TTL。
# 标签搜索 30 秒足够：它的成本是 1 次 HTTP，缓存主要是挡住重复点击。
# 作者搜索一页要发整页详情请求（24 条 × 1.33s ≈ 32s），30 秒的缓存会在用户
# 看完这一屏之前就失效，等于白缓存 —— 这里给 10 分钟。
# 代价是结果新鲜度，故作者搜索的缓存键必须带上屏蔽标签指纹（见
# _blocked_fingerprint），否则改完屏蔽标签要等十分钟才见效。
_USER_SEARCH_CACHE_TTL = 600.0

# 标签搜索专用的 TTL。
# 标签搜索的成本是 1 次 HTTP，30 秒的缓存常在用户"翻回上一页"时已经过期，
# 于是白等一次上游往返；120 秒覆盖了"看完一屏再回退"的实际节奏 —— 这是纯粹的
# 等待时间优化，不减少任何一次请求。
# 只有 `search_by_tag` 用它：发现页/关注页（`_SEARCH_CACHE_TTL`）与作者搜索
# （`_USER_SEARCH_CACHE_TTL`）的缓存成本与语义各不相同，不能顺手统一。
# 缓存键里**没有**屏蔽标签指纹，所以屏蔽标签增删必须靠 `clear_search_cache()`
# 整体清空才立即生效（见 routes_settings 的屏蔽标签路由）。
_TAG_SEARCH_CACHE_TTL = 120.0


def _blocked_fingerprint(blocked: set[str]) -> str:
    """屏蔽标签集合的短指纹，用于把"屏蔽标签变了"反映到缓存键里。

    hash() 带进程级随机盐，只在进程内稳定 —— 这正是内存缓存需要的范围。
    """
    return f'{len(blocked)}:{hash(frozenset(blocked)) & 0xfffffff:x}'


def _cache_get(key: str, ttl: float | None = None) -> tuple[list[dict], bool] | None:
    now = time.time()
    with _search_cache_lock:
        v = _SEARCH_CACHE.get(key)
        if v is None:
            return None
        ts, entry_ttl, value = v
        if now - ts > entry_ttl:
            _SEARCH_CACHE.pop(key, None)
            return None
        _SEARCH_CACHE.move_to_end(key)
        return value


def _cache_put(key: str, value: tuple[list[dict], bool], ttl: float | None = None) -> None:
    entry_ttl = _SEARCH_CACHE_TTL if ttl is None else ttl
    with _search_cache_lock:
        _SEARCH_CACHE[key] = (time.time(), entry_ttl, value)
        _SEARCH_CACHE.move_to_end(key)
        while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
            _SEARCH_CACHE.popitem(last=False)


def clear_search_cache() -> None:
    with _search_cache_lock:
        _SEARCH_CACHE.clear()


# ── 公共流水线 ──

def _mark_favorite(result: dict, fav: set[int]) -> dict:
    """按收藏集合给**单条**结果打标。

    只做"命中就置 True"：没命中时保持 `to_dict()` 给的 False，绝不写别的否定值 ——
    前端把缺失/未知与"明确未收藏"当成同一件事，写进去只会制造第二种含义。
    """
    if result.get('pixiv_id') in fav:
        result['is_favorite'] = True
    return result


def _insert_new_illusts(db, illusts: list[Illust]) -> dict[int, Illust]:
    """批量写新作品，撞 UNIQUE（pixiv_id 已存在）时静默跳过，返回赢家 pid→行 映射。

    必须用 `INSERT ... ON CONFLICT DO NOTHING`：`_process_items` 的"查重 → 拉详情
    （网络耗时）→ INSERT"之间存在并发窗口——同一 pid 可能已由并发线程（其他标签的
    预取、手动刷新、用户在途搜索）或本批重复条目插入；普通 `db.flush()` 会抛
    `UNIQUE constraint failed` 且整个事务作废、本页其余新作品全部丢失。
    冲突降级为 no-op 后再按 pid 回查赢家行（不区分谁赢，结果一致）。
    需要 SQLite ≥ 3.24（ON CONFLICT 语法；仓库最低要求本就高于此）。
    """
    pids = [i.pixiv_id for i in illusts]
    if not pids:
        return {}
    values = [
        {c.key: getattr(i, c.key) for c in Illust.__table__.columns if c.key != 'id'}
        for i in illusts
    ]
    db.execute(sqlite_insert(Illust).on_conflict_do_nothing(), values)
    rows = db.query(Illust).filter(Illust.pixiv_id.in_(pids)).all()
    return {i.pixiv_id: i for i in rows}


class _ProcessedItems(list):
    """`_process_items` 的结果列表，另带"本批是否被全局限流截断"。

    继承 list 是为了既有调用方零改动（迭代 / `==` 比较 / JSON 输出 / 测试下标访问
    照旧）；`rate_limited` 单独作属性，让调用方在 `safe_commit` 之后决定要不要以
    可重试状态收尾。不用 `None` 或空列表表达限流：那两者都已经是合法结果了
    （没有匹配 / 没有输入）。
    注意 `rate_limited` 只挂在这个对象上：切片/拷贝（`results[:n]`、`results + [...]`、
    `copy()`）都返回普通 list 并丢掉该标记，所以只在 `search_by_*` 边界读它才有意义。
    """

    def __init__(self, items=(), *, rate_limited: bool = False):
        super().__init__(items)
        self.rate_limited = rate_limited


def _process_items(db: Any, items: list[Any], id_extractor: Callable[[Any], int], illust_factory: Callable[[Any, dict], Illust], blocked: set[str], *,
                   min_bookmarks: int = 0, hide_r18: bool = False, defer_details: bool = False,
                   max_results: int = 0, limiter: _TokenBucket | None = None,
                   progress: Callable[[dict], None] | None = None) -> _ProcessedItems:
    """去重 → 过滤 → 并行拉取详情 → 存储，并逐条发布已通过过滤的结果。

    Args:
        db: SQLAlchemy 会话
        items: 原始作品字典列表（用户搜索时为 pixiv_id 整数列表）
        id_extractor: 可调用对象，接收 item 返回 int pixiv_id
        illust_factory: 可调用对象，接收 (item, detail) 返回 Illust 实例
        blocked: 被屏蔽标签的字符串集合
        min_bookmarks: 最低收藏数（0 表示不过滤）
        hide_r18: 若为 True，排除 R-18 标签作品
        defer_details: 若为 True 且 min_bookmarks=0，则用搜索条目自带的 tags/thumb
            立即返回列表（bookmark_count/original_urls 留空），后台异步补全详情。
            仅适用于 illust_factory 接受 detail=None 的工厂（如 _illust_from_item）。
        max_results: 收集到该数量的通过结果后提前停止拉取详情（0 = 不限制）。
            用于搜索流式过滤，凑够一页就停，避免拉取整页详情拖慢搜索。
        limiter: 详情请求限速器；不传用前台高速桶（搜索）。后台任务应传
            对应的低速桶（如 `pixiv_client._fill_limiter`），避免抢占交互搜索带宽。
        progress: 可选的逐条进度回调，在**调用线程**上调用，事件形态固定三种：
            {"type": "examined", "pixiv_id": pid}      每个输入 PID 恰好一次
            {"type": "detail_failed", "pixiv_id": pid} 请求完成但没拿到详情
            {"type": "result", "result": dict}         已通过该路径全部过滤的展示 dict
            回调只是"预览"：候选仍可能在最终批量入库时因并发冲突落空，也可能因为
            `_cancelled()`（旧任务已被新搜索取代）被跳过；可用的最终列表以返回值为准。
            回调里不碰 SQLAlchemy session，favorite 状态用调用线程一次性取到的
            收藏集合标记。

    Returns: `_ProcessedItems`（可直接用于 API 响应的 illust 字典列表）。
    """
    results: list[dict] = []
    if not items:
        return _ProcessedItems()

    fetch_stats = {'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0}
    fetch_start = time.time()

    # 收藏集合在调用线程取一次：进度回调会在任意详情完成的瞬间发布结果，每条都
    # 查一次收藏夹就是 N 次查询；回调里也不该再碰 session（见 progress 的说明）。
    fav_pids = get_favorite_pids(db)
    published_pids: set[int] = set()
    examined_pids: set[int] = set()
    rate_limited = False

    def _emit(event: dict) -> None:
        if progress is None or _cancelled():
            # 取消之后继续发布，只会给已被取代的旧任务刷进度（前端靠搜索代数丢弃它）；
            # 作品的入库语义不受影响，仍按原有事务收尾。
            return
        progress(event)

    def _publish(result: dict) -> None:
        """发布一条已通过过滤的结果；同一 PID 只发布一次。"""
        pid = result.get('pixiv_id')
        if progress is None or pid in published_pids:
            return
        published_pids.add(pid)
        _emit({'type': 'result', 'result': result})

    def _append_result(result: dict) -> None:
        """把一条已通过过滤的结果加入结果列表，并**当场**发布预览。

        为什么必须当场发布而不是等收尾统一兜底：已入库且通过过滤的记录（作者搜索
        里最常见的情形）不花任何网络请求就能定稿，若拖到收尾，它就得排在整页无关
        作品的详情请求后面（作者搜索一页 24 条 ≈ 30 秒）才出现在前端 —— 缓存命中
        反而变成最慢的一条，正好抹掉这条流水线渐进显示的意义。
        `_publish` 的 PID 去重保证详情回调路径已经发过的不再重发。
        """
        results.append(result)
        _publish(_mark_favorite(result, fav_pids))

    def _note_detail(pid: int, detail: dict | None) -> None:
        """详情完成的统一回调（collector 线程）：只记失败，成功由各路径自行发布。

        "没拿到详情"与"全局限流"是两件事：后者连请求都没发出，不会走到这里。
        """
        if detail is None:
            _emit({'type': 'detail_failed', 'pixiv_id': pid})

    # 条目按 pid 索引一次，供详情回调按 pid 找回原始条目造预览（重复输入取第一条，
    # 与原先前向查找 `next(...)` 的语义一致）
    pixiv_ids: list[int] = []
    items_by_pid: dict[int, Any] = {}
    for item in items:
        pid = id_extractor(item)
        pixiv_ids.append(pid)
        items_by_pid.setdefault(pid, item)

    existing_list = db.query(Illust).filter(Illust.pixiv_id.in_(pixiv_ids)).all()
    existing_map = {i.pixiv_id: i for i in existing_list}

    to_fetch: list[int] = []           # 同步拉详情（非 defer 路径）
    to_fill: list[int] = []            # 后台补全（defer 路径新写入 + 已有但缺原图/收藏数过期）
    to_refetch: list[int] = []         # 已有记录但 bookmark_count=0 或收藏数过期，需同步补全后重新判断过滤
    new_illusts: list[Illust] = []     # defer 路径批量写入

    now_utc = datetime.now(timezone.utc)

    for item in items:
        pixiv_id = id_extractor(item)
        if pixiv_id not in examined_pids:
            examined_pids.add(pixiv_id)
            # 每个输入 PID 恰好一次：放在判定之前，这样详情没拉到的（限流/取消）
            # 也仍然算"检查过"，进度计数不会漏。
            _emit({'type': 'examined', 'pixiv_id': pixiv_id})
        existing = existing_map.get(pixiv_id)
        if existing:
            stale = _is_bookmark_stale(existing)
            # bookmark_count 未补全（0）或收藏数过期：优先用列表接口自带的 bookmarkCount 修正
            if existing.bookmark_count == 0 or stale:
                # defer 路径：API 返回数据自带 bookmarkCount，直接更新跳过补全
                # （字段名归适配层：`item_bookmark_count` 认的就是 Pixiv 的 bookmarkCount）
                item_bm = item_bookmark_count(item) if isinstance(item, dict) else 0
                if defer_details and item_bm > 0:
                    existing.bookmark_count = item_bm
                    existing.bookmark_updated_at = now_utc
                elif min_bookmarks > 0:
                    # 用户设了最低收藏但条目无收藏数 → 同步重新拉详情判断
                    to_refetch.append(pixiv_id)
                    continue
            if not _rejected(existing.tags_list, existing.bookmark_count,
                             blocked, min_bookmarks, hide_r18):
                # 已存在且通过全部过滤：这一件不花任何网络请求，立即发布
                _append_result(existing.to_dict())
                # 缺原图或收藏数过期 → 后台补全刷新（非 defer 且设了最低收藏的批量
                # 路径除外，避免干扰其同步过滤语义；该路径失败记录另行兜底）
                if (not existing.original_urls_list or stale) \
                   and (defer_details or min_bookmarks == 0):
                    to_fill.append(pixiv_id)
            continue

        if defer_details:
            # tags 走适配层解析（Pixiv 的 tags 有多种历史形态，字段名只有一处定义）
            item_tags = parse_illust_summary(item)['tags'] if isinstance(item, dict) else []
            if _is_blocked(item_tags, blocked) or (hide_r18 and _is_r18(item_tags)):
                continue
            # 注意：defer 仅在 min_bookmarks==0 或显式 defer 时进入，此时无需收藏数过滤；
            # 列表接口不带 bookmarkCount，收藏数由后台补全写入，故此处无过滤分支
            illust = illust_factory(item, None)
            new_illusts.append(illust)
            to_fill.append(pixiv_id)
        else:
            to_fetch.append(pixiv_id)

    # 处理需要重新拉取详情的已有记录
    if to_refetch:
        refetch_batch = _fetch_details_parallel(
            to_refetch, limiter=limiter, on_detail=_note_detail)
        rate_limited = rate_limited or refetch_batch.rate_limited
        details, attempted = refetch_batch.details, refetch_batch.attempted
        _budget_consume(attempted)
        fetch_stats['detail_fetched'] += len(details)
        fetch_stats['detail_failed'] += attempted - len(details)
        for pixiv_id in to_refetch:
            detail = details.get(pixiv_id)
            if detail is None:
                # 详情拉取失败：不静默丢弃，排入后台补全，下次命中时再判断
                to_fill.append(pixiv_id)
                continue
            if _rejected(detail.get('tags', []), detail.get('bookmark_count', 0),
                         blocked, min_bookmarks, hide_r18):
                continue
            existing = existing_map[pixiv_id]
            existing.bookmark_count = detail.get('bookmark_count', existing.bookmark_count)
            existing.bookmark_updated_at = now_utc
            if detail.get('original_urls'):
                existing.original_urls_list = detail['original_urls']
            # 详情刚到手就发布：这条记录已经完整，没理由再等本页其余作品
            _append_result(existing.to_dict())

    if new_illusts:
        # 冲突容忍写入：并发/本批重复 pid 不炸整批（见 _insert_new_illusts）；
        # 同 pid 只进结果一次（本批重复条目在写入层被合并成一行）
        winners = _insert_new_illusts(db, new_illusts)
        seen_pids: set[int] = set()
        for illust in new_illusts:
            pid = illust.pixiv_id
            if pid in winners and pid not in seen_pids:
                seen_pids.add(pid)
                _append_result(winners[pid].to_dict())

    if to_fill:
        _kick_background_fill(to_fill)

    if to_fetch:
        # 流式过滤：拉取过程中直接判定过滤条件，凑够 max_results 条即提前终止
        passed = [0]

        def _early_stop(detail: dict | None) -> bool:
            if max_results <= 0 or detail is None:
                return False
            if _rejected(detail.get('tags', []), detail.get('bookmark_count', 0),
                         blocked, min_bookmarks, hide_r18):
                return False
            passed[0] += 1
            return passed[0] >= max_results

        def _publish_detail(pid: int, detail: dict | None) -> None:
            """新作品详情完成的回调（collector 线程）：通过过滤就立刻发布预览。"""
            _note_detail(pid, detail)
            if detail is None or progress is None:
                return
            if _rejected(detail.get('tags', []), detail.get('bookmark_count', 0),
                         blocked, min_bookmarks, hide_r18):
                return
            item = items_by_pid.get(pid)
            if item is None:
                return
            # 只是预览：正式结果仍走下面的批量冲突容忍写入（并发窗口里可能落空）
            _publish(_mark_favorite(illust_factory(item, detail).to_dict(), fav_pids))

        fetch_batch = _fetch_details_parallel(
            to_fetch, early_stop=_early_stop if max_results > 0 else None,
            limiter=limiter, on_detail=_publish_detail)
        rate_limited = rate_limited or fetch_batch.rate_limited
        details, attempted = fetch_batch.details, fetch_batch.attempted
        _budget_consume(attempted)
        fetch_stats['detail_fetched'] += len(details)
        fetch_stats['detail_failed'] += attempted - len(details)
        # 收集本页新作品，最后做一次冲突容忍批量写入（避免逐条 flush 撞 UNIQUE
        # 时整批事务作废；并发/重复 pid 的赢家行已存在，回查后结果一致）
        batch_illusts: list[Illust] = []
        batch_pids: list[int] = []
        seen_pids: set[int] = set()
        for pixiv_id in to_fetch:
            if pixiv_id in seen_pids:
                continue  # 本批重复条目并入一次
            seen_pids.add(pixiv_id)
            detail = details.get(pixiv_id)
            if detail is None:
                continue
            if _rejected(detail.get('tags', []), detail.get('bookmark_count', 0),
                         blocked, min_bookmarks, hide_r18):
                continue

            item = items_by_pid.get(pixiv_id)
            if item is None:
                continue

            illust = illust_factory(item, detail)
            illust.bookmark_updated_at = now_utc  # 详情同步拉取成功，收藏数为当前值
            batch_illusts.append(illust)
            batch_pids.append(pixiv_id)

        if batch_illusts:
            winners = _insert_new_illusts(db, batch_illusts)
            for pid in batch_pids:
                if pid in winners:
                    # 通常已在 `_publish_detail` 里发过（PID 去重会吞掉这次）；
                    # 走到这里仍未发布的（on_detail 没回调的实现）当场补发
                    _append_result(winners[pid].to_dict())

    # 收尾安全网：每条结果在加入列表的那一刻就已发布（见 `_append_result`），这里
    # 只兜住绕过 `_append_result` 进入 `results` 的路径（被 mock 的 seam、未来重构），
    # 保证结果列表里每条至少对应一个 result 事件（靠 published_pids 去重，不会重复发）。
    for r in results:
        _publish(_mark_favorite(r, fav_pids))

    if max_results > 0:
        fetch_stats['seconds'] = time.time() - fetch_start
        _last_fetch_stats.update(fetch_stats)

    return _ProcessedItems(results, rate_limited=rate_limited)


def _illust_from_item(item: dict, detail: dict | None = None) -> Illust:
    """从搜索/发现/关注 API 条目创建 Illust。

    大多数字段来自搜索结果条目（列表上下文）。
    detail 为 None 时表示详情尚未拉取，bookmark_count/original_urls 留空，
    由后台补全任务稍后填入。
    条目字段的读取全部经 `pixiv_client.parse_illust_summary`：Pixiv 改字段名
    （`userId` → …、`updateDate` → …）只需要改适配层一处。
    """
    summary = parse_illust_summary(item)
    illust = Illust(
        pixiv_id=summary['pixiv_id'],
        title=summary['title'],
        user_id=summary['user_id'],
        user_name=summary['user_name'],
        page_count=summary['page_count'],
        # 列表接口不返回 bookmarkCount（实测字段恒缺失），defer 写入时只能为 0，
        # 真实收藏数由后台补全任务写入
        bookmark_count=detail.get('bookmark_count', 0) if detail else 0,
        thumb_url=summary['thumb_url'],
        upload_date=_parse_date(summary['upload_date']),
    )
    illust.tags_list = summary['tags']
    illust.original_urls_list = detail.get('original_urls', []) if detail else []
    return illust


def _illust_from_detail(item: int, detail: dict) -> Illust:
    """从用户个人资料搜索创建 Illust（所有字段来自详情）。"""
    illust = Illust(
        pixiv_id=item,  # item IS the pixiv_id for user searches
        title=detail['title'],
        user_id=detail['user_id'],
        user_name=detail['user_name'],
        page_count=detail['page_count'],
        bookmark_count=detail['bookmark_count'],
        thumb_url=detail['thumb_url'],
        upload_date=_parse_date(detail['upload_date']),
    )
    illust.tags_list = detail['tags']
    illust.original_urls_list = detail['original_urls']
    return illust


# ── 搜索函数 ──

def search_by_tag(keyword: str, min_bookmarks: int = 0, page: int = 1,
                  sort_order: str = 'popular_d', max_pages: int = SEARCH_PAGES,
                  tag_mode: str = 'or', r18_mode: str = 'all',
                  defer_details: bool = False,
                  max_results: int = 0,
                  limiter: _TokenBucket | None = None,
                  progress: Callable[[dict], None] | None = None) -> tuple[list[dict], bool]:
    """按标签搜索 Pixiv。tag_mode: 'or' = 任一标签, 'and' = 全部标签。

    max_results: 流式过滤目标数量，凑够即提前停止拉取详情（0 = 不限制）。
    limiter: 详情请求限速器；不传用前台高速桶（搜索）。
    progress: 逐条进度回调（见 `_process_items`）；本页被限流截断时以
        `SearchRateLimitedError` 收尾，此时不写成功缓存。

    结果缓存用 `_TAG_SEARCH_CACHE_TTL`（120 秒）：它比发现/关注页的 30 秒长，
    因为命中与否只差"多等一次上游往返"；缓存键不含屏蔽标签指纹，屏蔽标签增删
    靠 `clear_search_cache()` 整体清空生效。
    """
    if page > max_pages:
        return [], False

    cache_key = f'tag|q={keyword}|p={page}|s={sort_order}|tm={tag_mode}|r={r18_mode}|mb={min_bookmarks}|mr={max_results}'
    # TTL 由**写入时**存进缓存条目（`_cache_put(ttl=)`），读取只认那一条：
    # 这里传 ttl 是为了让"这条路径该用哪个 TTL"在调用点一眼可见，与 search_by_user 同款。
    cached = _cache_get(cache_key, ttl=_TAG_SEARCH_CACHE_TTL)
    if cached is not None:
        # 缓存命中：本次未拉取详情，清零统计避免把上次搜索的耗时/失败归属到本次
        _last_fetch_stats.update({'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0})
        return cached

    # 检索词拼装（Pixiv 查询语法）+ 请求 + 信封判定都在适配层；
    # 这里只负责"拿条目 → 过滤/入库/缓存"
    pixiv_query = pixiv_client.build_search_query(keyword, tag_mode)

    session = build_pixiv_session()
    illusts_data, total = pixiv_client.fetch_search_illusts(
        session, pixiv_query,
        sort_order=sort_order, r18_mode=r18_mode, page=page)

    if not illusts_data:
        # 空结果**不**跟随 120 秒的标签 TTL，仍按 `_SEARCH_CACHE_TTL`（30 秒）缓存：
        # Cookie 过期时 Pixiv 会静默返回空页，而同条件重试命中同一个缓存键 ——
        # 把窗口拉到 120 秒等于让"换个关键词再搜还是空"多持续 90 秒，用户无法把
        # "真的没结果"和"凭据失效"分开。`search_by_user` 出于同一理由干脆不缓存空结果。
        _cache_put(cache_key, ([], False), ttl=_SEARCH_CACHE_TTL)
        return [], False

    defer = defer_details or (min_bookmarks == 0)
    with get_session() as db:
        blocked = _get_blocked_tags(db)
        results = _process_items(
            db, illusts_data,
            id_extractor=item_pixiv_id,
            illust_factory=_illust_from_item,
            blocked=blocked,
            min_bookmarks=min_bookmarks,
            defer_details=defer,
            max_results=max_results,
            limiter=limiter,
            progress=progress,
        )
        safe_commit(db)

    if results.rate_limited:
        # 限流是全局状态，不是"这些作品不匹配"：本页残缺，既不能写成功缓存
        #（下次命中还是这份残缺页），也不能让调用方以为筛选已经结束。
        raise SearchRateLimitedError(
            f'标签搜索「{keyword}」详情请求被限流，已确认的 {len(results)} 件结果不完整')

    total_pages = min((total + PER_PAGE - 1) // PER_PAGE, max_pages) if total else max_pages
    has_more = page < total_pages
    # 预算中途耗尽时本页还有条目没判定完（结果残缺、has_more 也随之失真），
    # 不能写成功缓存 —— 否则下次命中会拿到同一份残缺页，并把它固化满 120 秒。
    # 当前只有作者搜索会启用详情预算（routes_search 的 _user_fn），这条是防
    # "哪天给标签路径也开预算"时不静默退化的护栏。
    if budget_exhausted():
        return results, has_more
    _cache_put(cache_key, (results, has_more), ttl=_TAG_SEARCH_CACHE_TTL)
    return results, has_more


def browse_discovery(page: int = 1, sort_order: str = 'popular_d',
                     min_bookmarks: int = 0, r18_mode: str = 'all',
                     defer_details: bool = False,
                     max_results: int = 0,
                     limiter: _TokenBucket | None = None,
                     progress: Callable[[dict], None] | None = None) -> tuple[list[dict], bool]:
    """浏览 Pixiv 发现页（全部作品），无需指定标签。

    被限流截断时抛 `SearchRateLimitedError`，且不写成功缓存（与 `search_by_tag` 同）。
    """
    cache_key = f'disc|p={page}|s={sort_order}|r={r18_mode}|mb={min_bookmarks}|mr={max_results}'
    cached = _cache_get(cache_key)
    if cached is not None:
        _last_fetch_stats.update({'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0})
        return cached

    session = build_pixiv_session()
    # 发现页的条目在 `body.thumbnails.illust`（旧形态 `body.illusts`），按 type 过滤
    # 非插画条目、以及 `body.total` 的取法都是协议细节 → 适配层
    illusts_data, total = pixiv_client.fetch_discovery_artworks(
        session, sort_order=sort_order, r18_mode=r18_mode, page=page)

    if not illusts_data:
        _cache_put(cache_key, ([], False))
        return [], False

    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE) if total else 1
    has_more = page < total_pages

    defer = defer_details or (min_bookmarks == 0)
    with get_session() as db:
        blocked = _get_blocked_tags(db)
        results = _process_items(
            db, illusts_data,
            id_extractor=item_pixiv_id,
            illust_factory=_illust_from_item,
            blocked=blocked,
            min_bookmarks=min_bookmarks,
            defer_details=defer,
            max_results=max_results,
            limiter=limiter,
            progress=progress,
        )
        safe_commit(db)

    if results.rate_limited:
        # 与 search_by_tag 同理：残缺页不进缓存，交由调用方以可重试状态收尾
        raise SearchRateLimitedError(
            f'发现页 p={page} 详情请求被限流，已确认的 {len(results)} 件结果不完整')

    # 预算耗尽 = 本页条目没判定完，同 search_by_tag：不把残缺页固化进 30 秒缓存
    if budget_exhausted():
        return results, has_more
    _cache_put(cache_key, (results, has_more))
    return results, has_more


def search_by_user(user_id: str, min_bookmarks: int = 0, page: int = 1,
                   hide_r18: bool = False,
                   max_results: int = 0,
                   limiter: _TokenBucket | None = None,
                   progress: Callable[[dict], None] | None = None) -> tuple[list[dict], bool]:
    """按用户 ID 搜索。page 从 1 开始。返回 (results, has_more)。

    与 search_by_tag 的两处关键差异，改动前务必先读：

    1. **这是唯一强制同步拉详情的搜索路径**。`profile/all` 只给作品 id，
       而过滤条件（hide_r18 / min_bookmarks）要拿到详情的 tags 才能判定，
       所以本页每个未入库的 id 都要发一次详情请求。`defer_details` 在这里
       用不上 —— `_illust_from_detail` 必须有 detail 才能造出可展示的记录。
       单次搜索的详情总量由 paginated_search 的 detail_budget 兜底。
    2. **切片用 ITEMS_PER_PAGE（24）而不是 PER_PAGE（60）**。PER_PAGE 是
       标签搜索从 Pixiv 上游"白拿"的页大小（一次 HTTP 就回来 60 条，多拿
       不花额外请求）；这里每多切一条就多一次详情请求，60 是纯浪费。
       注意这会改变 cursor 里 pixiv_page 的步长，见 routes_search 的 ps 字段。
    """
    with get_session() as db:
        blocked = _get_blocked_tags(db)

    cache_key = (
        f'user|q={user_id}|p={page}|mb={min_bookmarks}|r={hide_r18}'
        f'|mr={max_results}|bt={_blocked_fingerprint(blocked)}'
    )
    cached = _cache_get(cache_key, ttl=_USER_SEARCH_CACHE_TTL)
    if cached is not None:
        # 缓存命中：本次未拉取详情，清零统计避免把上次搜索的耗时/失败归属到本次
        _last_fetch_stats.update({'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0})
        return cached

    session = build_pixiv_session()
    all_ids = _get_user_profile_ids(session, user_id)
    if not all_ids:
        # 拉不到（画师无作品 / Cookie 失效 / 网络故障）一律不缓存：
        # 后两者是暂时性的，缓存空结果会让用户在 TTL 内怎么刷新都是空的。
        return [], False

    total = len(all_ids)
    page_size = ITEMS_PER_PAGE
    start = (page - 1) * page_size
    end = min(start + page_size, total)
    page_ids = all_ids[start:end]

    if not page_ids:
        return [], False

    with get_session() as db:
        results = _process_items(
            db, page_ids,
            id_extractor=lambda x: x,
            illust_factory=_illust_from_detail,
            blocked=blocked,
            min_bookmarks=min_bookmarks,
            hide_r18=hide_r18,
            max_results=max_results,
            limiter=limiter,
            progress=progress,
        )
        safe_commit(db)

    if results.rate_limited:
        # 限流截断：本页还有 id 没判定。与预算耗尽同理不缓存，但更进一步 ——
        # 必须让调用方知道"这不是完整页"，所以抛出去（游标不许前移）。
        raise SearchRateLimitedError(
            f'画师 {user_id} 第 {page} 页详情请求被限流，'
            f'已确认的 {len(results)} 件结果不完整')

    max_pages = (total + page_size - 1) // page_size
    has_more = page < max_pages

    # 预算在这一页中途耗尽的，结果是残缺的（本页还有 id 没判定就收工），
    # 不缓存 —— 否则下次命中缓存会拿到同一份残缺结果，且 has_more 失真。
    if not budget_exhausted():
        _cache_put(cache_key, (results, has_more), ttl=_USER_SEARCH_CACHE_TTL)
    return results, has_more


# ── 用户 profile 缓存（避免大画师每次翻页重拉全量作品列表）──

_USER_PROFILE_CACHE: dict[str, tuple[float, list[int]]] = {}
_USER_PROFILE_TTL = 600.0  # 10 分钟
_USER_PROFILE_CACHE_MAX = 64
_user_profile_lock = threading.Lock()


def _get_user_profile_ids(session: requests.Session, user_id: str) -> list[int]:
    """`profile/all` 的**进程内缓存**包装（10 分钟）。

    为什么缓存留在业务层而不是适配层：它是"少发请求"的业务策略（大画师每次翻页
    都会重拉全量 id 列表），不是接口形态。请求本身在 `pixiv_client`。
    """
    with _user_profile_lock:
        hit = _USER_PROFILE_CACHE.get(user_id)
        if hit and time.time() - hit[0] < _USER_PROFILE_TTL:
            return hit[1]

    all_ids = pixiv_client.fetch_user_profile_ids(session, user_id)
    if not all_ids:
        return []

    with _user_profile_lock:
        _USER_PROFILE_CACHE[user_id] = (time.time(), all_ids)
        while len(_USER_PROFILE_CACHE) > _USER_PROFILE_CACHE_MAX:
            oldest = min(_USER_PROFILE_CACHE, key=lambda k: _USER_PROFILE_CACHE[k][0])
            del _USER_PROFILE_CACHE[oldest]
    return all_ids


def fetch_following(page: int = 1, r18_mode: str = 'all') -> tuple[list[dict], bool]:
    """获取关注画师的最新作品。"""
    cache_key = f'follow|p={page}|r={r18_mode}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    session = build_pixiv_session()
    illusts_data, has_next = pixiv_client.fetch_following_latest(
        session, r18_mode=r18_mode, page=page)

    if not illusts_data:
        _cache_put(cache_key, ([], False))
        return [], False

    with get_session() as db:
        blocked = _get_blocked_tags(db)
        results = _process_items(
            db, illusts_data,
            id_extractor=item_pixiv_id,
            illust_factory=_illust_from_item,
            blocked=blocked,
            defer_details=True,
            # Pixiv 的 follow_latest mode 参数并不总是过滤 R18（账号开启 R18 显示时
            # safe/all 可能返回相同结果），本地再按标签兜底过滤一层，与搜索一致
            hide_r18=(r18_mode == 'safe'),
        )
        safe_commit(db)

    # 关注页不需要"残缺页不进缓存"的守卫：它恒走 defer 路径且不传 min_bookmarks，
    # `_process_items` 里会消耗预算的 to_fetch / 会因限流截断的 to_refetch 两条分支
    # 都不会进（后者要求 min_bookmarks > 0），上游异常在到达这里之前就已抛出。
    _cache_put(cache_key, (results, has_next))
    return results, has_next


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
