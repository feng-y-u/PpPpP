# ── 搜索 / 缓存浏览 / 关注 路由 ──
# 异步搜索任务（_submit_search_task / _cleanup_search_tasks）与
# /search、/api/search/status、/api/cache/*、/api/following 路由。
from __future__ import annotations

import logging
import secrets
import threading
import time

from flask import Blueprint, Response, jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import fetcher
from background import _remove_pids_from_search_caches
from config import ITEMS_PER_PAGE, MAX_BOOKMARKS_DEFAULT
from fetcher import PixivAuthError, decode_cursor, fetch_following
from helpers import _safe_int, query_cached_tag
from middleware import _csrf_required
from models import CollectionItem, Illust, SearchCache, get_session, safe_commit
from runtime import _search_tasks, _search_tasks_lock

logger = logging.getLogger(__name__)

bp = Blueprint('search', __name__)


# ── 异步搜索任务 ──
# 搜索（含限速拉取详情）在后台线程执行，/search 立即返回 task_id，
# 前端轮询 /api/search/status/<id>。gunicorn -w 1 下搜索不再阻塞其他请求。

def _cleanup_search_tasks() -> None:
    import app  # 延迟导入读取 app.SEARCH_TASK_TTL：tests monkeypatch('app.SEARCH_TASK_TTL')
    #             （test_app.py TTL 清理用例），from runtime import 的独立绑定看不到该补丁
    now = time.time()
    with _search_tasks_lock:
        for tid in [t for t, v in _search_tasks.items()
                    if v['status'] in ('done', 'error', 'cancelled')
                    and v.get('finished_at') and now - v['finished_at'] > app.SEARCH_TASK_TTL]:
            del _search_tasks[tid]


def _submit_search_task(fn) -> str:
    """提交搜索任务到后台线程，返回 task_id。

    fn 返回元组 (results, cursor, has_more)。
    提交即取消所有在途任务：单人应用同时只该有一个搜索在跑，旧任务继续
    拉详情只会烧令牌桶（每次详情 1.33s），把新搜索拖得更慢。
    """
    _cleanup_search_tasks()
    with _search_tasks_lock:
        for other in _search_tasks.values():
            ev = other.get('cancel_event')
            if other['status'] == 'running' and ev is not None:
                ev.set()
    task_id = secrets.token_hex(8)
    task: dict = {
        'status': 'running',
        'results': [],
        'cursor': None,
        'has_more': False,
        'error': None,
        'fetch_stats': {},
        'created_at': time.time(),
        'finished_at': None,
        'cancel_event': threading.Event(),
    }
    with _search_tasks_lock:
        _search_tasks[task_id] = task

    def _run() -> None:
        try:
            # 取消事件绑定到本线程（fetcher 的检查点只认任务线程自己的事件），
            # fn 全程结束后必须解绑 —— 线程池线程会被复用，残留状态会污染下一个任务
            fetcher._cancel_begin(task['cancel_event'].is_set)
            try:
                results, next_cursor, has_more = fn()
            finally:
                fetcher._cancel_end()
            task['results'] = results
            task['cursor'] = next_cursor
            task['has_more'] = has_more
            task['fetch_stats'] = fetcher.get_last_fetch_stats()
            task['status'] = 'done'
            logger.info(
                f'[search] 完成 task={task_id} results={len(task["results"])} has_more={task["has_more"]} '
                f'details={task["fetch_stats"].get("detail_fetched", 0)} '
                f'failed={task["fetch_stats"].get("detail_failed", 0)} '
                f'seconds={(time.time() - task["created_at"]):.1f}'
            )
        except fetcher.SearchCancelledError:
            logger.info(f'[search] 取消 task={task_id}（被新搜索取代）')
            task['status'] = 'cancelled'
        except PixivAuthError:
            logger.warning(f'搜索任务 {task_id} 认证失败')
            task['status'] = 'error'
            task['error'] = 'auth'
        except FileNotFoundError as e:
            logger.error(f'搜索任务 {task_id} 文件缺失：{e}')
            task['status'] = 'error'
            task['error'] = f'缺少文件: {e}'
        except Exception as e:
            logger.error(f'搜索任务 {task_id} 失败：{e}', exc_info=True)
            task['status'] = 'error'
            task['error'] = '搜索服务暂时不可用，请稍后重试'
        finally:
            task['finished_at'] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return task_id


