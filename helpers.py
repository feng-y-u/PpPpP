from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
import time
from base64 import urlsafe_b64encode
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import config
from config import DOWNLOAD_DIR, MEDIUM_IMAGE_SIZE
from models import get_session, get_favorite_pids, Illust, BlockedTag, SearchCache
import fetcher
from fetcher import build_pixiv_session, _get_illust_detail
import runtime

logger = logging.getLogger(__name__)


# ── 文件系统/下载目录 ──

_image_cache_lock = threading.Lock()
_image_cache_last_scan = 0.0


def _atomic_write_json(path: str, data) -> None:
    """原子写 JSON 配置文件：同目录写 tmp → fsync → `os.replace` 覆盖（审计 S16）。

    为什么不能直接 `open(path, 'w')` + `json.dump`：那样写盘不是原子的，进程被 kill /
    磁盘写满 / 断电时会留下**截断的 settings.json**。而读者（`config.py` import 时覆盖
    常量、`_load_settings()`）碰到解析失败只能整体回退默认值 —— 用户刚改的一整份配置
    （预取标签参数、下载线程数、代理……）就全丢了，且文件内容是不可解析的半份。
    `os.replace` 在同一目录内是原子替换：读到的要么是旧内容、要么是新内容。

    `flush` + `fsync` 在替换之前：否则断电后可能出现「文件名已更新、内容还是空的」。
    异常一律原样抛出（不吞），由调用方决定回什么错误码。

    tmp 走同目录（`<path>.tmp`）而不是系统临时目录：跨设备时 `os.replace` 会退化成
    复制+删除，就不再是原子的。
    """
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp_path = f'{path}.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        # 失败路径（open/dump/fsync/replace 任一抛）都要清掉残留 tmp：留着它会让下次
        # 写入踩到半份内容，用户目录里也会多一个含义不明的文件。
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def enforce_image_cache_limit(cache_dir: str, force: bool = False) -> int:
    """把缩略图磁盘缓存压回容量上限，返回删除的字节数。

    该目录过去只写不删，磁盘会无限增长。按 mtime 从旧到新淘汰，直到总量回落到
    `IMAGE_CACHE_MAX_BYTES * IMAGE_CACHE_TARGET_RATIO`。

    注意是"最旧写入优先"，不是严格 LRU：命中缓存时**不**刷新 mtime，否则 ETag
    会跟着变，让浏览器那 7 天的本地缓存整体失效。代价是长期被看的旧图偶尔会被
    淘汰一次，随后自动重新成为最新的，会自我修正。

    只删除本缓存自己写的文件（32 位十六进制 md5 名，可带 .meta 后缀），目录里
    的其他文件一律不动。

    扫描要遍历整个目录，因此受 `IMAGE_CACHE_CLEANUP_INTERVAL` 节流；并发调用
    以非阻塞方式抢锁，抢不到就直接跳过（已有线程在扫）。`force=True` 跳过节流，
    用于启动时的兜底清理。
    """
    global _image_cache_last_scan

    now = time.time()
    if not _image_cache_lock.acquire(blocking=False):
        return 0
    try:
        if not force and now - _image_cache_last_scan < config.IMAGE_CACHE_CLEANUP_INTERVAL:
            return 0
        _image_cache_last_scan = now

        try:
            scanner = os.scandir(cache_dir)
        except OSError:
            return 0

        entries: list[tuple[float, int, str]] = []
        total = 0
        with scanner:
            for entry in scanner:
                try:
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                name = entry.name
                stem = name[:-5] if name.endswith('.meta') else name
                base = stem.rpartition('.')[0]
                # 安全兜底：不是本缓存命名格式的文件绝不碰
                if len(base) != 32:
                    continue
                try:
                    int(base, 16)
                except ValueError:
                    continue
                entries.append((st.st_mtime, st.st_size, entry.path))
                total += st.st_size

        if total <= config.IMAGE_CACHE_MAX_BYTES:
            return 0

        target = int(config.IMAGE_CACHE_MAX_BYTES * config.IMAGE_CACHE_TARGET_RATIO)
        entries.sort()  # mtime 升序，最旧的在前

        removed = 0
        for _mtime, size, path in entries:
            if total <= target:
                break
            try:
                os.remove(path)
            except OSError:
                continue
            total -= size
            removed += size
            if not path.endswith('.meta'):
                try:
                    os.remove(path + '.meta')
                except OSError:
                    pass

        if removed:
            logger.info(f'缩略图缓存超限，已淘汰 {removed / 1024 / 1024:.1f} MB '
                        f'（上限 {config.IMAGE_CACHE_MAX_BYTES / 1024 / 1024:.0f} MB）')
        return removed
    finally:
        _image_cache_lock.release()


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
    # 先写 data 再写 ts：反序会在两者之间留出一个"时间戳已刷新、数据仍是旧值"
    # 的窗口，并发请求读到旧数据却认为它新鲜，要等满 TTL 才纠正。
    runtime._scan_cache['data'] = result
    runtime._scan_cache['ts'] = now
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

