from __future__ import annotations

import logging
import os
import random
import re
import time
import hashlib
import hmac
import json
from base64 import urlsafe_b64encode, urlsafe_b64decode
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import (
    COOKIE_PATH, PIXIV_BASE_URL, SEARCH_PAGES, PER_PAGE,
    DETAIL_TIMEOUT, DETAIL_MAX_RETRIES, FETCH_DETAIL_WORKERS,
    PROXY, SSL_VERIFY, CURSOR_SECRET, ITEMS_PER_PAGE,
)
from models import Illust, BlockedTag, get_session, safe_commit

logger = logging.getLogger(__name__)

_cookie_mtime = 0
_cookie_value = ''
_pixiv_hostname = urlparse(PIXIV_BASE_URL).hostname or 'www.pixiv.net'


class PixivAuthError(Exception):
    """认证失败：Cookie 过期或无效。"""


def _is_auth_error(msg: str) -> bool:
    for kw in ('認証', 'auth', 'login', 'ログイン', 'session', 'expired'):
        if kw.lower() in msg.lower():
            return True
    return False


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


def paginated_search(search_fn, query_params: dict, items_per_page: int,
                     cursor_data: dict | None = None) -> tuple:
    """游标驱动的分页搜索。

    Args:
        search_fn: 搜索函数，签名为 (page: int, remaining: int) -> tuple[list[dict], bool]。
            remaining 为本页还需收集的条数，供流式过滤跨页累计提前终止。
        query_params: {type, query, sort, tag_mode, r18_mode, min_bookmarks}
        items_per_page: 每页件数
        cursor_data: 解码后的游标，None 表示新搜索

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

    while len(collected) < items_per_page and pages_scanned < _MAX_SCAN_PAGES:
        try:
            remaining = items_per_page - len(collected)
            results, has_more = search_fn(page=pixiv_page, remaining=remaining)
        except PixivAuthError:
            raise
        except Exception as e:
            logger.error(f'paginated_search: page {pixiv_page} failed: {e}')
            break

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


def _load_cookie() -> None:
    global _cookie_mtime, _cookie_value
    if not os.path.exists(COOKIE_PATH):
        raise FileNotFoundError(f'Cookie file not found: {COOKIE_PATH}')
    mtime = os.path.getmtime(COOKIE_PATH)
    if mtime != _cookie_mtime:
        with open(COOKIE_PATH) as f:
            raw = f.read().strip()
        if raw.startswith('PHPSESSID='):
            _cookie_value = raw.split('=', 1)[1]
        else:
            _cookie_value = raw
        _cookie_mtime = mtime


def build_pixiv_session() -> requests.Session:
    """构造访问 Pixiv 的 requests.Session（UA/Referer/Cookie/PROXY/SSL_VERIFY/重试 齐全）。

    所有指向 Pixiv 的请求（搜索、详情、下载、缩略图代理）必须经由此工厂，
    禁止裸建 requests.Session()（2026-07-25 审查 P0-2）。
    """
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': f'{PIXIV_BASE_URL}/',
        'Accept-Language': 'ja,zh-CN;q=0.9,zh;q=0.8,en;q=0.7',
    })

    _load_cookie()
    s.headers.update({'Cookie': f'PHPSESSID={_cookie_value}'})
    s.cookies.set('PHPSESSID', _cookie_value, domain=_pixiv_hostname)

    s.verify = SSL_VERIFY

    if PROXY:
        s.proxies = {'https': PROXY, 'http': PROXY}

    adapter = HTTPAdapter()
    retry = Retry(total=1, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503])
    adapter.max_retries = retry
    s.mount('https://', adapter)
    return s


# 向后兼容别名
_build_session = build_pixiv_session


def _split_tags(keyword: str) -> list[str]:
    raw = keyword.replace('，', ',').strip()
    parts = [t.strip() for t in raw.split(',') if t.strip()]
    return parts if parts else [raw]


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


R18_TAGS = {"R-18", "R-18G"}


def _is_r18(tags: list[str]) -> bool:
    return bool(set(tags) & R18_TAGS)


def _parse_tags(tags_data: Any) -> list[str]:
    if not tags_data:
        return []
    if isinstance(tags_data, list):
        if len(tags_data) == 0:
            return []
        if isinstance(tags_data[0], str):
            return tags_data
        if isinstance(tags_data[0], dict):
            return [t.get('tag', '') for t in tags_data if t.get('tag')]
    if isinstance(tags_data, dict):
        inner = tags_data.get('tags', [])
        if isinstance(inner, list) and len(inner) > 0 and isinstance(inner[0], dict):
            return [t.get('tag', '') for t in inner if t.get('tag')]
    return []


def _extract_original_urls(detail_body: dict) -> list[str]:
    urls = []
    meta_pages = detail_body.get('metaPages')
    if meta_pages and len(meta_pages) > 0:
        for page in meta_pages:
            u = page.get('urls', {}).get('original', '')
            if u:
                urls.append(u)
        return urls
    meta_single = detail_body.get('metaSinglePage')
    if meta_single and meta_single.get('originalImageUrl'):
        urls.append(meta_single['originalImageUrl'])
        return urls
    original = detail_body.get('urls', {}).get('original', '')
    if not original:
        return urls
    page_count = detail_body.get('pageCount', 1)
    if page_count <= 1:
        urls.append(original)
        return urls
    for i in range(page_count):
        page_url = re.sub(r'_p0(\.[a-zA-Z]+)(\?|$)', f'_p{i}\\1\\2', original)
        urls.append(page_url)
    return urls


class _TokenBucket:
    """全局请求限速器：所有并发 worker 共享，从根上防止触发 Pixiv 403/429 限流。

    rate_per_minute: 每分钟允许的请求数。限速器保证任意时刻全局请求间隔
    不小于 60/rate 秒，多 worker 并发时整体速率仍被压住。
    """

    def __init__(self, rate_per_minute: float):
        self._lock = threading.Lock()
        self._interval = 60.0 / rate_per_minute
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            delay = self._last + self._interval - now
            if delay > 0:
                time.sleep(delay)
            self._last = time.time()


# Pixiv 详情 API 限流保守速率（并发 3 时实测仍会 403，必须全局限速）。
# 前台同步拉取（搜索过滤）独占高速桶；后台补全走独立低速桶，避免抢占搜索带宽。
# 两个桶之上再设总速率闸，防止双桶并发时峰值超限重新触发 403。
DETAIL_RATE_PER_MINUTE = 45
FILL_RATE_PER_MINUTE = 20
BULK_RATE_PER_MINUTE = 15
TOTAL_RATE_PER_MINUTE = 60
_detail_limiter = _TokenBucket(DETAIL_RATE_PER_MINUTE)
_fill_limiter = _TokenBucket(FILL_RATE_PER_MINUTE)
_bulk_limiter = _TokenBucket(BULK_RATE_PER_MINUTE)
_total_limiter = _TokenBucket(TOTAL_RATE_PER_MINUTE)

# 最近一次搜索的详情拉取统计（供前端展示"为什么慢"）
_last_fetch_stats: dict = {'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0}


def get_last_fetch_stats() -> dict:
    return dict(_last_fetch_stats)


def _get_illust_detail(session: requests.Session, pixiv_id: int,
                       limiter: _TokenBucket | None = None) -> dict | None:
    url = f'{PIXIV_BASE_URL}/ajax/illust/{pixiv_id}'
    (limiter or _detail_limiter).wait()
    _total_limiter.wait()
    for attempt in range(DETAIL_MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=DETAIL_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get('error'):
                logger.warning(f'Detail API error for {pixiv_id}: {data.get("message")}')
                return None
            body = data['body']
            urls = body.get('urls', {})
            return {
                'title': body.get('illustTitle', ''),
                'user_id': int(body.get('userId', 0)),
                'user_name': body.get('userName', ''),
                'page_count': body.get('pageCount', 1),
                'bookmark_count': body.get('bookmarkCount', 0),
                'thumb_url': urls.get('thumb', urls.get('small', '')),
                'upload_date': body.get('uploadDate', body.get('createDate', '')),
                'original_urls': _extract_original_urls(body),
                'tags': _parse_tags(body.get('tags')),
            }
        except requests.RequestException as e:
            logger.warning(f'Detail API attempt {attempt + 1} failed for {pixiv_id}: {e}')
            if attempt < DETAIL_MAX_RETRIES:
                status = getattr(getattr(e, 'response', None), 'status_code', None)
                # 429/403 均为 Pixiv 限流（并发过高时返回 403），递增退避（3s/9s）
                time.sleep((3 * (3 ** attempt)) if status in (403, 429) else 1)
    return None


def _fetch_details_parallel(pixiv_ids: list[int],
                            early_stop: Callable[[dict | None], bool] | None = None,
                            limiter: _TokenBucket | None = None) -> tuple[dict[int, dict], int]:
    """并行拉取详情，支持提前终止。

    early_stop: 每完成一个详情后调用（参数为该详情或 None），返回 True 时
    取消未启动的拉取。**已启动的请求会全部处理完再返回**（不丢弃其结果，
    避免作品未入库导致分页漂移后跨页重复）；调用方需等待最慢的在途请求，
    代价受 DETAIL_TIMEOUT/退避上限约束。
    limiter: 请求限速器；不传时用前台高速桶（搜索），后台补全应传 _fill_limiter。

    Returns: (成功详情 dict, 实际发起的请求数)。early_stop 取消的未启动请求
    不计入 attempted，避免统计把"未尝试"误报为"失败"。
    """
    if not pixiv_ids:
        return {}, 0
    results = {}
    attempted = 0

    def _worker(pid: int) -> tuple[int, dict | None]:
        session = _build_session()
        return pid, _get_illust_detail(session, pid, limiter)

    executor = ThreadPoolExecutor(max_workers=FETCH_DETAIL_WORKERS)
    try:
        futures = {executor.submit(_worker, pid): pid for pid in pixiv_ids}
        for future in as_completed(futures):
            try:
                pid, detail = future.result()
            except CancelledError:
                continue  # 被 early_stop 取消的未发起请求：不计入 attempted/失败
            except Exception as e:
                logger.error(f'Parallel fetch failed for {futures[future]}: {e}')
                detail = None
            attempted += 1
            if detail is not None:
                results[pid] = detail
            if early_stop is not None and early_stop(detail):
                # 触发早停：只取消未启动的请求；已启动的照常处理完再返回。
                # 若丢弃已启动的结果，这些作品不会写入 DB，Pixiv 分页漂移后
                # 会再次出现并被当作新作品 → 跨页重复（2026-08 bug 修复）。
                for f in futures:
                    f.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return results, attempted


# ── 后台详情补全 ──

_fill_lock = threading.Lock()
_filling_ids: set[int] = set()
_fill_last_attempt: dict[int, float] = {}
_FILL_ATTEMPT_INTERVAL = 300.0  # 同一作品两次补全尝试的最小间隔（秒），防 429 限流


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
        _filling_ids.update(new_ids)
    try:
        details, _ = _fetch_details_parallel(new_ids, limiter=_fill_limiter)
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

_SEARCH_CACHE: 'OrderedDict[str, tuple[float, tuple[list[dict], bool]]]' = OrderedDict()
_SEARCH_CACHE_TTL = 30.0
_SEARCH_CACHE_MAX = 64
_search_cache_lock = threading.Lock()


def _cache_get(key: str) -> tuple[list[dict], bool] | None:
    now = time.time()
    with _search_cache_lock:
        v = _SEARCH_CACHE.get(key)
        if v is None:
            return None
        ts, value = v
        if now - ts > _SEARCH_CACHE_TTL:
            _SEARCH_CACHE.pop(key, None)
            return None
        _SEARCH_CACHE.move_to_end(key)
        return value


def _cache_put(key: str, value: tuple[list[dict], bool]) -> None:
    with _search_cache_lock:
        _SEARCH_CACHE[key] = (time.time(), value)
        _SEARCH_CACHE.move_to_end(key)
        while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
            _SEARCH_CACHE.popitem(last=False)


def clear_search_cache() -> None:
    with _search_cache_lock:
        _SEARCH_CACHE.clear()


# ── 公共流水线 ──

def _process_items(db: Any, items: list[Any], id_extractor: Callable[[Any], int], illust_factory: Callable[[Any, dict], Illust], blocked: set[str], *,
                   min_bookmarks: int = 0, hide_r18: bool = False, defer_details: bool = False,
                   max_results: int = 0, limiter: _TokenBucket | None = None) -> list[dict]:
    """去重 → 过滤 → 并行拉取详情 → 存储。

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
        limiter: 详情请求限速器；不传用前台高速桶（搜索）。批量下载应传
            _bulk_limiter，避免大任务抢占交互搜索带宽。

    Returns: 可直接用于 API 响应的 illust 字典列表
    """
    results: list[dict] = []
    if not items:
        return results

    fetch_stats = {'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0}
    fetch_start = time.time()

    pixiv_ids = [id_extractor(item) for item in items]
    existing_list = db.query(Illust).filter(Illust.pixiv_id.in_(pixiv_ids)).all()
    existing_map = {i.pixiv_id: i for i in existing_list}

    to_fetch: list[int] = []           # 同步拉详情（非 defer 路径）
    to_fill: list[int] = []            # 后台补全（defer 路径新写入 + 已有但缺原图/收藏数过期）
    to_refetch: list[int] = []         # 已有记录但 bookmark_count=0 或收藏数过期，需同步补全后重新判断过滤
    new_illusts: list[Illust] = []     # defer 路径批量写入

    now_utc = datetime.now(timezone.utc)

    for item in items:
        pixiv_id = id_extractor(item)
        existing = existing_map.get(pixiv_id)
        if existing:
            stale = _is_bookmark_stale(existing)
            # bookmark_count 未补全（0）或收藏数过期：优先用列表接口自带的 bookmarkCount 修正
            if existing.bookmark_count == 0 or stale:
                # defer 路径：API 返回数据自带 bookmarkCount，直接更新跳过补全
                if defer_details and isinstance(item, dict) and item.get('bookmarkCount', 0) > 0:
                    existing.bookmark_count = item['bookmarkCount']
                    existing.bookmark_updated_at = now_utc
                elif min_bookmarks > 0:
                    # 用户设了最低收藏但条目无收藏数 → 同步重新拉详情判断
                    to_refetch.append(pixiv_id)
                    continue
            if not _is_blocked(existing.tags_list, blocked) \
               and existing.bookmark_count >= min_bookmarks \
               and not (hide_r18 and _is_r18(existing.tags_list)):
                results.append(existing.to_dict())
                # 缺原图或收藏数过期 → 后台补全刷新（非 defer 且设了最低收藏的批量
                # 路径除外，避免干扰其同步过滤语义；该路径失败记录另行兜底）
                if (not existing.original_urls_list or stale) \
                   and (defer_details or min_bookmarks == 0):
                    to_fill.append(pixiv_id)
            continue

        if defer_details:
            item_tags = _parse_tags(item.get('tags', [])) if isinstance(item, dict) else []
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
        details, attempted = _fetch_details_parallel(to_refetch, limiter=limiter)
        fetch_stats['detail_fetched'] += len(details)
        fetch_stats['detail_failed'] += attempted - len(details)
        for pixiv_id in to_refetch:
            detail = details.get(pixiv_id)
            if detail is None:
                # 详情拉取失败：不静默丢弃，排入后台补全，下次命中时再判断
                to_fill.append(pixiv_id)
                continue
            if _is_blocked(detail.get('tags', []), blocked) \
               or detail.get('bookmark_count', 0) < min_bookmarks \
               or (hide_r18 and _is_r18(detail.get('tags', []))):
                continue
            existing = existing_map[pixiv_id]
            existing.bookmark_count = detail.get('bookmark_count', existing.bookmark_count)
            existing.bookmark_updated_at = now_utc
            if detail.get('original_urls'):
                existing.original_urls_list = detail['original_urls']
            results.append(existing.to_dict())

    if new_illusts:
        db.add_all(new_illusts)
        db.flush()
        for illust in new_illusts:
            results.append(illust.to_dict())

    if to_fill:
        _kick_background_fill(to_fill)
    if defer_details:
        if max_results > 0:
            _last_fetch_stats.update(fetch_stats)
        return results

    if to_fetch:
        # 流式过滤：拉取过程中直接判定过滤条件，凑够 max_results 条即提前终止
        passed = [0]

        def _early_stop(detail: dict | None) -> bool:
            if max_results <= 0 or detail is None:
                return False
            if _is_blocked(detail.get('tags', []), blocked) \
               or detail.get('bookmark_count', 0) < min_bookmarks \
               or (hide_r18 and _is_r18(detail.get('tags', []))):
                return False
            passed[0] += 1
            return passed[0] >= max_results

        details, attempted = _fetch_details_parallel(
            to_fetch, early_stop=_early_stop if max_results > 0 else None,
            limiter=limiter)
        fetch_stats['detail_fetched'] += len(details)
        fetch_stats['detail_failed'] += attempted - len(details)
        for pixiv_id in to_fetch:
            detail = details.get(pixiv_id)
            if detail is None:
                continue
            if _is_blocked(detail.get('tags', []), blocked) \
               or detail.get('bookmark_count', 0) < min_bookmarks \
               or (hide_r18 and _is_r18(detail.get('tags', []))):
                continue

            item = next((i for i in items if id_extractor(i) == pixiv_id), None)
            if item is None:
                continue

            illust = illust_factory(item, detail)
            illust.bookmark_updated_at = now_utc  # 详情同步拉取成功，收藏数为当前值
            db.add(illust)
            db.flush()
            results.append(illust.to_dict())

    if max_results > 0:
        fetch_stats['seconds'] = time.time() - fetch_start
        _last_fetch_stats.update(fetch_stats)

    return results


def _illust_from_item(item: dict, detail: dict | None = None) -> Illust:
    """从搜索/发现/关注 API 条目创建 Illust。

    大多数字段来自搜索结果条目（列表上下文）。
    detail 为 None 时表示详情尚未拉取，bookmark_count/original_urls 留空，
    由后台补全任务稍后填入。
    """
    illust = Illust(
        pixiv_id=int(item['id']),
        title=item.get('title', ''),
        user_id=int(item.get('userId', 0)),
        user_name=item.get('userName', ''),
        page_count=item.get('pageCount', 1),
        # 列表接口不返回 bookmarkCount（实测字段恒缺失），defer 写入时只能为 0，
        # 真实收藏数由后台补全任务写入
        bookmark_count=detail.get('bookmark_count', 0) if detail else 0,
        thumb_url=item.get('url', ''),
        upload_date=_parse_date(item.get('updateDate')),
    )
    illust.tags_list = _parse_tags(item.get('tags', []))
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
                  sort_order: str = 'popular_d', max_pages: int = 10,
                  tag_mode: str = 'or', r18_mode: str = 'all',
                  defer_details: bool = False,
                  max_results: int = 0,
                  limiter: _TokenBucket | None = None) -> tuple[list[dict], bool]:
    """按标签搜索 Pixiv。tag_mode: 'or' = 任一标签, 'and' = 全部标签。

    max_results: 流式过滤目标数量，凑够即提前停止拉取详情（0 = 不限制）。
    limiter: 详情请求限速器；批量下载传 _bulk_limiter 隔离带宽。
    """
    if page > max_pages:
        return [], False

    cache_key = f'tag|q={keyword}|p={page}|s={sort_order}|tm={tag_mode}|r={r18_mode}|mb={min_bookmarks}|mr={max_results}'
    cached = _cache_get(cache_key)
    if cached is not None:
        # 缓存命中：本次未拉取详情，清零统计避免把上次搜索的耗时/失败归属到本次
        _last_fetch_stats.update({'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0})
        return cached

    tags = _split_tags(keyword)
    if len(tags) == 1:
        pixiv_query = tags[0]
    elif tag_mode == 'and':
        pixiv_query = ' '.join(tags)
    else:
        pixiv_query = '(' + ' OR '.join(tags) + ')'

    session = _build_session()
    quoted = requests.utils.quote(pixiv_query)
    search_url = (
        f'{PIXIV_BASE_URL}/ajax/search/illustrations/{quoted}'
        f'?word={quoted}&order={sort_order}&mode={r18_mode}&p={page}'
        f'&s_mode=s_tag&type=illust'
    )

    try:
        resp = session.get(search_url, timeout=DETAIL_TIMEOUT)
        resp.raise_for_status()
        search_data = resp.json()
    except requests.RequestException as e:
        logger.error(f'Search API failed: {e}')
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        if status in (401, 403):
            raise PixivAuthError(f'Pixiv API returned HTTP {status}')
        return [], False

    if search_data.get('error'):
        msg = str(search_data.get('message', ''))
        logger.error(f'Search API error: {msg}')
        if _is_auth_error(msg):
            raise PixivAuthError(msg)
        return [], False

    illusts_data = (
        search_data.get('body', {})
        .get('illust', {})
        .get('data', [])
    )
    total = search_data.get('body', {}).get('illust', {}).get('total', 0)

    if not illusts_data:
        _cache_put(cache_key, ([], False))
        return [], False

    defer = defer_details or (min_bookmarks == 0)
    with get_session() as db:
        blocked = _get_blocked_tags(db)
        results = _process_items(
            db, illusts_data,
            id_extractor=lambda item: int(item['id']),
            illust_factory=_illust_from_item,
            blocked=blocked,
            min_bookmarks=min_bookmarks,
            defer_details=defer,
            max_results=max_results,
            limiter=limiter,
        )
        safe_commit(db)

    total_pages = min((total + PER_PAGE - 1) // PER_PAGE, max_pages) if total else max_pages
    has_more = page < total_pages
    _cache_put(cache_key, (results, has_more))
    return results, has_more


def browse_discovery(page: int = 1, sort_order: str = 'popular_d',
                     min_bookmarks: int = 0, r18_mode: str = 'all',
                     defer_details: bool = False,
                     max_results: int = 0,
                     limiter: _TokenBucket | None = None) -> tuple[list[dict], bool]:
    """浏览 Pixiv 发现页（全部作品），无需指定标签。"""
    cache_key = f'disc|p={page}|s={sort_order}|r={r18_mode}|mb={min_bookmarks}|mr={max_results}'
    cached = _cache_get(cache_key)
    if cached is not None:
        _last_fetch_stats.update({'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0})
        return cached

    session = _build_session()
    url = (
        f'{PIXIV_BASE_URL}/ajax/discovery/artworks'
        f'?mode={r18_mode}&p={page}&limit=60&order={sort_order}'
    )

    try:
        resp = session.get(url, timeout=DETAIL_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f'Discovery API failed: {e}')
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        if status in (401, 403):
            raise PixivAuthError(f'Pixiv API returned HTTP {status}')
        return [], False

    if data.get('error'):
        msg = str(data.get('message', ''))
        logger.error(f'Discovery API error: {msg}')
        if _is_auth_error(msg):
            raise PixivAuthError(msg)
        return [], False

    body = data.get('body', {})
    thumbnails = body.get('thumbnails', {}).get('illust', body.get('illusts', []))
    illusts_data = [t for t in thumbnails if not t.get('type') or t.get('type') == 'illust']
    if not illusts_data:
        _cache_put(cache_key, ([], False))
        return [], False

    total = body.get('total', 0)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE) if total else 1
    has_more = page < total_pages

    defer = defer_details or (min_bookmarks == 0)
    with get_session() as db:
        blocked = _get_blocked_tags(db)
        results = _process_items(
            db, illusts_data,
            id_extractor=lambda item: int(item['id']),
            illust_factory=_illust_from_item,
            blocked=blocked,
            min_bookmarks=min_bookmarks,
            defer_details=defer,
            max_results=max_results,
            limiter=limiter,
        )
        safe_commit(db)

    _cache_put(cache_key, (results, has_more))
    return results, has_more


def search_by_user(user_id: str, min_bookmarks: int = 0, page: int = 1,
                   hide_r18: bool = False,
                   max_results: int = 0,
                   limiter: _TokenBucket | None = None) -> tuple[list[dict], bool]:
    """按用户 ID 搜索。page 从 1 开始。返回 (results, has_more)。"""
    session = _build_session()
    all_ids = _get_user_profile_ids(session, user_id)
    if not all_ids:
        return [], False

    total = len(all_ids)
    start = (page - 1) * PER_PAGE
    end = min(start + PER_PAGE, total)
    page_ids = all_ids[start:end]

    if not page_ids:
        return [], False

    with get_session() as db:
        blocked = _get_blocked_tags(db)
        results = _process_items(
            db, page_ids,
            id_extractor=lambda x: x,
            illust_factory=_illust_from_detail,
            blocked=blocked,
            min_bookmarks=min_bookmarks,
            hide_r18=hide_r18,
            max_results=max_results,
            limiter=limiter,
        )
        safe_commit(db)

    max_pages = (total + PER_PAGE - 1) // PER_PAGE
    has_more = page < max_pages
    return results, has_more


# ── 用户 profile 缓存（避免大画师每次翻页重拉全量作品列表）──

_USER_PROFILE_CACHE: dict[str, tuple[float, list[int]]] = {}
_USER_PROFILE_TTL = 600.0  # 10 分钟
_USER_PROFILE_CACHE_MAX = 64
_user_profile_lock = threading.Lock()


def _get_user_profile_ids(session: requests.Session, user_id: str) -> list[int]:
    with _user_profile_lock:
        hit = _USER_PROFILE_CACHE.get(user_id)
        if hit and time.time() - hit[0] < _USER_PROFILE_TTL:
            return hit[1]

    profile_url = f'{PIXIV_BASE_URL}/ajax/user/{user_id}/profile/all'
    try:
        resp = session.get(profile_url, timeout=DETAIL_TIMEOUT)
        resp.raise_for_status()
        profile_data = resp.json()
    except requests.RequestException as e:
        logger.error(f'User profile API failed: {e}')
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        if status in (401, 403):
            raise PixivAuthError(f'Pixiv API returned HTTP {status}')
        return []

    if profile_data.get('error'):
        msg = str(profile_data.get('message', ''))
        logger.error(f'User profile API error: {msg}')
        if _is_auth_error(msg):
            raise PixivAuthError(msg)
        return []

    all_illusts = profile_data.get('body', {}).get('illusts', {})
    all_ids = sorted([int(iid) for iid in all_illusts.keys()], reverse=True)
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

    session = _build_session()
    url = f'{PIXIV_BASE_URL}/ajax/follow_latest/illust?mode={r18_mode}&p={page}'
    try:
        resp = session.get(url, timeout=DETAIL_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f'Follow latest API failed: {e}')
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        if status in (401, 403):
            raise PixivAuthError(f'Pixiv API returned HTTP {status}')
        return [], False

    if data.get('error'):
        msg = str(data.get('message', ''))
        logger.error(f'Follow latest API error: {msg}')
        if _is_auth_error(msg):
            raise PixivAuthError(msg)
        return [], False

    body = data.get('body', {})
    illusts_data = body.get('thumbnails', {}).get('illust', [])
    if not illusts_data:
        _cache_put(cache_key, ([], False))
        return [], False

    has_next = not body.get('page', {}).get('isLastPage', True)

    with get_session() as db:
        blocked = _get_blocked_tags(db)
        results = _process_items(
            db, illusts_data,
            id_extractor=lambda item: int(item['id']),
            illust_factory=_illust_from_item,
            blocked=blocked,
            defer_details=True,
        )
        safe_commit(db)

    _cache_put(cache_key, (results, has_next))
    return results, has_next


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
