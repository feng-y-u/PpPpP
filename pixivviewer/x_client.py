"""X（Twitter）适配层：搜索时间线、解析推文、错误分类、凭据与限流。

与 `pixiv_client` 同一个定位：**X 的接口细节只出现在这个文件里**，路由、后台
线程、去重模块一律只用这里的规范结构（`XTweet` / `XMedia`）。

现实约束（决定了这里为什么这么写）：
- X 没有免费可用的搜索 API，只能走网页端内部 GraphQL，`query_id` 随发版漂移，
  所以它放在 `instance/x_api.json` 里，坏了自己改配置，不用改代码；
- 搜索必须带登录 Cookie（`auth_token` + `ct0`），没有匿名路径；
- 网页端限流很紧（约 15 分钟 50 次），这里默认按 15 次/15 分钟保守走；
- 失败必须能区分「Cookie 过期」「限流」「接口版本过期」，否则用户只会看到
  "抓不到图"。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

from . import config

logger = logging.getLogger(__name__)

# X 的 id 是 64 位整数：只做字符串处理，不转 int（JS 侧也只按字符串用）
_TWEET_ID_RE = re.compile(r'/status(?:es)?/(\d{1,20})')
_PIXIV_ID_RE = re.compile(r'pixiv\.net/(?:[a-z]{2}/)?(?:artworks?|i)/(\d+)', re.IGNORECASE)

_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)

# 搜索端点要求的一组特性开关（X 用它控制返回结构）。默认集通常可用；
# 结构变了就改 instance/x_api.json 的 features。
_DEFAULT_FEATURES = {
    'rweb_video_screen_enabled': False,
    'payments_enabled': False,
    'profile_label_improvements_pcf_label_in_post_enabled': True,
    'rweb_tipjar_consumption_enabled': True,
    'verified_phone_label_enabled': False,
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'premium_content_api_read_enabled': False,
    'communities_web_enable_tweet_community_results_fetch': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'responsive_web_grok_analyze_button_fetch_trends_enabled': False,
    'responsive_web_grok_analyze_post_followups_enabled': True,
    'responsive_web_jetfuel_frame': False,
    'responsive_web_grok_share_attachment_enabled': True,
    'articles_preview_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'tweet_awards_web_tipping_enabled': False,
    'responsive_web_grok_show_grok_translated_post': False,
    'responsive_web_grok_analysis_button_from_backend': False,
    'creator_subscriptions_quote_tweet_preview_enabled': False,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    'responsive_web_grok_image_annotation_enabled': True,
    'responsive_web_grok_community_note_auto_translation_is_enabled': False,
    'responsive_web_enhance_cards_enabled': False,
}


class XError(Exception):
    """X 适配层的基类异常。"""


class XAuthError(XError):
    """Cookie 缺失/过期/被拒（401、403）。"""


class XRateLimited(XError):
    """429：本轮别再来。"""


class XQueryStale(XError):
    """query_id 过期或响应结构变了 —— 需要更新 instance/x_api.json。"""


class XNetworkError(XError):
    """连接/超时/代理不可达。"""


@dataclass
class XMedia:
    url: str
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict:
        return {'url': self.url, 'w': self.width, 'h': self.height}


@dataclass
class XTweet:
    tweet_id: str
    author_name: str = ''
    author_screen_name: str = ''
    text: str = ''
    tweeted_at: Optional[datetime] = None
    media: list[XMedia] = field(default_factory=list)
    expanded_urls: list[str] = field(default_factory=list)

    @property
    def media_count(self) -> int:
        return len(self.media)


# ── 配置与凭据 ─────────────────────────────────────────────────────────

_api_cache: dict = {'mtime': None, 'data': None}
_api_lock = threading.Lock()
_cookie_cache: dict = {'mtime': None, 'data': None}
_cookie_lock = threading.Lock()


def _load_api_config() -> dict:
    """读 instance/x_api.json（可缺省）；按 mtime 缓存，改完不用重启进程。"""
    path = getattr(config, 'X_API_CONFIG_PATH', '')
    try:
        mtime = os.path.getmtime(path) if path and os.path.exists(path) else None
    except OSError:
        mtime = None
    with _api_lock:
        if _api_cache['mtime'] == mtime and _api_cache['data'] is not None:
            return _api_cache['data']
        data: dict = {}
        if mtime is not None:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    data = raw
            except (OSError, ValueError) as e:
                logger.error(f'[x] 读取 {path} 失败，使用默认配置：{e}')
        _api_cache['mtime'] = mtime
        _api_cache['data'] = data
        return data


def load_cookie() -> tuple[str, str]:
    """读 `instance/x_cookies.txt`，返回 `(auth_token, ct0)`。

    只认这两个键，其余忽略；格式与浏览器 Cookie 头一致：
    `auth_token=...; ct0=...`（也容忍一行一个 `k=v`）。
    读不到或缺少任一键 → XAuthError（调用方据此提示"未配置/已失效"）。
    """
    path = getattr(config, 'X_COOKIE_PATH', '')
    try:
        mtime = os.path.getmtime(path) if path and os.path.exists(path) else None
    except OSError:
        mtime = None
    with _cookie_lock:
        if _cookie_cache['mtime'] != mtime or _cookie_cache['data'] is None:
            values: dict[str, str] = {}
            if mtime is not None:
                try:
                    with open(path, 'r', encoding='utf-8', errors='replace') as f:
                        text = f.read()
                except OSError as e:
                    logger.error(f'[x] 读取 Cookie 失败：{e}')
                    text = ''
                for part in re.split(r'[;\n\r]+', text):
                    if '=' in part:
                        key, _, value = part.partition('=')
                        values[key.strip()] = value.strip()
            _cookie_cache['mtime'] = mtime
            _cookie_cache['data'] = values
        values = _cookie_cache['data']
    auth_token = values.get('auth_token', '')
    ct0 = values.get('ct0', '')
    if not auth_token or not ct0:
        raise XAuthError('未配置 X Cookie（instance/x_cookies.txt 需要 auth_token 与 ct0）')
    return auth_token, ct0


def clear_cookie_cache() -> None:
    """设置页写完 Cookie 后调用，避免旧值按 mtime 缓存在内存里。"""
    with _cookie_lock:
        _cookie_cache['mtime'] = None
        _cookie_cache['data'] = None


# ── 限流 ───────────────────────────────────────────────────────────────

class _TokenBucket:
    """保守的令牌桶：默认 15 次 / 15 分钟，跨线程共享。"""

    def __init__(self, capacity: int, refill_seconds: float) -> None:
        self.capacity = capacity
        self.refill = refill_seconds
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def _take(self) -> bool:
        now = time.monotonic()
        with self._lock:
            elapsed = now - self.updated
            if elapsed > 0:
                self.tokens = min(self.capacity, self.tokens + elapsed / self.refill)
                self.updated = now
            if self.tokens < 1:
                return False
            self.tokens -= 1
            return True

    def wait(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while not self._take():
            if time.monotonic() >= deadline:
                raise XRateLimited('X 请求预算已用尽，稍后重试')
            time.sleep(0.5)


def _bucket() -> _TokenBucket:
    global _limiter
    if _limiter is None:
        _limiter = _TokenBucket(
            int(getattr(config, 'X_RATE_PER_15MIN', 15)),
            15 * 60 / max(1, int(getattr(config, 'X_RATE_PER_15MIN', 15))),
        )
    return _limiter


_limiter: Optional[_TokenBucket] = None
_limiter_lock = threading.Lock()


def reset_limiter() -> None:
    """测试用：清空令牌桶。"""
    global _limiter
    with _limiter_lock:
        _limiter = None


# ── 请求 ───────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    auth_token, ct0 = load_cookie()
    session = requests.Session()
    proxy = getattr(config, 'PROXY', '')
    if proxy:
        session.proxies = {'http': proxy, 'https': proxy}
    session.headers.update({
        'Authorization': f'Bearer {_load_api_config().get("bearer") or config.X_DEFAULT_BEARER}',
        'X-Csrf-Token': ct0,
        'X-Twitter-Auth-Type': 'OAuth2Session',
        'X-Twitter-Active-User': 'yes',
        'X-Twitter-Client-Language': 'zh-cn',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': _USER_AGENT,
        'Referer': f'{config.X_BASE_URL}/search',
    })
    session.cookies.set('auth_token', auth_token, domain='.x.com')
    session.cookies.set('ct0', ct0, domain='.x.com')
    session.verify = bool(getattr(config, 'SSL_VERIFY', True))
    return session


def build_search_url(tag: str, cursor: Optional[str] = None) -> str:
    """拼 SearchTimeline 请求地址；query_id 缺失时抛 XQueryStale 而不是拼出坏地址。"""
    api = _load_api_config()
    query_id = str(api.get('query_id') or getattr(config, 'X_SEARCH_QUERY_ID', '') or '').strip()
    if not query_id:
        raise XQueryStale('未配置 X 搜索 query_id（实例目录下 x_api.json 或设置项 x_search_query_id）')
    variables = {
        'rawQuery': tag,
        'count': 20,
        'querySource': 'typed_query',
        'product': 'Latest',
    }
    if cursor:
        variables['cursor'] = cursor
    features = api.get('features') or _DEFAULT_FEATURES
    base = str(api.get('base_url') or config.X_API_BASE).rstrip('/')
    return (
        f'{base}/{query_id}/SearchTimeline'
        f'?variables={requests.utils.quote(json.dumps(variables, ensure_ascii=False))}'
        f'&features={requests.utils.quote(json.dumps(features, ensure_ascii=False))}'
    )


def _classify_http(status: int, body: str) -> XError:
    snippet = (body or '')[:200].replace('\n', ' ')
    if status in (401, 403):
        return XAuthError(f'X 拒绝了请求（HTTP {status}）：Cookie 可能已失效')
    if status == 429:
        return XRateLimited('X 限流（HTTP 429）')
    if status in (404, 422):
        return XQueryStale(f'X 搜索接口不可用（HTTP {status}）：query_id 可能已过期')
    return XError(f'X 请求失败（HTTP {status}）：{snippet}')


def parse_tweet_id(url: str) -> Optional[str]:
    """从推文链接里取 tweet_id（`x.com/<user>/status/<id>` / `twitter.com/...`）。"""
    m = _TWEET_ID_RE.search(url or '')
    return m.group(1) if m else None


def extract_pixiv_id(urls: list[str], text: str = '') -> Optional[int]:
    """从推文正文/展开链接里找 Pixiv 作品 id（E1：同一作品的决定性证据）。"""
    for value in list(urls or []) + [(text or '')]:
        m = _PIXIV_ID_RE.search(value)
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                continue
    return None


# X 的 created_at 恒为英文星期/月份缩写（"Mon Nov 17 10:00:00 +0000 2026"）。
# 不用 strptime 的 %a/%b：它们按进程 locale 解释名称，非 C/英文 locale 下会
# ValueError，推文时间全部变 None、H1 的时间窗就永远算不出来。
_CREATED_AT_MONTHS = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12,
}


def _parse_created_at(value: str) -> Optional[datetime]:
    if not value:
        return None
    parts = str(value).split()
    if len(parts) != 6:
        return None
    _wd, mon, day_s, hms, _tz, year_s = parts
    month = _CREATED_AT_MONTHS.get(mon)
    if month is None:
        return None
    try:
        hms_parts = hms.split(':')
        if len(hms_parts) != 3:
            return None
        # 沿用旧行为：保留墙面时钟（X 搜索响应恒为 +0000，墙面钟即 UTC）
        return datetime(int(year_s), month, int(day_s),
                        int(hms_parts[0]), int(hms_parts[1]), int(hms_parts[2]))
    except (TypeError, ValueError):
        return None


def _unwrap_tweet(result: dict) -> Optional[dict]:
    if not isinstance(result, dict):
        return None
    if result.get('__typename') == 'TweetWithVisibilityResults':
        result = result.get('tweet') or {}
    if not isinstance(result, dict):
        return None
    return result if result.get('legacy') else None


def _entry_tweet(entry: dict) -> Optional[dict]:
    content = entry.get('content') or {}
    item = content.get('itemContent') or {}
    result = (item.get('tweet_results') or {}).get('result')
    return _unwrap_tweet(result)


def _to_original(url: str) -> str:
    """把 pbs.twimg.com 的图片地址统一成原图（`?name=orig`）。

    同一个媒体在搜索响应里可能已经是 `?name=small`，直接拼 `?name=orig` 会得到
    两个 name 参数（X 取第一个），所以按 query 覆盖而不是字符串拼接。
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(url)
    if not parts.query:
        return url + '?name=orig'
    query = dict(parse_qsl(parts.query))
    query['name'] = 'orig'
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def _tweet_from_legacy(rest_id: str, tweet: dict) -> Optional[XTweet]:
    legacy = tweet.get('legacy') or {}
    user = (((tweet.get('core') or {}).get('user_results') or {}).get('result')) or {}
    user_legacy = user.get('legacy') or {}
    media_raw = ((legacy.get('extended_entities') or {}).get('media')
                 or (legacy.get('entities') or {}).get('media') or [])
    if not isinstance(media_raw, list):
        return None
    media: list[XMedia] = []
    for item in media_raw:
        if not isinstance(item, dict) or item.get('type') != 'photo':
            continue
        url = item.get('media_url_https') or item.get('media_url') or ''
        if not url:
            continue
        url = _to_original(url)
        info = item.get('original_info') or {}
        media.append(XMedia(
            url=url,
            width=int(info.get('width') or item.get('sizes', {}).get('large', {}).get('w') or 0),
            height=int(info.get('height') or item.get('sizes', {}).get('large', {}).get('h') or 0),
        ))
    if not media:
        return None
    urls = [
        u.get('expanded_url') or u.get('url') or ''
        for u in ((legacy.get('entities') or {}).get('urls') or [])
        if isinstance(u, dict)
    ]
    note = (((tweet.get('note_tweet') or {}).get('note_tweet_results') or {}).get('result') or {})
    text = (note.get('text') if isinstance(note, dict) else '') or legacy.get('full_text') or ''
    return XTweet(
        tweet_id=str(rest_id or tweet.get('rest_id') or ''),
        author_name=user_legacy.get('name') or '',
        author_screen_name=user_legacy.get('screen_name') or '',
        text=text,
        tweeted_at=_parse_created_at(legacy.get('created_at') or ''),
        media=media[:getattr(config, 'X_MAX_MEDIA_PER_TWEET', 30)],
        expanded_urls=urls,
    )


