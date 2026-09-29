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

import contextlib
import logging
import math
import os
import re
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
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


# ── 详情熔断闸（在途上限 + 全局冷却 + 半开探测） ──
#
# 为什么需要它：403/429 是"风控已生效"的**全局**信号（实测详情并发 3 即触发 403），
# 而详情重试是逐条退避（3s/9s）的——多个 worker 各自重试只会把风控喂得更狠：整轮
# 搜索白烧几十分钟，还留下一批假的"详情失败"。闸把这件事变成进程级事实：开路期间
# 新请求直接拒绝，冷却到期只放一个探测去验证是否恢复。

# 在途详情 HTTP 上限。限流桶管的是**速率**，管不住"同时挂起几个"——慢响应重叠
# 本身就是触发 403 的原因之一。这是保守初值，未经实测不得上调。
_DETAIL_MAX_IN_FLIGHT = 2
# 连续 403 的判定窗口与"不同作品"个数：单个作品的 R18/权限/地域问题也会回 403。
_DETAIL_403_WINDOW = 60.0
_DETAIL_403_DISTINCT_LIMIT = 3
# 初始冷却（403 触发 / 429 无有效 Retry-After）与指数退避上限（15 分钟）。
_DETAIL_INITIAL_COOLDOWN = 60.0
_DETAIL_MAX_COOLDOWN = 900.0
# 服务器 Retry-After 的**有限**天花板（6 小时）：服务器指示是权威的、不受上面那个
# 900 秒自派生上限约束，但也必须有个尽头 —— 理由与取舍见 `_open_locked`。
_DETAIL_MAX_SERVER_COOLDOWN = 6 * 3600.0


class PixivRateLimitedError(Exception):
    """详情请求被全局限流闸拒绝：这条请求不应发出。

    与 `PixivAuthError` 并列但修法相反：认证失效换 Cookie 就好，限流只能等/降速。
    调用方不得把它降级成 `None`（那等于把"受限"误判成"作品不匹配"），应保留已确认
    的结果、以可重试的状态收尾。
    """


# 外部文本（响应头部、API message）进日志前的长度上限。这些内容完全由外部（Pixiv，
# 或 `PIXIV_BASE_URL` 指向的代理/镜像）决定：换行与控制字符能凭空伪造日志行，超长值
# 能把日志刷爆。上限只约束**日志里怎么显示**，不参与任何判定（冷却计算走
# `_parse_retry_after`，报错报文另由 `_record_detail_error` 按 200 字符采样）。
_HEADER_LOG_LIMIT = 40


def _log_safe_header(value: Any) -> str:
    """把外部文本（响应头部或 API message）裁成一行可安全写日志的短文本。

    不可打印字符替换为 `·`（换行即在此被消掉），超长值截断并标注省略了多少字符。
    """
    if value is None:
        return '(无)'
    text = str(value)
    cleaned = ''.join(ch if ch.isprintable() else '·' for ch in text)
    if len(cleaned) > _HEADER_LOG_LIMIT:
        cleaned = cleaned[:_HEADER_LOG_LIMIT] + f'…(+{len(cleaned) - _HEADER_LOG_LIMIT})'
    return cleaned


