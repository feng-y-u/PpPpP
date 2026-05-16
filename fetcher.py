import hashlib
import logging
import os
import time
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    COOKIE_PATH, SEARCH_PAGES, PER_PAGE,
    DETAIL_TIMEOUT, DETAIL_MAX_RETRIES,
)
from models import Illust, Setting, DeletedRecord, get_session

logger = logging.getLogger(__name__)

_cookie_mtime = 0
_cookie_value = ''


def _load_cookie():
    global _cookie_mtime, _cookie_value
    if not os.path.exists(COOKIE_PATH):
        raise FileNotFoundError(f'Cookie file not found: {COOKIE_PATH}')
    mtime = os.path.getmtime(COOKIE_PATH)
    if mtime != _cookie_mtime:
        with open(COOKIE_PATH) as f:
            _cookie_value = f.read().strip()
        _cookie_mtime = mtime


def _build_session() -> requests.Session:
    _load_cookie()
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': 'https://www.pixiv.net/',
        'Accept-Language': 'ja,zh-CN;q=0.9,zh;q=0.8,en;q=0.7',
    })
    s.cookies.set('PHPSESSID', _cookie_value, domain='.pixiv.net')
    retry = Retry(total=1, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount('https://', adapter)
    return s


def _make_search_key(search_type: str, query: str, min_bookmarks: int) -> str:
    raw = f'{search_type}:{query}:min{min_bookmarks}'
    return hashlib.md5(raw.encode()).hexdigest()


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
    if original:
        urls.append(original)
    return urls


def _get_illust_detail(session: requests.Session, pixiv_id: int) -> dict | None:
    url = f'https://www.pixiv.net/ajax/illust/{pixiv_id}'
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


def search_by_tag(keyword: str, min_bookmarks: int = 0, page: int = 1) -> tuple[list[dict], bool]:
    """Search Pixiv by tag. page is 1-based. Returns (results, has_more)."""
    if page > SEARCH_PAGES:
        return [], False

    search_key = _make_search_key('tag', keyword, min_bookmarks)
    session = _build_session()

    quoted = requests.utils.quote(keyword)
    search_url = (
        f'https://www.pixiv.net/ajax/search/illustrations/{quoted}'
        f'?word={quoted}&order=popular_d&mode=all&p={page}'
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
        .get('illustManga', {})
        .get('data', [])
    )
    total = search_data.get('body', {}).get('illustManga', {}).get('total', 0)

    if not illusts_data:
        return [], False

    with get_session() as db:
        results = []
        for item in illusts_data:
            pixiv_id = int(item['id'])

            if db.query(DeletedRecord).filter(DeletedRecord.pixiv_id == pixiv_id).first():
                continue

            existing = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
            if existing:
                if existing.bookmark_count >= min_bookmarks:
                    results.append(existing.to_dict())
                continue

            detail = _get_illust_detail(session, pixiv_id)
            if detail is None:
                continue

            if detail['bookmark_count'] < min_bookmarks:
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

            time.sleep(0.5)

        # 记录已抓取到的最大页码
        setting = db.query(Setting).filter(Setting.key == search_key).first()
        if setting:
            setting.current_page = max(setting.current_page, page)
        else:
            db.add(Setting(key=search_key, current_page=page))
        db.commit()

    total_pages = min((total + PER_PAGE - 1) // PER_PAGE, SEARCH_PAGES) if total else SEARCH_PAGES
    has_more = page < total_pages

    return results, has_more


def search_by_user(user_id: str, min_bookmarks: int = 0, page: int = 1) -> tuple[list[dict], bool]:
    """Search by user ID. page is 1-based. Returns (results, has_more)."""
    search_key = _make_search_key('user', user_id, min_bookmarks)
    session = _build_session()

    profile_url = f'https://www.pixiv.net/ajax/user/{user_id}/profile/all'
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
        for pixiv_id in page_ids:
            if db.query(DeletedRecord).filter(DeletedRecord.pixiv_id == pixiv_id).first():
                continue

            existing = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
            if existing:
                if existing.bookmark_count >= min_bookmarks:
                    results.append(existing.to_dict())
                continue

            detail = _get_illust_detail(session, pixiv_id)
            if detail is None:
                continue

            if detail['bookmark_count'] < min_bookmarks:
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

            time.sleep(0.5)

        setting = db.query(Setting).filter(Setting.key == search_key).first()
        if setting:
            setting.current_page = max(setting.current_page, page)
        else:
            db.add(Setting(key=search_key, current_page=page))
        db.commit()

    max_pages = (total + PER_PAGE - 1) // PER_PAGE
    has_more = page < max_pages

    return results, has_more


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
