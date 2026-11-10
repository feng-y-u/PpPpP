# ── 首页（最近 48 小时热门）路由 ──
# /api/home：只读本地库，按收藏数取最近时间窗内的一批作品。
# 页面 / 由 app.py 渲染 templates/home.html。
from __future__ import annotations

import threading
import time

from flask import Blueprint, Response, jsonify, request

from config import ITEMS_PER_PAGE
from helpers import HOME_WINDOW_HOURS, _original_to_resized, _proxy_thumb, query_home_feed

bp = Blueprint('home', __name__)

# 60 秒进程内缓存：首页每次刷新都扫库+排序，单人自部署虽然不疼，但图片懒加载
# 会让页面在短时间内被反复请求。代价是"收藏数变了要等一分钟"，可接受 ——
# 屏蔽标签增删会主动清缓存（见 clear_home_cache 的调用点）。
_HOME_CACHE_TTL = 60.0
_home_cache: dict[str, tuple[float, dict]] = {}
_home_cache_lock = threading.Lock()


def clear_home_cache() -> None:
    """清空首页缓存：屏蔽标签变了必须立刻生效，不能等 TTL。"""
    with _home_cache_lock:
        _home_cache.clear()


def _build_payload(window: str) -> dict:
    # 页大小跟随全局设置（与图库一致）：改 items_per_page 后首页也变
    results, used_hours = query_home_feed(ITEMS_PER_PAGE, window=window)
    for r in results:
        # 瀑布流要的是中图（master1200），不是列表接口那个 250px 方形缩略图。
        # 走详情页同一条链路：原图地址 → `_original_to_resized`（扩展名恒 .jpg）→ `/thumb` 代理。
        # 没有原图地址（defer 入库、后台补全尚未跑到的行）就退回缩略图 —— 首页不主动
        # 拉 Pixiv 详情，宁可这张先用缩略图，也不为了画质偷偷发请求。
        originals = r.get('original_urls') or []
        medium = _original_to_resized(originals[0]) if originals else ''
        r['medium_url'] = _proxy_thumb(medium) if medium else _proxy_thumb(r.get('thumb_url') or '')
    return {
        'results': results,
        'window_hours': used_hours,
        'requested_window': window,
        'fell_back': used_hours != HOME_WINDOW_HOURS,
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
