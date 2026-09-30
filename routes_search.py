# ── 搜索 / 缓存浏览 / 关注 路由 ──
# 异步搜索任务（_submit_search_task / _cleanup_search_tasks）与
# /search、/api/search/status、/api/cache/*、/api/following 路由。
from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable

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

# 限流部分页（partial）给前端的提示：详情请求被 Pixiv 限流闸截断，本页结果不完整。
# 刻意不透传 `SearchRateLimitedError` 的原文（里面带着内部计数与搜索关键词），
# 详细原因只进日志。
_RATE_LIMIT_WARNING = 'Pixiv 详情请求被限流，本页结果不完整，请稍后重试'


# ── 异步搜索任务 ──
# 搜索（含限速拉取详情）在后台线程执行，/search 立即返回 task_id，
# 前端轮询 /api/search/status/<id>。gunicorn -w 1 下搜索不再阻塞其他请求。

def _cleanup_search_tasks() -> None:
    import app  # 延迟导入读取 app.SEARCH_TASK_TTL：tests monkeypatch('app.SEARCH_TASK_TTL')
    #             （test_app.py TTL 清理用例），from runtime import 的独立绑定看不到该补丁
    now = time.time()
    with _search_tasks_lock:
        for tid in [t for t, v in _search_tasks.items()
                    if v['status'] in ('done', 'error', 'cancelled', 'partial')
                    and v.get('finished_at') and now - v['finished_at'] > app.SEARCH_TASK_TTL]:
            del _search_tasks[tid]


def _zero_progress() -> dict[str, int]:
    """终态计数器（`error` / `cancelled` 等"结果不可信"收尾时用）。

    为什么要归零而不是保留：`progress` 与 `results` 同源于同一批发布事件。`error` /
    `cancelled` 已经清空 `results`（已发布的预览可能对应随后回滚的行），若还留着非零
    `accepted`，两者就自相矛盾 —— 前端"已找到 N 件"取的是 `accepted`，会在空网格上
    显示幽灵数字（那些行恰恰是被判定不可信才清掉的）。

    每次返回**新** dict：任务字典里的 `progress` 由 publisher 原地累加，多个任务共享
    一个字面量对象会让一段终态代码污染另一段运行中的计数。
    """
    return {'examined': 0, 'accepted': 0, 'detail_failed': 0}


def _store_task_state(task: dict, status: str | None = None, **fields: object) -> None:
    """在 `_search_tasks_lock` 内一次性写入任务字段，最后才写 `status`，并推进 revision。

    为什么整体在锁内：`search_status` 也在同一把锁下复制快照，绕过锁写字段就会让
    某次轮询读到"新 results + 旧 status"（或反向）的拼接视图。
    为什么 status 最后写：沿用旧实现的理由 —— 读到终态时其余字段必已就绪。
    revision 是快照版本号（只增不减），任何改变快照内容的写入都 +1，前端据此判断
    "这次轮询有没有新东西"，所以计数变化也要推进它。
    """
    with _search_tasks_lock:
        for key, value in fields.items():
            task[key] = value
        if status is not None:
            task['status'] = status
        task['revision'] += 1


