import base64
import io
import json
import os
import pathlib
import re
import threading
import time
import zipfile
from unittest.mock import patch

import pytest
from sqlalchemy import text

import app
from pixivviewer import fetcher
from pixivviewer import helpers
from pixivviewer import models
from pixivviewer import pixiv_client
from pixivviewer.routes import download as routes_download
from pixivviewer.routes import gallery as routes_gallery
from pixivviewer import runtime
from pixivviewer.config import ITEMS_PER_PAGE

# error / cancelled 等"结果不可信"的终态响应的计数器必须全零（与 results=[] 一致）：
# accepted 是前端"已找到 N 件"的数据源，留着非零值就会在空网格上显示幽灵数字。
_ZERO_PROGRESS = {'examined': 0, 'accepted': 0, 'detail_failed': 0}


class TestIndexRoute:
    def test_get_returns_200(self, client):
        """搜索页挪到 `/explore`（`/` 现在是首页）；`/search` 仍是异步搜索 API。"""
        resp = client.get('/explore')
        assert resp.status_code == 200
        assert b'Pixiv' in resp.data or b'\xe6\x90\x9c\xe7\xb4\xa2' in resp.data


def _route_shape(route: str) -> str:
    """把路由占位符（`<int:pixiv_id>`）与具体 id 都抹掉，便于矩阵与源码对账。

    `/api/gallery/<int:pixiv_id>` 与矩阵里的 `/api/gallery/999999`
    归一后都是 `/api/gallery/`。
    """
    return re.sub(r'\d+', '', re.sub(r'<[^>]+>', '', route))