@bp.route('/search')
def search() -> Response:
    import app  # 延迟导入经 app 命名空间调用搜索函数：tests monkeypatch('app.search_by_tag' /
    #             'app.search_by_user' / 'app.browse_discovery' / 'app.paginated_search')，
    #             from fetcher import 的独立绑定看不到补丁（与 background._prefetch_one_tag 同款先例）
    search_type = request.args.get('type', 'tag')
    query = request.args.get('query', '').strip()
    min_bookmarks = request.args.get('min_bookmarks', MAX_BOOKMARKS_DEFAULT)
    sort_order = request.args.get('sort', 'date_d')
    cursor_str = request.args.get('cursor', '')

    if search_type == 'user' and not cursor_str and not query:
        return jsonify({'error': '请输入画师ID'}), 400

    try:
        min_bookmarks = int(min_bookmarks)
    except (ValueError, TypeError):
        min_bookmarks = MAX_BOOKMARKS_DEFAULT

    tag_mode = request.args.get('tag_mode', 'or')
    if tag_mode not in ('or', 'and'):
        tag_mode = 'or'

    if sort_order not in ('popular_d', 'date_d'):
        sort_order = 'date_d'

    r18_mode = request.args.get('r18_mode', 'safe')
    if r18_mode not in ('all', 'safe'):
        r18_mode = 'safe'

    # 解析游标（同步快速校验）
    cursor_data = None
    if cursor_str:
        cursor_data = decode_cursor(cursor_str)
        if cursor_data is None:
            return jsonify({'error': '游标无效', 'error_code': 'CURSOR_INVALID'}), 400
        # 游标 24 小时过期。Pixiv 的 p 参数翻页长期有效（无服务端会话），
        # 过期保护只用于拦截极端陈旧参数；分页漂移由前端去重兜底
        if time.time() - cursor_data.get('created_at', 0) > 86400:
            return jsonify({'error': '搜索已过期，请重新搜索', 'error_code': 'CURSOR_EXPIRED'}), 400
        # 从游标恢复搜索参数
        search_type = cursor_data.get('type', search_type)
        query = cursor_data.get('query', query)
        sort_order = cursor_data.get('sort', sort_order)
        tag_mode = cursor_data.get('tag_mode', tag_mode)
        r18_mode = cursor_data.get('r18_mode', r18_mode)
        min_bookmarks = cursor_data.get('min_bookmarks', min_bookmarks)
        # 作者搜索的切片大小就是游标里 pixiv_page 的步长。步长对不上（部署前后
        # ITEMS_PER_PAGE 变过、或游标来自旧版代码）就丢弃游标重新搜索 —— 沿用旧
        # 步长会在错误的 id 区间上翻页，表现为跳件/重复，比重新搜一遍难排查得多。
        if search_type == 'user' and cursor_data.get('ps', ITEMS_PER_PAGE) != ITEMS_PER_PAGE:
            cursor_data = None

    query_params = {
        'type': search_type,
        'query': query,
        'sort': sort_order,
        'tag_mode': tag_mode,
        'r18_mode': r18_mode,
        'min_bookmarks': min_bookmarks,
    }

    # 组装后台执行闭包
    if search_type == 'tag':
        if len(query) > 200:
            return jsonify({'error': '搜索关键词过长'}), 400
        if not query:
            def _browse_fn(page, remaining=None):
                return app.browse_discovery(page, sort_order, min_bookmarks, r18_mode=r18_mode,
                                            max_results=remaining or ITEMS_PER_PAGE)

            def _fn():
                return app.paginated_search(_browse_fn, query_params, ITEMS_PER_PAGE, cursor_data)
        else:
            def _tag_fn(page, remaining=None):
                return app.search_by_tag(query, min_bookmarks, page, sort_order, 9999, tag_mode, r18_mode=r18_mode,
                                         max_results=remaining or ITEMS_PER_PAGE)

            def _fn():
                return app.paginated_search(_tag_fn, query_params, ITEMS_PER_PAGE, cursor_data)
    else:
        if not cursor_str and not query.isdigit():
            return jsonify({'error': '画师ID必须为数字'}), 400

        # ps 写进游标一起带出去，供下次请求校验步长（见上方游标恢复处的说明）
        query_params['ps'] = ITEMS_PER_PAGE

        def _user_fn(page, remaining=None):
            return app.search_by_user(query, min_bookmarks, page, hide_r18=(r18_mode == 'safe'),
                                      max_results=remaining or ITEMS_PER_PAGE)

        def _fn():
            # 作者搜索是唯一"必须拉完详情才知道能不能要"的路径，给它一个总预算，
            # 免得筛选严格时扫满 _MAX_SCAN_PAGES 页。其余路径走默认 0（不限）。
            return app.paginated_search(
                _user_fn, query_params, ITEMS_PER_PAGE, cursor_data,
                detail_budget=ITEMS_PER_PAGE * fetcher.USER_SEARCH_DETAIL_BUDGET_PAGES)

    task_id = _submit_search_task(_fn)
    logger.info(
        f'[search] 已提交 task={task_id} type={search_type} query={query!r} min={min_bookmarks} '
        f'sort={sort_order} tag_mode={tag_mode} r18={r18_mode} cursor={"yes" if cursor_str else "no"}'
    )
    return jsonify({'task_id': task_id, 'status': 'running'})


