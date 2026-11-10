from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
import time
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import config
from config import DOWNLOAD_DIR, MEDIUM_IMAGE_SIZE
import pixiv_client
import runtime

# ⚠ 本模块**不能**在模块级 `from models import ...`：`models.py` 在 import 那一刻就
# `create_engine(config.DATABASE_PATH)`，于是"谁先 import 到 models"决定了 engine 绑哪个库
# —— 2026-09-30 的测试清空生产库事故正是这条链（`-p` 插件 `import helpers` → helpers →
# models → engine 绑生产库 → 夹具清表）。本模块唯一需要 DB 的 `query_cached_tag` 改成
# 函数内延迟 import，helpers 于是重新成为**叶子**（只依赖 config/runtime/pixiv_client）。
# 回归用例：tests/test_helpers.py::test_helpers_does_not_import_models_at_module_level。
if TYPE_CHECKING:   # 仅供注解可读，运行时**不**执行
    from models import Illust


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


def _atomic_write_text(path: str, text: str) -> None:
    """原子写纯文本文件：同目录 tmp → fsync → `os.replace` 覆盖（审计 S19 遗留）。

    与 `_atomic_write_json` 同款纪律（tmp 走同目录以免跨设备退化成复制、替换前
    fsync、失败清 tmp），但**刻意不去改动/复用那个函数** —— 它服务 settings.json，
    行为已稳定且有既有用例；这里唯一多出来的语义是"保留目标文件原有权限位"（见下）。

    为什么 Cookie 需要原子写：读侧 `fetcher._load_cookie()` 在**其它线程**里读同一
    路径（生产是 `gunicorn -w 1 --threads 8`）。`open(path, 'w')` 会先截断再写，
    读侧有机会读到空串，而它读到空串时会把 `_cookie_value` 置空并**连同 mtime
    一起缓存**（见 `_load_cookie`）—— 于是那条线程/那个连接池会一直用空 Cookie，
    直到文件 mtime 再变。症状是"设置页保存成功了，搜索仍 401/空结果，重启才恢复"。
    已有回归用例 `test_concurrent_reader_never_sees_a_truncated_cookie` 能在旧实现上
    复现（120 轮写入期间读到过空串）。

    权限：`open(path, 'w')` 对**已存在**的文件保留原模式，而 tmp 的模式来自 umask，
    直接 replace 会把管理员加固过的 0600 悄悄放宽成 0644 —— 所以目标已存在时把它的
    模式搬到 tmp 上（保持既有语义，不是新增收紧）。目标还不存在时保持默认模式。

    不创建父目录：调用方给的落点必须已经存在（与旧行为一致，目录写错要明确失败，
    而不是被静默补出来）。异常一律原样抛出，由调用方决定回什么错误码。
    """
    tmp_path = f'{path}.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, os.stat(path).st_mode & 0o777)
        except OSError:
            pass
        # Windows：目标文件正被读侧打开时 `os.replace` 会因共享冲突抛 PermissionError
        # （POSIX 的 rename 不受影响，所以生产 Linux 从未见过）。读侧持句柄的时间极短，
        # 有界重试即可；仍然失败就原样抛出，由调用方回 500。
        replace_error = None
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                replace_error = None
                break
            except PermissionError as e:
                replace_error = e
                time.sleep(0.02 * (attempt + 1))
        if replace_error is not None:
            raise replace_error
    finally:
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
    # 延迟 import（见文件顶部注释）：把"helpers 依赖 models"收窄到真正需要 DB 的这一个
    # 函数体内，模块 import 顺序就不再能决定 engine 绑到哪个库。
    from models import BlockedTag, Illust, SearchCache, get_session

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
            r18_phs = ','.join(f':r18_{i}' for i in range(len(pixiv_client.R18_TAGS)))
            tag_wheres.append(f'NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) je WHERE je.value IN ({r18_phs}))')
            params.update({f'r18_{i}': t for i, t in enumerate(pixiv_client.R18_TAGS)})
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

    has_more = (offset + limit) < total
    next_offset = offset + limit if has_more else 0
    return page_dicts, has_more, next_offset, total


# ── 首页热门（只读本地库）──
#
# 首页回答"最近 48 小时内收藏数最高的作品"。上游没有对应接口（popular_d 需
# Premium 且不限定时间窗），所以只能聚合本地库 —— 覆盖率来自预取 / 自动关注 /
# 手搜，这一点由空状态文案如实交代，不能让人误以为 Pixiv 上就这些。

