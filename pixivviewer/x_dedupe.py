"""X 作品与图库 Pixiv 作品的「同一作品」判定。

三层证据，强度完全不同，**不要混用**：

- E1 链接：推文里带了 `pixiv.net/artworks/<id>` —— 决定性，零成本。
- E2 指纹：图片 dHash 汉明距离 ≤ 阈值 —— 决定性（阈值内），代价是解码图片。
- H1 弱信号：同作者 + 时间窗 + 张数相同 —— **只能算疑似**，同作者不推出同作品，
  必须由用户在页面上确认。

所有函数只读/只写判定字段，绝不删文件、不改图库行。
"""
from __future__ import annotations

import os
import threading
from datetime import timedelta
from typing import Optional

from .models import ArtistLink, Illust, ImageHash, WorkLink, XWork
from .x_client import extract_pixiv_id

# Pixiv 的 upload_date 是 JST 墙面时间（写库时丢了偏移），tweet 的 time 是 UTC
_JST_OFFSET = timedelta(hours=9)
_AUTHOR_WINDOW = timedelta(hours=48)


class HashUnavailable(RuntimeError):
    """Pillow 未安装，E2 不可用（E1/H1 不受影响）。"""


try:                      # Pillow 是可选依赖：只影响 E2，不影响 E1/H1
    import PIL  # noqa: F401
    PIL_AVAILABLE = True
except ImportError:       # pragma: no cover - 环境相关
    PIL_AVAILABLE = False


# ── E2：感知哈希 ────────────────────────────────────────────────────────

def dhash(path: str) -> str:
    """9×8 灰度相邻像素比较，返回 16 位十六进制 dHash。

    用 Pillow 实现；没装 Pillow 抛 HashUnavailable，由调用方降级而不是崩溃。
    尺寸无关（先缩放），对重新压缩/轻微调色不敏感，正好覆盖"同一张图发两个平台"。
    """
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover - 环境相关
        raise HashUnavailable('未安装 Pillow，图像指纹去重不可用（pip install Pillow）') from e
    resample = getattr(getattr(Image, 'Resampling', None), 'LANCZOS', None) or Image.LANCZOS
    with Image.open(path) as im:
        small = im.convert('L').resize((9, 8), resample)
        pixels = list(small.getdata())
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return f'{bits:016x}'


def hamming(a: str, b: str) -> int:
    """两个 16 位十六进制哈希的汉明距离；格式不对返回一个很大的数。"""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count('1')
    except (TypeError, ValueError):
        return 999


_index_lock = threading.Lock()


def index_local_files(session, limit: int = 200) -> dict:
    """给已下载的 Pixiv 作品建指纹索引（只算首图，控制成本）。

    幂等：按 path + mtime + size 判断是否变化；文件已不在的旧行会被清掉。
    返回 {'indexed': n, 'seen': n, 'missing': n}。

    串行执行：设置页的「重建指纹索引」和后台 `_hash_index_loop` 都会调它，
    两个会话并发对同一批文件做"查重 → INSERT"会撞 `image_hashes` 主键
    （一边 flush 后另一边才查到不存在），锁住整个扫描换确定性。
    """
    with _index_lock:
        rows = (session.query(Illust)
                .filter(Illust.download_status == 'done')
                .filter(Illust.local_paths.isnot(None))
                .order_by(Illust.id.desc())
                .limit(limit * 4)          # 多取一些，跳过文件缺失/已索引的行
                .all())
        existing = {row.path: row for row in session.query(ImageHash).all()}
        indexed = seen = missing = 0
        for illust in rows:
            paths = illust.local_paths_list or []
            if not paths:
                continue
            path = paths[0]
            if not os.path.isfile(path):
                stale = existing.get(path)
                if stale is not None:
                    session.delete(stale)
                    missing += 1
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            row = existing.get(path)
            if row is not None and abs(row.mtime - stat.st_mtime) < 0.5 and row.size == stat.st_size:
                seen += 1
                continue
            try:
                digest = dhash(path)
            except (HashUnavailable, OSError, ValueError):
                continue
            if row is None:
                row = ImageHash(path=path)
                session.add(row)
            row.pixiv_id = illust.pixiv_id
            row.dhash = digest
            row.mtime = stat.st_mtime
            row.size = stat.st_size
            indexed += 1
            if indexed >= limit:
                break
        return {'indexed': indexed, 'seen': seen, 'missing': missing}


