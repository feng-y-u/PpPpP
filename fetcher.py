import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import socket
from urllib3.connection import HTTPSConnection, _ssl_wrap_socket_and_match_hostname
from urllib3.connectionpool import HTTPSConnectionPool

from config import (
    COOKIE_PATH, PIXIV_BASE_URL, SEARCH_PAGES, PER_PAGE,
    DETAIL_TIMEOUT, DETAIL_MAX_RETRIES, FETCH_DETAIL_WORKERS,
    PROXY, BYPASS_SNI, PIXIV_ORIGIN_IPS,
)
from models import Illust, BlockedTag, get_session, safe_commit

logger = logging.getLogger(__name__)

_cookie_mtime = 0
_cookie_value = ''
_pixiv_hostname = urlparse(PIXIV_BASE_URL).hostname or 'www.pixiv.net'


def _load_cookie():
    global _cookie_mtime, _cookie_value
    if not os.path.exists(COOKIE_PATH):
        raise FileNotFoundError(f'Cookie file not found: {COOKIE_PATH}')
    mtime = os.path.getmtime(COOKIE_PATH)
    if mtime != _cookie_mtime:
        with open(COOKIE_PATH) as f:
            raw = f.read().strip()
        # 支持两种格式: "PHPSESSID=xxxxx" 或纯 "xxxxx"
        if raw.startswith('PHPSESSID='):
            _cookie_value = raw.split('=', 1)[1]
        else:
            _cookie_value = raw
        _cookie_mtime = mtime


def _build_session() -> requests.Session:
    _load_cookie()
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': '{PIXIV_BASE_URL}/',
        'Accept-Language': 'ja,zh-CN;q=0.9,zh;q=0.8,en;q=0.7',
    })
    s.headers.update({'Cookie': f'PHPSESSID={_cookie_value}'})
    s.cookies.set('PHPSESSID', _cookie_value, domain=_pixiv_hostname)
    s.verify = False

    if PROXY:
        s.proxies = {'https': PROXY, 'http': PROXY}

    if BYPASS_SNI:
        adapter = _create_sni_bypass_adapter()
    else:
        adapter = HTTPAdapter()

    retry = Retry(total=1, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503])
    adapter.max_retries = retry

    s.mount('https://', adapter)
    return s


def _probe_pixiv_ip() -> str | None:
    """Test Pixiv origin IPs and return the first reachable one."""
    for ip in PIXIV_ORIGIN_IPS:
        try:
            sock = socket.create_connection((ip, 443), timeout=3)
            sock.close()
            logger.info(f'SNI bypass: using Pixiv IP {ip}')
            return ip
        except OSError:
            continue
    logger.warning('SNI bypass: no Pixiv origin IP is reachable')
    return None


_pixiv_bypass_ip = None


def _create_sni_bypass_adapter() -> HTTPAdapter:
    """Create an HTTPAdapter that connects to hardcoded Pixiv origin IPs
    and omits SNI from the TLS handshake to bypass GFW detection."""
    global _pixiv_bypass_ip

    if _pixiv_bypass_ip is None:
        _pixiv_bypass_ip = _probe_pixiv_ip()
    ip = _pixiv_bypass_ip
    if not ip:
        return HTTPAdapter()

    class BypassHTTPSConnection(HTTPSConnection):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.server_hostname = ''
            self.assert_hostname = False

        def connect(self):
            self.sock = socket.create_connection(
                (ip, self.port),
                timeout=self.timeout,
                source_address=self.source_address,
                socket_options=self.socket_options,
            )
            if self._tunnel_host:
                self._tunnel()
            server_hostname_rm_dot = (self.server_hostname or '').rstrip('.')
            sock_and_verified = _ssl_wrap_socket_and_match_hostname(
                sock=self.sock,
                cert_reqs=self.cert_reqs,
                ssl_version=self.ssl_version,
                ssl_minimum_version=self.ssl_minimum_version,
                ssl_maximum_version=self.ssl_maximum_version,
                ca_certs=self.ca_certs,
                ca_cert_dir=self.ca_cert_dir,
                ca_cert_data=self.ca_cert_data,
                cert_file=self.cert_file,
                key_file=self.key_file,
                key_password=self.key_password,
                server_hostname=server_hostname_rm_dot,
                ssl_context=self.ssl_context,
                tls_in_tls=False,
                assert_hostname=False,
                assert_fingerprint=self.assert_fingerprint,
            )
            self.sock = sock_and_verified.socket
            self.is_verified = sock_and_verified.is_verified
            self._has_connected_to_proxy = bool(self.proxy)
            if self._has_connected_to_proxy and self.proxy_is_verified is None:
                self.proxy_is_verified = sock_and_verified.is_verified

    class BypassPool(HTTPSConnectionPool):
        connection_cls = BypassHTTPSConnection

    class BypassAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme['https'] = BypassPool

    return BypassAdapter()