def parse_search_page(payload: dict) -> tuple[list[XTweet], Optional[str]]:
    """解析 SearchTimeline 响应：只保留带图推文，返回 `(tweets, next_cursor)`。"""
    if not isinstance(payload, dict):
        raise XQueryStale('X 响应不是 JSON 对象')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise XQueryStale(f'X 响应缺少 data 字段：{str(payload)[:200]}')
    timeline = (((data.get('search_by_raw_query') or {}).get('search_timeline') or {})
                .get('timeline') or {})
    instructions = timeline.get('instructions')
    if not isinstance(instructions, list):
        raise XQueryStale('X 响应缺少 timeline.instructions（接口结构可能已变化）')

    tweets: list[XTweet] = []
    seen: set[str] = set()
    next_cursor: Optional[str] = None
    for inst in instructions:
        if not isinstance(inst, dict):
            continue
        for entry in inst.get('entries') or []:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get('entryId') or '')
            if entry_id.startswith('cursor-bottom'):
                value = (entry.get('content') or {}).get('value')
                if value:
                    next_cursor = value
                continue
            raw = _entry_tweet(entry)
            if not raw:
                continue
            rest_id = str(raw.get('rest_id') or '')
            if not rest_id or rest_id in seen:
                continue
            tweet = _tweet_from_legacy(rest_id, raw)
            if tweet is None:
                continue
            seen.add(rest_id)
            tweets.append(tweet)
    return tweets, next_cursor


