from __future__ import annotations

import json
import logging
import os
import re
import time
from base64 import urlsafe_b64encode

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from config import DOWNLOAD_DIR, MEDIUM_IMAGE_SIZE
from models import get_session, get_favorite_pids, Illust, BlockedTag, SearchCache
import fetcher
from fetcher import build_pixiv_session, _get_illust_detail
import runtime

logger = logging.getLogger(__name__)


# ── 文件系统/下载目录 ──

def _get_download_dir(pixiv_id: int) -> str:
    return os.path.join(DOWNLOAD_DIR, str(pixiv_id))


def _page_sort_key(path: str) -> tuple[int, str]:
    """按文件名页号排序：'xxx_p10.png' 应排在 '_p2' 之后（字典序会把 p10 排在 p2 前）。"""
    m = re.search(r'_p(\d+)\.', os.path.basename(path))
    return (int(m.group(1)), path) if m else (10 ** 9, path)


def _scan_local_downloads() -> dict[int, list[str]]:
    """扫描 downloads/ 目录，返回 {pixiv_id: [file_paths]}（带 TTL 缓存）。"""
    now = time.time()
    if now - runtime._scan_cache['ts'] < runtime._SCAN_CACHE_TTL:
        return runtime._scan_cache['data']
    result: dict[int, list[str]] = {}
    if not os.path.isdir(DOWNLOAD_DIR):
        return result
    for entry in os.listdir(DOWNLOAD_DIR):
        subdir = os.path.join(DOWNLOAD_DIR, entry)
        if not os.path.isdir(subdir):
            continue
        try:
            pid = int(entry)
        except ValueError:
            continue
        files = sorted(
            (os.path.join(subdir, f) for f in os.listdir(subdir)
             if os.path.isfile(os.path.join(subdir, f))),
            key=_page_sort_key,
        )
        if files:
            result[pid] = files
    runtime._scan_cache['ts'] = now
    runtime._scan_cache['data'] = result
    return result


def _build_orphan_dicts(pixiv_ids: list[int], local_items: dict[int, list[str]]) -> list[dict]:
    """为不在 DB 的本地文件构建虚拟 illust 字典。"""
    results = []
    for pid in pixiv_ids:
        paths = local_items.get(pid, [])
        if not paths:
            continue
        total_size = sum(os.path.getsize(p) for p in paths if os.path.isfile(p))
        results.append({
            'id': 0,
            'pixiv_id': pid,
            'title': str(pid),
            'user_id': 0,
            'user_name': '',
            'tags': [],
            'page_count': len(paths),
            'bookmark_count': 0,
            'upload_date': None,
            'thumb_url': '',
            'original_urls': [],
            'local_paths': paths,
            'file_count': len(paths),
            'local_urls': [f'/api/image/{pid}/{n}' for n in range(len(paths))],
            'local_dir': os.path.abspath(_get_download_dir(pid)),
            'download_status': 'done',
            'downloaded_at': None,
            'file_size': total_size,
            'is_favorite': False,
            'created_at': None,
        })
    return results


# ── 库内查询 ──

def _pid_in_clause(all_ids: list[int]) -> tuple[str, dict]:
    """把 illust_ids 分片拼进 IN 子句，避免触碰 SQLite 绑定变量上限。"""
    clauses: list[str] = []
    params: dict[str, int] = {}
    for ci, chunk in enumerate(all_ids[i:i + 500] for i in range(0, len(all_ids), 500)):
        phs = ','.join(f':pid_{ci}_{j}' for j in range(len(chunk)))
        clauses.append(f'illusts.pixiv_id IN ({phs})')
        for j, pid in enumerate(chunk):
            params[f'pid_{ci}_{j}'] = pid
    return '(' + ' OR '.join(clauses) + ')', params