HOME_WINDOW_HOURS = 48          # 首页默认时间窗（小时）
HOME_FALLBACK_HOURS = 168       # 不足一批时放宽到的小时数（7 天）
# 首页第二屏起「高收藏」阈值：随机推荐只抽收藏数严格大于它的缓存作品。
# 不做降级 —— 门槛是用户明确要求的，静默降到 100 等于换了个口径。
HOME_RANDOM_MIN_BOOKMARKS = 500


def _home_tag_conditions(blocked: list[str]) -> tuple[list[str], dict]:
    """首页两类查询共用的标签过滤：R18 固定隐藏 + 全局屏蔽标签。

    抽成一处是为了让"第一屏热门"和"第二屏随机"不会各自漂移 —— 两份 SQL 各写一次
    标签条件，改了一处忘了另一处是最常见的口径事故。

    返回 `(tag_wheres, params)`：条件与参数分开交给调用方，损坏 tags JSON 触发
    `OperationalError` 时可以只丢掉这一组重查（保留其余过滤）。
    """
    r18 = sorted(pixiv_client.R18_TAGS)
    r18_phs = ','.join(f':r18_{i}' for i in range(len(r18)))
    tag_wheres = [
        f'NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) je WHERE je.value IN ({r18_phs}))',
    ]
    params = {f'r18_{i}': t for i, t in enumerate(r18)}
    if blocked:
        blk_phs = ','.join(f':blk_{i}' for i in range(len(blocked)))
        tag_wheres.append(
            f'NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) je WHERE je.value IN ({blk_phs}))')
        params.update({f'blk_{i}': t for i, t in enumerate(blocked)})
    return tag_wheres, params


def home_since(hours: int) -> datetime:
    """时间窗左边界，**JST 朴素时间**。

    `illusts.upload_date` 存的是 Pixiv 的 JST 墙面时间（写库时 +09:00 偏移被
    SQLite 丢掉），直接拿 UTC now 去比会整体偏 9 小时 —— 首页窗口必须同口径。
    """
    jst_now = datetime.now(timezone.utc) + timedelta(hours=9)
    return (jst_now - timedelta(hours=hours)).replace(tzinfo=None)


