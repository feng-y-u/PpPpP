from __future__ import annotations

import logging
import os
import random
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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
    PROXY, SSL_VERIFY, PIXIV_USERNAME, PIXIV_PASSWORD,
)
from models import Illust, BlockedTag, get_session, safe_commit

logger = logging.getLogger(__name__)

_cookie_mtime = 0
_cookie_value = ''
_pixiv_hostname = urlparse(PIXIV_BASE_URL).hostname or 'www.pixiv.net'


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


# ── OAuth ──

PIXIV_CLIENT_ID = 'MOBrBDS8blbauoSck0ZfDbtuzpyT'
PIXIV_CLIENT_SECRET = 'lsACyCD94FhDUtGTXi3QjcFE2uP2qW'

_token_data = {}  # { 'access_token': str, 'refresh_token': str, 'expires_at': float }
_device_token = str(uuid.uuid4())


def _oauth_login() -> None:
    """首次登录：用户名/密码 → access_token + refresh_token。"""
    global _token_data
    try:
        resp = requests.post('https://oauth.secure.pixiv.net/auth/token', data={
            'client_id': PIXIV_CLIENT_ID,
            'client_secret': PIXIV_CLIENT_SECRET,
            'grant_type': 'password',
            'username': PIXIV_USERNAME,
            'password': PIXIV_PASSWORD,
            'device_token': _device_token,
        }, verify=SSL_VERIFY, timeout=(5, 15))
        resp.raise_for_status()
        body = resp.json()
        _token_data = {
            'access_token': body['access_token'],
            'refresh_token': body['refresh_token'],
            'expires_at': time.time() + body.get('expires_in', 3600),
        }
        logger.info('OAuth login successful')
    except Exception as e:
        logger.error(f'OAuth login failed: {e}')
        raise


def _oauth_refresh() -> None:
    """使用 refresh_token 刷新 Token。"""
    global _token_data
    try:
        resp = requests.post('https://oauth.secure.pixiv.net/auth/token', data={
            'client_id': PIXIV_CLIENT_ID,
            'client_secret': PIXIV_CLIENT_SECRET,
            'grant_type': 'refresh_token',
            'refresh_token': _token_data['refresh_token'],
            'device_token': _device_token,
        }, verify=SSL_VERIFY, timeout=(5, 15))
        resp.raise_for_status()
        body = resp.json()
        _token_data = {
            'access_token': body['access_token'],
            'refresh_token': body['refresh_token'],
            'expires_at': time.time() + body.get('expires_in', 3600),
        }
        logger.info('OAuth token refreshed')
    except Exception as e:
        logger.error(f'OAuth refresh failed: {e}')
        _token_data = {}
        raise


def _ensure_token() -> None:
    """确保存在有效的 Token（必要时登录或刷新）。"""
    if not _token_data:
        _oauth_login()
    elif _token_data['expires_at'] - time.time() < 300:  # 5 min buffer
        _oauth_refresh()


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': f'{PIXIV_BASE_URL}/',
        'Accept-Language': 'ja,zh-CN;q=0.9,zh;q=0.8,en;q=0.7',
    })

    if PIXIV_USERNAME and PIXIV_PASSWORD:
        _ensure_token()
        s.headers.update({'Authorization': f'Bearer {_token_data["access_token"]}'})
    else:
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


def _split_tags(keyword: str) -> list[str]:
    raw = keyword.replace('，', ',').strip()
    parts = [t.strip() for t in raw.split(',') if t.strip()]
    return parts if parts else [raw]


def _get_blocked_tags(db: Any) -> set[str]:
    return {t.tag for t in db.query(BlockedTag).all()}


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


def _get_illust_detail(session: requests.Session, pixiv_id: int) -> dict | None:
    url = f'{PIXIV_BASE_URL}/ajax/illust/{pixiv_id}'
    time.sleep(random.uniform(0.1, 0.6))
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
                'description': body.get('description', ''),
            }
        except requests.RequestException as e:
            logger.warning(f'Detail API attempt {attempt + 1} failed for {pixiv_id}: {e}')
            if attempt < DETAIL_MAX_RETRIES:
                time.sleep(1)
    return None