def query_cached_tag(tag: str, min_bookmarks: int, sort_order: str,
                     tag_mode: str, r18_mode: str, offset: int = 0,
                     limit: int = 24, filter_tag: str = '') -> tuple[list[dict], bool, int, int]:
    """从 SearchCache + Illust 表查询预取结果，支持库内过滤排序分页。

    不限制 SearchCache.status（fetching/error 时也能查看已累积的缓存数据）。
    filter_tag: 按作品标签（Illust.tags）精确过滤，空串不过滤。
    全局屏蔽标签（BlockedTag）同搜索/图库一致生效。

    过滤/排序/分页/计数全部下推 SQLite，只取本页几行对象回内存——
    避免按标签总量全量加载（省内存，适配低内存机器）。

    Returns:
        (results_dicts, has_more, next_offset, filtered_total)
    """
    with get_session() as db:
        blocked = {t.tag for t in db.query(BlockedTag).all()}
        sc = db.query(SearchCache).filter(
            SearchCache.tag == tag,
        ).first()
        if not sc:
            return [], False, 0, 0

        try:
            all_ids = json.loads(sc.illust_ids) if sc.illust_ids else []
        except (json.JSONDecodeError, TypeError):
            all_ids = []
        if not all_ids:
            return [], False, 0, 0

        wheres: list[str] = []
        params: dict = {}
        pid_clause, pid_params = _pid_in_clause(all_ids)
        wheres.append(pid_clause)
        params.update(pid_params)
        if min_bookmarks > 0:
            wheres.append('illusts.bookmark_count >= :min_bookmarks')
            params['min_bookmarks'] = min_bookmarks
        if r18_mode == 'safe':
            r18_phs = ','.join(f':r18_{i}' for i in range(len(fetcher.R18_TAGS)))
            wheres.append(f'NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) je WHERE je.value IN ({r18_phs}))')
            params.update({f'r18_{i}': t for i, t in enumerate(fetcher.R18_TAGS)})
        if blocked:
            blk_phs = ','.join(f':blk_{i}' for i in range(len(blocked)))
            wheres.append(f'NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) je WHERE je.value IN ({blk_phs}))')
            params.update({f'blk_{i}': t for i, t in enumerate(blocked)})
        if filter_tag:
            wheres.append('EXISTS (SELECT 1 FROM json_each(illusts.tags) je WHERE je.value = :filter_tag)')
            params['filter_tag'] = filter_tag

        # 排序：date_d 时 SQLite DESC 下 NULL 沉底（与旧 Python 实现"无日期排最后"一致）
        order = 'bookmark_count DESC, illusts.id ASC' if sort_order == 'popular_d' \
            else 'upload_date DESC, illusts.id ASC'
        where_clause = ' AND '.join(wheres)

        def _run(wc: str, p: dict) -> tuple[int, list[int]]:
            page_params = {**p, 'lim': limit, 'off': offset}
            total = db.execute(text(f'SELECT COUNT(*) FROM illusts WHERE {wc}'), p).scalar() or 0
            pk_ids = db.execute(
                text(f'SELECT id FROM illusts WHERE {wc} ORDER BY {order} LIMIT :lim OFFSET :off'),
                page_params,
            ).scalars().all()
            return total, pk_ids

        try:
            total, pk_ids = _run(where_clause, params)
        except OperationalError:
            # 单条损坏 tags 会让 json_each 抛错：降级去掉标签相关过滤重查
            logger.warning('缓存查询因 tags 数据异常降级（跳过标签过滤）')
            wc2 = ' AND '.join(w for w in wheres if 'json_each' not in w)
            params2 = {k: v for k, v in params.items()
                       if k.startswith('pid_') or k == 'min_bookmarks'}
            total, pk_ids = _run(wc2, params2)

        illusts = db.query(Illust).filter(Illust.id.in_(pk_ids)).all()
        id_order = {id_: i for i, id_ in enumerate(pk_ids)}
        illusts.sort(key=lambda x: id_order.get(x.id, 0))
        page_dicts = [i.to_dict() for i in illusts]

    if page_dicts:
        with get_session() as fav_db:
            fav = get_favorite_pids(fav_db)
        for d in page_dicts:
            d['is_favorite'] = d.get('pixiv_id') in fav

    has_more = (offset + limit) < total
    next_offset = offset + limit if has_more else 0
    return page_dicts, has_more, next_offset, total