def _make_search_publisher(task: dict) -> Callable[[dict], None]:
    """构造逐条进度 publisher：把 fetcher 的 progress 事件折叠进任务快照。

    事件形态见 `fetcher._process_items`：examined / detail_failed / result。
    回调可能来自任务线程自己跑的 collector 循环，所以更新一律在 `_search_tasks_lock`
    内完成；只改内存 dict，不做网络 I/O（持锁发请求会阻塞提交与状态查询）。
    """
    published_pids: set[int] = set()

    def _publish(event: dict) -> None:
        etype = event.get('type')
        with _search_tasks_lock:
            # 终态守卫：任务一旦收尾就不再接受任何发布。当前发布全在同步路径上，
            # `_emit` 的 `_cancelled()` 抑制已经挡得住收尾后的迟到事件；但 `_cancel_end()`
            # 之后抑制即失效，一旦哪天把发布挪到异步路径，迟到预览会追加到已定稿的页面上
            # （done/error 的 results 与 progress 已定稿）。结构性加固比"调用顺序永不改变"
            # 这个隐含前提便宜。
            if task['status'] != 'running':
                return
            progress = task['progress']
            if etype == 'examined':
                progress['examined'] += 1
            elif etype == 'detail_failed':
                progress['detail_failed'] += 1
            elif etype == 'result':
                result = event.get('result') or {}
                pid = result.get('pixiv_id')
                # 去重按 `pixiv_id`：预览 dict 来自**未入库**的模型（`id`/`created_at`
                # 为 None），和 canonical 页里的行 id 不是一回事；而 fetcher 侧的去重
                # 集只覆盖单次 `_process_items`，一次搜索翻多页会重复发同一个 pid。
                if pid is None or pid in published_pids:
                    return
                published_pids.add(pid)
                # 预览最多一页：它只是给前端"边搜边显示"用的，多了会让轮询载荷随
                # 扫描页数无限增长，而 done 时无论如何都会被 canonical 页替换。
                # accepted 是"已确认件数"，照实累计 —— 界面上的"已找到 N 件"不该
                # 随渲染上限打折（它和预览条数本就允许不一致）。
                if len(task['results']) < ITEMS_PER_PAGE:
                    # 新 dict 快照：任务里的每条预览与调用方持有的对象解耦
                    task['results'] = task['results'] + [dict(result)]
                progress['accepted'] += 1
            else:
                # 未知事件类型不能静默丢弃：fetcher 若新增第四种事件，这里就会变成
                # 又一个"接线通过、测试全绿、端到端零事件"的陷阱，日志是唯一的线索。
                logger.debug(f'[search] 忽略未知的进度事件类型：{etype!r}')
                return
            task['revision'] += 1

    return _publish