def _parse_retry_after(value: Any, now: float) -> float | None:
    """把 `Retry-After` 解析成冷却秒数；无法使用时返回 None，由调用方回退默认冷却。

    支持 HTTP 规范的两种形态：delta-seconds 与 HTTP-date。**本函数必须是全函数**：
    头部内容完全由外部（Pixiv，或 `PIXIV_BASE_URL` 指向的代理/镜像）决定，解析失败
    只能是"这个值用不了"，绝不能把异常抛到调用方 —— 那会把一次普通的 429 变成
    未分类的 `ValueError`，绕过既有的降级路径。

    因此 delta-seconds 按 RFC 7231 只认 ASCII 数字（`1*DIGIT`）：`str.isdigit()` 对
    上标 `²`、阿拉伯-印度数字 `١٢` 这类 Unicode 字符也为真，交给 `float()` 要么抛
    `ValueError`，要么被静默当成另一个数（`١٢` → 12 秒），两者都不是服务器写的值。
    另外拒绝非有限值：`float('9' * 400)` 溢出成 `inf`，那会让冷却变成永久（见
    `_open_locked` 的钳制）。

    非正数（含 `Retry-After: 0` 与已过去的时间）一律按无效处理 —— 它们等于"不冷却"，
    真被限流时会让闸形同虚设。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isascii() and text.isdigit():
        seconds = float(text)
    else:
        try:
            deadline = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if deadline is None:
            return None
        if deadline.tzinfo is None:
            # 规范要求 GMT；缺时区时按 UTC 兜底比当作本地时间更接近意图
            deadline = deadline.replace(tzinfo=timezone.utc)
        seconds = deadline.timestamp() - now
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


class _DetailRequestGate:
    """详情请求的共享熔断闸：在途上限 + 全局冷却 + 半开探测。

    规则与理由（每条都对应一次实测或踩坑）：

    1. 在途上限 2（`BoundedSemaphore`）。槽位只在真实 HTTP 期间持有，令牌桶等待由
       调用方在进入本闸**之前**完成——否则排队等令牌的线程会一直占着槽位，实际
       并发被压到 1，45/分钟的桶反而更用不满。
    2. HTTP 429 立即开路。服务器已经明确拒绝，等待后再逐条重试没有意义。冷却
       优先取 `Retry-After`（delta-seconds 或 HTTP-date，服务器比我们更清楚要等多久），
       取不到才回退 60 秒。这里不给 Retry-After 套 900 秒上限：那个上限约束的是
       我们自己的指数翻倍，不是服务器的明确指示。
       **默认配置下这条分支收不到 429**：适配层的 `Retry(status_forcelist=[429, ...])`
       在 urllib3 里就把 429 拦下重试，耗尽后 requests 抛出**不带 `.response` 的**
       `RetryError`，`fetch_illust_detail` 拿不到响应对象、也就无从在这里上报。
       于是真实 429 被应用层归入"其他"→ 1s，`Retry-After` 由传输层自己睡掉
       （睡在闸槽位内 —— 已知遗留，见「重试策略」的 62s 放大说明）。所以默认
       配置下**真正触发开路的限流信号是 403**；429 分支只在 `PIXIV_BASE_URL` 为
       `http://`（适配器只 `mount('https://', ...)`，http 走默认适配器
       `max_retries=0`）或将来改 `status_forcelist` 时才会被走到 —— 那是安全网，
       不是死代码。
    3. 只有连续 60 秒窗口内 3 个**不同作品**的 403 才开路。只看 403 次数会把
       "个别作品不可见"误判成全局风控（这类 403 很常见，详见 `_warn_403`）。任何
       一个非 403 的 HTTP 响应（2xx/401/404/5xx）都说明上游并没有在限流我们，清零
       连续集合；但这些状态码的**原错误分类不变**，分类由调用方判定，本闸只观察。
    4. 冷却到期只放**一个**半开探测。全放会立刻回到触发风控的并发，不放则永远无法
       恢复。探测拿到任何非 403/429 的 HTTP 响应即认为风控解除（复位冷却与失败
       集合）；探测再次受限说明退避不足，冷却翻倍，封顶 900 秒。
    5. 状态更新与"读→判定→写"全程同一把锁，锁内不做任何 I/O。否则两个线程会各自
       读到未计入对方的中间状态——同一个探测名额发两次、403 计数丢一次。
    """

    def __init__(self, clock=None):
        """`clock` 是可注入的"当前时间"来源，默认 `time.time`。

        用 `time.time` 而非 `time.monotonic`：`Retry-After` 可能是 HTTP-date，
        必须与它同一时间基准。注入假时钟后测试不需要真实 sleep。
        """
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(_DETAIL_MAX_IN_FLIGHT)
        self._open = False
        self._opened_at = 0.0
        self._cooldown = _DETAIL_INITIAL_COOLDOWN
        self._probe_in_flight = False
        self._probe_pid: int | None = None
        self._recent_403: list[tuple[float, int]] = []

    @property
    def is_open(self) -> bool:
        """熔断闸是否处于开路状态（含冷却到期后的半开探测窗口）。

        半开探测期间仍算开路：那时**只有**那个探测请求可发，其余调用依旧会收到
        `PixivRateLimitedError`——对调用方而言"闸没关"才是它要的语义。
        """
        with self._lock:
            return self._open

    @contextlib.contextmanager
    def request_slot(self, pixiv_id: int):
        """占一个详情在途槽位；闸开路时在**发出请求之前**抛 `PixivRateLimitedError`。

        令牌桶等待由调用方在本 context manager 之前完成（见类 docstring 规则 1）。
        """
        admitted_as_probe = False
        with self._lock:
            if self._open:
                now = self._clock()
                if now - self._opened_at < self._cooldown:
                    raise PixivRateLimitedError(
                        f'详情熔断闸开路，剩余冷却 {self._cooldown - (now - self._opened_at):.0f}s'
                    )
                if self._probe_in_flight:
                    raise PixivRateLimitedError('详情熔断闸半开：已有探测在途，不再放行新请求')
                # 探测名额在锁内预留：两个线程同时等到冷却到期时只能有一个拿到
                admitted_as_probe = True
                self._probe_in_flight = True
                self._probe_pid = pixiv_id
        self._sem.acquire()
        try:
            yield
        finally:
            try:
                if admitted_as_probe:
                    with self._lock:
                        # 探测拿到 HTTP 响应时由 observe_response 归还名额；这里兜住
                        # "根本没拿到响应"的情形（连接错误/超时）——否则名额永远不还，
                        # 闸会卡在"已半开但无人能探测"的死状态里。
                        if self._probe_in_flight and self._probe_pid == pixiv_id:
                            self._probe_in_flight = False
                            self._probe_pid = None
            finally:
                self._sem.release()

    def observe_response(self, pixiv_id: int, status: int, retry_after: Any = None) -> None:
        """上报一次详情 HTTP 响应的状态码。

        调用点有两条硬约束：
        1. 在 `raise_for_status()` **之前**（否则先抛异常，判决送不到这里）；
        2. 仍在 `with gate.request_slot(...)` **内部** —— 判决与它归还的探测名额必须
           属于同一次持槽。放到 `with` 之后再调用，槽位已还、闸却还开着，成功判决被
           丢掉且没有任何东西会关闸，直到下一次探测。

        只观察状态码、不改错误分类：401/404/5xx 的原语义仍由调用方判定。
        探测名额按 pid 归属（接口固定，调用方没有额外令牌可传）：**同一 pid** 的陈旧
        在途响应（旧探测退出后又被授予的新预约）可能把新预约误清掉，最坏情况下让第二个
        探测同时进入半开窗口。这里能保证的边界是：总在途数仍由信号量压在 2 以内，冷却
        只增不减，因此不会越过并发红线 —— 但"半开严格只有一个探测"并非绝对，只是常见
        情形。
        """
        with self._lock:
            # 时钟必须在锁内读：规则 5 要求"读→判定→写"整体在锁内（request_slot 同）。
            now = self._clock()
            if self._probe_in_flight and self._probe_pid == pixiv_id:
                self._probe_in_flight = False
                self._probe_pid = None
                if status in (403, 429):
                    # 这里只约束**我们自己**推的指数翻倍；服务器给的 Retry-After 由
                    # _open_locked 的 max() 决定，不受这个上限钳制（见那里的注释）。
                    cooldown = min(self._cooldown * 2, _DETAIL_MAX_COOLDOWN)
                    logger.warning(f'详情熔断闸半开探测仍然受限（HTTP {status}），冷却翻倍到 {cooldown:.0f}s')
                    self._open_locked(now, cooldown)
                else:
                    logger.info(f'详情熔断闸半开探测成功（HTTP {status}），熔断复位')
                    self._close_locked()
                return
            if status == 429:
                cooldown = _parse_retry_after(retry_after, now) or _DETAIL_INITIAL_COOLDOWN
                self._open_locked(now, cooldown)
                # 记 `self._cooldown` 而不是服务器原值：钳制（天花板）与"只增不减"之后
                # 的实际冷却才是排障要看的东西，否则日志会说"开路 86400s"而闸其实
                # 6 小时后就放探测。
                logger.warning(
                    f'详情请求收到 HTTP 429，熔断闸开路 {self._cooldown:.0f}s'
                    f'（Retry-After={_log_safe_header(retry_after)}）')
                return
            if status == 403:
                self._observe_403_locked(now, pixiv_id)
                return
            # 非 403 的 HTTP 响应：上游没有在限流我们，清零连续 403 集合。
            # 注意这**不**解除已经打开的开路状态：开路是全局事实，只有半开探测才能证明恢复。
            self._recent_403.clear()

    def _observe_403_locked(self, now: float, pixiv_id: int) -> None:
        if self._open:
            # 开路前发出的在途请求陆续返回 403：已经在熔断中，不叠加、也不延长冷却
            return
        self._recent_403 = [(t, pid) for t, pid in self._recent_403
                            if now - t <= _DETAIL_403_WINDOW]
        if not any(pid == pixiv_id for _, pid in self._recent_403):
            self._recent_403.append((now, pixiv_id))
        if len(self._recent_403) >= _DETAIL_403_DISTINCT_LIMIT:
            logger.warning(
                f'详情请求 {_DETAIL_403_WINDOW:.0f} 秒内连续 {len(self._recent_403)} 个不同作品返回 403，'
                f'判定为全局限流/风控，熔断闸开路 {_DETAIL_INITIAL_COOLDOWN:.0f}s'
            )
            self._open_locked(now, _DETAIL_INITIAL_COOLDOWN)

    def _open_locked(self, now: float, cooldown: float) -> None:
        """在锁内开路。冷却只增不减：服务器给的 Retry-After 比当前退避短时不回退。"""
        # 900 秒上限（_DETAIL_MAX_COOLDOWN）只约束我们自己的指数翻倍（observe_response
        # 里 self._cooldown * 2），不约束**服务器**给的 Retry-After。
        # 但服务器的权威性并非无限：畸形/敌意的头部能把冷却推到 `inf`（`'9' * 400`
        # 这种 400 位数字 `float()` 后就是 inf），而这里 `now - _opened_at < _cooldown`
        # 一旦恒真，本进程生命周期内就再也不会放行任何探测 —— 详情拉取对搜索、预取与
        # 后台补全全部死掉，只留一行 WARNING，直到重启。所以用一把**有限**的尺子收口：
        # 服务器要等 6 小时以上时按 6 小时冷却（比要求更早恢复，但仍有明确尽头），
        # 而 900 秒那把尺子依旧只管我们自己的翻倍。inf 另在 _parse_retry_after 就被拒。
        cooldown = min(cooldown, _DETAIL_MAX_SERVER_COOLDOWN)
        self._cooldown = max(cooldown, self._cooldown if self._open else 0.0)
        self._open = True
        self._opened_at = now
        self._probe_in_flight = False
        self._probe_pid = None
        self._recent_403.clear()

    def _close_locked(self) -> None:
        """在锁内复位：开路状态、连续 403 集合与退避级数一起清零。"""
        self._open = False
        self._opened_at = 0.0
        self._cooldown = _DETAIL_INITIAL_COOLDOWN
        self._probe_in_flight = False
        self._probe_pid = None
        self._recent_403.clear()


# 详情熔断闸的**进程级单例**：搜索、预取与后台补全共用这一条。
# 为什么必须是单例：限流是账户级的全局事实，"各调用点各自一个闸"等于没有熔断 ——
# 每个调用点都会认为自己没被限流而继续发请求。有状态，所以补丁/读取一律对
# `pixiv_client`（见 docs/architecture.md「有状态符号只在 pixiv_client」表）。
_detail_gate = _DetailRequestGate()


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

def _observe_detail_response(resp: Any, pixiv_id: int) -> None:
    """把一次**真实 HTTP 响应**的状态码与 `Retry-After` 交给熔断闸。

    必须在 `raise_for_status()` **之前**、且仍在 `gate.request_slot()` 内部调用
    （见 `_DetailRequestGate.observe_response` 的两条硬约束）。头部只在这里读取：
    响应头完全由外部决定，解析与钳制全在闸内（`_parse_retry_after`），本函数不得
    因为畸形头部抛异常 —— 那会把一次普通的 429 变成未分类错误。
    连接错误/超时不会有响应对象，走不到这里（闸的槽位与探测名额由 context manager
    的 finally 兜住）。
    """
    status = getattr(resp, 'status_code', None)
    if not isinstance(status, int):
        return
    headers = getattr(resp, 'headers', None)
    retry_after = headers.get('Retry-After') if headers is not None else None
    _detail_gate.observe_response(pixiv_id, status, retry_after)


def _gate_refusal(return_dead: bool, pixiv_id: int, reason: str) -> object:
    """熔断闸拒绝放行时的统一出口。

    刷新路径（`return_dead=True`）要的是"全局暂时失败"哨兵：它既不能被当成该作品的
    永久失败写进退避标记，也不能让异常冒泡（冒泡会跳过 `_prefetch_loop` 的容量清理）。
    其余调用方（搜索 / 后台补全）必须抛出：降级成 `None` 会被 `_process_items` 当成
    "这件作品详情失败"，限流就这样被静默吞掉、`partial` 语义随之失效。
    """
    if return_dead:
        logger.warning(f'详情请求 {pixiv_id} 被熔断闸拒绝（{reason}），按全局暂时失败处理')
        return RETRYABLE_GLOBAL_DETAIL
    raise PixivRateLimitedError(f'详情请求 {pixiv_id} 被熔断闸拒绝：{reason}')


def fetch_illust_detail(session: requests.Session, pixiv_id: int,
                        limiter: _TokenBucket | None = None,
                        return_dead: bool = False) -> dict | None | object:
    """拉取单条作品详情（`/ajax/illust/<pid>`），返回规范化字段。

    return_dead=True 时区分两类失败：永久死亡（404 / 删除类报错）→ `DEAD_DETAIL`，
    全局性暂时失败（403/429 重试耗尽、连接错误、被熔断闸拒绝）→ `RETRYABLE_GLOBAL_DETAIL`；
    其余调用方（不传该参数）保持收到 None 的旧语义，但**被熔断闸拒绝时会抛
    `PixivRateLimitedError`** —— 那是"上游正在限流、这条请求根本没发出"，与"作品不匹配"
    是两件事，降级成 None 会让调用方把它当成永久失败。
    404 不再按一般错误重试：资源已不存在，重试是纯浪费。

    每次真实 attempt（含 403 的退避重试；429 默认在传输层就被拦下，见循环内注释）
    都**重新**取 detail/fill 桶与总桶的令牌，再进熔断闸占槽位。次序是硬约束：令牌桶
    等待必须在占槽位之前 —— 否则排队等令牌的线程会一直占着在途槽位，实际并发被压到
    1，45/分钟的桶反而更用不满。
    响应状态码与 `Retry-After` 在 `raise_for_status()` 之前上报给闸；闸一旦开路，
    逐条 3/9 秒重试立即作废（开路期间重试只会被拒，空等没有意义）。
    """
    url = endpoint_illust_detail(pixiv_id)
    detail_limiter = limiter or _detail_limiter
    last_status = None
    for attempt in range(DETAIL_MAX_RETRIES + 1):
        if attempt:
            if _detail_gate.is_open:
                # 开路是全局事实，不是这个作品的问题；冷却到期后由新调用去半开探测。
                return _gate_refusal(return_dead, pixiv_id, '熔断闸开路，不再逐条退避重试')
            # 增退避只对**到达应用层**的限流有意义：默认配置下那是 403（并发过高时
            # Pixiv 返回的状态码）。429 在传输层就被 `Retry(status_forcelist=[429, ...])`
            # 拦下 —— urllib3 重试耗尽后 requests 抛**不带 `.response` 的 `RetryError`**，
            # 下面只能按 last_status=None 归入"其他"（1s），闸也收不到那个 429。
            # 元组里留着 429 是给 `PIXIV_BASE_URL=http://` 的镜像（适配器只挂 https://）：
            # 那时 429 会到达闸并当场开路，下一次 attempt 在闸前就被拒，3s/9s 同样轮不到它。
            time.sleep((3 * (3 ** (attempt - 1))) if last_status in (403, 429) else 1)
        detail_limiter.wait()
        _total_limiter.wait()
        try:
            with _detail_gate.request_slot(pixiv_id):
                resp = session.get(url, timeout=DETAIL_TIMEOUT)
                _observe_detail_response(resp, pixiv_id)
                resp.raise_for_status()
                data = resp.json()
                if data.get('error'):
                    msg = str(data.get('message', ''))
                    if is_auth_error(msg):
                        raise PixivAuthError(msg)
                    if is_permanently_removed_message(msg):
                        logger.warning(
                            f'Detail API 永久失败（已删除/非公開）{pixiv_id}: {_log_safe_header(msg)}')
                        return DEAD_DETAIL if return_dead else None
                    _record_detail_error(msg)
                    logger.warning(f'Detail API error for {pixiv_id}: {_log_safe_header(msg)}')
                    return None
                return parse_illust_detail(data['body'])
        except PixivRateLimitedError as e:
            # 闸在**发出请求之前**拒绝（request_slot 抛出）：这条请求没发出，
            # 因此不算"这件作品详情失败"。
            return _gate_refusal(return_dead, pixiv_id, str(e))
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
    # 重试耗尽：限流类失败是全局状态（不是该作品的问题），刷新路径据此熔断
    if return_dead and last_status in (403, 429):
        return RETRYABLE_GLOBAL_DETAIL
    return None


def fetch_original_urls(session: requests.Session, pixiv_id: int) -> list[str]:
    """按需拉详情、只取原图地址（详情页惰性补全用）。

    地址合法性不在这里判定：调用方仍走 `helpers.check_image_url` + 白名单分级。
    被熔断闸拒绝时按"没拉到"返回空列表（不是抛出去）：本函数的契约是列表，调用方
    （详情页 / 下载入口）本来就把空列表当"暂时取不到原图"降级 —— 抛出去会变成下载
    入口的 HTTP 500，而"正在被限流"并不是"该作品不能下载"。
    """
    try:
        detail = fetch_illust_detail(session, pixiv_id)
    except PixivRateLimitedError as e:
        logger.warning(f'详情请求被熔断闸拒绝，本次不取原图 {pixiv_id}: {e}')
        return []
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