def _split_tags(keyword: str) -> list[str]:
    """Split by comma only. Spaces within each tag are preserved."""
    raw = keyword.replace('，', ',').strip()
    parts = [t.strip() for t in raw.split(',') if t.strip()]
    return parts if parts else [raw]


def _get_blocked_tags(db) -> set[str]:
    return {t.tag for t in db.query(BlockedTag).all()}


def _is_blocked(tags: list[str], blocked: set[str]) -> bool:
    if not blocked:
        return False
    return bool(set(tags) & blocked)


R18_TAGS = {"R-18", "R-18G"}


def _is_r18(tags: list[str]) -> bool:
    return bool(set(tags) & R18_TAGS)


def _parse_tags(tags_data) -> list[str]:
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
    # Pixiv removed metaPages from the API; construct page URLs from _p0 pattern
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
            }
        except requests.RequestException as e:
            logger.warning(f'Detail API attempt {attempt + 1} failed for {pixiv_id}: {e}')
            if attempt < DETAIL_MAX_RETRIES:
                time.sleep(1)
    return None


def _fetch_details_parallel(pixiv_ids: list[int]) -> dict[int, dict]:
    """Fetch illust details in parallel. Returns {pixiv_id: detail}."""
    if not pixiv_ids:
        return {}

    results = {}

    def _worker(pid):
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


