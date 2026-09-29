"""Pixiv 适配层（Client/Adapter）：Ajax 端点、认证、限流、响应解析的唯一归属地。

为什么单独一层：本项目用的是 Pixiv **非官方内部 Ajax API**，路径、查询参数名、响应
信封（`error`/`message`/`body`）和 payload 字段名全由对方单方面决定，随时可能变。
这些细节此前散落在 `fetcher.py` 的业务流水线（过滤/缓存/分页/入库）之间，改一个字段
就要在业务代码里逐处找，也没有任何"接口形态"的回归网。

分层契约（改动前必读）：
- **本模块**只负责"怎么跟 Pixiv 说话"：Cookie(`PHPSESSID`)、Session 与线程内连接池、
  令牌桶限流、5 个 Ajax 端点的 URL 拼装、响应信封判定、payload → 规范字段的解析。
  它**不碰数据库**、不做业务过滤、不认识 `Illust` 模型。
- **`fetcher.py`** 是业务层：屏蔽/R18/收藏数过滤、搜索缓存、游标分页、取消与详情预算、
  入库。它建 session 后调用本模块，并把本模块符号再导出给历史调用方与测试补丁 seam。
- **`routes_*` / `background` / `helpers`** 不拼 Pixiv URL、不认识响应字段。
- Session 一律由调用方创建后传入（`fetch_*(session, …)`），本模块不隐式取全局 session。

刻意保留、不要"顺手"重写的机制：
- 认证仍是 Cookie（`PHPSESSID`）+ 可被 `config`/`PIXIV_INSTANCE_DIR` 覆盖的
  `COOKIE_PATH`；不迁移 PixivPy、不引入浏览器自动化。
- 原图地址仍来自详情接口的 `metaPages/metaSinglePage/urls`，由 `extract_original_urls`
  解析。**地址安全校验不在这层**：`helpers.check_image_url` + `config.IMAGE_HOST_ALLOWLIST`
  + 凭据分级仍在下游（那是"要不要发这个请求"的策略，不是"怎么跟 Pixiv 说话"）。
- 凭据分级不变：`build_pixiv_session()` 带 PHPSESSID；白名单外主机用
  `build_credentialless_session()`（显式摘掉**会话级** Cookie 头，否则等于交出凭据）。
- 热点路径必须复用 `get_pooled_session()`，不要在循环里 `build_pixiv_session()`。
- 日志 logger 名是 `pixiv_client`（不再是 `fetcher`）：按 logger 名过滤日志的用例/运维
  规则要跟着改。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import (
    COOKIE_PATH, PIXIV_BASE_URL, DETAIL_TIMEOUT, DETAIL_MAX_RETRIES,
    PROXY, SSL_VERIFY,
)

logger = logging.getLogger(__name__)


# ── 错误分类 ──

class PixivAuthError(Exception):
    """认证失败：Cookie 过期或无效。"""


def is_auth_error(msg: str) -> bool:
    for kw in ('認証', 'auth', 'login', 'ログイン', 'session', 'expired'):
        if kw.lower() in msg.lower():
            return True
    return False


# 历史私有名：`fetcher._is_auth_error` 曾是对外可见的符号，再导出时保持同一函数
_is_auth_error = is_auth_error


def _warn_403(api: str) -> None:
    """记一条 403 的分类告警（审计 S13）。

    Pixiv 对**并发/频率过高**也回 403（详情并发 3 实测即触发），所以 403 不能当作
    "Cookie 失效"上报：
      - 预取路径收到 `PixivAuthError` 会中止整轮（含容量清理），而换个节奏重试
        其实就能成功；
      - 前端会提示用户重新登录，而重登修不好限流。
    这里按既有失败形态返回空结果，并留下这条可检索的告警 —— 同一个 403 是"限流"
    还是"真被风控"只能靠频率与时机判断。
    """
    logger.warning(f'{api} API 返回 HTTP 403，疑似限流/风控（非认证失效），按失败返回空结果')


# ── 端点（Ajax 路径字符串的唯一来源）──
#
# 改接口路径/参数名时只改这一段 + 对应的 fetch_*；契约测试
# （tests/test_pixiv_contract.py）以这些函数为契约。

_PATH_ILLUST_DETAIL = '/ajax/illust/{pixiv_id}'
_PATH_SEARCH_ILLUSTRATIONS = '/ajax/search/illustrations/{word}'
_PATH_DISCOVERY_ARTWORKS = '/ajax/discovery/artworks'
_PATH_USER_PROFILE_ALL = '/ajax/user/{user_id}/profile/all'
_PATH_FOLLOW_LATEST_ILLUST = '/ajax/follow_latest/illust'

# 列表接口的排序/分级参数取值由业务层给定（date_d / popular_d、all / safe / r18）
_S_MODE_TAG = 's_tag'


def endpoint_illust_detail(pixiv_id: int) -> str:
    """作品详情：`/ajax/illust/<pid>`。"""
    return PIXIV_BASE_URL + _PATH_ILLUST_DETAIL.format(pixiv_id=pixiv_id)


def endpoint_search_illustrations(word: str, *, order: str, mode: str, page: int) -> str:
    """标签/关键词搜索。

    `word` 会被 URL-encode 两次：一次作为路径段（Pixiv 要求），一次作为 `word=`
    查询参数。两者必须同源 —— 只 encode 一处会让 Pixiv 拿到不同的检索词。
    """
    quoted = requests.utils.quote(word)
    return (
        PIXIV_BASE_URL + _PATH_SEARCH_ILLUSTRATIONS.format(word=quoted)
        + f'?word={quoted}&order={order}&mode={mode}&p={page}'
        + f'&s_mode={_S_MODE_TAG}&type=illust'
    )


def endpoint_discovery_artworks(*, mode: str, page: int, limit: int, order: str) -> str:
    """发现页（无标签浏览）：`/ajax/discovery/artworks`。"""
    return (
        PIXIV_BASE_URL + _PATH_DISCOVERY_ARTWORKS
        + f'?mode={mode}&p={page}&limit={limit}&order={order}'
    )


def endpoint_user_profile_all(user_id: str) -> str:
    """画师全部作品 id（只有 id，没有标签/收藏数）：`/ajax/user/<uid>/profile/all`。"""
    return PIXIV_BASE_URL + _PATH_USER_PROFILE_ALL.format(user_id=user_id)


def endpoint_follow_latest_illust(*, mode: str, page: int) -> str:
    """关注画师的最新作品：`/ajax/follow_latest/illust`。"""
    return PIXIV_BASE_URL + _PATH_FOLLOW_LATEST_ILLUST + f'?mode={mode}&p={page}'


# ── 检索词拼装（Pixiv 查询语法）──

def split_tags(keyword: str) -> list[str]:
    """用户关键词 → 标签列表。

    中文逗号（，）与英文逗号必须等价：用户分不清该打哪个（输入法状态不同打出来
    就是不同字符）。全是分隔符时回退成**归一化后**的原始串（而不是空列表 ——
    空列表会让上游拿不到任何关键词）。
    """
    raw = keyword.replace('，', ',').strip()
    parts = [t.strip() for t in raw.split(',') if t.strip()]
    return parts if parts else [raw]


def build_search_query(keyword: str, tag_mode: str) -> str:
    """关键词 + 标签逻辑 → Pixiv 的检索词。

    `tag_mode='and'` 用空格（Pixiv 语义即"全部包含"），`or` 需要显式 `(a OR b)`。
    这是 Pixiv 的查询语法，不是用户输入的语义 —— 故不放在业务层。
    """
    tags = split_tags(keyword)
    if len(tags) == 1:
        return tags[0]
    if tag_mode == 'and':
        return ' '.join(tags)
    return '(' + ' OR '.join(tags) + ')'


# ── 响应信封与请求异常分类 ──

def envelope_error(data: Any, *, log_prefix: str = '') -> str | None:
    """响应信封判定：`error:true` 时返回 message，认证类直接抛 `PixivAuthError`。

    为什么只做分类不做兜底返回：5 个端点的信封形状一致（`{error, message, body}`），
    但"出错之后怎么办"各不相同 —— 列表/资料端点返回空结果，详情端点还要判死（404 /
    删除类报文）与采样。兜底放在各 `fetch_*` 里，这里只回答"是不是错误、是不是认证错误"。

    `log_prefix` 非空时按旧格式记一条 error（保持现有日志措辞与级别不变）。
    """
    if not isinstance(data, dict) or not data.get('error'):
        return None
    msg = str(data.get('message', ''))
    if log_prefix:
        logger.error(f'{log_prefix}: {msg}')
    if is_auth_error(msg):
        raise PixivAuthError(msg)
    return msg


def handle_list_request_error(api: str, exc: requests.RequestException) -> None:
    """列表/资料端点的请求异常分类：401 上报认证失效，403 记限流告警，其余静默。

    403 绝不能当认证失效上报：预取路径收到 `PixivAuthError` 会中止整轮并让前端提示
    重新登录，而重登修不好限流（当时只是请求节奏太密）。调用方据"是否抛出"决定返回
    空结果还是冒泡。
    """
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if status == 401:
        raise PixivAuthError(f'Pixiv API returned HTTP {status}')
    if status == 403:
        _warn_403(api)


# ── payload 解析：Pixiv 字段名的唯一来源 ──

# R18 标签的本地判定词表：`follow_latest` 的 mode 参数并不可靠（账号开启 R18 显示时
# safe/all 可能返回相同结果），下游要按标签兜底过滤一层。
R18_TAGS = {"R-18", "R-18G"}


def parse_tags(tags_data: Any) -> list[str]:
    """Pixiv 的 tags 有多种历史形态（字符串列表 / `[{tag: …}]` / `{tags: [...]}`）。"""
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


def extract_original_urls(detail_body: dict) -> list[str]:
    """详情 body → 原图地址列表。

    三条优先级路径（Pixiv 多图/单图作品的字段形态不同）：
      1. `metaPages[].urls.original` —— 多图作品；
      2. `metaSinglePage.originalImageUrl` —— 单图作品；
      3. `urls.original` + `pageCount` 按 `_p0` → `_pN` 推导 —— 老形态兜底。

    只做"取地址"，不做合法性判定（那是 `helpers.check_image_url` 的事）。
    """
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


def item_pixiv_id(item: Any) -> int:
    """列表条目的作品 id。

    字段缺失即协议不符，照常抛 `KeyError` —— 不静默兜底成 0（那会把一批作品
    悄悄写成同一个 id）。
    """
    return int(item['id'])


def item_bookmark_count(item: Any) -> int:
    """列表条目自带的收藏数；多数列表接口不带该字段（缺失即 0）。"""
    try:
        return int(item.get('bookmarkCount') or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def parse_illust_summary(item: dict) -> dict:
    """列表条目（搜索/发现/关注）→ 规范字段。

    列表条目在各层间**保持 Pixiv 原始 dict 形态**传递（`_process_items` 的 defer 路径
    要拿 tags 判定屏蔽/R18），但字段访问必须经本函数或 `item_*`：
    Pixiv 改字段名时只需要改这里。
    """
    return {
        'pixiv_id': item_pixiv_id(item),
        'title': item.get('title', ''),
        'user_id': int(item.get('userId', 0)),
        'user_name': item.get('userName', ''),
        'page_count': item.get('pageCount', 1),
        'thumb_url': item.get('url', ''),
        'upload_date': item.get('updateDate'),
        'tags': parse_tags(item.get('tags', [])),
    }


def parse_illust_detail(body: dict) -> dict:
    """详情 `body` → 规范字段（与历史实现逐键一致，消费方无需改动）。"""
    urls = body.get('urls', {})
    return {
        'title': body.get('illustTitle', ''),
        'user_id': int(body.get('userId', 0)),
        'user_name': body.get('userName', ''),
        'page_count': body.get('pageCount', 1),
        'bookmark_count': body.get('bookmarkCount', 0),
        'thumb_url': urls.get('thumb', urls.get('small', '')),
        'upload_date': body.get('uploadDate', body.get('createDate', '')),
        'original_urls': extract_original_urls(body),
        'tags': parse_tags(body.get('tags')),
    }


def parse_discovery_items(body: dict) -> list[dict]:
    """发现页 body → 作品条目列表。

    `thumbnails.illust` 是新形态，`illusts` 是旧形态；两者都还要按 `type` 过滤掉
    非插画（漫画/小说）条目。
    """
    thumbnails = body.get('thumbnails', {}).get('illust', body.get('illusts', []))
    return [t for t in thumbnails if not t.get('type') or t.get('type') == 'illust']


def parse_follow_latest(body: dict) -> tuple[list[dict], bool]:
    """关注最新 body → (条目列表, 是否还有下一页)。"""
    items = body.get('thumbnails', {}).get('illust', [])
    has_next = not body.get('page', {}).get('isLastPage', True)
    return items, has_next


# ── 认证（Cookie / PHPSESSID）与 Session ──

_cookie_mtime = 0
_cookie_value = ''
_pixiv_hostname = urlparse(PIXIV_BASE_URL).hostname or 'www.pixiv.net'


def _load_cookie() -> None:
    """按 mtime 惰性读 Cookie 文件（多线程读同一路径，见 `set_cookie_cache`）。"""
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


def set_cookie_cache(value: str, mtime: float) -> None:
    """外部（设置页）改写了 Cookie 文件后，同步本进程的 Cookie 缓存。

    为什么必须显式同步：`_load_cookie` 只在 **mtime 变化**时回读磁盘，而
    `get_pooled_session` 用 mtime 当连接池失效戳 —— 不同步的话，在途连接池会继续
    用旧 Cookie 打 Pixiv（症状："设置页保存成功但搜索仍 401/空，重启才恢复"）。
    `mtime` 由调用方从**同一个 `COOKIE_PATH`** 取（`os.path.getmtime`），两处必须
    是同一把尺子。
    """
    global _cookie_mtime, _cookie_value
    _cookie_value = value
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
    # connect=0：连接建立失败（超时/拒绝/DNS/代理）不在 urllib3 层重试。连接类
    # 错误几乎必然重复失败，重试只会线性放大等待——应用层另有 DETAIL_MAX_RETRIES
    # 次重试，两层叠加会把 10s 连接超时放大成 62s。429/5xx 与读取错误仍重试一次。
    retry = Retry(total=1, connect=0, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503])
    adapter.max_retries = retry
    s.mount('https://', adapter)
    return s


def build_credentialless_session() -> requests.Session:
    """构造**不带 Pixiv 凭据**的 session（白名单外的图片主机用）。

    `build_pixiv_session()` 挂的是**会话级** `Cookie` 头（`s.headers.update`），
    requests 会把它发给任意主机 —— 紧随其后的 `s.cookies.set(..., domain=...)`
    才是主机作用域的。所以访问非 Pixiv 域名时必须显式摘掉这个头，否则等于把
    PHPSESSID 交给第三方：对方拿它就能以你的账号身份调用 Pixiv API。
    """
    s = build_pixiv_session()
    s.headers.pop('Cookie', None)
    # 域级 Cookie 也清掉：RequestsCookieJar 里的 PHPSESSID 是 host-only/带域的，
    # 留着它就不是"无凭据会话"，将来任何同域/子域跳转都可能把它带出去。
    s.cookies.clear()
    return s


# ── 线程内连接池（图片代理 / 并发详情共用）──
# 调用方曾对每张图、每个作品都 build_pixiv_session() 再 close()，于是每次请求
# 都要重做 TCP + TLS 握手（实测 30 次请求 = 30 条连接，复用后 = 1 条；本地无
# TLS 就已快 3 倍，真实环境还要叠加每次 1~2 个 RTT 的 TLS 握手）。批量加载
# 图片或并发拉详情时，握手开销可能超过响应体本身，是图库/灯箱/搜索变慢的主因。
#
# 按线程缓存而非全局共享：requests.Session 不保证线程安全，但同线程内跨请求
# 复用连接池完全安全，且已覆盖 gunicorn sync worker 的主线程与线程池 executor
# 的各工作线程（每个线程一条连接，跨请求复用）。
_thread_local = threading.local()


def _cookie_file_stamp() -> float | None:
    """Cookie 文件 mtime；文件缺失时返回 None（行为同旧代码：由 _load_cookie 抛出）。"""
    try:
        return os.path.getmtime(COOKIE_PATH)
    except OSError:
        return None


def get_pooled_session(with_cookie: bool = True) -> requests.Session:
    """取本线程复用的 Pixiv session。Cookie 文件内容变化时自动重建。

    `with_cookie=False` 取的是**无凭据**变体（白名单外的图片主机专用）：两条
    连接池按线程各自缓存、互不影响。
    """
    slot = 'session' if with_cookie else 'anon_session'
    stamp_slot = 'stamp' if with_cookie else 'anon_stamp'
    stamp = _cookie_file_stamp()
    session = getattr(_thread_local, slot, None)
    if session is not None and getattr(_thread_local, stamp_slot, None) == stamp:
        return session
    if session is not None:
        try:
            session.close()
        except Exception:
            pass
    # build_pixiv_session() 内部的 _load_cookie() 会刷新 _cookie_mtime/_cookie_value
    session = build_pixiv_session() if with_cookie else build_credentialless_session()
    setattr(_thread_local, slot, session)
    setattr(_thread_local, stamp_slot, stamp)
    return session


def reset_pooled_session() -> None:
    """丢弃本线程的连接池（凭据与无凭据两个都丢）。

    复用的 keep-alive 连接被对端关闭后需要重建；两条池都丢是因为触发场景
    （连接异常）无法区分坏的连接属于哪条池。
    """
    for slot, stamp_slot in (('session', 'stamp'), ('anon_session', 'anon_stamp')):
        session = getattr(_thread_local, slot, None)
        if session is None:
            continue
        try:
            session.close()
        except Exception:
            pass
        setattr(_thread_local, slot, None)
        setattr(_thread_local, stamp_slot, None)


# ── 令牌桶限流 ──

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
TOTAL_RATE_PER_MINUTE = 60
_detail_limiter = _TokenBucket(DETAIL_RATE_PER_MINUTE)
_fill_limiter = _TokenBucket(FILL_RATE_PER_MINUTE)
_total_limiter = _TokenBucket(TOTAL_RATE_PER_MINUTE)


# ── 详情失败形态与采样 ──

# 详情"永久死亡"哨兵：作品已删除/非公開/不存在，重试无意义。
# 仅背景刷新路径经 return_dead=True 请求它；其余调用方（搜索/后台补全）不传该参数，
# 保持收到 None 的旧语义。
DEAD_DETAIL = object()

# 详情"全局性暂时失败"哨兵：Pixiv 限流（403/429 重试耗尽）或连接错误。
# 这类失败不是单个作品的问题，刷新路径不能把它记成该作品的退避——否则
# 限流期间会把整队列刷上 24h 退避、且每条白烧 3s+9s 退避时间。刷新侧
# 应据此中止本轮（见 background.PREFETCH_REFRESH_ABORT_STREAK）。
RETRYABLE_GLOBAL_DETAIL = object()

# 删除类报错关键词（保守集合）：命中即永久死亡。R18 权限类 message
# （如「年龄确认」）不在其中，按暂时性失败退避重试 —— 换好 Cookie 后可恢复，
# 绝不误删。可按服务器日志（'Detail API error for ...'）实测报文微调。
_PERMANENT_REMOVE_KEYWORDS = frozenset((
    '削除', '删除', '被删除', '不存在', '非公開', '非公开',
    'not found', 'not exist',
))


def is_permanently_removed_message(msg: str) -> bool:
    low = msg.lower()
    return any(k.lower() in low for k in _PERMANENT_REMOVE_KEYWORDS)


# 未识别详情报错采样：记录**没命中**删除关键词的 error:true 报文（message → 次数）。
# 用途：不必 SSH 翻日志就能在设置页看到 Pixiv 的真实措辞，据此补充关键词清单。
# 进程内、重启即清；上限 _DETAIL_ERROR_SAMPLE_LIMIT 种，避免无界增长。
_DETAIL_ERROR_SAMPLE_LIMIT = 20
_detail_error_samples: dict[str, int] = {}
_detail_error_lock = threading.Lock()


def _record_detail_error(msg: str) -> None:
    key = (msg or '').strip()[:200] or '(空 message)'
    with _detail_error_lock:
        if key in _detail_error_samples:
            _detail_error_samples[key] += 1
        elif len(_detail_error_samples) < _DETAIL_ERROR_SAMPLE_LIMIT:
            _detail_error_samples[key] = 1


def get_detail_error_samples() -> dict[str, int]:
    """未识别报错样本（message → 次数），供 /api/prefetch/status 展示。"""
    with _detail_error_lock:
        return dict(_detail_error_samples)


# 历史私有名（只在本模块内使用；`_detail_error_samples` 归本模块所有，
# 外部补丁必须打在 pixiv_client 上）
_is_permanently_removed_message = is_permanently_removed_message


# ── 请求：一个端点一个函数 ──

def fetch_illust_detail(session: requests.Session, pixiv_id: int,
                        limiter: _TokenBucket | None = None,
                        return_dead: bool = False) -> dict | None | object:
    """拉取单条作品详情（`/ajax/illust/<pid>`），返回规范化字段。

    return_dead=True 时区分两类失败：永久死亡（404 / 删除类报错）→ `DEAD_DETAIL`，
    全局性暂时失败（403/429 重试耗尽、连接错误）→ `RETRYABLE_GLOBAL_DETAIL`；
    其余调用方（不传该参数）保持收到 None 的旧语义。
    404 不再按一般错误重试：资源已不存在，重试是纯浪费。
    """
    url = endpoint_illust_detail(pixiv_id)
    (limiter or _detail_limiter).wait()
    _total_limiter.wait()
    last_status = None
    for attempt in range(DETAIL_MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=DETAIL_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get('error'):
                msg = str(data.get('message', ''))
                if is_auth_error(msg):
                    raise PixivAuthError(msg)
                if is_permanently_removed_message(msg):
                    logger.warning(f'Detail API 永久失败（已删除/非公開）{pixiv_id}: {msg}')
                    return DEAD_DETAIL if return_dead else None
                _record_detail_error(msg)
                logger.warning(f'Detail API error for {pixiv_id}: {msg}')
                return None
            return parse_illust_detail(data['body'])
        except requests.ConnectionError as e:
            # 连接建立失败（超时 / 拒绝 / DNS / 代理）：重试几乎必然重复失败，
            # 且每次都要空等满 DETAIL_TIMEOUT 的连接超时，3 次就是 30s+ 的
            # 无谓等待。直接放弃，交由调用方降级——后台补全
            # _kick_background_fill 之后还会兜一次。
            logger.warning(f'Detail API 连接失败 {pixiv_id}: {e}')
            return RETRYABLE_GLOBAL_DETAIL if return_dead else None
        except requests.RequestException as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            last_status = status
            if status == 401:
                # 认证失效（cookie 过期等）：重试无意义，与检索路径一致上报，
                # 避免 _process_items 把整页作品静默过滤成空结果。
                raise PixivAuthError('Pixiv API returned HTTP 401 (认证已失效，请更新 cookies.txt)')
            if status == 404:
                # 作品不存在/已删除：确定性永久失败，重试纯浪费。
                logger.warning(f'Detail API 404（作品不存在/已删除）{pixiv_id}')
                return DEAD_DETAIL if return_dead else None
            logger.warning(f'Detail API attempt {attempt + 1} failed for {pixiv_id}: {e}')
            if attempt < DETAIL_MAX_RETRIES:
                # 429/403 均为 Pixiv 限流（并发过高时返回 403），递增退避（3s/9s）
                time.sleep((3 * (3 ** attempt)) if status in (403, 429) else 1)
    # 重试耗尽：限流类失败是全局状态（不是该作品的问题），刷新路径据此熔断
    if return_dead and last_status in (403, 429):
        return RETRYABLE_GLOBAL_DETAIL
    return None


def fetch_original_urls(session: requests.Session, pixiv_id: int) -> list[str]:
    """按需拉详情、只取原图地址（详情页惰性补全用）。

    地址合法性不在这里判定：调用方仍走 `helpers.check_image_url` + 白名单分级。
    """
    detail = fetch_illust_detail(session, pixiv_id)
    return detail.get('original_urls', []) if detail else []


def fetch_search_illusts(session: requests.Session, query: str, *,
                         sort_order: str, r18_mode: str, page: int) -> tuple[list[dict], int]:
    """关键词搜索，返回 `(条目列表, 上游总数)`。

    条目保持 Pixiv 原始 dict 形态；字段访问一律经 `item_*` / `parse_illust_summary`。
    """
    url = endpoint_search_illustrations(
        query, order=sort_order, mode=r18_mode, page=page)
    try:
        resp = session.get(url, timeout=DETAIL_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f'Search API failed: {e}')
        handle_list_request_error('Search', e)
        return [], 0

    if envelope_error(data, log_prefix='Search API error'):
        return [], 0

    illust = (data.get('body', {}) or {}).get('illust', {}) or {}
    return list(illust.get('data', []) or []), illust.get('total', 0)


def fetch_discovery_artworks(session: requests.Session, *,
                             sort_order: str, r18_mode: str, page: int,
                             limit: int = 60) -> tuple[list[dict], int]:
    """发现页（无标签浏览），返回 `(条目列表, 上游总数)`。"""
    url = endpoint_discovery_artworks(
        mode=r18_mode, page=page, limit=limit, order=sort_order)
    try:
        resp = session.get(url, timeout=DETAIL_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f'Discovery API failed: {e}')
        handle_list_request_error('Discovery', e)
        return [], 0

    if envelope_error(data, log_prefix='Discovery API error'):
        return [], 0

    body = data.get('body', {}) or {}
    return parse_discovery_items(body), body.get('total', 0)


def fetch_user_profile_ids(session: requests.Session, user_id: str) -> list[int]:
    """画师的全部作品 id（新→旧）。拉不到返回 `[]`。

    注意这里**只返回 id**：过滤条件（hide_r18/min_bookmarks）要拿到详情的 tags
    才能判定，所以作者搜索是唯一必然逐条拉详情的路径（见 `fetcher.search_by_user`）。
    """
    url = endpoint_user_profile_all(user_id)
    try:
        _total_limiter.wait()  # 与其他 Pixiv 请求共用总限速，防止触发 429/403
        resp = session.get(url, timeout=DETAIL_TIMEOUT)
        resp.raise_for_status()
        profile_data = resp.json()
    except requests.RequestException as e:
        logger.error(f'User profile API failed: {e}')
        handle_list_request_error('User profile', e)
        return []

    if envelope_error(profile_data, log_prefix='User profile API error'):
        return []

    all_illusts = (profile_data.get('body', {}) or {}).get('illusts', {}) or {}
    return sorted([int(iid) for iid in all_illusts.keys()], reverse=True)


def fetch_following_latest(session: requests.Session, *,
                           r18_mode: str, page: int) -> tuple[list[dict], bool]:
    """关注画师的最新作品，返回 `(条目列表, 是否还有下一页)`。"""
    url = endpoint_follow_latest_illust(mode=r18_mode, page=page)
    try:
        resp = session.get(url, timeout=DETAIL_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f'Follow latest API failed: {e}')
        handle_list_request_error('Follow latest', e)
        return [], False

    if envelope_error(data, log_prefix='Follow latest API error'):
        return [], False

    return parse_follow_latest(data.get('body', {}) or {})