def _fetch_details_parallel(pixiv_ids: list[int]) -> dict[int, dict]:
    if not pixiv_ids:
        return {}
    results = {}

    def _worker(pid: int) -> tuple[int, dict | None]:
        session = _build_session()
        return pid, _get_illust_detail(session, pid)

    with ThreadPoolExecutor(max_workers=FETCH_DETAIL_WORKERS) as executor:
        futures = {executor.submit(_worker, pid): pid for pid in pixiv_ids}
        for future in as_completed(futures):
            try:
                pid, detail = future.result()
                if detail is not None:
                    results[pid] = detail
            except Exception as e:
                logger.error(f'Parallel fetch failed for {futures[future]}: {e}')

    return results


# ── 公共流水线 ──

def _process_items(db: Any, items: list[Any], id_extractor: Callable[[Any], int], illust_factory: Callable[[Any, dict], Illust], blocked: set[str], *,
                   min_bookmarks: int = 0, hide_r18: bool = False) -> list[dict]:
    """去重 → 过滤 → 并行拉取详情 → 存储。

    Args:
        db: SQLAlchemy 会话
        items: 原始作品字典列表（用户搜索时为 pixiv_id 整数列表）
        id_extractor: 可调用对象，接收 item 返回 int pixiv_id
        illust_factory: 可调用对象，接收 (item, detail) 返回 Illust 实例
        blocked: 被屏蔽标签的字符串集合
        min_bookmarks: 最低收藏数（0 表示不过滤）
        hide_r18: 若为 True，排除 R-18 标签作品

    Returns: 可直接用于 API 响应的 illust 字典列表
    """
    results = []
    to_fetch = []

    if not items:
        return results

    pixiv_ids = [id_extractor(item) for item in items]
    existing_list = db.query(Illust).filter(Illust.pixiv_id.in_(pixiv_ids)).all()
    existing_map = {i.pixiv_id: i for i in existing_list}

    for item in items:
        pixiv_id = id_extractor(item)
        existing = existing_map.get(pixiv_id)
        if existing:
            if not _is_blocked(existing.tags_list, blocked) \
               and existing.bookmark_count >= min_bookmarks \
               and not (hide_r18 and _is_r18(existing.tags_list)):
                results.append(existing.to_dict())
            continue
        to_fetch.append(pixiv_id)

    if to_fetch:
        details = _fetch_details_parallel(to_fetch)
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
            db.add(illust)
            db.flush()
            results.append(illust.to_dict())

    return results


def _illust_from_item(item: dict, detail: dict) -> Illust:
    """从搜索/发现/关注 API 条目创建 Illust。

    大多数字段来自搜索结果条目（列表上下文）。
    bookmark_count 和 original_urls 来自详情响应。
    """
    illust = Illust(
        pixiv_id=int(item['id']),
        title=item.get('title', ''),
        user_id=int(item.get('userId', 0)),
        user_name=item.get('userName', ''),
        page_count=item.get('pageCount', 1),
        bookmark_count=detail['bookmark_count'],
        thumb_url=item.get('url', ''),
        upload_date=_parse_date(item.get('updateDate')),
        description=detail.get('description', ''),
    )
    illust.tags_list = _parse_tags(item.get('tags', []))
    illust.original_urls_list = detail['original_urls']
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
        description=detail.get('description', ''),
    )
    illust.tags_list = detail['tags']
    illust.original_urls_list = detail['original_urls']
    return illust


# ── 搜索函数 ──