# ── URL/展示工具 ──

def _extract_ext(url: str) -> str:
    """从图片 URL 中提取文件扩展名。"""
    match = re.search(r'\.(jpg|jpeg|png|gif|webp)(?:\?|$)', url, re.IGNORECASE)
    return match.group(1) if match else 'jpg'


def _safe_int(value, default: int = 0) -> int:
    """安全整数解析：None / 空 / 非数字一律返回 default，不抛异常。"""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _original_to_resized(url: str) -> str:
    """Pixiv 原图 URL → 中图（尺寸可配）。"""
    m = re.match(r'(https://i\.pximg\.net/)img-original/img/(.+)\.(\w+)(\?.*)?$', url)
    if not m:
        return url
    size = MEDIUM_IMAGE_SIZE
    return f'{m.group(1)}c/{size}x{size}/img-master/img/{m.group(2)}_master1200.{m.group(3)}'


def _proxy_thumb(url: str) -> str:
    if not url:
        return ''
    return '/thumb/' + urlsafe_b64encode(url.encode()).decode().rstrip('=').replace('+', '-').replace('/', '_')


def _fetch_original_urls(pixiv_id: int) -> list[str]:
    """按需拉取 Pixiv 详情，返回 original_urls。用于惰性详情场景。"""
    session = build_pixiv_session()
    try:
        detail = _get_illust_detail(session, pixiv_id)
    finally:
        session.close()
    return detail.get('original_urls', []) if detail else []


def _fmt_num(n: int | str) -> str:
    if not n:
        return '0'
    n = int(n)
    return f'{n/10000:.1f}w' if n >= 10000 else str(n)


# ── 文件删除与收藏夹位置 ──

def _delete_illust_files(illust: Illust) -> int:
    """删除作品的已下载文件及目录。返回删除的文件数。"""
    paths = illust.local_paths_list or []
    deleted = 0
    for p in paths:
        try:
            if os.path.isfile(p):
                os.remove(p)
                deleted += 1
        except OSError:
            pass
    if paths:
        work_dir = os.path.dirname(paths[0])
        try:
            if os.path.isdir(work_dir) and not os.listdir(work_dir):
                os.rmdir(work_dir)
        except OSError:
            pass
    illust.download_status = None
    illust.local_paths = None
    return deleted


def _next_collection_position(db, collection_id: int) -> float:
    """计算收藏夹下一个可用位置：当前最大位置 + 1000（单语句，统一三处调用）。"""
    return float(db.execute(text(
        'SELECT COALESCE(MAX(position), 0) + 1000.0 FROM collection_items WHERE collection_id = :cid'
    ), {'cid': collection_id}).scalar() or 1000.0)


def _compute_move_position(items: list, idx: int, direction: str):
    """返回 (new_pos, needs_rebalance, error_code)。
    items 是 [(id, position), ...] 元组列表，按 position ASC 排序。
    error_code 为 None（成功）或 400（边界）。"""
    n = len(items)
    if direction == 'up':
        if idx == 0:
            return None, False, 400
        prev = items[idx - 1]
        prev_pos = prev[1]
        if idx == 1:
            return prev_pos - 1000.0, False, None
        prev_of_prev = items[idx - 2]
        pop_pos = prev_of_prev[1]
        if prev_pos - pop_pos < 1.0:
            return None, True, None
        return (pop_pos + prev_pos) / 2.0, False, None
    else:  # down
        if idx == n - 1:
            return None, False, 400
        nxt = items[idx + 1]
        nxt_pos = nxt[1]
        if idx + 1 == n - 1:
            return nxt_pos + 1000.0, False, None
        next_of_next = items[idx + 2]
        non_pos = next_of_next[1]
        if non_pos - nxt_pos < 1.0:
            return None, True, None
        return (nxt_pos + non_pos) / 2.0, False, None
