# ── 首页（最近 48 小时热门 + 缓存随机推荐）路由 ──
# /api/home：只读本地库，按收藏数取最近时间窗内的一批作品（第一屏，60 秒缓存）。
# /api/home/more：第一屏之后，从预取缓存里随机抽高收藏作品（每次重新洗牌，不缓存）。
# 页面 / 由 app.py 渲染 templates/home.html。
from __future__ import annotations

import threading
import time

from flask import Blueprint, Response, jsonify, request

from config import ITEMS_PER_PAGE
from helpers import (
    HOME_RANDOM_MIN_BOOKMARKS, HOME_WINDOW_HOURS,
    _original_to_resized, _proxy_thumb, query_home_feed, query_home_random,
)
from middleware import _csrf_required

bp = Blueprint('home', __name__)

# 60 秒进程内缓存：首页每次刷新都扫库+排序，单人自部署虽然不疼，但图片懒加载
# 会让页面在短时间内被反复请求。代价是"收藏数变了要等一分钟"，可接受 ——
# 屏蔽标签增删会主动清缓存（见 clear_home_cache 的调用点）。
# 只服务第一屏：随机推荐接口不能缓存，否则"随机"会在一分钟内定住。
_HOME_CACHE_TTL = 60.0
_home_cache: dict[str, tuple[float, dict]] = {}
_home_cache_lock = threading.Lock()

# 随机推荐单次最多返回多少条：前端每屏 24，给一倍余量但不允许一次拉爆。
_MORE_MAX_LIMIT = 60
# exclude 上限：列表随滚动增长，这里只防异常巨大的请求体（正常几千条足够）。
_MORE_MAX_EXCLUDE = 5000


def clear_home_cache() -> None:
    """清空首页缓存：屏蔽标签变了必须立刻生效，不能等 TTL。"""
    with _home_cache_lock:
        _home_cache.clear()


def _attach_medium(results: list[dict]) -> list[dict]:
    """给每个作品挂上瀑布流要的中图地址（master1200 的 `/thumb` 代理）。

    走详情页同一条链路：原图地址 → `_original_to_resized`（扩展名恒 .jpg）→ `/thumb`。
    没有原图地址（defer 入库、后台补全尚未跑到的行）就退回缩略图 —— 首页不主动
    拉 Pixiv 详情，宁可这张先用缩略图，也不为了画质偷偷发请求。
    """
    for r in results:
        originals = r.get('original_urls') or []
        medium = _original_to_resized(originals[0]) if originals else ''
        r['medium_url'] = _proxy_thumb(medium) if medium else _proxy_thumb(r.get('thumb_url') or '')
    return results


def _build_payload(window: str) -> dict:
    # 页大小跟随全局设置（与图库一致）：改 items_per_page 后首页也变
    results, used_hours = query_home_feed(ITEMS_PER_PAGE, window=window)
    return {
        'results': _attach_medium(results),
        'window_hours': used_hours,
        'requested_window': window,
        'fell_back': used_hours != HOME_WINDOW_HOURS,
        # 前端靠它标注"第 N 张起为随机推荐（收藏数 > X）"，避免门槛在前端再写一份
        'random_min_bookmarks': HOME_RANDOM_MIN_BOOKMARKS,
    }


@bp.route('/api/home')
def api_home() -> Response:
    """首页数据：`?window=48` 禁止放宽到 7 天，其余一律 auto。

    未知取值不报 400：前端拼错参数不该让整页空白，退化成默认行为即可。
    """
    window = '48' if request.args.get('window') == '48' else 'auto'

    now = time.time()
    with _home_cache_lock:
        hit = _home_cache.get(window)
        if hit is not None and now - hit[0] < _HOME_CACHE_TTL:
            return jsonify(hit[1])

    # 锁外查库：并发请求各查一次可以接受，卡住别的请求不行
    payload = _build_payload(window)
    with _home_cache_lock:
        _home_cache[window] = (time.time(), payload)
    return jsonify(payload)


@bp.route('/api/home/more', methods=['POST'])
@_csrf_required
def api_home_more() -> Response:
    """第一屏之后：从预取缓存里随机抽高收藏作品。

    用 POST 而不是 GET：`exclude` 随滚动不断变长（每屏 24 个 id），挂在 query string
    上会撞反代默认 8KB 的请求头上限（nginx `large_client_header_buffers`）。

    body: `{"exclude": [已展示的 pixiv_id...], "limit": 每屏条数}`。
    `has_more` 由排除后的候选总数算出，前端据此决定继续观察还是收尾。
    """
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}

    try:
        limit = int(body.get('limit', ITEMS_PER_PAGE))
    except (TypeError, ValueError):
        limit = ITEMS_PER_PAGE   # body 里脏值不该让首页整段空白
    limit = max(1, min(limit, _MORE_MAX_LIMIT))

    exclude: list[int] = []
    raw_exclude = body.get('exclude')
    if isinstance(raw_exclude, list):
        for item in raw_exclude[:_MORE_MAX_EXCLUDE]:
            try:
                exclude.append(int(item))
            except (TypeError, ValueError):
                continue    # 单个坏 id 忽略，不影响其余去重

    results, pool_total = query_home_random(limit, HOME_RANDOM_MIN_BOOKMARKS, exclude)
    return jsonify({
        'results': _attach_medium(results),
        'min_bookmarks': HOME_RANDOM_MIN_BOOKMARKS,
        'has_more': pool_total > len(results),
    })