@bp.route('/api/search/status/<task_id>')
def search_status(task_id: str) -> Response:
    # 访问即清理过期任务（无需依赖下次提交），控制内存上限
    _cleanup_search_tasks()
    # 无锁读取：CPython 下 dict 读取原子，且写入方最后才置 status，
    # 读到 done/error 时其余字段必已写入完成，视图一致
    task = _search_tasks.get(task_id)
    if not task:
        return jsonify({'error': '搜索任务不存在或已过期，请重新搜索',
                        'error_code': 'TASK_LOST'}), 404
    resp = {
        'status': task['status'],
        'results': task['results'],
        'cursor': task['cursor'],
        'has_more': task['has_more'],
        'fetch_stats': task['fetch_stats'],
    }
    if task['status'] == 'error':
        resp['error'] = task['error']
        if task['error'] == 'auth':
            return jsonify(resp), 401
        return jsonify(resp), 502
    return jsonify(resp)


@bp.route('/api/cache/items')
def cache_items() -> Response:
    """浏览预取缓存：库内过滤/排序/分页，不请求 Pixiv。"""
    tag = request.args.get('tag', '').strip()
    if not tag:
        return jsonify({'error': '缺少标签参数'}), 400

    min_bookmarks = max(0, _safe_int(request.args.get('min_bookmarks'), 0))

    sort_order = request.args.get('sort', 'date_d')
    if sort_order not in ('popular_d', 'date_d'):
        sort_order = 'date_d'

    offset = max(0, _safe_int(request.args.get('offset'), 0))
    filter_tag = request.args.get('filter_tag', '').strip()

    # R18 过滤：默认 safe（不含 R18）；显式 r18=all 才包含
    r18_mode = request.args.get('r18', 'safe')
    if r18_mode not in ('all', 'safe'):
        r18_mode = 'safe'

    with get_session() as db:
        sc = db.query(SearchCache).filter(SearchCache.tag == tag).first()
        if not sc:
            return jsonify({'error': '标签不存在'}), 404
        cached_at = sc.cached_at.isoformat() if sc.cached_at else None
        sc_status = sc.status
        sc_total = sc.total

    results, has_more, _next, filtered_total = query_cached_tag(
        tag, min_bookmarks, sort_order, 'or', r18_mode,
        offset=offset, limit=ITEMS_PER_PAGE, filter_tag=filter_tag,
    )
    return jsonify({
        'tag': tag,
        'cached_at': cached_at,
        'status': sc_status,
        'total': sc_total,
        'filtered_total': filtered_total,
        'offset': offset,
        'page_size': ITEMS_PER_PAGE,
        'results': results,
        'has_more': has_more,
    })


@bp.route('/api/cache/tags')
def api_cache_tags() -> Response:
    """缓存作品（预取来源）中出现过的标签列表，供前端 datalist 提示。"""
    try:
        with get_session() as db:
            rows = db.execute(text("""
                SELECT DISTINCT j.value AS tag
                FROM illusts, json_each(illusts.tags) AS j
                WHERE illusts.prefetch_source = 1
                ORDER BY tag
                LIMIT 500
            """)).all()
            return jsonify([row[0] for row in rows])
    except OperationalError:
        # 单条损坏 tags JSON 会让 json_each 抛错：降级返回空列表
        logger.warning('缓存标签列表查询失败（可能含损坏 tags），返回空列表')
        return jsonify([])


@bp.route('/api/cache/items/<int:pixiv_id>/delete', methods=['POST'])
@_csrf_required
def cache_item_delete(pixiv_id: int) -> Response:
    """从预取缓存删除单条作品（移除 SearchCache 引用 + Illust 行）。"""
    with get_session() as db:
        illust = db.query(Illust).filter(Illust.pixiv_id == pixiv_id).first()
        if not illust or not illust.prefetch_source:
            return jsonify({'error': '作品不在预取缓存中'}), 404
        if illust.download_status in ('done', 'downloading') or illust.local_paths_list:
            return jsonify({'error': '已下载/下载中的作品请在图库中处理'}), 400
        if db.query(CollectionItem).filter(CollectionItem.pixiv_id == pixiv_id).first():
            return jsonify({'error': '已收藏的作品不能从缓存删除'}), 400
        _remove_pids_from_search_caches(db, [pixiv_id])
        db.delete(illust)
        safe_commit(db)
    return jsonify({'status': 'deleted'})


@bp.route('/api/following')
def api_following() -> Response:
    page = request.args.get('page', '1')
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        page = 1
    r18_mode = request.args.get('r18_mode', 'safe')
    if r18_mode not in ('all', 'safe'):
        r18_mode = 'safe'
    try:
        results, has_more = fetch_following(page, r18_mode=r18_mode)
    except PixivAuthError as e:
        logger.warning(f'关注列表认证失败：{e}')
        return jsonify({'error': 'Cookie 已过期，请更新 cookies.txt 后重试'}), 401
    return jsonify({'results': results, 'has_more': has_more})