def find_hash_match(session, digest: str, max_distance: int) -> Optional[tuple[int, int]]:
    """返回 `(pixiv_id, 距离)`；没有命中返回 None。"""
    best: Optional[tuple[int, int]] = None
    for row in session.query(ImageHash).all():
        distance = hamming(row.dhash, digest)
        if distance <= max_distance and (best is None or distance < best[1]):
            best = (row.pixiv_id, distance)
    return best


def match_local_image(session, path: str, max_distance: int) -> Optional[tuple[int, int]]:
    """对磁盘上的一张 X 图片算哈希并比对索引本，返回 `(pixiv_id, 距离)`。"""
    try:
        digest = dhash(path)
    except (HashUnavailable, OSError, ValueError):
        return None
    return find_hash_match(session, digest, max_distance)


# ── E1 + H1：入库时判定 ─────────────────────────────────────────────────

def _illust_exists(session, pixiv_id: int) -> bool:
    return session.query(Illust.id).filter(Illust.pixiv_id == pixiv_id).first() is not None


def _confirmed_link(session, tweet_id: str, pixiv_id: int) -> bool:
    return (session.query(WorkLink)
            .filter(WorkLink.tweet_id == tweet_id, WorkLink.pixiv_id == pixiv_id)
            .first()) is not None


def apply_ingest_dedupe(session, work: XWork, tweet) -> None:
    """入库后立刻跑 E1 与 H1，把结论写到 `work.dup_*` 字段上。

    E1 命中 → confirmed（不管 Pixiv 侧有没有这行：链接本身就是同一作品的证据）。
    H1 命中 → suspected（只提示，绝不自动隐藏）。
    人工确认过的 `work_links` 优先于两者。
    """
    pixiv_id = extract_pixiv_id(getattr(tweet, 'expanded_urls', []), getattr(tweet, 'text', ''))
    if pixiv_id:
        work.link_pixiv_id = pixiv_id
    if pixiv_id and _confirmed_link(session, work.tweet_id, pixiv_id):
        work.dup_status = 'confirmed'
        work.dup_reason = 'manual'
        work.dup_pixiv_id = pixiv_id
        return
    if pixiv_id:
        work.dup_status = 'confirmed'
        work.dup_reason = 'link'
        work.dup_pixiv_id = pixiv_id
        return

    suspect = suspect_by_author(session, work)
    if suspect is not None:
        work.dup_status = 'suspected'
        work.dup_reason = 'author'
        work.dup_pixiv_id = suspect


def suspect_by_author(session, work: XWork) -> Optional[int]:
    """弱信号：同作者 + 时间窗 + 张数相同 → 候选 pixiv_id（仅供人工确认）。

    只比较 `page_count`，没有比宽高比：图库侧没有逐页尺寸数据，硬造一个假精度
    反而会让"疑似"看起来比实际更可信。
    """
    screen_name = (work.author_screen_name or '').strip().lower()
    if not screen_name or not work.tweeted_at:
        return None
    link = (session.query(ArtistLink)
            .filter(ArtistLink.x_screen_name == screen_name)
            .first())
    if link is None:
        return None
    # tweet 是 UTC，upload_date 是 JST 墙面时间：先换算再比，否则整体偏 9 小时
    local_time = work.tweeted_at + _JST_OFFSET
    window_start = local_time - _AUTHOR_WINDOW
    window_end = local_time + _AUTHOR_WINDOW
    candidates = (session.query(Illust)
                  .filter(Illust.user_id == link.pixiv_user_id)
                  .filter(Illust.page_count == work.media_count)
                  .filter(Illust.upload_date.isnot(None))
                  .filter(Illust.upload_date >= window_start)
                  .filter(Illust.upload_date <= window_end)
                  .all())
    if len(candidates) == 1:
        return candidates[0].pixiv_id
    return None


def confirm_pair(session, work: XWork, pixiv_id: Optional[int] = None, source: str = 'manual') -> None:
    """人工确认「同一作品」：写 work_links 并固化为 confirmed。"""
    pid = int(pixiv_id or work.dup_pixiv_id or work.link_pixiv_id or 0)
    if not pid:
        return
    if not _confirmed_link(session, work.tweet_id, pid):
        session.add(WorkLink(pixiv_id=pid, tweet_id=work.tweet_id, source=source))
    work.dup_status = 'confirmed'
    work.dup_reason = source
    work.dup_pixiv_id = pid


def ignore_pair(session, work: XWork) -> None:
    """人工否认：永久忽略，不再提示（不删任何数据）。"""
    work.dup_status = 'ignored'
    work.dup_reason = 'manual'