class TestCsrfProtection:
    def _get_token(self, client):
        resp = client.get('/csrf-token')
        return resp.get_json()['token']

    def test_csrf_endpoint(self, client):
        resp = client.get('/csrf-token')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'token' in data
        assert len(data['token']) == 32

    # 全部修改型端点（POST/PUT/DELETE）必须挂 @_csrf_required。
    # 参数是"有副作用的真实请求"：漏挂装饰器的端点会真的执行下去（例如删除已下载作品、
    # 写 settings.json），所以用例只用必然无效的 ID/空 body —— 万一哪天装饰器被摘掉，
    # 这里会变成 404/400 而不是 403，测试失败且不会造成破坏。
    MUTATING_ENDPOINTS = [
        ('POST', '/login'),
        ('POST', '/api/settings'),
        ('POST', '/api/settings/unlock'),
        ('POST', '/api/blocked-tags'),
        ('DELETE', '/api/blocked-tags/999999'),
        ('POST', '/api/auto-follow/config'),
        ('POST', '/download/999999'),
        ('POST', '/api/download/batch'),
        ('POST', '/download/cancel/999999'),
        ('POST', '/download/reset/999999'),
        ('DELETE', '/api/gallery/999999'),
        ('POST', '/api/gallery/batch-delete'),
        ('DELETE', '/api/thumb/redirect-hosts'),
        ('POST', '/api/open-dir'),
        ('POST', '/api/prefetch/config'),
        ('POST', '/api/prefetch/tags'),
        ('DELETE', '/api/prefetch/tags/999999'),
        ('POST', '/api/prefetch/tags/999999/aliases'),
        ('DELETE', '/api/prefetch/tags/999999/aliases'),
        ('POST', '/api/prefetch/refresh'),
        ('POST', '/api/prefetch/refresh-reset'),
        ('POST', '/api/cache/items/999999/delete'),
        ('POST', '/api/home/more'),
    ]

    def test_all_mutating_endpoints_require_csrf(self, client):
        """每个修改型端点缺 CSRF 头一律 403 —— 防止将来新增路由漏挂装饰器。

        矩阵写成一个循环而不是 `parametrize`：这是**一个不变式**（"缺头就 403"），
        `MUTATING_ENDPOINTS` 里每条只是输入变体，失败信息带 `{method} {path}` 就能定位。
        末尾额外用"带真实 JSON body"的请求复验一次：`_csrf_required` 是装饰器，
        body 解析（以及任何视图逻辑）都不得越过它 —— 空 body 与有 body 都得 403。
        """
        for method, path in self.MUTATING_ENDPOINTS:
            resp = client.open(path, method=method, json={})

            assert resp.status_code == 403, f'{method} {path} 未受 CSRF 保护'
            assert resp.get_json()['error'] == 'CSRF校验失败', f'{method} {path} 的错误文案不对'

        resp = client.post('/api/blocked-tags',
                           data=json.dumps({'tag': 'test'}),
                           content_type='application/json')
        assert resp.status_code == 403, '带真实 JSON body 的 POST 同样必须 403'
        assert resp.get_json()['error'] == 'CSRF校验失败'

    def test_mutating_endpoint_matrix_is_complete(self):
        """矩阵必须覆盖源码里所有修改型路由（将来新增路由时这里会先失败）。

        只做静态对账：从包内 `pixivviewer/routes/*.py` 抓 `@bp.route(..., methods=[...])`
        与矩阵比对。包化前是根目录 `routes_*.py` —— 路径跟着模块搬家，扫描范围不能漏。
        "装饰器是否真的挂在函数上"由矩阵循环（`test_all_mutating_endpoints_require_csrf`）
        负责 —— 两件事都得有人管，缺一个都会让"漏挂 CSRF"重新变成静默风险。
        """
        pattern = re.compile(r"""@bp\.route\(\s*['"]([^'"]+)['"]([^)]*)\)""", re.S)
        found = set()
        routes_dir = pathlib.Path(app.__file__).resolve().parent / 'pixivviewer' / 'routes'
        for source_file in routes_dir.glob('*.py'):
            source = source_file.read_text(encoding='utf-8')
            for route, tail in pattern.findall(source):
                if 'methods=' not in tail:
                    continue                       # 纯 GET 路由不受 CSRF 约束
                for method in re.findall(r"""['"](POST|PUT|DELETE|PATCH)['"]""", tail):
                    found.add((method, _route_shape(route)))

        covered = {(method, _route_shape(path)) for method, path in self.MUTATING_ENDPOINTS}

        assert not found - covered, f'以下修改型端点未被 CSRF 矩阵覆盖：{sorted(found - covered)}'

    def test_post_with_valid_csrf_succeeds(self, client, clean_db):
        import time
        tag = f'csrf-test-{int(time.time())}'
        token = self._get_token(client)
        resp = client.post('/api/blocked-tags',
                           data=json.dumps({'tag': tag}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'added'

    def test_csrf_changes_per_session(self, client):
        t1 = self._get_token(client)
        t2 = self._get_token(client)
        assert t1 == t2


class TestBlockedTags:
    def _get_token(self, client):
        resp = client.get('/csrf-token')
        return resp.get_json()['token']

    def test_list_empty(self, client, db):
        db.query(models.BlockedTag).delete()
        db.commit()
        resp = client.get('/api/blocked-tags')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_add_and_list(self, client):
        token = self._get_token(client)
        client.post('/api/blocked-tags',
                    data=json.dumps({'tag': 'R-18'}),
                    content_type='application/json',
                    headers={'X-CSRF-Token': token})
        resp = client.get('/api/blocked-tags')
        assert 'R-18' in resp.get_json()

    def test_add_duplicate_returns_409(self, client):
        token = self._get_token(client)
        client.post('/api/blocked-tags',
                    data=json.dumps({'tag': 'dupe'}),
                    content_type='application/json',
                    headers={'X-CSRF-Token': token})
        resp = client.post('/api/blocked-tags',
                           data=json.dumps({'tag': 'dupe'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 409

    def test_delete(self, client):
        token = self._get_token(client)
        client.post('/api/blocked-tags',
                    data=json.dumps({'tag': 'delete-me'}),
                    content_type='application/json',
                    headers={'X-CSRF-Token': token})
        resp = client.delete('/api/blocked-tags/delete-me',
                             headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        resp2 = client.get('/api/blocked-tags')
        assert 'delete-me' not in resp2.get_json()

    def test_delete_nonexistent_returns_404(self, client):
        token = self._get_token(client)
        resp = client.delete('/api/blocked-tags/no-such-tag',
                             headers={'X-CSRF-Token': token})
        assert resp.status_code == 404


def _poll_search_status(client, task_id, timeout=50):
    """轮询异步搜索任务直到终态（done/partial 200 / error 401·502 / 丢失 404）。"""
    for _ in range(timeout):
        r = client.get(f'/api/search/status/{task_id}')
        if r.status_code == 404:
            return r
        data = r.get_json()
        if data and data.get('status') != 'running':
            return r
        time.sleep(0.05)
    raise AssertionError(f'搜索任务 {task_id} 超时未完成')


class TestSearch:
    def _poll(self, client, task_id, timeout=50):
        """轮询异步搜索任务直到终态（done 200 / error 401·502 / 丢失 404）。"""
        return _poll_search_status(client, task_id, timeout)

    def test_search_dispatches_to_the_matching_backend(self, client):
        """`type` 决定派发到哪个搜索后端，空查询回退 `browse_discovery`。

        三条输入变体、**同一个不变式**（路由按 type 分派 + 查询词原样透传），
        故写成一个循环而不是三条用例。
        """
        for target, path, said in (
                ('browse_discovery', '/search', None),
                ('search_by_tag', '/search?type=tag&query=初音ミク', '初音ミク'),
                ('search_by_user', '/search?type=user&query=12345', None)):
            with patch(f'app.{target}') as mock_fn:
                mock_fn.return_value = ([], False)
                resp = client.get(path)
                assert resp.status_code == 200, f'{target} 的搜索入口应返回 200'
                final = self._poll(client, resp.get_json()['task_id'])
                assert final.get_json()['status'] == 'done', f'{target} 应正常收尾'
                mock_fn.assert_called_once()
                if said is not None:
                    args, _ = mock_fn.call_args
                    assert said in args, f'查询词必须原样传给 {target}'

    @patch('app.paginated_search')
    def test_search_user_passes_detail_budget(self, mock_paginated, client):
        """作者搜索是唯一需要详情预算的路径，标签搜索仍是默认的不限。"""
        mock_paginated.return_value = ([], None, False)
        resp = client.get('/search?type=user&query=12345')
        self._poll(client, resp.get_json()['task_id'])
        assert mock_paginated.call_args.kwargs['detail_budget'] > 0

        mock_paginated.reset_mock()
        resp = client.get('/search?type=tag&query=test')
        self._poll(client, resp.get_json()['task_id'])
        assert mock_paginated.call_args.kwargs.get('detail_budget', 0) == 0

    @patch('app.paginated_search')
    def test_user_cursor_carries_page_stride(self, mock_paginated, client):
        """游标带上切片步长，供下次请求校验。"""
        mock_paginated.return_value = ([], None, False)
        resp = client.get('/search?type=user&query=12345')
        self._poll(client, resp.get_json()['task_id'])
        query_params = mock_paginated.call_args[0][1]
        assert query_params['ps'] == ITEMS_PER_PAGE

    @patch('app.paginated_search')
    def test_user_cursor_with_stale_stride_restarts(self, mock_paginated, client):
        """步长对不上（旧版游标）→ 丢弃游标重新搜索，而不是在错误的 id 区间翻页。"""
        mock_paginated.return_value = ([], None, False)
        stale = fetcher.encode_cursor({
            'type': 'user', 'query': '12345', 'sort': 'date_d', 'tag_mode': 'or',
            'r18_mode': 'safe', 'min_bookmarks': 0,
            'pixiv_page': 3, 'skip_count': 0, 'ps': 60,   # 旧步长
            'created_at': int(time.time()),
        })
        resp = client.get(f'/search?type=user&query=12345&cursor={stale}')
        assert resp.status_code == 200
        self._poll(client, resp.get_json()['task_id'])
        assert mock_paginated.call_args[0][3] is None, '旧步长游标应被丢弃，按新搜索处理'

    @patch('app.paginated_search')
    def test_new_search_cancels_running_task(self, mock_paginated, client):
        """搜索中途改条件重搜：提交新任务的请求内**同步**取消在途旧任务。

        旧任务在下一个检查点（翻页前/后、详情请求前）以 cancelled 终态结束，不再继续
        烧令牌桶；新任务不受影响，正常完成。断言全部来自**同一套**"旧任务挂起等取消"
        的数据准备：cancel_event 当场被置位（不等旧任务自然结束）、旧任务以
        cancelled + 零计数收尾（cancelled 不是错误，不走 502）、新任务正常 done。
        """
        from pixivviewer.runtime import _search_tasks
        started = threading.Event()
        calls = []

        def slow_paginated(search_fn, query_params, items_per_page,
                           cursor_data=None, detail_budget=0):
            calls.append(1)
            if len(calls) > 1:
                return ([{'pixiv_id': 1, 'title': 't'}], None, False)
            started.set()   # 第一个任务：挂起等取消
            deadline = time.time() + 10
            while time.time() < deadline and not fetcher._cancelled():
                time.sleep(0.01)
            raise fetcher.SearchCancelledError()

        mock_paginated.side_effect = slow_paginated

        resp1 = client.get('/search?type=user&query=12345')
        task1 = resp1.get_json()['task_id']
        assert started.wait(5), '旧任务应已开始运行'

        # 改条件（min_bookmarks 100 -> 20）重搜
        resp2 = client.get('/search?type=user&query=12345&min_bookmarks=20')
        task2 = resp2.get_json()['task_id']
        assert task1 != task2
        assert _search_tasks[task1]['cancel_event'].is_set(), \
            '取消必须在提交新任务的请求内同步完成，不等旧任务自然结束'

        final1 = self._poll(client, task1)
        assert final1.status_code == 200, 'cancelled 不是错误，不应走 502'
        data1 = final1.get_json()
        assert data1['status'] == 'cancelled'
        assert data1['results'] == []
        assert data1['progress'] == _ZERO_PROGRESS

        final2 = self._poll(client, task2)
        assert final2.get_json()['status'] == 'done'
        assert len(final2.get_json()['results']) == 1

    def test_invalid_search_params_return_400(self, client):
        """非法查询参数在提交任务**之前**就被拒（不是先建任务再报错）。

        两档输入同一个不变式（参数校验前置），故写成一个循环。
        """
        for label, qs in (('作者 id 非数字', 'type=user&query=abc'),
                          ('查询串超长（>200 字符）', 'type=tag&query=' + 'a' * 201)):
            resp = client.get(f'/search?{qs}')
            assert resp.status_code == 400, f'{label} 必须 400'

    @patch('app.paginated_search')
    def test_search_with_all_params(self, mock_paginated, client):
        mock_paginated.return_value = ([{'pixiv_id': 1, 'title': 't'}], 'cursor_abc', True)
        resp = client.get(
            '/search?type=tag&query=test&min_bookmarks=500'
            '&page=2&sort=date_d&tag_mode=and&r18_mode=safe'
        )
        assert resp.status_code == 200
        task_id = resp.get_json()['task_id']
        final = self._poll(client, task_id)
        data = final.get_json()
        assert data['status'] == 'done'
        assert data['has_more'] is True
        assert data['cursor'] == 'cursor_abc'
        assert len(data['results']) == 1

    @patch('app.browse_discovery')
    def test_invalid_sort_fallback(self, mock_discovery, client):
        mock_discovery.return_value = ([], False)
        resp = client.get('/search?sort=invalid')
        assert resp.status_code == 200
        task_id = resp.get_json()['task_id']
        self._poll(client, task_id)
        args, kwargs = mock_discovery.call_args
        assert args[1] == 'date_d'

    def test_task_error_maps_exception_to_status_code(self, client):
        """任务内异常按类别映射成 HTTP 状态码：未预期异常 502、凭据失效 401。

        两档都必须是"终态 + 计数归零"（`error`/`401` 时带着非零 accepted，前端就会在
        空网格上显示幽灵"已找到 N 件"）；502 额外必须带 `error` 说明。分类矩阵写成一个
        循环：这是同一个不变式（异常 → 状态码 + 终态形状），只有输入与期望值不同。
        """
        for label, target, exc, expected in (
                ('未预期异常', 'paginated_search', RuntimeError('boom'), 502),
                ('凭据失效', 'search_by_tag', fetcher.PixivAuthError('auth'), 401)):
            with patch(f'app.{target}', side_effect=exc):
                resp = client.get('/search?type=tag&query=test')
                assert resp.status_code == 200, f'{label}：提交搜索本身应当成功'
                final = self._poll(client, resp.get_json()['task_id'])
                assert final.status_code == expected, f'{label} 应映射成 {expected}'
                assert final.get_json()['progress'] == _ZERO_PROGRESS, \
                    f'{label}：计数必须归零，实际 {final.get_json()["progress"]}'
                if expected == 502:
                    assert final.get_json()['error'], f'{label}：502 必须带 error 说明'

    def test_task_not_found_returns_404(self, client):
        resp = client.get('/api/search/status/no-such-task')
        assert resp.status_code == 404


class TestSearchTaskSnapshot:
    """运行中快照与 `partial` 终态（Task 4）。

    这些用例走**真实** `fetcher.paginated_search`，只补丁具体的搜索函数 —— 于是
    "publisher 由路由经闭包注入 `search_by_*` / `browse_discovery`" 这条接线
    （执行勘误第 1 条：publisher **不得**传给 `paginated_search`）也在覆盖范围内。
    `test_route_wiring_publishes_real_progress_end_to_end` 更进一步：连 `search_by_*`
    本身都不补丁，只堵住网络 seam，于是路由侧 `progress=` 关键字的名字写错也会被抓住
    （`MagicMock` 收得下任何关键字，那是本条用例存在的理由）。
    """

    def test_running_snapshot_then_done_replaces_preview_with_canonical(self, client):
        """运行中轮询能看到已确认结果与计数；done 时 canonical 页**替换**预览。"""
        published = threading.Event()
        released = threading.Event()
        preview = {'pixiv_id': 101, 'title': 'a', 'id': None, 'created_at': None}
        canonical = {'pixiv_id': 101, 'title': 'a', 'id': 7,
                     'created_at': '2026-01-01T00:00:00'}

        def fake_search_by_tag(*args, **kwargs):
            progress = kwargs['progress']
            assert callable(progress), 'publisher 必须经 progress 形参注入 search_by_tag'
            progress({'type': 'examined', 'pixiv_id': 101})
            progress({'type': 'result', 'result': preview})
            published.set()
            assert released.wait(10), '测试未放行 worker'
            return ([canonical], False)

        try:
            with patch('app.search_by_tag', side_effect=fake_search_by_tag):
                resp = client.get('/search?type=tag&query=test')
                task_id = resp.get_json()['task_id']
                assert published.wait(5), 'worker 应已发布预览'

                mid = client.get(f'/api/search/status/{task_id}')
                assert mid.status_code == 200
                data = mid.get_json()
                assert data['status'] == 'running'
                assert [r['pixiv_id'] for r in data['results']] == [101]
                assert data['results'][0]['id'] is None    # 预览来自未入库的模型
                assert data['revision'] > 0
                assert data['complete'] is False
                assert data['warning'] is None
                assert data['progress'] == {'examined': 1, 'accepted': 1, 'detail_failed': 0}
        finally:
            released.set()

        final = _poll_search_status(client, task_id)
        assert final.status_code == 200
        data = final.get_json()
        assert data['status'] == 'done'
        assert data['complete'] is True
        assert data['results'] == [canonical], 'done 必须用 canonical 页替换预览，而不是合并'

    def test_result_events_deduped_and_preview_capped_to_one_page(self, client):
        """result 事件按 `pixiv_id` 去重、预览只留一页；accepted 照实累计。"""
        published = threading.Event()
        released = threading.Event()
        # 每个 pid 发两次（跨页/跨路径重复），且总数超过一页
        pids = [1] + [pid for pid in range(2, ITEMS_PER_PAGE + 6) for _ in range(2)]

        def fake_search_by_tag(*args, **kwargs):
            progress = kwargs['progress']
            for pid in pids:
                progress({'type': 'result',
                          'result': {'pixiv_id': pid, 'title': f't{pid}', 'id': None}})
            published.set()
            assert released.wait(10)
            return ([], False)

        try:
            with patch('app.search_by_tag', side_effect=fake_search_by_tag):
                task_id = client.get('/search?type=tag&query=test').get_json()['task_id']
                assert published.wait(5)
                data = client.get(f'/api/search/status/{task_id}').get_json()
        finally:
            released.set()

        assert data['status'] == 'running'
        assert len(data['results']) == ITEMS_PER_PAGE, '预览最多一页，避免轮询载荷无限增长'
        assert data['results'][-1]['pixiv_id'] == ITEMS_PER_PAGE
        # 预览有渲染上限，但 accepted 是"已确认件数"：照实累计、不随渲染上限打折
        assert data['progress']['accepted'] == ITEMS_PER_PAGE + 5

    def test_rate_limited_finishes_partial_keeping_preview_and_cursor(self, client):
        """限流截断 → HTTP 200 + partial，载荷形状固定：保留已发布预览、complete=false、
        带 warning、不伪造 has_more；游标**退回输入值**（没有输入游标时就是 None）。

        有/无输入游标只是同一个 partial 收尾路径的两档输入（前置准备、断言都同构），
        故写成一个循环：翻页中途被限流时必须退回原游标，用户冷却后才能重试同一页。
        """
        preview = {'pixiv_id': 201, 'title': 'p', 'id': None}
        cursor = fetcher.encode_cursor({
            'type': 'tag', 'query': 'test', 'sort': 'date_d', 'tag_mode': 'or',
            'r18_mode': 'safe', 'min_bookmarks': 0,
            'pixiv_page': 3, 'skip_count': 0, 'created_at': int(time.time()),
        })

        def fake_search_by_tag(*args, **kwargs):
            kwargs['progress']({'type': 'result', 'result': preview})
            raise fetcher.SearchRateLimitedError('详情请求被限流')

        for label, input_cursor, expected_cursor in (
                ('首页被限流（无输入游标）', None, None),
                ('翻页中途被限流', cursor, cursor)):
            qs = '/search?type=tag&query=test'
            if input_cursor:
                qs += f'&cursor={input_cursor}'
            with patch('app.search_by_tag', side_effect=fake_search_by_tag):
                resp = client.get(qs)
                assert resp.status_code == 200, f'{label}：提交搜索本身应当成功'
                final = _poll_search_status(client, resp.get_json()['task_id'])

            assert final.status_code == 200, f'{label}：partial 是"部分成功"，不应走 502'
            data = final.get_json()
            assert data['status'] == 'partial', f'{label}：状态应为 partial'
            assert [r['pixiv_id'] for r in data['results']] == [201], \
                f'{label}：已发布预览必须保留'
            assert data['complete'] is False, f'{label}：complete 必须为 False'
            assert data['warning'], f'{label}：partial 必须带 warning 说明'
            assert data['has_more'] is False, f'{label}：partial 不伪造 has_more'
            assert data['cursor'] == expected_cursor, f'{label}：游标应退回输入值'

    def test_partial_does_not_echo_discarded_cursor(self, client):
        """被丢弃的游标（步长不匹配）不得回吐：那个位置本次任务从未翻到。

        路由在 `ps` 不匹配时把 `cursor_data` 置 None 并从第 1 页重搜。若仍把请求携带
        的原始游标当作 `input_cursor`，partial 终态就会告诉前端"你的翻页位置保住了"，
        前端据此在**没有搜过的 id 区间**继续翻页 —— 跳件/重复，比丢一页难排查得多。
        """
        stale = fetcher.encode_cursor({
            'type': 'user', 'query': '12345', 'sort': 'date_d', 'tag_mode': 'or',
            'r18_mode': 'safe', 'min_bookmarks': 0,
            'pixiv_page': 3, 'skip_count': 0, 'ps': 60,   # 旧步长：必须被丢弃
            'created_at': int(time.time()),
        })
        with patch('app.paginated_search', side_effect=fetcher.SearchRateLimitedError('限流')):
            resp = client.get(f'/search?type=user&query=12345&cursor={stale}')
            assert resp.status_code == 200
            final = _poll_search_status(client, resp.get_json()['task_id'])

        data = final.get_json()
        assert data['status'] == 'partial'
        assert data['cursor'] is None, '被丢弃的游标不能回吐（该位置从未被本次任务使用）'

    def test_terminal_tasks_cleaned_up_after_ttl(self, client, monkeypatch):
        """终态任务（done 与 partial）过了 TTL 都必须被清理，否则任务表只增不减。

        `_cleanup_search_tasks` 只回收**终态** —— 两档输入是同一个不变式，只有"怎么走到
        终态"不同（正常收尾 / 限流截断）。"partial 也是终态"曾经是漏掉的那一档，所以
        它必须在这条循环里，而不是被 done 那条顺带覆盖。
        """
        cases = (
            ('done', patch('app.search_by_tag', return_value=([], False))),
            ('partial', patch('app.search_by_tag',
                              side_effect=fetcher.SearchRateLimitedError('限流'))),
        )
        for label, patcher in cases:
            with patcher:
                task_id = client.get('/search?type=tag&query=test').get_json()['task_id']
                final = _poll_search_status(client, task_id)
                assert final.get_json()['status'] == label, \
                    f'{label} 档的终态不对：{final.get_json()}'

            # 缩短 TTL 并强制清理（用 context：清完立刻还原，免得影响下一档的轮询）
            with monkeypatch.context() as m:
                m.setattr(app, 'SEARCH_TASK_TTL', -1)
                app._cleanup_search_tasks()
                assert client.get(f'/api/search/status/{task_id}').status_code == 404, \
                    f'{label} 是终态：过了 TTL 必须被清理'

    def test_error_terminal_drops_previews(self, client):
        """error 终态不得带预览：已发布的预览可能对应随后回滚的行（勘误第 2 条）。"""
        def fake_search_by_tag(*args, **kwargs):
            kwargs['progress']({'type': 'result', 'result': {'pixiv_id': 301, 'title': 'x'}})
            return ([], False)

        def fake_paginated(search_fn, *args, **kwargs):
            # 先让页内搜索发布一条预览，再让整个任务以错误收尾
            search_fn(page=1, remaining=ITEMS_PER_PAGE)
            raise RuntimeError('boom')

        with patch('app.search_by_tag', side_effect=fake_search_by_tag), \
                patch('app.paginated_search', side_effect=fake_paginated):
            task_id = client.get('/search?type=tag&query=test').get_json()['task_id']
            final = _poll_search_status(client, task_id)

        assert final.status_code == 502
        data = final.get_json()
        assert data['results'] == []
        assert data['complete'] is False
        assert data['error']
        # 计数与预览同源：results 清了，accepted 也必须归零，否则前端会在空网格上
        # 显示"已找到 1 件"（该预览正是被判为不可信才清掉的）
        assert data['progress'] == _ZERO_PROGRESS, f'实际 progress={data["progress"]}'

    def test_cancelled_terminal_drops_previews(self, client):
        """被新搜索取代的旧任务不得把预览渲染到新搜索的页面上。"""
        def fake_search_by_tag(*args, **kwargs):
            kwargs['progress']({'type': 'result', 'result': {'pixiv_id': 401, 'title': 'x'}})
            raise fetcher.SearchCancelledError()

        with patch('app.search_by_tag', side_effect=fake_search_by_tag):
            task_id = client.get('/search?type=tag&query=test').get_json()['task_id']
            final = _poll_search_status(client, task_id)

        assert final.status_code == 200
        data = final.get_json()
        assert data['status'] == 'cancelled'
        assert data['results'] == []
        assert data['progress'] == _ZERO_PROGRESS, \
            f'取消同样要清计数（幽灵"已找到 N 件"）；实际 progress={data["progress"]}'

    def test_publisher_goes_to_search_fn_not_paginated_search(self, client):
        """publisher 只注入 `search_by_*` / `browse_discovery`（三条输入变体一次覆盖）。

        `paginated_search` 只看页边界、看不到条目，收下 `progress` 也只能原样丢掉
        （执行勘误第 1 条：那是"接线通过、测试全绿、端到端零事件"的静默陷阱）。
        """
        real_paginated = app.paginated_search

        def spy_paginated(*args, **kwargs):
            return real_paginated(*args, **kwargs)

        for target, path in (
                ('search_by_tag', '/search?type=tag&query=test'),
                ('search_by_user', '/search?type=user&query=12345'),
                ('browse_discovery', '/search')):
            with patch('app.paginated_search', side_effect=spy_paginated) as mock_paginated, \
                    patch(f'app.{target}', return_value=([], False)) as mock_fn:
                resp = client.get(path)
                assert _poll_search_status(
                    client, resp.get_json()['task_id']).get_json()['status'] == 'done', \
                    f'{target} 那条路应正常收尾'

                assert 'progress' not in mock_paginated.call_args.kwargs, \
                    f'{target} 的 publisher 不得进 paginated_search'
                assert callable(mock_fn.call_args.kwargs['progress']), \
                    f'{target} 必须收到 publisher'

    def test_published_preview_is_a_snapshot_owned_by_task(self, client):
        """任务存的是**副本**，不是调用方持有的那个预览 dict。

        为什么要有这条：预览由 fetcher 用 `to_dict()` 现造后交给 publisher，交付之后
        调用方（搜索线程/收尾逻辑）仍可能继续改自己那份对象；任务若直接存引用，状态
        接口就会把这种"事后改动"当成搜索结果报给前端。这里只断言"存的是副本"——
        浅拷贝即可：嵌套 list 仍共享，fetcher 造完预览就不再动它，故不深究。
        """
        published = threading.Event()
        released = threading.Event()
        preview = {'pixiv_id': 301, 'title': '原始标题', 'tags': ['tagA'], 'id': None}

        def fake_search_by_tag(*args, **kwargs):
            kwargs['progress']({'type': 'result', 'result': preview})
            published.set()
            assert released.wait(10), '测试未放行 worker'
            return ([], False)

        try:
            with patch('app.search_by_tag', side_effect=fake_search_by_tag):
                task_id = client.get('/search?type=tag&query=test').get_json()['task_id']
                assert published.wait(5), 'worker 应已发布预览'

                # 调用方继续改自己那份 dict：`title` 直接改键值，`tags` 必须**重新赋值**
                # （浅拷贝下嵌套 list 仍与快照共享，原地 append 会连快照一起改，
                #   那是另一个层面的事，不在本用例的断言范围内）
                preview['title'] = '被调用方改过'
                preview['tags'] = preview['tags'] + ['sentinel']

                data = client.get(f'/api/search/status/{task_id}').get_json()
        finally:
            released.set()

        assert data['status'] == 'running'
        assert [r['pixiv_id'] for r in data['results']] == [301]
        assert data['results'][0]['title'] == '原始标题', '快照必须与调用方持有的对象解耦'
        assert 'sentinel' not in data['results'][0]['tags'], 'sentinel 不得出现在预览里'

    def test_route_wiring_publishes_real_progress_end_to_end(self, client, clean_db):
        """端到端接线：路由 → 真实 `search_by_tag` → 真实 `_process_items` → 真实 publisher。

        为什么不能只靠 `test_publisher_goes_to_search_fn_not_paginated_search`：那条
        用例把 `search_by_*` 换成 `MagicMock`，只验证"路由传出了一个叫 progress 的关键字"
        —— 真实搜索函数是否收得下这个形参、事件是否真的走到 publisher、结果是否真的回到
        响应里，它一概看不见。真实链路里关键字对不上时，`search_by_tag` 抛 `TypeError`
        并被 `paginated_search` 的宽 `except Exception` 吞掉（变异验证时捕获到的日志：
        `search_by_tag() got an unexpected keyword argument`），任务照常以 `done` +
        空结果收尾 —— 用户看到"搜索成功但一件都没有"。本用例把整条链路的**产出**都钉在
        断言上：真实 examined / result 事件落到快照，响应里是喂进去的那个 pid。

        这里只补丁网络 seam（`fetch_search_illusts` / `_fetch_details_parallel` /
        `build_pixiv_session`），`app.search_by_tag` 保持真实。
        """
        pid = 9101

        def fake_item(pixiv_id: int) -> dict:
            """列表条目形状（与 `tests/test_fetcher.py::_item` 同款）。"""
            return {
                'id': str(pixiv_id), 'title': f't{pixiv_id}', 'userId': 1, 'userName': 'u',
                'pageCount': 1, 'url': f'https://i.pximg.net/thumb/{pixiv_id}.jpg',
                'updateDate': '2026-01-01T00:00:00+09:00', 'tags': [{'tag': 'a'}],
            }

        fake_detail = {
            'title': f't{pid}', 'user_id': 1, 'user_name': 'u', 'page_count': 1,
            'bookmark_count': 600, 'thumb_url': f'https://x/{pid}.jpg',
            'upload_date': '2026-01-01T00:00:00+09:00',
            'original_urls': [f'https://i.pximg.net/{pid}_p0.jpg'], 'tags': ['a'],
        }
        batch = fetcher._DetailFetchBatch({pid: fake_detail}, 1)

        def fake_fetch_illusts(session, pixiv_query, **kwargs):
            # 只第一页有条目：`paginated_search` 会在攒满一页前多问一页，
            # 空页 + has_more=False 才让它收尾（否则同一 pid 会被重复收集）
            if kwargs.get('page', 1) == 1:
                return [fake_item(pid)], 1
            return [], 1

        # 走**非 defer** 路径（min_bookmarks>0）：详情请求被补丁成常量，
        # 不会踢后台补全线程，也不会碰真实网络
        fetcher.clear_search_cache()
        try:
            with patch('pixivviewer.pixiv_client.fetch_search_illusts', side_effect=fake_fetch_illusts), \
                    patch('pixivviewer.fetcher._fetch_details_parallel', return_value=batch), \
                    patch('pixivviewer.fetcher.build_pixiv_session'):
                resp = client.get('/search?type=tag&query=e2e-wiring&min_bookmarks=1')
                assert resp.status_code == 200
                final = _poll_search_status(client, resp.get_json()['task_id'])
        finally:
            fetcher.clear_search_cache()

        assert final.status_code == 200
        data = final.get_json()
        assert data['status'] == 'done', f'真实搜索链路应以 done 收尾，实际 {data}'
        # 进度计数器只能由真实 publisher 累加出来：路由的 progress 形参没接上时为 0
        #（失败信息里带上整个快照：关键字写错时它的形态正是"done + 空结果 + 零计数"）
        assert data['progress']['examined'] >= 1, (
            f'真实 _process_items 的 examined 事件必须落到快照里 —— '
            f'status={data["status"]} results={data["results"]} progress={data["progress"]}')
        assert data['progress']['accepted'] >= 1
        assert [r['pixiv_id'] for r in data['results']] == [pid]

    def test_terminal_branch_failure_is_pinned_to_error(self, client, monkeypatch):
        """终态分支自身抛异常时，`finally` 兜底必须把任务钉成 error。

        `except` 体里的异常不会被兄弟 `except Exception` 兜住：任务于是停在 `running`
        且 `finished_at` 已写 —— `_cleanup_search_tasks` 只回收终态，前端会永远轮询一个
        既不会结束、也不会被清理的任务。

        线程钩子被换成记录器：异常逃出任务线程正是本条用例的前提，顺带避免 pytest 把
        预期的 traceback 报成 unhandled-thread-exception 警告（那是噪声，不是信号）。
        """
        def fake_stats():
            raise RuntimeError('stats exploded')

        escaped: list[BaseException] = []
        hooked = threading.Event()

        def hook(args):
            escaped.append(args.exc_value)
            hooked.set()

        monkeypatch.setattr(threading, 'excepthook', hook)
        with patch('app.search_by_tag', side_effect=fetcher.SearchRateLimitedError('限流')), \
                patch('pixivviewer.fetcher.get_last_fetch_stats', side_effect=fake_stats):
            task_id = client.get('/search?type=tag&query=test').get_json()['task_id']
            final = _poll_search_status(client, task_id, timeout=20)
            assert hooked.wait(5), '终态分支的异常应逃出任务线程（本用例的前提）'

        assert isinstance(escaped[0], RuntimeError)
        assert final.status_code == 502, '兜底终态是 error（非 auth）'
        data = final.get_json()
        assert data['status'] == 'error', '不能停在 running，否则任务永远不会被清理'
        assert data['complete'] is False
        assert data['results'] == []
        assert data['progress'] == _ZERO_PROGRESS


# ── 搜索预览可查看（/detail 的内存快照兜底）──
# 背景：搜索的提交粒度是**整页**（`safe_commit` 在 `search_by_*` 里，整页详情拉完之后），
# 而预览事件在每条详情到手时就发出。不兜底的话，用户在整页筛完之前点开预览卡只有 404
#（作者搜索一页 24 条详情 ≈30s）。详见 docs/superpowers/specs/2026-09-30-search-preview-viewable-design.md。

@pytest.fixture
def preview_task():
    """往 `runtime._search_tasks` 装搜索任务快照，用例结束自动摘掉。

    为什么必须清理：`_search_tasks` 是进程级内存状态，**不在** conftest 的 `clean_db`
    覆盖范围内（那里只清表）—— 残留的 running 任务会让后续用例的 `/detail` 兜底命中。
    """
    installed: list[str] = []

    def _install(results, *, status='running', created_at=1.0, task_id=None):
        tid = task_id or f'preview-task-{len(installed)}'
        with runtime._search_tasks_lock:
            runtime._search_tasks[tid] = {
                'status': status,
                'results': [dict(r) for r in results],
                'cursor': None,
                'has_more': False,
                'error': None,
                'fetch_stats': {},
                'created_at': created_at,
                'finished_at': None,
                'cancel_event': threading.Event(),
                'input_cursor': None,
                'revision': 1,
                'progress': {'examined': len(results), 'accepted': len(results),
                             'detail_failed': 0},
                'complete': False,
                'warning': None,
            }
        installed.append(tid)
        return tid

    yield _install

    with runtime._search_tasks_lock:
        for tid in installed:
            runtime._search_tasks.pop(tid, None)


def _preview_result(pixiv_id: int, **overrides) -> dict:
    """一条 `Illust.to_dict()` 形状的预览。

    **必须**带 `original_urls`：详情页在快照没有原图地址时会真的去拉 `_fetch_original_urls`
    （见仓库约定"详情/下载类用例必须预置 original_urls"），离线环境下一次请求就拖慢几十秒。
    """
    data = {
        'id': None,
        'pixiv_id': pixiv_id,
        'title': f'预览作品 {pixiv_id}',
        'user_id': 777,
        'user_name': '预览画师',
        'tags': ['preview-tag'],
        'page_count': 2,
        'bookmark_count': 321,
        'upload_date': '2026-01-01T00:00:00+00:00',
        'thumb_url': f'https://i.pximg.net/c/250x250/img/{pixiv_id}.jpg',
        'original_urls': [
            f'https://i.pximg.net/img-original/img/2026/01/01/00/00/00/{pixiv_id}_p0.jpg',
        ],
        'download_status': 'pending',
        'downloaded_at': None,
        'file_size': None,
        'created_at': None,
    }
    data.update(overrides)
    return data


class TestFindRunningPreview:
    """`runtime.find_running_preview`：只认 running、取最新、返回副本。"""

    def test_returns_copy_of_snapshot(self, clean_db, preview_task):
        """详情页会往返回的 dict 里补展示字段，绝不能污染任务快照。

        快照还要供 `/api/search/status` 轮询与 `done` 时的 canonical 替换使用 ——
        被详情页塞进 `local_urls` 之类的字段就是数据污染。
        """
        pid = 92001
        preview_task([_preview_result(pid, title='原始标题')])

        found = runtime.find_running_preview(pid)
        assert found is not None
        assert found['title'] == '原始标题'

        found['title'] = '被详情页改过'
        found['local_urls'] = [f'/api/image/{pid}/0']

        again = runtime.find_running_preview(pid)
        assert again['title'] == '原始标题'
        assert 'local_urls' not in again

    def test_only_running_tasks_are_a_source(self, clean_db, preview_task):
        """终态任务与不存在的 pid 都不返回：done/partial 的行已提交，error/cancelled 的行已回滚。"""
        for n, status in enumerate(('done', 'partial', 'cancelled', 'error')):
            pid = 92100 + n
            preview_task([_preview_result(pid)], status=status)
            assert runtime.find_running_preview(pid) is None, f'{status} 不该被当成预览来源'

        assert runtime.find_running_preview(92099) is None, '不存在的 pid 应返回 None'

    def test_newest_running_task_wins(self, clean_db, preview_task):
        """旧任务在自己的线程察觉取消之前仍是 running，此时它的快照已被取代。"""
        pid = 92002
        preview_task([_preview_result(pid, title='旧任务')], created_at=1.0, task_id='old')
        preview_task([_preview_result(pid, title='新任务')], created_at=2.0, task_id='new')
        assert runtime.find_running_preview(pid)['title'] == '新任务'


class TestDetailPagePreviewFallback:
    """`/detail/<pid>` 对"运行中搜索已确认但未落库"的预览做内存快照兜底。

    没有这层兜底，预览卡就是"看得见点不开"：用户必须等整页详情拉完（作者搜索 ≈30s，
    `safe_commit` 在 `search_by_*` 里）才能进入详情页。
    """

    def test_running_preview_renders_without_db_row(self, client, clean_db, preview_task):
        pid = 91001
        preview_task([_preview_result(pid, title='内存里的预览标题')])

        # 快照自带 original_urls → 不该发起任何原图拉取（离线可跑；这条"零网络请求"
        # 的属性本身也要钉住：将来有人把预置 URL 去掉，这里会立刻炸成几十秒的挂起）
        with patch('pixivviewer.routes.gallery._fetch_original_urls',
                   side_effect=AssertionError('预览快照自带 original_urls，不该发网络请求')):
            resp = client.get(f'/detail/{pid}')

        assert resp.status_code == 200, '已筛出的预览作品必须能打开详情页'
        html = resp.get_data(as_text=True)
        assert '内存里的预览标题' in html
        assert '预览画师' in html
        assert '<div class="preview-notice">' in html, '预览态必须显式提示"筛选中"'
        # 未落库 → 下载必然 404，入口必须禁用（按钮态由模板按 preview 决定）
        assert re.search(r'<button[^>]*id="downloadBtn"[^>]*disabled', html), \
            '预览态的下载按钮必须 disabled'
        assert clean_db.query(models.Illust).filter(models.Illust.pixiv_id == pid).first() is None, \
            '本用例前提：库里没有这行（兜底才是唯一数据来源）'

    def test_no_trustworthy_source_is_404(self, client, clean_db, preview_task):
        """终态任务一律 404（兜底会渲染出"不可信的结果"）：done/partial 查库即命中
        —— 此处库里没有这行，所以是 404；error/cancelled 的行已随事务回滚。

        末尾一并覆盖"既无任务也无库行"这条最朴素的路径。
        """
        for n, status in enumerate(('done', 'partial', 'cancelled', 'error')):
            pid = 91100 + n
            preview_task([_preview_result(pid)], status=status)
            assert client.get(f'/detail/{pid}').status_code == 404, f'{status} 不该被兜底渲染'

        assert client.get('/detail/91199').status_code == 404, '既无任务也无库行 → 404'

    def test_preview_from_real_publisher_is_viewable(self, client, clean_db):
        """端到端：**真实 publisher** 发布的预览 → 详情页兜底渲染。

        为什么不能只靠 `preview_task` 夹具装的快照：那是手工构造的任务字典。publisher
        把预览存在哪个键、任务字典长什么样，都由它决定 —— 夹具与详情页各写一份"约定"，
        两边一起写错也能同时通过（仓库里那条"接线通过、测试全绿、端到端零事件"的陷阱）。
        这条用例让 `routes_search._make_search_publisher` 与 `runtime.find_running_preview`
        真对接一次：只有发布侧的存储形态与读取侧一致，断言才会绿。
        """
        pid = 91005
        published = threading.Event()
        released = threading.Event()

        def fake_search_by_tag(*args, **kwargs):
            kwargs['progress']({'type': 'result', 'result': _preview_result(pid)})
            published.set()
            assert released.wait(10), '测试未放行 worker'
            return ([], False)

        try:
            with patch('app.search_by_tag', side_effect=fake_search_by_tag):
                client.get('/search?type=tag&query=preview-e2e')
                assert published.wait(5), 'worker 应已发布预览'
                # 此刻任务仍是 running，库里没有这行 —— 详情页只能靠快照渲染
                assert clean_db.query(models.Illust).filter(
                    models.Illust.pixiv_id == pid).first() is None
                with patch('pixivviewer.routes.gallery._fetch_original_urls',
                           side_effect=AssertionError('快照自带 original_urls，不该发网络请求')):
                    resp = client.get(f'/detail/{pid}')
        finally:
            released.set()

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert f'预览作品 {pid}' in html
        assert '<div class="preview-notice">' in html

    def test_committed_row_wins_over_preview_snapshot(self, client, clean_db, preview_task):
        """DB 是权威数据源：同一 pid 既有行又有运行中预览时，渲染的是行。"""
        pid = 91004
        illust = models.Illust(
            pixiv_id=pid, title='数据库里的标题', user_id=1, user_name='db画师',
            page_count=1, bookmark_count=5,
            thumb_url=f'https://i.pximg.net/c/250x250/img/{pid}.jpg',
        )
        illust.tags_list = ['db-tag']
        illust.original_urls_list = [
            f'https://i.pximg.net/img-original/img/2026/01/01/00/00/00/{pid}_p0.jpg',
        ]
        clean_db.add(illust)
        models.safe_commit(clean_db)

        preview_task([_preview_result(pid, title='过期的预览标题')])

        resp = client.get(f'/detail/{pid}')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert '数据库里的标题' in html
        assert '过期的预览标题' not in html
        assert '<div class="preview-notice">' not in html, '行已落库，不是预览态'


class TestRoutes:
    def test_gallery_page(self, client):
        resp = client.get('/gallery')
        assert resp.status_code == 200
        # 页面逻辑已抽离到静态 JS，验证外部脚本被正确引用
        assert b'page-gallery.js' in resp.data

    def test_settings_page(self, client):
        resp = client.get('/settings')
        assert resp.status_code == 200

    def test_downloads_page(self, client):
        resp = client.get('/downloads')
        assert resp.status_code == 200

    def test_index_page_cards_link_to_detail_without_lightbox(self, client):
        """搜索页卡片一律跳详情页 —— 共享灯箱已移除（2026-11-10）。

        历史：已提交卡曾弹共享灯箱（「打开详情」再跳转），预览卡因未落库
        （`/api/detail/<pid>` 返回 404、灯箱探测不到页源）单独走详情页。灯箱不好用、
        整体移除后两档合流为"点卡片即跳详情页"；预览卡仍由详情页的内存快照兜底给出中图。
        """
        resp = client.get('/explore')
        assert resp.status_code == 200
        assert b'lightbox' not in resp.data, '灯箱已移除，模板不得再引入 lightbox.js'

        js = pathlib.Path('pixivviewer/static/page-index.js').read_text(encoding='utf-8')
        assert 'lightbox' not in js, '搜索页脚本不得再引用灯箱'
        # 卡片点击 = 跳详情页（预览卡与已提交卡同一入口）
        assert 'window.location.href = `/detail/${r.pixiv_id}`' in js
        # 预览卡不渲染下载按钮：这条约束与灯箱无关，仍然成立
        assert '可预览 · 暂不可下载' in js


class TestDbIsolation:
    def test_engine_uses_temp_db(self):
        """P0-1 回归测试：测试 engine 必须指向临时库，而非生产 instance/pixiv.db。"""
        from pixivviewer import models
        assert 'pixiv_test_' in str(models.engine.url)


class TestSessionFactory:
    def _build(self, monkeypatch, proxy=''):
        # Session 构建在适配层：代理配置、Cookie 缓存等状态归 pixiv_client 所有，
        # 补丁必须打在它身上（fetcher 只再导出函数，见 pixiv_client 模块 docstring）。
        from pixivviewer import pixiv_client
        monkeypatch.setattr(pixiv_client, 'PROXY', proxy)
        monkeypatch.setattr(pixiv_client, '_load_cookie', lambda: None)
        monkeypatch.setattr(pixiv_client, '_cookie_value', 'test')
        return pixiv_client.build_pixiv_session()

    def test_proxy_applied_only_when_configured(self, monkeypatch):
        """PROXY 设了才挂上（http/https 两键都要），为空即直连（`proxies == {}`）。

        两档输入共用同一套前置（`_build`）、断言同构，故写成一个循环。
        """
        for proxy, expected in (
                ('', {}),
                ('http://127.0.0.1:7890',
                 {'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'})):
            s = self._build(monkeypatch, proxy=proxy)
            assert s.proxies == expected, f'proxy={proxy!r} 时 proxies 应为 {expected}'

    def test_pixiv_headers_present(self, monkeypatch):
        s = self._build(monkeypatch)
        assert s.headers['Referer'].startswith('https://')
        assert 'Mozilla' in s.headers['User-Agent']
        assert 'PHPSESSID=test' in s.headers['Cookie']


class TestCollectionApiRemoved:
    """收藏夹端点整体移除后必须 404（不是 500/403/302）。

    只断言"某个 URL 不通"证明不了路由删干净了：忘注册蓝图是 404，但视图内部引用
    已删模型会变成 500，装饰器/中间件拦下会变成 403 —— 后两者都会掩盖"端点还在"。
    这里按 spec 的端点清单逐条钉住方法+路径，并断言蓝图不再注册。
    """

    REMOVED_ENDPOINTS = [
        ('GET', '/api/collections'),
        ('POST', '/api/collections'),
        ('PUT', '/api/collections/999999'),
        ('DELETE', '/api/collections/999999'),
        ('GET', '/api/collections/999999/items'),
        ('POST', '/api/collections/999999/items'),
        ('DELETE', '/api/collections/999999/items/999999'),
        ('POST', '/api/collections/999999/items/batch'),
        ('DELETE', '/api/collections/999999/items/batch'),
        ('POST', '/api/collections/999999/items/999999/move'),
        ('GET', '/api/illust/999999/collections'),
        ('GET', '/api/favorite/999999'),
        ('POST', '/api/favorite/999999'),
    ]

    def test_removed_collection_endpoints_return_404(self, client):
        """13 个已删端点（方法+路径矩阵）都必须 404。

        矩阵写成一个循环而不是 `parametrize`：这是**一个不变式**（"收藏夹端点整块摘掉"），
        每条只是输入变体，失败信息带 `{method} {path}` 就能定位到具体端点。
        """
        # 带合法 CSRF token 与 body：403/400 都不算通过，只有"路由不存在"才是 404
        token = client.get('/csrf-token').get_json()['token']
        for method, path in self.REMOVED_ENDPOINTS:
            resp = client.open(path, method=method, json={},
                               headers={'X-CSRF-Token': token})
            assert resp.status_code == 404, f'{method} {path} 仍被注册'

    def test_collections_blueprint_not_registered(self, app):
        """蓝图清单是白名单：collections 蓝图必须整块摘掉，新增蓝图要显式过一遍这条。

        历史：移除收藏夹时是 7 → 6；2026-11-10 新增 `home`（首页）后又回到 7 个。
        写成精确清单而不是"不含 collections"，是为了让任何新蓝图都必须在此处登记，
        顺带提醒"路由归属哪个蓝图"这件事需要被想过。
        """
        assert 'collections' not in app.blueprints
        assert sorted(app.blueprints) == [
            'download', 'gallery', 'home', 'middleware', 'prefetch', 'search', 'settings',
        ]


class TestGalleryTriggersBookmarkFill:
    def test_gallery_kicks_fill_only_for_unfilled_zero_bookmark_records(
            self, client, clean_db, monkeypatch):
        """图库页只对"收藏数=0 **且**没有原图 URL"的记录触发后台详情补全。

        两档输入同一个不变式（补全触发判定），只有"是否已补全 / 收藏数"不同：
        - 收藏数=0 且未补全 → 必须进补全名单；
        - 收藏数 1500 或已带原图 URL → 不进（前者不需要补，后者补过了别重复拉）。
        """
        from pixivviewer import fetcher
        called = []
        monkeypatch.setattr(fetcher, '_kick_background_fill', lambda ids: called.append(list(ids)))
        clean_db.add(models.Illust(pixiv_id=91001, title='a', bookmark_count=0,
                                   download_status='done'))
        clean_db.add(models.Illust(pixiv_id=91002, title='b', bookmark_count=1500,
                                   download_status='done'))
        filled = models.Illust(pixiv_id=91003, title='c', bookmark_count=0,
                               download_status='done')
        filled.original_urls_list = ['https://i.pximg.net/91003_p0.jpg']
        clean_db.add(filled)
        clean_db.commit()

        r = client.get('/api/gallery?limit=10')
        assert r.status_code == 200
        assert called and 91001 in called[0], '未补全的 0 收藏记录必须触发补全'
        assert 91002 not in called[0], '收藏数非 0 的记录不需要补全'
        assert 91003 not in [pid for batch in called for pid in batch], \
            '已补全（有原图 URL）的记录不得重复触发补全'


class TestGalleryR18Filter:
    """图库 R18 过滤：默认不含 R18，显式 r18=all 才包含（与缓存页一致）。"""

    def test_gallery_r18_default_safe(self, client, clean_db):
        r18 = models.Illust(pixiv_id=95001, title='r18', download_status='done')
        r18.tags_list = ['R-18', 'original']
        r18g = models.Illust(pixiv_id=95002, title='r18g', download_status='done')
        r18g.tags_list = ['R-18G']
        safe = models.Illust(pixiv_id=95003, title='safe', download_status='done')
        safe.tags_list = ['original']
        clean_db.add_all([r18, r18g, safe])
        clean_db.commit()

        # 缺省 = safe：R-18 与 R-18G 都隐藏
        resp = client.get('/api/gallery?limit=10')
        assert resp.status_code == 200
        pids = [d['pixiv_id'] for d in resp.get_json()['data']]
        assert 95003 in pids
        assert 95001 not in pids
        assert 95002 not in pids

        # 显式 r18=all 时包含 R18
        resp = client.get('/api/gallery?limit=10&r18=all')
        assert resp.status_code == 200
        pids = [d['pixiv_id'] for d in resp.get_json()['data']]
        assert 95001 in pids and 95002 in pids and 95003 in pids


class TestFollowingRouteR18:
    """/api/following 的 R18 过滤：缺省 safe（默认不显示 R18），显式 r18_mode=all 才包含。"""

    def _patch_following(self, monkeypatch):
        from pixivviewer.routes import search as routes_search
        calls = []
        def fake_following(page=1, r18_mode='all'):
            calls.append({'page': page, 'r18_mode': r18_mode})
            return [{'pixiv_id': page}], False
        monkeypatch.setattr(routes_search, 'fetch_following', fake_following)
        return calls

    def test_following_r18_mode_defaults_to_safe(self, client, monkeypatch):
        """缺省 safe（默认不显示 R18），显式 `r18_mode=all` 才原样透传给 `fetch_following`。

        两档输入同一个不变式（缺省值 + 透传），前置是同一个 `_patch_following`，
        断言同构，故写成一个循环。
        """
        for qs, expected in (('', 'safe'), ('?r18_mode=all', 'all')):
            calls = self._patch_following(monkeypatch)
            resp = client.get('/api/following' + qs)
            assert resp.status_code == 200, f'/api/following{qs} 应返回 200'
            assert calls and calls[0]['r18_mode'] == expected, \
                f'{qs or "缺省"} 时 fetch_following 收到的 r18_mode 应为 {expected}，实际 {calls}'


class TestGalleryDeleteOrphans:
    """删除接口支持无 DB 记录的孤儿作品。

    孤儿 = downloads/<pid> 有文件但 Illust 表无行（DB 重置/丢行等原因产生）。
    旧行为：DELETE 查无行直接 404，孤儿在图库里删不掉、永久残留。
    """

    def _token(self, client):
        return client.get('/csrf-token').get_json()['token']

    def _patch_download_dir(self, monkeypatch, tmp_path):
        from pixivviewer import helpers
        monkeypatch.setattr(helpers, 'DOWNLOAD_DIR', str(tmp_path))
        return tmp_path

    def test_delete_orphan_removes_dir_and_logs(self, client, clean_db, monkeypatch, tmp_path):
        ddir = self._patch_download_dir(monkeypatch, tmp_path)
        orphan = ddir / '70001'
        orphan.mkdir()
        (orphan / '70001_p0.jpg').write_bytes(b'x' * 10)
        (orphan / '70001_p1.jpg').write_bytes(b'x' * 10)

        resp = client.delete('/api/gallery/70001',
                             headers={'X-CSRF-Token': self._token(client)})
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'deleted'
        assert not orphan.exists(), '孤儿目录应被删除'
        log = clean_db.query(models.DownloadLog).filter_by(pixiv_id=70001).all()
        assert len(log) == 1 and log[0].action == 'deleted'

    def test_delete_without_row_and_dir_returns_404(self, client, clean_db, monkeypatch, tmp_path):
        """既无 DB 行也无本地目录才算不存在，保持原 404 语义。"""
        self._patch_download_dir(monkeypatch, tmp_path)
        resp = client.delete('/api/gallery/70002',
                             headers={'X-CSRF-Token': self._token(client)})
        assert resp.status_code == 404

    def test_batch_delete_rejects_too_many_ids(self, client, clean_db):
        """批量删除必须限制单次 id 数（审计 P2-4）。

        ids 直接拼进 `IN (...)`，1MB body 能塞进几万个 id，逼近 SQLite 绑定参数
        上限（32766）并对每个 id 做一次磁盘删除。上限取 500，远超正常一屏操作。
        """
        resp = client.post('/api/gallery/batch-delete',
                           data=json.dumps({'ids': list(range(1, 502))}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': self._token(client)})
        assert resp.status_code == 400
        assert '500' in resp.get_json()['error']

    def test_batch_delete_covers_orphans(self, client, clean_db, monkeypatch, tmp_path):
        ddir = self._patch_download_dir(monkeypatch, tmp_path)
        orphan = ddir / '70003'
        orphan.mkdir()
        (orphan / '70003_p0.jpg').write_bytes(b'x' * 10)
        # 有 DB 行的作品：local_paths 指向临时目录里的真实文件，走原有删除路径
        known = ddir / '70004'
        known.mkdir()
        f = known / '70004_p0.jpg'
        f.write_bytes(b'x' * 10)
        illust = models.Illust(pixiv_id=70004, title='known', download_status='done')
        illust.local_paths_list = [str(f)]
        clean_db.add(illust)
        clean_db.commit()

        resp = client.post('/api/gallery/batch-delete',
                           data=json.dumps({'ids': [70003, 70004]}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': self._token(client)})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['deleted'] == 2 and data['failed'] == 0
        assert not orphan.exists() and not known.exists()


class TestGalleryDeleteTakesEffectImmediately:
    """删除后列表必须**立刻**反映，且重复删除要如实说"此前已无文件"。

    回归来源（用户报告）：删了带 DB 行的作品后卡片还会"存在几秒"，再删一次提示
    "已删除 0 个作品/文件"。根因是列表查询
    `download_status='done' OR pixiv_id IN (本地扫描到的 pid)`：删文件只把状态置回
    `None`、行还在，而**只有孤儿分支**让 `_scan_cache` 失效 —— 扫描缓存（TTL 30s）
    还认为文件在磁盘上，那一支就继续把这张卡捞出来。
    """

    def _token(self, client):
        return client.get('/csrf-token').get_json()['token']

    def _done_illust(self, clean_db, monkeypatch, tmp_path, pid):
        from pixivviewer import helpers
        monkeypatch.setattr(helpers, 'DOWNLOAD_DIR', str(tmp_path))
        work = tmp_path / str(pid)
        work.mkdir()
        f = work / f'{pid}_p0.jpg'
        f.write_bytes(b'x' * 10)
        illust = models.Illust(pixiv_id=pid, title='t', download_status='done',
                               bookmark_count=5)   # 非 0 收藏数：免触发后台补全的真实请求
        illust.local_paths_list = [str(f)]
        clean_db.add(illust)
        models.safe_commit(clean_db)
        return work

    def test_delete_takes_effect_at_once_and_repeat_says_already_gone(
            self, client, clean_db, monkeypatch, tmp_path):
        """一次删除要同时满足两件事：列表**立刻**不再有它；再删一次如实说"此前已删除"。

        前者是回归主症状（不失效 `_scan_cache` 时，列表查询的
        `pixiv_id IN (本地扫描到的 pid)` 那支会继续把卡片捞到 TTL 过期）；后者是这次
        删除的收尾语义（旧实现回 "已删除 0 个文件"，与"删除失败"在界面上无法区分
        —— 前端只看 `resp.ok` 与 `message`）。两件事共用同一套数据准备，故合在一起。
        """
        pid = 70011
        self._done_illust(clean_db, monkeypatch, tmp_path, pid)

        # 先请求一次把扫描缓存填上（模拟"页面已经打开过"）；不先填的话缓存本来就是
        # 空的，这条用例会退化成恒真
        first = client.get('/api/gallery?limit=10').get_json()
        assert [r['pixiv_id'] for r in first['data']] == [pid]

        resp = client.delete(f'/api/gallery/{pid}', headers={'X-CSRF-Token': self._token(client)})
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'deleted'

        after = client.get('/api/gallery?limit=10').get_json()
        assert after['data'] == [], '删除后必须立刻消失，而不是等目录扫描缓存过期（最多 30s）'
        assert after['total'] == 0

        again = client.delete(f'/api/gallery/{pid}', headers={'X-CSRF-Token': self._token(client)})
        assert again.status_code == 200, '重复删除不是错误：卡片照常移掉即可'
        body = again.get_json()
        assert body['status'] == 'no_file'
        assert '此前已删除' in body['message'], f'实际提示：{body}'

    def test_batch_delete_counts_already_gone_separately(
            self, client, clean_db, monkeypatch, tmp_path):
        """批量删除里"文件已不在"的作品单独计数，并让扫描缓存失效。"""
        from pixivviewer import helpers
        monkeypatch.setattr(helpers, 'DOWNLOAD_DIR', str(tmp_path / 'downloads'))
        (tmp_path / 'downloads').mkdir()
        gone = models.Illust(pixiv_id=70013, title='t', download_status='done',
                             bookmark_count=5)   # 无文件；非 0 收藏数免触发后台补全
        clean_db.add(gone)
        models.safe_commit(clean_db)

        resp = client.post('/api/gallery/batch-delete',
                           data=json.dumps({'ids': [70013]}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': self._token(client)})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['deleted'] == 0 and data['no_file'] == 1 and data['failed'] == 0
        assert '此前已无文件' in data['message'], f'实际提示：{data["message"]}'


class TestGalleryPageSize:
    """图库页大小必须跟随 `items_per_page`（服务端下发），不得在 JS 里硬编码。

    回归来源（用户报告）：图库里"一行四个作品，最后空出两个空位"，而设置页里的
    "每页展示作品数"在图库上完全不生效 —— 搜索页/缓存页都跟随设置，只有图库把
    50 写死在 JS 里（`col-lg-3` 一行 4 个 → 50 = 12×4 + 2）。
    """

    def test_page_size_wiring_is_server_driven(self, client, monkeypatch):
        """两端一起钉：服务端下发 meta，前端读 meta 且不再硬编码。"""
        monkeypatch.setattr(routes_gallery, 'ITEMS_PER_PAGE', 24)

        html = client.get('/gallery').get_data(as_text=True)

        assert '<meta name="items-per-page" content="24">' in html
        js = pathlib.Path('pixivviewer/static/page-gallery.js').read_text(encoding='utf-8')
        assert 'meta[name="items-per-page"]' in js, '页大小必须读服务端下发的值'
        assert 'const PAGE_SIZE = 50' not in js, '不得再硬编码页大小'

    def test_detail_navigation_page_size_is_server_driven(self, client, clean_db, monkeypatch):
        """详情页跨作品翻页也必须跟随 `items_per_page`（审计 P1-3）。

        `page-detail.js` 曾把页大小硬编码成 50，而图库按 `items_per_page`（默认
        24）分页：图库写入的全局 `pos` 与详情页按 50 重算的页号/偏移对不上，
        第 2 页起"上一作/下一作"会跳错作品或直接失效。这里同时钉住三件事：
        详情页下发 meta、详情 JS 读 meta、图库与详情对 sessionStorage 序列的
        版本号一致（v=2）。
        """
        monkeypatch.setattr(routes_gallery, 'ITEMS_PER_PAGE', 24)
        illust = models.Illust(pixiv_id=88001, title='nav', user_id=1,
                               page_count=1, bookmark_count=5)
        illust.original_urls_list = ['https://i.pximg.net/img-original/img/a_p0.jpg']
        clean_db.add(illust)
        models.safe_commit(clean_db)

        html = client.get('/detail/88001').get_data(as_text=True)

        assert '<meta name="items-per-page" content="24">' in html
        detail_js = pathlib.Path('pixivviewer/static/page-detail.js').read_text(encoding='utf-8')
        assert 'meta[name="items-per-page"]' in detail_js, '详情页页大小必须读服务端下发的值'
        assert 'const PAGE_SIZE = 50' not in detail_js, '不得再硬编码页大小'
        gallery_js = pathlib.Path('pixivviewer/static/page-gallery.js').read_text(encoding='utf-8')
        assert 'v: 2' in gallery_js, '图库写入的序列版本必须与详情页读取的一致'
        assert 's.v !== 2' in detail_js, '详情页读取的序列版本必须与图库写入的一致'

    def test_api_gallery_default_limit_follows_setting(self, client, clean_db, monkeypatch,
                                                       tmp_path):
        """不传 limit 时默认页大小同样是 `items_per_page`（前后端同源）。"""
        from pixivviewer import helpers
        monkeypatch.setattr(routes_gallery, 'ITEMS_PER_PAGE', 3)
        monkeypatch.setattr(helpers, 'DOWNLOAD_DIR', str(tmp_path))
        for pid in range(70020, 70026):        # 6 件已下载
            clean_db.add(models.Illust(pixiv_id=pid, title=f't{pid}', download_status='done',
                                       bookmark_count=5))   # 非 0：免触发后台补全
        models.safe_commit(clean_db)

        data = client.get('/api/gallery').get_json()

        assert len(data['data']) == 3, f'默认页大小应为设置值 3，实际 {len(data["data"])}'
        assert data['total'] == 6
        assert data['has_more'] is True


class TestDownloadFileZip:
    """`/download_file` 打包下载（审计 S15：大包不再拼在内存里）。"""

    @staticmethod
    def _make_illust(clean_db, tmp_path, pid, sizes):
        files = []
        for i, size in enumerate(sizes):
            f = tmp_path / f'{pid}_p{i}.jpg'
            f.write_bytes(b'x' * size)
            files.append(str(f))
        illust = models.Illust(pixiv_id=pid, title='zip-me', download_status='done')
        illust.local_paths_list = files
        clean_db.add(illust)
        clean_db.commit()
        return files, illust

    @staticmethod
    def _spy_tempfile(monkeypatch):
        """记录真正被创建的临时文件路径（默认阈值 200MB，普通用例不会走到）。"""
        import tempfile as _tempfile
        created: list[str] = []
        real = _tempfile.NamedTemporaryFile

        def _factory(*args, **kwargs):
            handle = real(*args, **kwargs)
            created.append(handle.name)
            return handle

        monkeypatch.setattr(routes_download.tempfile, 'NamedTemporaryFile', _factory)
        return created

    def test_threshold_switches_memory_buffer_to_tempfile(self, client, clean_db, tmp_path,
                                                          monkeypatch):
        """阈值之内走内存缓冲（不落临时文件），超过阈值改落临时文件 —— 包内容两者一致。

        两档是**同一个不变式**（阈值决定缓冲方式），故合在一起：先后各跑一次，而不是
        拆成两条只有"阈值/期望"不同的用例。小包仍然走内存才说明"大包才落盘"没被改坏。
        """
        created = self._spy_tempfile(monkeypatch)

        # 档 1：阈值内（默认 200MB）→ 内存缓冲，一份临时文件都不建
        self._make_illust(clean_db, tmp_path, 88001, [100, 200, 300])
        resp = client.get('/download_file/88001')

        assert resp.status_code == 200
        assert resp.mimetype == 'application/zip'
        assert 'zip-me.zip' in resp.headers['Content-Disposition']
        assert created == [], '阈值内不该落临时文件'
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            assert sorted(zf.namelist()) == ['zip-me_p0.jpg', 'zip-me_p1.jpg', 'zip-me_p2.jpg']
            assert zf.read('zip-me_p1.jpg') == b'x' * 200

        # 档 2：阈值压到 10 字节 → 落临时文件，内容与内存档一致
        monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
        self._make_illust(clean_db, tmp_path, 88002, [100, 200, 300])
        resp = client.get('/download_file/88002')

        assert resp.status_code == 200
        assert resp.mimetype == 'application/zip'
        assert len(created) == 1, f'超过阈值应当只建一份临时文件：{created}'
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            assert sorted(zf.namelist()) == ['zip-me_p0.jpg', 'zip-me_p1.jpg', 'zip-me_p2.jpg']
            assert zf.read('zip-me_p2.jpg') == b'x' * 300
        resp.close()

    def test_tempfile_is_cleaned_on_every_exit_path(self, client, clean_db, tmp_path,
                                                    monkeypatch):
        """临时文件在**每一条**响应出口上都不许残留（大包几百 MB，漏一份就是垃圾）。

        六档输入是**同一个不变式**（清理必须兜住所有出口），只有"出口"不同 —— 所以合
        成一条循环，而不是六条只有请求形状不同的用例：
        - GET + close：body 包装器的 close 路径（响应结束时清）；
        - GET 读完整包：EOF 路径，**不靠 close** 也得清（清理挂在包装器上）；
        - HEAD：根本没有 body（不会迭代），只能靠 close 兜住；
        - 416（Range 不可满足，`send_file` 抛异常）：异常出口；
        - 206（Range 正常，body 走 `_RangeWrapper`）：包装器不得破坏分片；
        - 304（非内容响应：body 为空、清理挂不上）：必须**当场**删且释放句柄。真实路径
          上很难自然触发（每次请求都重建临时文件，ETag 必然不同），故用替身把
          `send_file` 的返回码改成 304 —— 它是"清理挂不上 body"这条兜底分支的唯一入口：
          既不能漏文件，也不能因为句柄还开着而删不掉。
        """
        # (标签, 方法, 请求头, 期望状态码, body 检查, 是否必须先 close 才会消失)
        cases = (
            ('GET + close（响应结束路径）', 'get', None, 200, None, True),
            ('GET 读完整包（EOF 路径）', 'get', None, 200, 'full-read', False),
            ('HEAD（不发 body）', 'head', None, 200, None, True),
            ('Range 不可满足 → 416', 'get', {'Range': 'bytes=999999999-'}, 416, None, False),
            ('Range 0-99 → 206', 'get', {'Range': 'bytes=0-99'}, 206, 100, True),
        )
        for n, (label, verb, headers, expected, body, close_first) in enumerate(cases):
            pid = 88100 + n
            created = self._spy_tempfile(monkeypatch)
            monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
            self._make_illust(clean_db, tmp_path, pid, [100, 200])

            resp = getattr(client, verb)(f'/download_file/{pid}', headers=headers)

            assert resp.status_code == expected, f'{label}：状态码应为 {expected}'
            assert len(created) == 1, f'{label}：应只建一份临时文件，实际 {created}'
            if body == 'full-read':
                with zipfile.ZipFile(io.BytesIO(resp.get_data())) as zf:
                    assert len(zf.namelist()) == 2, f'{label}：整包内容必须完整'
            elif isinstance(body, int):
                assert len(resp.get_data()) == body, f'{label}：{body} 字节的分片长度必须正确'

            if close_first:
                # close 是这条出口的清理触发点：必须先关再断言，否则等于在测另一条路径
                resp.close()
                assert not os.path.exists(created[0]), f'{label}：临时文件必须被删除'
            else:
                assert not os.path.exists(created[0]), \
                    f'{label}：不靠 close 也必须清掉临时文件'
                resp.close()

        # 304 档：send_file 替身把响应改成无内容（清理挂不上 body）
        created = self._spy_tempfile(monkeypatch)
        monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
        self._make_illust(clean_db, tmp_path, 88199, [100, 200])
        real_send_file = routes_download.send_file

        def _not_modified(path, **kwargs):
            r = real_send_file(path, **kwargs)
            r.status_code = 304
            return r

        monkeypatch.setattr(routes_download, 'send_file', _not_modified)

        resp = client.get('/download_file/88199')

        assert resp.status_code == 304, '304 档：替身必须让响应变成无内容'
        assert len(created) == 1, f'304 档：应只建一份临时文件，实际 {created}'
        assert not os.path.exists(created[0]), '304 档：无内容响应也必须当场清掉临时文件'

    def test_download_file_skips_disappeared_file(self, client, clean_db, tmp_path, monkeypatch):
        """打包途中某个文件消失（TOCTOU）：跳过它，其余照常打包，不 500。"""
        files, _ = self._make_illust(clean_db, tmp_path, 88004, [100, 100])
        monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
        created = self._spy_tempfile(monkeypatch)

        real_write = zipfile.ZipFile.write

        def _flaky_write(self, filename, arcname=None, **kwargs):
            if str(filename) == files[0]:
                raise FileNotFoundError(f'模拟打包途中文件消失: {filename}')
            return real_write(self, filename, arcname, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, 'write', _flaky_write)

        resp = client.get('/download_file/88004')

        assert resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            assert zf.namelist() == ['zip-me_p1.jpg'], '消失的那个应被跳过，其余照常'
        resp.close()

    def test_download_file_all_files_gone_returns_404(self, client, clean_db, tmp_path,
                                                      monkeypatch):
        """全部条目都在打包时消失 → 404「文件已丢失」，且临时文件不残留。"""
        files, _ = self._make_illust(clean_db, tmp_path, 88005, [100, 100])
        monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
        created = self._spy_tempfile(monkeypatch)

        def _always_fail(self, filename, arcname=None, **kwargs):
            raise FileNotFoundError(f'模拟打包途中文件消失: {filename}')

        monkeypatch.setattr(zipfile.ZipFile, 'write', _always_fail)

        resp = client.get('/download_file/88005')

        assert resp.status_code == 404
        assert '文件已丢失' in resp.get_json()['error']
        assert files, '用例前提：文件确实曾经存在'
        assert len(created) == 1 and not os.path.exists(created[0]), '失败路径也必须清临时文件'

    def test_download_file_single_file_not_zipped(self, client, clean_db, tmp_path):
        """单文件仍直接返回原图（不打包）——行为不变。"""
        files, _ = self._make_illust(clean_db, tmp_path, 88006, [64])

        resp = client.get('/download_file/88006')

        assert resp.status_code == 200
        assert resp.data == b'x' * 64
        assert resp.mimetype == 'image/jpeg'
        assert resp.headers['Content-Disposition'].endswith('zip-me.jpg')


def _decode_proxied(proxied: str) -> str:
    """`/thumb/<b64>` → 真实图片 URL（前端候选链拿到的就是这个地址）。"""
    assert proxied.startswith('/thumb/'), f'图源必须走 /thumb 代理：{proxied}'
    encoded = proxied[len('/thumb/'):]
    return base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode()


class TestDetailApiMediumUrls:
    def test_medium_urls_are_proxied_jpg_masters(self, client, clean_db):
        """/api/detail 的 `medium_urls`：数量与原图一致、都走 /thumb、都指向 `_master1200.jpg`。

        含 PNG 原图这一档：master 是图床**统一重编码的 JPEG**，拼成 `.png` 必然 404
        —— 这正是"很多作品的中图加载不出来"的根因（中图 404 → 失败冷却 → 退到 1~5MB
        的原图，常超过 /thumb 的 30s 读超时 → 只剩缩略图兜底）。
        """
        multi = models.Illust(pixiv_id=92001, title='multi-page', page_count=2)
        multi.original_urls_list = [
            'https://i.pximg.net/img-original/img/0001/01/15/00/00/00/92001_p0.jpg',
            'https://i.pximg.net/img-original/img/0001/01/15/00/00/00/92001_p1.jpg',
        ]
        png = models.Illust(pixiv_id=92002, title='png-original', page_count=1)
        png.original_urls_list = [
            'https://i.pximg.net/img-original/img/2026/08/29/17/21/55/92002_p0.png',
        ]
        clean_db.add_all([multi, png])
        clean_db.commit()

        r = client.get('/api/detail/92001')
        assert r.status_code == 200
        data = r.get_json()
        medium = data['medium_urls']
        assert len(medium) == len(multi.original_urls_list) == 2, '中图数量必须与原图页数一致'
        for u in medium:
            assert _decode_proxied(u).endswith('_master1200.jpg'), f'中图地址不对：{u}'

        [proxied] = client.get('/api/detail/92002').get_json()['medium_urls']
        assert _decode_proxied(proxied) == (
            'https://i.pximg.net/c/600x600/img-master/img/2026/08/29/17/21/55/'
            '92002_p0_master1200.jpg'), '中图扩展名必须固定 .jpg，不能沿用原图的 .png'


class TestDetailPageMediumUrls:
    """详情页嵌入给前端的 `mediumUrls`（`page-detail.js` 的图源候选链第一档）。"""

    def test_png_original_yields_jpg_medium_source(self, client, clean_db):
        """PNG 原图的作品，详情页首图源必须是可用的中图地址（能直接请求到 200）。"""
        illust = models.Illust(pixiv_id=92003, title='png-original', page_count=1,
                               thumb_url='https://i.pximg.net/c/250x250_80_a2/img-master/'
                                         'img/2026/08/29/17/21/55/92003_p0_square1200.jpg')
        illust.original_urls_list = [
            'https://i.pximg.net/img-original/img/2026/08/29/17/21/55/92003_p0.png',
        ]
        clean_db.add(illust)
        models.safe_commit(clean_db)

        resp = client.get('/detail/92003')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        blob = re.search(r'<script type="application/json" id="detailData">(.*?)</script>',
                         html, re.S)
        assert blob, '详情页必须把图源以 JSON 块交给前端'
        data = json.loads(blob.group(1))
        assert data['mediumUrls'], 'PNG 原图的作品也必须有中图候选（否则首图只能退到缩略图）'
        [proxied] = data['mediumUrls']
        assert _decode_proxied(proxied).endswith('92003_p0_master1200.jpg')
        assert data['originalProxied'], '原图档作为兜底仍在'