def search_by_tag(keyword: str, min_bookmarks: int = 0, page: int = 1,
                  sort_order: str = 'popular_d', max_pages: int = 10,
                  tag_mode: str = 'or', r18_mode: str = 'all') -> tuple[list[dict], bool]:
    """按标签搜索 Pixiv。tag_mode: 'or' = 任一标签, 'and' = 全部标签。"""
    if page > max_pages:
        return [], False

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
        return [], False

    if search_data.get('error'):
        logger.error(f'Search API error: {search_data.get("message")}')
        return [], False

    illusts_data = (
        search_data.get('body', {})
        .get('illust', {})
        .get('data', [])
    )
    total = search_data.get('body', {}).get('illust', {}).get('total', 0)

    if not illusts_data:
        return [], False

    with get_session() as db:
        blocked = _get_blocked_tags(db)
        results = _process_items(
            db, illusts_data,
            id_extractor=lambda item: int(item['id']),
            illust_factory=_illust_from_item,
            blocked=blocked,
            min_bookmarks=min_bookmarks,
        )
        safe_commit(db)

    total_pages = min((total + PER_PAGE - 1) // PER_PAGE, max_pages) if total else max_pages
    has_more = page < total_pages
    return results, has_more


def browse_discovery(page: int = 1, sort_order: str = 'popular_d',
                     min_bookmarks: int = 0, r18_mode: str = 'all') -> tuple[list[dict], bool]:
    """浏览 Pixiv 发现页（全部作品），无需指定标签。"""
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
        return [], False

    if data.get('error'):
        logger.error(f'Discovery API error: {data.get("message")}')
        return [], False

    body = data.get('body', {})
    thumbnails = body.get('thumbnails', {}).get('illust', body.get('illusts', []))
    illusts_data = [t for t in thumbnails if not t.get('type') or t.get('type') == 'illust']
    if not illusts_data:
        return [], False

    total = body.get('total', 0)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE) if total else 1
    has_more = page < total_pages

    with get_session() as db:
        blocked = _get_blocked_tags(db)
        results = _process_items(
            db, illusts_data,
            id_extractor=lambda item: int(item['id']),
            illust_factory=_illust_from_item,
            blocked=blocked,
            min_bookmarks=min_bookmarks,
        )
        safe_commit(db)

    return results, has_more


def search_by_user(user_id: str, min_bookmarks: int = 0, page: int = 1,
                   hide_r18: bool = False) -> tuple[list[dict], bool]:
    """按用户 ID 搜索。page 从 1 开始。返回 (results, has_more)。"""
    session = _build_session()
    profile_url = f'{PIXIV_BASE_URL}/ajax/user/{user_id}/profile/all'
    try:
        resp = session.get(profile_url, timeout=DETAIL_TIMEOUT)
        resp.raise_for_status()
        profile_data = resp.json()
    except requests.RequestException as e:
        logger.error(f'User profile API failed: {e}')
        return [], False

    if profile_data.get('error'):
        logger.error(f'User profile API error: {profile_data.get("message")}')
        return [], False

    all_illusts = profile_data.get('body', {}).get('illusts', {})
    if not all_illusts:
        return [], False

    all_ids = sorted([int(iid) for iid in all_illusts.keys()], reverse=True)
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
        )
        safe_commit(db)

    max_pages = (total + PER_PAGE - 1) // PER_PAGE
    has_more = page < max_pages
    return results, has_more


def fetch_following(page: int = 1, r18_mode: str = 'all') -> tuple[list[dict], bool]:
    """获取关注画师的最新作品。"""
    session = _build_session()
    url = f'{PIXIV_BASE_URL}/ajax/follow_latest/illust?mode={r18_mode}&p={page}'
    try:
        resp = session.get(url, timeout=DETAIL_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f'Follow latest API failed: {e}')
        return [], False

    if data.get('error'):
        logger.error(f'Follow latest API error: {data.get("message")}')
        return [], False

    body = data.get('body', {})
    illusts_data = body.get('thumbnails', {}).get('illust', [])
    if not illusts_data:
        return [], False

    has_next = not body.get('page', {}).get('isLastPage', True)

    with get_session() as db:
        blocked = _get_blocked_tags(db)
        results = _process_items(
            db, illusts_data,
            id_extractor=lambda item: int(item['id']),
            illust_factory=_illust_from_item,
            blocked=blocked,
        )
        safe_commit(db)

    return results, has_next


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