def _pid_filter(all_ids: list[int]) -> tuple[str, dict]:
    """把缓存 id 数组下推成 WHERE 条件。

    用 `json_each` 把整个数组作为**一个**绑定参数交给 SQLite，而不是拼分块
    IN：后者在 8000 个 id 时要生成 16k 个绑定参数，实测 64ms → 7ms（快 8.8 倍）。
    JSON1 扩展在本项目已被标签过滤大量使用，可放心依赖。
    """
    return ('illusts.pixiv_id IN (SELECT value FROM json_each(:cached_ids))',
            {'cached_ids': json.dumps(all_ids)})


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

        pid_clause, params = _pid_filter(all_ids)
        wheres: list[str] = [pid_clause]
        # 依赖 illusts.tags 的条件单独收集：单条损坏 JSON 会让 json_each 抛错，
        # 降级时只丢这一类，保留 id 过滤与收藏数过滤。
        tag_wheres: list[str] = []
        if min_bookmarks > 0:
            wheres.append('illusts.bookmark_count >= :min_bookmarks')
            params['min_bookmarks'] = min_bookmarks
        if r18_mode == 'safe':
            r18_phs = ','.join(f':r18_{i}' for i in range(len(fetcher.R18_TAGS)))
            tag_wheres.append(f'NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) je WHERE je.value IN ({r18_phs}))')
            params.update({f'r18_{i}': t for i, t in enumerate(fetcher.R18_TAGS)})
        if blocked:
            blk_phs = ','.join(f':blk_{i}' for i in range(len(blocked)))
            tag_wheres.append(f'NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) je WHERE je.value IN ({blk_phs}))')
            params.update({f'blk_{i}': t for i, t in enumerate(blocked)})
        if filter_tag:
            tag_wheres.append('EXISTS (SELECT 1 FROM json_each(illusts.tags) je WHERE je.value = :filter_tag)')
            params['filter_tag'] = filter_tag

        # 排序：date_d 时 SQLite DESC 下 NULL 沉底（与旧 Python 实现"无日期排最后"一致）
        order = 'bookmark_count DESC, illusts.id ASC' if sort_order == 'popular_d' \
            else 'upload_date DESC, illusts.id ASC'
        where_clause = ' AND '.join(wheres + tag_wheres)

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
            # 单条损坏 tags 会让 json_each 抛错：降级只丢标签相关条件重查
            logger.warning('缓存查询因 tags 数据异常降级（跳过标签过滤）')
            total, pk_ids = _run(
                ' AND '.join(wheres),
                {k: v for k, v in params.items() if k in ('cached_ids', 'min_bookmarks')},
            )

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

def check_image_url(url: str) -> tuple[str, str | None]:
    """解析并校验一个图片地址，返回 `(host, 拒绝原因)`；原因为 None 表示允许请求。

    硬性条件（下载引擎与缩略图重定向共用同一份判定）：
      - 必须 `https`（明文会让中间人直接替换图片内容）
      - 不得含 userinfo（`https://user@host/` 会让凭据随 URL 走）
      - 端口必须是 443 或省略
      - 主机名不得是 `localhost` / `*.local` / `*.internal`
      - 主机是 IP 字面量时必须是**公网地址**（封死 127/8、10/8、172.16/12、
        192.168/16、169.254/16 云元数据端点、::1、fc00::/7、fe80::/10）

    刻意**不做**域名解析：DNS 结果随时可变，靠"解析后再比对 IP"做 SSRF 防护既
    不可靠又慢；这里只封"字面量内网地址"，域名侧交给证书校验与白名单分级
    （白名单外的公网主机用无凭据会话访问）。
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return '', '地址无法解析'
    if parts.scheme != 'https':
        return '', f'非 https（{parts.scheme or "无 scheme"}）'
    if '@' in parts.netloc:
        return '', '地址含 userinfo'
    host = (parts.hostname or '').lower()
    if not host:
        return '', '缺少主机名'
    try:
        port = parts.port
    except ValueError:
        return host, '端口非法'
    if port not in (None, 443):
        return host, f'非 443 端口（{port}）'
    if host == 'localhost' or host.endswith(('.local', '.internal')):
        return host, '本机/内网主机名'
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host, None
    if not ip.is_global:
        return host, f'非公网地址（{ip}）'
    return host, None


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


def _delete_orphan_files(pixiv_id: int) -> int:
    """删除无 DB 记录的孤儿作品目录（downloads/<pid>）。返回删除的文件数。

    孤儿 = 本地有下载目录但 Illust 表无对应行（DB 重置/丢行等原因产生）。
    目录内只删文件，删空后移除目录本身，非空目录保留（与 _delete_illust_files
    同样的保守策略）。
    """
    work_dir = _get_download_dir(pixiv_id)
    if not os.path.isdir(work_dir):
        return 0
    deleted = 0
    for name in os.listdir(work_dir):
        p = os.path.join(work_dir, name)
        try:
            if os.path.isfile(p):
                os.remove(p)
                deleted += 1
        except OSError:
            pass
    try:
        if not os.listdir(work_dir):
            os.rmdir(work_dir)
    except OSError:
        pass
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