def search_by_tag(keyword: str, min_bookmarks: int = 0, page: int = 1,
                  sort_order: str = 'popular_d', max_pages: int = 10,
                  tag_mode: str = 'or', r18_mode: str = 'all') -> tuple[list[dict], bool]:
    """Search Pixiv by tag(s). tag_mode: 'or' = any tag, 'and' = all tags."""
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
        results = []
        to_fetch = []  # (pixiv_id, item_dict)
        blocked = _get_blocked_tags(db)

        # 第一遍：分离已入库和需抓取的作品
        for item in illusts_data:
            pixiv_id = int(item['id'])

            existing = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
            if existing:
                if existing.bookmark_count >= min_bookmarks and not _is_blocked(existing.tags_list, blocked):
                    results.append(existing.to_dict())
                continue

            to_fetch.append(pixiv_id)

        # 并行获取详情
        if to_fetch:
            details = _fetch_details_parallel(to_fetch)
            for pixiv_id in to_fetch:
                detail = details.get(pixiv_id)
                if detail is None or detail['bookmark_count'] < min_bookmarks:
                    continue
                if _is_blocked(detail.get('tags', []), blocked):
                    continue

                item = next((i for i in illusts_data if int(i['id']) == pixiv_id), None)
                if item is None:
                    continue

                illust = Illust(
                    pixiv_id=pixiv_id,
                    title=item.get('title', ''),
                    user_id=int(item.get('userId', 0)),
                    user_name=item.get('userName', ''),
                    page_count=item.get('pageCount', 1),
                    bookmark_count=detail['bookmark_count'],
                    thumb_url=item.get('url', ''),
                    upload_date=_parse_date(item.get('updateDate')),
                )
                illust.tags_list = _parse_tags(item.get('tags', []))
                illust.original_urls_list = detail['original_urls']
                db.add(illust)
                db.flush()
                results.append(illust.to_dict())

        safe_commit(db)

    total_pages = min((total + PER_PAGE - 1) // PER_PAGE, max_pages) if total else max_pages
    has_more = page < total_pages

    return results, has_more


def browse_discovery(page: int = 1, sort_order: str = 'popular_d',
                     min_bookmarks: int = 0, r18_mode: str = 'all') -> tuple[list[dict], bool]:
    """Browse Pixiv discovery (all works) without a specific tag."""
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
        results = []
        to_fetch = []
        blocked = _get_blocked_tags(db)

        for item in illusts_data:
            pixiv_id = int(item['id'])

            existing = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
            if existing:
                if existing.bookmark_count >= min_bookmarks and not _is_blocked(existing.tags_list, blocked):
                    results.append(existing.to_dict())
                continue

            to_fetch.append(pixiv_id)

        if to_fetch:
            details = _fetch_details_parallel(to_fetch)
            for pixiv_id in to_fetch:
                detail = details.get(pixiv_id)
                if detail is None or detail['bookmark_count'] < min_bookmarks:
                    continue
                if _is_blocked(detail.get('tags', []), blocked):
                    continue

                item = next((i for i in illusts_data if int(i['id']) == pixiv_id), None)
                if item is None:
                    continue

                illust = Illust(
                    pixiv_id=pixiv_id,
                    title=item.get('title', ''),
                    user_id=int(item.get('userId', 0)),
                    user_name=item.get('userName', ''),
                    page_count=item.get('pageCount', 1),
                    bookmark_count=detail['bookmark_count'],
                    thumb_url=item.get('url', ''),
                    upload_date=_parse_date(item.get('updateDate')),
                )
                illust.tags_list = _parse_tags(item.get('tags', []))
                illust.original_urls_list = detail['original_urls']
                db.add(illust)
                db.flush()
                results.append(illust.to_dict())

        safe_commit(db)

    return results, has_more


def search_by_user(user_id: str, min_bookmarks: int = 0, page: int = 1,
                    hide_r18: bool = False) -> tuple[list[dict], bool]:
    """Search by user ID. page is 1-based. Returns (results, has_more)."""
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
        results = []
        to_fetch = []
        blocked = _get_blocked_tags(db)

        # 第一遍：分离已入库和需抓取的作品
        for pixiv_id in page_ids:
            existing = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
            if existing:
                if existing.bookmark_count >= min_bookmarks and not _is_blocked(existing.tags_list, blocked):
                    if not (hide_r18 and _is_r18(existing.tags_list)):
                        results.append(existing.to_dict())
                continue

            to_fetch.append(pixiv_id)

        # 并行获取详情
        if to_fetch:
            details = _fetch_details_parallel(to_fetch)
            for pixiv_id in to_fetch:
                detail = details.get(pixiv_id)
                if detail is None or detail['bookmark_count'] < min_bookmarks:
                    continue
                if _is_blocked(detail.get('tags', []), blocked):
                    continue
                if hide_r18 and _is_r18(detail.get('tags', [])):
                    continue

                illust = Illust(
                    pixiv_id=pixiv_id,
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
                db.add(illust)
                db.flush()
                results.append(illust.to_dict())

        safe_commit(db)

    max_pages = (total + PER_PAGE - 1) // PER_PAGE
    has_more = page < max_pages

    return results, has_more


def fetch_following(page: int = 1, r18_mode: str = 'all') -> tuple[list[dict], bool]:
    """Fetch latest works from followed artists."""
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
        results = []
        to_fetch = []
        blocked = _get_blocked_tags(db)

        for item in illusts_data:
            pixiv_id = int(item['id'])

            existing = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
            if existing:
                if not _is_blocked(existing.tags_list, blocked):
                    results.append(existing.to_dict())
                continue

            to_fetch.append(pixiv_id)

        if to_fetch:
            details = _fetch_details_parallel(to_fetch)
            for pixiv_id in to_fetch:
                detail = details.get(pixiv_id)
                if detail is None:
                    continue
                if _is_blocked(detail.get('tags', []), blocked):
                    continue

                item = next((i for i in illusts_data if int(i['id']) == pixiv_id), None)
                if item is None:
                    continue

                illust = Illust(
                    pixiv_id=pixiv_id,
                    title=item.get('title', ''),
                    user_id=int(item.get('userId', 0)),
                    user_name=item.get('userName', ''),
                    page_count=item.get('pageCount', 1),
                    bookmark_count=detail['bookmark_count'],
                    thumb_url=item.get('url', ''),
                    upload_date=_parse_date(item.get('updateDate')),
                )
                illust.tags_list = _parse_tags(item.get('tags', []))
                illust.original_urls_list = detail['original_urls']
                db.add(illust)
                db.flush()
                results.append(illust.to_dict())

        safe_commit(db)

    return results, has_next


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