def _submit_search_task(fn, input_cursor: str | None = None) -> str:
    """提交搜索任务到后台线程，返回 task_id。

    fn 接收逐条进度 publisher（可选形参），返回元组 (results, cursor, has_more)。
    input_cursor: 本次请求**实际使用**的原始游标（无则 None）。限流导致的部分页必须把
        任务游标退回它 —— 否则前端以为本页已翻过去，拿残缺页的下一页位置继续翻会跳件。
        注意是"实际使用"而不是"请求携带"：`ps` 不匹配被丢弃的游标（路由侧
        `cursor_data = None`，任务从第 1 页重搜）绝不能回吐，否则等于告诉前端"你翻到
        一半的位置保住了"，而那个位置根本没被这次任务用过。
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
        # 空串归一成 None：partial 时原样回吐，`''` 会被前端当成"有游标"
        'input_cursor': input_cursor or None,
        # 运行中快照字段：publisher 增量填充，终态时补齐
        'revision': 0,
        'progress': _zero_progress(),
        'complete': False,
        'warning': None,
    }
    publisher = _make_search_publisher(task)
    with _search_tasks_lock:
        _search_tasks[task_id] = task

    def _run() -> None:
        try:
            # 取消事件绑定到本线程（fetcher 的检查点只认任务线程自己的事件），
            # fn 全程结束后必须解绑 —— 线程池线程会被复用，残留状态会污染下一个任务
            fetcher._cancel_begin(task['cancel_event'].is_set)
            try:
                results, next_cursor, has_more = fn(publisher)
            finally:
                fetcher._cancel_end()
            _store_task_state(
                task,
                status='done',
                # canonical 页**替换**预览（不是合并）：预览按完成顺序，canonical 按
                # 扫描顺序，多页扫描时两边合法地可以不一致
                results=results,
                cursor=next_cursor,
                has_more=has_more,
                fetch_stats=fetcher.get_last_fetch_stats(),
                complete=True,
            )
            logger.info(
                f'[search] 完成 task={task_id} results={len(task["results"])} has_more={task["has_more"]} '
                f'details={task["fetch_stats"].get("detail_fetched", 0)} '
                f'failed={task["fetch_stats"].get("detail_failed", 0)} '
                f'seconds={(time.time() - task["created_at"]):.1f}'
            )
        except fetcher.SearchRateLimitedError as e:
            logger.warning(f'搜索任务 {task_id} 详情请求被限流，以 partial 收尾：{e}')
            _store_task_state(
                task,
                status='partial',
                # 预览保留：已确认的结果对用户仍然是有效信息，只是本页没搜完
                cursor=task['input_cursor'],
                # 不伪造 has_more：本页是否还有更多无从判断，说 True 是编造、说 False
                # 也只是一页不完整的结果；前端按自己保留的 nextCursor 决定重试
                has_more=False,
                fetch_stats=fetcher.get_last_fetch_stats(),
                # 显式写死 complete=False：partial 永远不是"完整页"，别依赖初始值
                complete=False,
                warning=_RATE_LIMIT_WARNING,
            )
        except fetcher.SearchCancelledError:
            logger.info(f'[search] 取消 task={task_id}（被新搜索取代）')
            # 预览与计数一起清空：旧任务已被取代，留着只会把幽灵卡片（以及"已找到 N 件"
            # 的幽灵数字）渲染到新搜索的页面上
            _store_task_state(task, status='cancelled', results=[], progress=_zero_progress())
        except PixivAuthError:
            logger.warning(f'搜索任务 {task_id} 认证失败')
            _store_task_state(task, status='error', results=[], progress=_zero_progress(),
                              error='auth')
        except FileNotFoundError as e:
            logger.error(f'搜索任务 {task_id} 文件缺失：{e}')
            _store_task_state(task, status='error', results=[], progress=_zero_progress(),
                              error=f'缺少文件: {e}')
        except Exception as e:
            logger.error(f'搜索任务 {task_id} 失败：{e}', exc_info=True)
            # 预览清空：已发布的预览可能对应随后回滚的行（`_publish` 发生在
            # `safe_commit` 之前），error 终态带上它们就是给用户看不存在的结果。
            # 计数必须同批归零：`accepted` 与预览同源，留着非零值就是"空网格 + 已找到 N 件"。
            _store_task_state(
                task, status='error', results=[], progress=_zero_progress(),
                error='搜索服务暂时不可用，请稍后重试')
        finally:
            # 同样在锁内写：任务字典只留"锁内写"这一种写者，避免锁内读/锁外写的拼接视图。
            # finished_at 不进状态响应，所以不推进 revision。
            with _search_tasks_lock:
                # 兜底：终态分支**自身**抛异常时（例如 `fetcher.get_last_fetch_stats()`
                # 出错），Python 不会再用兄弟 except 兜住它，任务就会停在 running 而
                # finished_at 已写 —— `_cleanup_search_tasks` 只回收终态，前端于是永远
                # 轮询一个不会结束、也不会被清理的任务。这里钉成 error（计数同样归零，
                # 见 `_zero_progress`），并推进 revision 让前端看得见这次变化。
                if task['status'] == 'running':
                    logger.error(f'搜索任务 {task_id} 未落到终态，兜底标记为 error')
                    task['status'] = 'error'
                    task['results'] = []
                    task['progress'] = _zero_progress()
                    task['error'] = '搜索任务异常终止，请稍后重试'
                    task['revision'] += 1
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

    # 组装后台执行闭包。publisher 由 `_submit_search_task` 注入（可选形参），沿
    # `search_by_*` / `browse_discovery` 的 progress 形参下传 —— **不得**传给
    # `paginated_search`：它只看页边界、看不到条目，收下也只能原样丢掉
    # （"接线通过、测试全绿、端到端零事件"的静默陷阱，见执行勘误 Task 4 第 1 条）。
    if search_type == 'tag':
        if len(query) > 200:
            return jsonify({'error': '搜索关键词过长'}), 400
        if not query:
            def _fn(publisher=None):
                def _browse_fn(page, remaining=None):
                    return app.browse_discovery(page, sort_order, min_bookmarks, r18_mode=r18_mode,
                                                max_results=remaining or ITEMS_PER_PAGE,
                                                progress=publisher)

                return app.paginated_search(_browse_fn, query_params, ITEMS_PER_PAGE, cursor_data)
        else:
            def _fn(publisher=None):
                def _tag_fn(page, remaining=None):
                    return app.search_by_tag(query, min_bookmarks, page, sort_order, 9999, tag_mode,
                                             r18_mode=r18_mode, max_results=remaining or ITEMS_PER_PAGE,
                                             progress=publisher)

                return app.paginated_search(_tag_fn, query_params, ITEMS_PER_PAGE, cursor_data)
    else:
        if not cursor_str and not query.isdigit():
            return jsonify({'error': '画师ID必须为数字'}), 400

        # ps 写进游标一起带出去，供下次请求校验步长（见上方游标恢复处的说明）
        query_params['ps'] = ITEMS_PER_PAGE

        def _fn(publisher=None):
            def _user_fn(page, remaining=None):
                return app.search_by_user(query, min_bookmarks, page, hide_r18=(r18_mode == 'safe'),
                                          max_results=remaining or ITEMS_PER_PAGE,
                                          progress=publisher)

            # 作者搜索是唯一"必须拉完详情才知道能不能要"的路径，给它一个总预算，
            # 免得筛选严格时扫满 _MAX_SCAN_PAGES 页。其余路径走默认 0（不限）。
            return app.paginated_search(
                _user_fn, query_params, ITEMS_PER_PAGE, cursor_data,
                detail_budget=ITEMS_PER_PAGE * fetcher.USER_SEARCH_DETAIL_BUDGET_PAGES)

    # 只回报**实际使用**的游标：`ps` 不匹配时上面已把 cursor_data 置 None（从第 1 页
    # 重搜），此时再把原始 cursor_str 交给任务，partial 终态就会回吐一个本次任务从未
    # 使用过的位置，前端会以为"翻页位置保住了"而在错误的区间上继续。
    task_id = _submit_search_task(_fn, cursor_str if cursor_data else '')
    logger.info(
        f'[search] 已提交 task={task_id} type={search_type} query={query!r} min={min_bookmarks} '
        f'sort={sort_order} tag_mode={tag_mode} r18={r18_mode} cursor={"yes" if cursor_str else "no"}'
    )
    return jsonify({'task_id': task_id, 'status': 'running'})


@bp.route('/api/search/status/<task_id>')
def search_status(task_id: str) -> Response:
    """任务状态快照。

    锁内复制、锁外序列化：`results` / `progress` / `revision` 会被后台 publisher
    原地更新，直接 `jsonify` 任务字典可能序列化到一半就看见半新半旧的内容；
    持锁做 JSON 序列化又会卡住后台发布与任务提交。

    字段权威性按状态分级：`done` / `error` / `cancelled` / `partial` 下
    `status` / `complete` / `warning` 是权威判据，而 `partial` 的 `cursor` / `has_more`
    **只是建议值** —— 本页没搜完时既不知道真实的下一页位置，也不该伪造 `has_more`，
    由前端按自己保留的 nextCursor 决定是否重试（别把它们当"任务给出了翻页位置"）。
    """
    # 访问即清理过期任务（无需依赖下次提交），控制内存上限
    _cleanup_search_tasks()
    with _search_tasks_lock:
        task = _search_tasks.get(task_id)
        if task is not None:
            resp = {
                'status': task['status'],
                'results': list(task['results']),
                'cursor': task['cursor'],
                'has_more': task['has_more'],
                'fetch_stats': dict(task['fetch_stats']),
                'revision': task['revision'],
                'progress': dict(task['progress']),
                'complete': task['complete'],
                'warning': task['warning'],
            }
            error = task['error']
        else:
            resp = None
            error = None

    if resp is None:
        return jsonify({'error': '搜索任务不存在或已过期，请重新搜索',
                        'error_code': 'TASK_LOST'}), 404
    if resp['status'] == 'error':
        resp['error'] = error
        if error == 'auth':
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