def query_home_feed(limit: int = 24, window: str = 'auto') -> tuple[list[dict], int]:
    """首页「最近 N 小时热门」：只读本地库，按收藏数降序。

    只统计 `bookmark_count > 0` 的行：为 0 有两种含义（真没人收藏 / 详情从未补全），
    无法区分，而后者会让新入库作品全部沉底甚至占满一页。候选集变小，但排序可信。

    window: 'auto' —— 48 小时内不足 `limit` 件时放宽到 7 天；
            '48'   —— 禁止放宽（页面上"仍只看 48 小时"走这条）。

    只统计 `bookmark_count > 0` **且带原图地址**的行：
      - 收藏数为 0 有两种含义（真没人收藏 / 详情从未补全），无法区分，而后者会让
        新入库作品全部沉底甚至占满一页；
      - 首页要的是中图，中图地址由原图地址拼出来 —— 没有原图地址的行只能退回 250px
        方形缩略图，丢进大图瀑布流里很显眼。实测生产库里 `bookmark_count > 0` 与
        "有原图地址"完全同步（收藏数只在拉详情时写入，拉详情同时写原图），所以这条
        条件在实践中不额外排除任何作品，只是把"首页只有中图"从巧合变成保证。

    Returns: `(results_dicts, window_hours)`。`window_hours` 是**实际**窗口，
    auto 模式放宽后为 168，前端据此标注口径。
    """
    # 延迟 import（见文件顶部注释）：helpers 依赖 models 只发生在这类函数体内
    from models import BlockedTag, Illust, get_session

    with get_session() as db:
        blocked = sorted({t.tag for t in db.query(BlockedTag).all()})

        # 标签条件单独收集：单条损坏 JSON 会让 json_each 抛错，降级时只丢这一类，
        # 保留时间窗与收藏数过滤（与 query_cached_tag 同一套降级口径）。
        tag_wheres, tag_params = _home_tag_conditions(blocked)

        order = 'illusts.bookmark_count DESC, illusts.upload_date DESC, illusts.id ASC'
        base_wheres = [
            'illusts.upload_date IS NOT NULL',
            'illusts.upload_date >= :since',
            'illusts.bookmark_count > 0',
            # 中图地址从原图地址拼出来；没有原图的行走不了中图（见 docstring）
            "COALESCE(illusts.original_urls, '') NOT IN ('', '[]')",
        ]

        def _fetch(hours: int) -> list[int]:
            params = {'since': home_since(hours), 'lim': limit, **tag_params}
            sql = (f"SELECT illusts.id FROM illusts "
                   f"WHERE {' AND '.join(base_wheres + tag_wheres)} "
                   f'ORDER BY {order} LIMIT :lim')
            try:
                return db.execute(text(sql), params).scalars().all()
            except OperationalError:
                logger.warning('首页查询因 tags 数据异常降级（跳过标签过滤）')
                sql = (f"SELECT illusts.id FROM illusts "
                       f"WHERE {' AND '.join(base_wheres)} "
                       f'ORDER BY {order} LIMIT :lim')
                return db.execute(
                    text(sql), {'since': params['since'], 'lim': limit}).scalars().all()

        used_hours = HOME_WINDOW_HOURS
        ids: list[int] = []
        if limit > 0:
            ids = _fetch(HOME_WINDOW_HOURS)
            if window != '48' and len(ids) < limit:
                wider = _fetch(HOME_FALLBACK_HOURS)
                # 放宽后没有变多就维持 48 小时口径：既然没捞到新东西，
                # 没必要让页面声称"已放宽到 7 天"
                if len(wider) > len(ids):
                    ids, used_hours = wider, HOME_FALLBACK_HOURS

        if not ids:
            return [], used_hours

        illusts = db.query(Illust).filter(Illust.id.in_(ids)).all()
        id_order = {pk: i for i, pk in enumerate(ids)}
        illusts.sort(key=lambda x: id_order.get(x.id, 0))
        return [i.to_dict() for i in illusts], used_hours