def search_timeline(tag: str, cursor: Optional[str] = None) -> tuple[list[XTweet], Optional[str]]:
    """搜索一个标签（`product=Latest`），返回 `(tweets, next_cursor)`。"""
    tag = (tag or '').strip()
    if not tag:
        raise XError('标签不能为空')
    url = build_search_url(tag, cursor)
    _bucket().wait()
    session = _session()
    try:
        resp = session.get(url, timeout=getattr(config, 'X_TIMEOUT', (10, 30)),
                           allow_redirects=False)
    except requests.ConnectionError as e:
        raise XNetworkError(f'连不上 X：{e}') from e
    except requests.Timeout as e:
        raise XNetworkError(f'X 请求超时：{e}') from e
    finally:
        session.close()
    if resp.status_code != 200:
        raise _classify_http(resp.status_code, resp.text)
    try:
        payload = resp.json()
    except ValueError as e:
        raise XQueryStale('X 响应不是合法 JSON（可能被风控页替换）') from e
    return parse_search_page(payload)


def probe_credentials(tag: str = '#テスト') -> dict:
    """设置页「测试连接」：跑一次真实搜索并返回可展示的结论。"""
    try:
        tweets, _ = search_timeline(tag)
    except XAuthError as e:
        return {'ok': False, 'kind': 'auth', 'message': str(e)}
    except XRateLimited as e:
        return {'ok': False, 'kind': 'rate_limited', 'message': str(e)}
    except XQueryStale as e:
        return {'ok': False, 'kind': 'query_stale', 'message': str(e)}
    except XNetworkError as e:
        return {'ok': False, 'kind': 'network', 'message': str(e)}
    except XError as e:
        return {'ok': False, 'kind': 'error', 'message': str(e)}
    with_media = sum(1 for t in tweets if t.media)
    return {
        'ok': True,
        'kind': 'ok',
        'message': f'连接正常：返回 {len(tweets)} 条推文，其中 {with_media} 条带图',
        'sample': tweets[0].media[0].url if tweets and tweets[0].media else '',
    }