def query_home_random(limit: int = 24, min_bookmarks: int = HOME_RANDOM_MIN_BOOKMARKS,
                      exclude: list[int] | None = None) -> tuple[list[dict], int]:
    """首页第二屏起：从预取缓存里随机抽「收藏数 > 阈值」的作品。

    与 `query_home_feed` 的差别只有三处，其余过滤（R18 / 屏蔽标签 / 有原图）完全一致：
      - 只要 `prefetch_source = 1` 的缓存作品，且**不限时间窗** —— 随机推荐的作用是
        把缓存里的好作品翻出来，再套 48 小时窗口池子会立刻抽空；
      - `bookmark_count > min_bookmarks`（严格大于，500 本身不进池）；
      - `ORDER BY RANDOM()`：每次请求重新洗牌。随机序不能用 offset 分页（每次顺序都
        不同，offset 会跳号），跨屏去重靠 `exclude` —— 前端把已展示的 pid 带回来，
        第一屏的热门大概率也在池子里，不排除必然重复。

    Returns: `(results, pool_total)`。`pool_total` 是排除 `exclude` 后的候选总数，
    路由用它算 `has_more`，避免"恰好抽满一屏"时前端还要多打一次空请求。
    """
    from models import BlockedTag, Illust, get_session

    excl: set[int] = set()
    for pid in exclude or []:
        try:
            excl.add(int(pid))
        except (TypeError, ValueError):
            continue   # 调用方传了脏值不该让整个首页 500，跳过即可

    with get_session() as db:
        blocked = sorted({t.tag for t in db.query(BlockedTag).all()})
        tag_wheres, tag_params = _home_tag_conditions(blocked)

        wheres = [
            'illusts.prefetch_source = 1',
            'illusts.bookmark_count > :min_bookmarks',
            # 中图地址从原图地址拼出来，没有原图只能退回 250px 缩略图（与首页同一承诺）
            "COALESCE(illusts.original_urls, '') NOT IN ('', '[]')",
        ]
        params: dict = {'min_bookmarks': min_bookmarks, **tag_params}
        if excl:
            wheres.append('illusts.pixiv_id NOT IN (SELECT value FROM json_each(:exclude_ids))')
            params['exclude_ids'] = json.dumps(sorted(excl))

        where_clause = ' AND '.join(wheres + tag_wheres)
        # 降级时只丢标签条件：来源/收藏数/原图/排除这四条都不能丢
        fallback_clause = ' AND '.join(wheres)
        fallback_params = {k: v for k, v in params.items() if k in ('min_bookmarks', 'exclude_ids')}

        def _count(wc: str, p: dict) -> int:
            return db.execute(text(f'SELECT COUNT(*) FROM illusts WHERE {wc}'), p).scalar() or 0

        def _ids(wc: str, p: dict) -> list[int]:
            return db.execute(
                text(f'SELECT illusts.id FROM illusts WHERE {wc} ORDER BY RANDOM() LIMIT :lim'),
                {**p, 'lim': limit},
            ).scalars().all()

        try:
            total = _count(where_clause, params)
            pk_ids = _ids(where_clause, params)
        except OperationalError:
            logger.warning('首页随机推荐因 tags 数据异常降级（跳过标签过滤）')
            total = _count(fallback_clause, fallback_params)
            pk_ids = _ids(fallback_clause, fallback_params)

        if not pk_ids:
            return [], total

        illusts = db.query(Illust).filter(Illust.id.in_(pk_ids)).all()
        id_order = {pk: i for i, pk in enumerate(pk_ids)}
        illusts.sort(key=lambda x: id_order.get(x.id, 0))
        return [i.to_dict() for i in illusts], total


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
    """Pixiv 原图 URL → 中图（尺寸可配）。

    ⚠️ **中图的扩展名必须固定为 `.jpg`，绝不能沿用原图的扩展名。** master1200 是
    Pixiv 统一重编码的 JPEG，与原图格式无关：原图是 PNG 的作品，`_master1200.png`
    这个地址在图床上一律 **404**（实测同一作品 `.png` → 404 / `.jpg` → 200，
    150~230KB）。曾经的实现用 `m.group(3)` 拼扩展名，于是"原图是 PNG"的作品中图全挂，
    症状是"很多作品的中图加载不出来"：
      ① 中图 404 → /thumb 记失败冷却 30s → 前端候选链退到原图代理；
      ② PNG 原图动辄 1~5MB，经代理要几秒到几十秒（实测有一张 65s），
         超过 /thumb 的 (10, 30) 读超时就直接失败；
      ③ 最后只剩 250px 缩略图兜底（第 1 页之外连兜底都没有，显示"图片加载失败"）。
    而库里 PNG 原图是多数（实测某实例 2120 条原图地址里 1375 条是 `.png`），
    所以这不是边缘情况。

    非 `img-original`（或没有扩展名）的 URL 原样返回 —— 调用方拿到的中图地址等于
    原图地址，仍可显示（前端候选链本就有原图这一档）。
    """
    m = re.match(r'(https://i\.pximg\.net/)img-original/img/(.+)\.(\w+)(\?.*)?$', url)
    if not m:
        return url
    size = MEDIUM_IMAGE_SIZE
    # 扩展名硬编码 .jpg：`m.group(3)`（原图扩展名）**故意不用**，原因见 docstring
    return f'{m.group(1)}c/{size}x{size}/img-master/img/{m.group(2)}_master1200.jpg'


def _proxy_thumb(url: str) -> str:
    if not url:
        return ''
    return '/thumb/' + urlsafe_b64encode(url.encode()).decode().rstrip('=').replace('+', '-').replace('/', '_')


def _fetch_original_urls(pixiv_id: int) -> list[str]:
    """按需拉取 Pixiv 详情，返回 original_urls。用于惰性详情场景。

    经**适配层**而不是 `fetcher`：本模块是叶子工具层，不该反向依赖业务层
    （原图地址的三条解析路径也在适配层）。地址合法性不在这里判定 —— 取回后仍由
    `check_image_url` + `config.IMAGE_HOST_ALLOWLIST` 分级决定要不要发请求。
    """
    session = pixiv_client.build_pixiv_session()
    try:
        return pixiv_client.fetch_original_urls(session, pixiv_id)
    finally:
        session.close()


def _fmt_num(n: int | str) -> str:
    if not n:
        return '0'
    n = int(n)
    return f'{n/10000:.1f}w' if n >= 10000 else str(n)


# ── 文件删除与位置计算 ──

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
