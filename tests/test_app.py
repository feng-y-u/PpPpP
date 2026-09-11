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
import fetcher
import helpers
import models
import routes_download
from config import ITEMS_PER_PAGE


class TestIndexRoute:
    def test_get_returns_200(self, client):
        resp = client.get('/')
        assert resp.status_code == 200
        assert b'Pixiv' in resp.data or b'\xe6\x90\x9c\xe7\xb4\xa2' in resp.data


def _route_shape(route: str) -> str:
    """把路由占位符（`<int:pixiv_id>`）与具体 id 都抹掉，便于矩阵与源码对账。

    `/api/collections/<int:collection_id>` 与矩阵里的 `/api/collections/999999`
    归一后都是 `/api/collections/`。
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
    # 参数是"有副作用的真实请求"：漏挂装饰器的端点会真的执行下去（例如删掉收藏夹、
    # 写 settings.json），所以用例只用必然无效的 ID/空 body —— 万一哪天装饰器被摘掉，
    # 这里会变成 404/400 而不是 403，测试失败且不会造成破坏。
    MUTATING_ENDPOINTS = [
        ('POST', '/login'),
        ('POST', '/api/settings'),
        ('POST', '/api/settings/unlock'),
        ('POST', '/api/blocked-tags'),
        ('DELETE', '/api/blocked-tags/999999'),
        ('POST', '/api/auto-follow/config'),
        ('POST', '/api/collections'),
        ('PUT', '/api/collections/999999'),
        ('DELETE', '/api/collections/999999'),
        ('POST', '/api/collections/999999/items'),
        ('DELETE', '/api/collections/999999/items/999999'),
        ('POST', '/api/collections/999999/items/batch'),
        ('DELETE', '/api/collections/999999/items/batch'),
        ('POST', '/api/collections/999999/items/999999/move'),
        ('POST', '/download/999999'),
        ('POST', '/api/download/batch'),
        ('POST', '/download/cancel/999999'),
        ('POST', '/download/reset/999999'),
        ('DELETE', '/api/gallery/999999'),
        ('POST', '/api/gallery/batch-delete'),
        ('DELETE', '/api/thumb/redirect-hosts'),
        ('POST', '/api/open-dir'),
        ('POST', '/api/favorite/999999'),
        ('POST', '/api/prefetch/config'),
        ('POST', '/api/prefetch/tags'),
        ('DELETE', '/api/prefetch/tags/999999'),
        ('POST', '/api/prefetch/refresh'),
        ('POST', '/api/prefetch/refresh-reset'),
        ('POST', '/api/cache/items/999999/delete'),
    ]

    @pytest.mark.parametrize('method,path', MUTATING_ENDPOINTS,
                             ids=[f'{m} {p}' for m, p in MUTATING_ENDPOINTS])
    def test_all_mutating_endpoints_require_csrf(self, client, method, path):
        """每个修改型端点缺 CSRF 头一律 403 —— 防止将来新增路由漏挂装饰器。"""
        resp = client.open(path, method=method, json={})

        assert resp.status_code == 403, f'{method} {path} 未受 CSRF 保护'
        assert resp.get_json()['error'] == 'CSRF校验失败'

    def test_mutating_endpoint_matrix_is_complete(self):
        """矩阵必须覆盖源码里所有修改型路由（将来新增路由时这里会先失败）。

        只做静态对账：从 `routes_*.py` 抓 `@bp.route(..., methods=[...])` 与矩阵比对。
        "装饰器是否真的挂在函数上"由参数化用例负责 —— 两件事都得有人管，缺一个都会
        让"漏挂 CSRF"重新变成静默风险。
        """
        pattern = re.compile(r"""@bp\.route\(\s*['"]([^'"]+)['"]([^)]*)\)""", re.S)
        found = set()
        for source_file in pathlib.Path(app.__file__).resolve().parent.glob('routes_*.py'):
            source = source_file.read_text(encoding='utf-8')
            for route, tail in pattern.findall(source):
                if 'methods=' not in tail:
                    continue                       # 纯 GET 路由不受 CSRF 约束
                for method in re.findall(r"""['"](POST|PUT|DELETE|PATCH)['"]""", tail):
                    found.add((method, _route_shape(route)))

        covered = {(method, _route_shape(path)) for method, path in self.MUTATING_ENDPOINTS}

        assert not found - covered, f'以下修改型端点未被 CSRF 矩阵覆盖：{sorted(found - covered)}'

    def test_post_without_csrf_returns_403(self, client):
        resp = client.post('/api/blocked-tags',
                           data=json.dumps({'tag': 'test'}),
                           content_type='application/json')
        assert resp.status_code == 403
        assert resp.get_json()['error'] == 'CSRF校验失败'

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


class TestSearch:
    def _poll(self, client, task_id, timeout=50):
        """轮询异步搜索任务直到终态（done 200 / error 401·502 / 丢失 404）。"""
        import time
        for _ in range(timeout):
            r = client.get(f'/api/search/status/{task_id}')
            if r.status_code == 404:
                return r
            data = r.get_json()
            if data and data.get('status') != 'running':
                return r
            time.sleep(0.05)
        raise AssertionError(f'搜索任务 {task_id} 超时未完成')

    @patch('app.browse_discovery')
    def test_empty_query_calls_discovery(self, mock_discovery, client):
        mock_discovery.return_value = ([], False)
        resp = client.get('/search')
        assert resp.status_code == 200
        task_id = resp.get_json()['task_id']
        final = self._poll(client, task_id)
        assert final.get_json()['status'] == 'done'
        mock_discovery.assert_called_once()

    @patch('app.search_by_tag')
    def test_search_by_tag_called(self, mock_search, client):
        mock_search.return_value = ([], False)
        resp = client.get('/search?type=tag&query=初音ミク')
        assert resp.status_code == 200
        task_id = resp.get_json()['task_id']
        self._poll(client, task_id)
        mock_search.assert_called_once()
        args, kwargs = mock_search.call_args
        assert '初音ミク' in args

    @patch('app.search_by_user')
    def test_search_by_user_called(self, mock_search, client):
        mock_search.return_value = ([], False)
        resp = client.get('/search?type=user&query=12345')
        assert resp.status_code == 200
        task_id = resp.get_json()['task_id']
        self._poll(client, task_id)
        mock_search.assert_called_once()

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

    def test_search_user_non_digit_returns_400(self, client):
        resp = client.get('/search?type=user&query=abc')
        assert resp.status_code == 400

    @patch('app.paginated_search')
    def test_new_search_cancels_running_task(self, mock_paginated, client):
        """搜索中途改条件重搜：提交新任务自动取消在途旧任务。

        旧任务在下一个检查点（翻页前/后、详情请求前）以 cancelled 终态结束，
        不再继续烧令牌桶；新任务不受影响，正常完成。
        """
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

        final1 = self._poll(client, task1)
        assert final1.status_code == 200, 'cancelled 不是错误，不应走 502'
        data1 = final1.get_json()
        assert data1['status'] == 'cancelled'
        assert data1['results'] == []

        final2 = self._poll(client, task2)
        assert final2.get_json()['status'] == 'done'
        assert len(final2.get_json()['results']) == 1

    @patch('app.paginated_search')
    def test_cancel_event_set_synchronously_on_submit(self, mock_paginated, client):
        """取消在提交新任务的请求内同步完成，不等旧任务自然结束。"""
        from runtime import _search_tasks
        started = threading.Event()

        def blocking_fn(*args, **kwargs):
            started.set()
            deadline = time.time() + 10
            while time.time() < deadline and not fetcher._cancelled():
                time.sleep(0.01)
            raise fetcher.SearchCancelledError()

        mock_paginated.side_effect = blocking_fn
        resp1 = client.get('/search?type=user&query=111')
        task1 = resp1.get_json()['task_id']
        assert started.wait(5), '旧任务应已开始运行'

        client.get('/search?type=user&query=222')   # 提交新搜索
        assert _search_tasks[task1]['cancel_event'].is_set()

        final = self._poll(client, task1)
        assert final.get_json()['status'] == 'cancelled'

    def test_search_long_query_returns_400(self, client):
        resp = client.get('/search?type=tag&query=' + 'a' * 201)
        assert resp.status_code == 400

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

    @patch('app.paginated_search', side_effect=RuntimeError('boom'))
    def test_task_error_returns_502(self, mock_paginated, client):
        resp = client.get('/search?type=tag&query=test')
        assert resp.status_code == 200
        task_id = resp.get_json()['task_id']
        final = self._poll(client, task_id)
        assert final.status_code == 502
        assert final.get_json()['error']

    def test_task_auth_error_returns_401(self, client):
        from fetcher import PixivAuthError
        with patch('app.search_by_tag', side_effect=PixivAuthError('auth')):
            resp = client.get('/search?type=tag&query=test')
            task_id = resp.get_json()['task_id']
            final = self._poll(client, task_id)
            assert final.status_code == 401

    def test_task_not_found_returns_404(self, client):
        resp = client.get('/api/search/status/no-such-task')
        assert resp.status_code == 404

    def test_task_cleanup_after_ttl(self, client, monkeypatch):
        import app
        with patch('app.search_by_tag', return_value=([], False)):
            resp = client.get('/search?type=tag&query=test')
            task_id = resp.get_json()['task_id']
            self._poll(client, task_id)
        # 缩短 TTL 并强制清理
        monkeypatch.setattr('app.SEARCH_TASK_TTL', -1)
        app._cleanup_search_tasks()
        r = client.get(f'/api/search/status/{task_id}')
        assert r.status_code == 404


class TestRoutes:
    def test_csrf_token(self, client):
        resp = client.get('/csrf-token')
        assert resp.status_code == 200
        assert 'token' in resp.get_json()

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


class TestDbIsolation:
    def test_engine_uses_temp_db(self):
        """P0-1 回归测试：测试 engine 必须指向临时库，而非生产 instance/pixiv.db。"""
        import models
        assert 'pixiv_test_' in str(models.engine.url)


class TestSessionFactory:
    def _build(self, monkeypatch, proxy=''):
        import fetcher
        monkeypatch.setattr(fetcher, 'PROXY', proxy)
        monkeypatch.setattr(fetcher, '_load_cookie', lambda: None)
        monkeypatch.setattr(fetcher, '_cookie_value', 'test')
        return fetcher.build_pixiv_session()

    def test_proxy_applied(self, monkeypatch):
        s = self._build(monkeypatch, proxy='http://127.0.0.1:7890')
        assert s.proxies == {'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'}

    def test_no_proxy_by_default(self, monkeypatch):
        s = self._build(monkeypatch, proxy='')
        assert s.proxies == {}

    def test_pixiv_headers_present(self, monkeypatch):
        s = self._build(monkeypatch)
        assert s.headers['Referer'].startswith('https://')
        assert 'Mozilla' in s.headers['User-Agent']
        assert 'PHPSESSID=test' in s.headers['Cookie']


class TestCollectionItemPositionAssignment:
    def _token(self, client):
        return client.get('/csrf-token').get_json()['token']

    def _create_coll(self, client):
        token = self._token(client)
        r = client.post('/api/collections',
                        data=json.dumps({'name': 'pos-test'}),
                        content_type='application/json',
                        headers={'X-CSRF-Token': token})
        return r.get_json()['id'], token

    def test_first_item_gets_1000(self, client, clean_db):
        cid, token = self._create_coll(client)
        r = client.post(f'/api/collections/{cid}/items',
                        data=json.dumps({'pixiv_id': 70001}),
                        content_type='application/json',
                        headers={'X-CSRF-Token': token})
        assert r.status_code == 201
        assert r.get_json()['position'] == 1000.0

    def test_second_item_gets_2000(self, client, clean_db):
        cid, token = self._create_coll(client)
        client.post(f'/api/collections/{cid}/items',
                    data=json.dumps({'pixiv_id': 70001}),
                    content_type='application/json',
                    headers={'X-CSRF-Token': token})
        r = client.post(f'/api/collections/{cid}/items',
                        data=json.dumps({'pixiv_id': 70002}),
                        content_type='application/json',
                        headers={'X-CSRF-Token': token})
        assert r.status_code == 201
        assert r.get_json()['position'] == 2000.0

    def test_batch_add_increments(self, client, clean_db):
        cid, token = self._create_coll(client)
        r = client.post(f'/api/collections/{cid}/items/batch',
                        data=json.dumps({'pixiv_ids': [70010, 70011, 70012]}),
                        content_type='application/json',
                        headers={'X-CSRF-Token': token})
        assert r.status_code == 200
        import models
        with models.get_session() as s:
            items = s.query(models.CollectionItem).filter(
                models.CollectionItem.collection_id == cid
            ).order_by(models.CollectionItem.pixiv_id).all()
        assert sorted(it.position for it in items) == [1000.0, 2000.0, 3000.0]

    def test_list_returns_by_position(self, client, clean_db):
        import models
        coll = models.Collection(name='list-order-test')
        clean_db.add(coll); clean_db.commit()
        for pid, pos in [(30100, 3000.0), (30101, 1000.0), (30102, 2000.0)]:
            clean_db.add(models.CollectionItem(collection_id=coll.id, pixiv_id=pid, position=pos))
        clean_db.commit()
        r = client.get(f'/api/collections/{coll.id}/items?limit=10')
        assert r.status_code == 200
        data = r.get_json()
        assert [d['pixiv_id'] for d in data['data']] == [30101, 30102, 30100]


class TestGalleryPositionOrder:
    def test_gallery_orders_by_position_when_collection(self, client, clean_db):
        import models
        coll = models.Collection(name='gallery-pos')
        clean_db.add(coll); clean_db.commit()
        pids = [40001, 40002, 40003]
        for pid in pids:
            il = models.Illust(pixiv_id=pid, title=f'p{pid}', download_status='done')
            clean_db.add(il)
        clean_db.commit()
        positions = {40001: 3000.0, 40002: 1000.0, 40003: 2000.0}
        for pid, pos in positions.items():
            clean_db.add(models.CollectionItem(collection_id=coll.id, pixiv_id=pid, position=pos))
        clean_db.commit()
        r = client.get(f'/api/gallery?collection_id={coll.id}&limit=10')
        assert r.status_code == 200
        data = r.get_json()
        returned_pids = [item['pixiv_id'] for item in data['data'] if item.get('pixiv_id') in pids]
        assert returned_pids == [40002, 40003, 40001]


class TestCollectionItemMove:
    def _token(self, client):
        return client.get('/csrf-token').get_json()['token']

    def _setup(self, client, clean_db, n=3):
        import models
        coll = models.Collection(name='move-test')
        clean_db.add(coll); clean_db.commit()
        token = self._token(client)
        for i in range(n):
            clean_db.add(models.CollectionItem(collection_id=coll.id, pixiv_id=50000 + i,
                                               position=(i + 1) * 1000.0))
        clean_db.commit()
        return coll.id, token

    def test_move_up_inserts_midpoint(self, client, clean_db):
        cid, token = self._setup(client, clean_db)  # [50000@1000, 50001@2000, 50002@3000]
        r = client.post(f'/api/collections/{cid}/items/50002/move',
                        data=json.dumps({'direction': 'up'}),
                        content_type='application/json',
                        headers={'X-CSRF-Token': token})
        assert r.status_code == 200
        assert r.get_json()['position'] == 1500.0
        assert r.get_json()['rebalanced'] is False
        import models
        with models.get_session() as s:
            order = [it.pixiv_id for it in s.query(models.CollectionItem)
                     .filter(models.CollectionItem.collection_id == cid)
                     .order_by(models.CollectionItem.position).all()]
        assert order == [50000, 50002, 50001]

    def test_move_up_to_top_when_second(self, client, clean_db):
        cid, token = self._setup(client, clean_db, n=2)
        r = client.post(f'/api/collections/{cid}/items/50001/move',
                        data=json.dumps({'direction': 'up'}),
                        content_type='application/json',
                        headers={'X-CSRF-Token': token})
        assert r.status_code == 200
        assert r.get_json()['position'] == 0.0

    def test_move_up_on_first_returns_400(self, client, clean_db):
        cid, token = self._setup(client, clean_db)
        r = client.post(f'/api/collections/{cid}/items/50000/move',
                        data=json.dumps({'direction': 'up'}),
                        content_type='application/json',
                        headers={'X-CSRF-Token': token})
        assert r.status_code == 400

    def test_move_down_on_last_returns_400(self, client, clean_db):
        cid, token = self._setup(client, clean_db)
        r = client.post(f'/api/collections/{cid}/items/50002/move',
                        data=json.dumps({'direction': 'down'}),
                        content_type='application/json',
                        headers={'X-CSRF-Token': token})
        assert r.status_code == 400

    def test_move_down_two_items(self, client, clean_db):
        cid, token = self._setup(client, clean_db, n=2)
        r = client.post(f'/api/collections/{cid}/items/50000/move',
                        data=json.dumps({'direction': 'down'}),
                        content_type='application/json',
                        headers={'X-CSRF-Token': token})
        assert r.status_code == 200
        assert r.get_json()['position'] == 3000.0

    def test_optimistic_lock_valid(self, client, clean_db):
        cid, token = self._setup(client, clean_db)
        import models
        with models.engine.connect() as conn:
            r = conn.execute(text(
                'UPDATE collection_items SET position=:np WHERE collection_id=:c AND pixiv_id=:p AND position=:op'
            ), {'np': 555.0, 'c': cid, 'p': 50002, 'op': 3000.0})
            conn.commit()
            assert r.rowcount == 1
        with models.engine.connect() as conn:
            r = conn.execute(text(
                'UPDATE collection_items SET position=:np WHERE collection_id=:c AND pixiv_id=:p AND position=:op'
            ), {'np': 555.0, 'c': cid, 'p': 50002, 'op': 9999.0})
            conn.commit()
            assert r.rowcount == 0

    def test_move_rebalance_uses_refreshed_position(self, client, clean_db):
        """回归：重排后条目位置已变化时，乐观锁须用重排后的新位置（曾误报 409）。"""
        import models
        coll = models.Collection(name='reb-test2')
        clean_db.add(coll); clean_db.commit()
        # 三个紧密间距（gap<1.0）→ 移动必触发 rebalance，且 70003 重排后位置会变化
        for pid, pos in [(70001, 1000.0), (70002, 1000.4), (70003, 1000.8)]:
            clean_db.add(models.CollectionItem(collection_id=coll.id, pixiv_id=pid, position=pos))
        clean_db.commit()
        token = self._token(client)
        r = client.post(f'/api/collections/{coll.id}/items/70003/move',
                        data=json.dumps({'direction': 'up'}),
                        content_type='application/json',
                        headers={'X-CSRF-Token': token})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['rebalanced'] is True
        with models.get_session() as s:
            items = s.query(models.CollectionItem).filter(
                models.CollectionItem.collection_id == coll.id
            ).order_by(models.CollectionItem.position).all()
        assert [it.pixiv_id for it in items] == [70001, 70003, 70002]

        import models
        coll = models.Collection(name='reb-test')
        clean_db.add(coll); clean_db.commit()
        for pid, pos in [(70001, 1000.0), (70002, 1000.4), (70003, 3000.0)]:
            clean_db.add(models.CollectionItem(collection_id=coll.id, pixiv_id=pid, position=pos))
        clean_db.commit()
        token = self._token(client)
        r = client.post(f'/api/collections/{coll.id}/items/70003/move',
                        data=json.dumps({'direction': 'up'}),
                        content_type='application/json',
                        headers={'X-CSRF-Token': token})
        assert r.status_code == 200
        assert r.get_json()['rebalanced'] is True
        with models.get_session() as s:
            items = s.query(models.CollectionItem).filter(
                models.CollectionItem.collection_id == coll.id
            ).order_by(models.CollectionItem.position).all()
        assert [it.pixiv_id for it in items] == [70001, 70003, 70002]
        assert [it.position for it in items] == [1000.0, 1500.0, 2000.0]


class TestFavoriteMembershipContract:
    def _default_coll(self, clean_db):
        import models
        c = models.Collection(name='我的收藏')
        clean_db.add(c); clean_db.commit()
        return c.id

    def test_gallery_favorites_only_returns_membership(self, client, clean_db):
        import models
        default_id = self._default_coll(clean_db)
        for pid in [90001, 90002, 90003]:
            clean_db.add(models.Illust(pixiv_id=pid, title=f'p{pid}', download_status='done'))
        clean_db.commit()
        clean_db.add(models.CollectionItem(collection_id=default_id, pixiv_id=90002, position=1000.0))
        clean_db.commit()
        r = client.get('/api/gallery?favorites=true&limit=10')
        assert r.status_code == 200
        data = r.get_json()
        returned = {item['pixiv_id'] for item in data['data']}
        assert 90002 in returned
        assert 90001 not in returned
        assert 90003 not in returned
        assert data['favorite_total'] > 0

    def test_detail_page_reflects_favorite_membership(self, client, clean_db):
        """回归：详情页收藏按钮初始状态须反映'我的收藏'归属（曾恒为未收藏）。"""
        import models
        default_id = self._default_coll(clean_db)
        # 必须预置 original_urls：/detail 在该列为空时会惰性调
        # _fetch_original_urls() 走真实网络，离线时单次 60s+ 把整轮套件拖到 140s。
        for pid, title in ((90060, 'fav-item'), (90061, 'plain-item')):
            item = models.Illust(pixiv_id=pid, title=title, download_status='done')
            item.original_urls_list = [f'https://i.pximg.net/img-original/img/x/{pid}_p0.jpg']
            clean_db.add(item)
        clean_db.add(models.CollectionItem(collection_id=default_id, pixiv_id=90060, position=1000.0))
        clean_db.commit()

        fav = client.get('/detail/90060')
        assert fav.status_code == 200
        assert 'is_favorite": true' in fav.get_data(as_text=True) or '"is_favorite": true' in fav.get_data(as_text=True)
        plain = client.get('/detail/90061')
        assert plain.status_code == 200
        assert '"is_favorite": false' in plain.get_data(as_text=True)

    def test_favorite_get_returns_membership(self, client, clean_db):
        import models
        default_id = self._default_coll(clean_db)
        clean_db.add(models.Illust(pixiv_id=90050, title='t', download_status='done'))
        clean_db.commit()
        r = client.get('/api/favorite/90050')
        assert r.status_code == 200
        assert r.get_json()['is_favorite'] is False
        clean_db.add(models.CollectionItem(collection_id=default_id, pixiv_id=90050, position=1000.0))
        clean_db.commit()
        r2 = client.get('/api/favorite/90050')
        assert r2.get_json()['is_favorite'] is True

    def test_favorite_post_toggles_membership(self, client, clean_db):
        import models
        self._default_coll(clean_db)
        token = client.get('/csrf-token').get_json()['token']
        clean_db.add(models.Illust(pixiv_id=90080, title='t', download_status='done'))
        clean_db.commit()
        r = client.post('/api/favorite/90080',
                        data='{}', content_type='application/json',
                        headers={'X-CSRF-Token': token})
        assert r.status_code == 200
        assert r.get_json()['is_favorite'] is True
        r2 = client.post('/api/favorite/90080',
                         data='{}', content_type='application/json',
                         headers={'X-CSRF-Token': token})
        assert r2.get_json()['is_favorite'] is False


class TestGalleryTriggersBookmarkFill:
    def test_gallery_kicks_fill_for_zero_bookmark_records(self, client, clean_db, monkeypatch):
        """图库页返回收藏数=0 且未补全原图的记录时，应触发后台详情补全。"""
        import fetcher
        called = []
        monkeypatch.setattr(fetcher, '_kick_background_fill', lambda ids: called.append(list(ids)))
        clean_db.add(models.Illust(pixiv_id=91001, title='a', bookmark_count=0,
                                   download_status='done'))
        clean_db.add(models.Illust(pixiv_id=91002, title='b', bookmark_count=1500,
                                   download_status='done'))
        clean_db.commit()

        r = client.get('/api/gallery?limit=10')
        assert r.status_code == 200
        assert called and 91001 in called[0]
        assert 91002 not in called[0]

    def test_gallery_skips_fill_for_filled_records(self, client, clean_db, monkeypatch):
        """已补全（有原图 URL）的 0 收藏记录不应重复触发补全。"""
        import fetcher
        called = []
        monkeypatch.setattr(fetcher, '_kick_background_fill', lambda ids: called.append(list(ids)))
        illust = models.Illust(pixiv_id=91003, title='c', bookmark_count=0,
                               download_status='done')
        illust.original_urls_list = ['https://i.pximg.net/91003_p0.jpg']
        clean_db.add(illust)
        clean_db.commit()

        r = client.get('/api/gallery?limit=10')
        assert r.status_code == 200
        assert called == [] or 91003 not in [x for sub in called for x in sub]


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

    def test_gallery_r18_filter_applies_to_collection_view(self, client, clean_db):
        coll = models.Collection(name='col-r18')
        clean_db.add(coll)
        clean_db.commit()
        r18 = models.Illust(pixiv_id=95011, title='r18', download_status='done')
        r18.tags_list = ['R-18']
        safe = models.Illust(pixiv_id=95012, title='safe', download_status='done')
        safe.tags_list = ['original']
        clean_db.add_all([r18, safe])
        clean_db.commit()
        clean_db.add_all([
            models.CollectionItem(collection_id=coll.id, pixiv_id=95011, position=1000.0),
            models.CollectionItem(collection_id=coll.id, pixiv_id=95012, position=2000.0),
        ])
        clean_db.commit()

        resp = client.get(f'/api/gallery?collection_id={coll.id}&limit=10')
        assert resp.status_code == 200
        pids = [d['pixiv_id'] for d in resp.get_json()['data']]
        assert pids == [95012]

        resp = client.get(f'/api/gallery?collection_id={coll.id}&limit=10&r18=all')
        assert resp.status_code == 200
        pids = [d['pixiv_id'] for d in resp.get_json()['data']]
        assert pids == [95011, 95012]


class TestFollowingRouteR18:
    """/api/following 的 R18 过滤：缺省 safe（默认不显示 R18），显式 r18_mode=all 才包含。"""

    def _patch_following(self, monkeypatch):
        import routes_search
        calls = []
        def fake_following(page=1, r18_mode='all'):
            calls.append({'page': page, 'r18_mode': r18_mode})
            return [{'pixiv_id': page}], False
        monkeypatch.setattr(routes_search, 'fetch_following', fake_following)
        return calls

    def test_following_default_safe(self, client, monkeypatch):
        calls = self._patch_following(monkeypatch)
        resp = client.get('/api/following')
        assert resp.status_code == 200
        assert calls and calls[0]['r18_mode'] == 'safe'

    def test_following_explicit_all(self, client, monkeypatch):
        calls = self._patch_following(monkeypatch)
        resp = client.get('/api/following?r18_mode=all')
        assert resp.status_code == 200
        assert calls and calls[0]['r18_mode'] == 'all'


class TestGalleryDeleteOrphans:
    """删除接口支持无 DB 记录的孤儿作品。

    孤儿 = downloads/<pid> 有文件但 Illust 表无行（DB 重置/丢行等原因产生）。
    旧行为：DELETE 查无行直接 404，孤儿在图库里删不掉、永久残留。
    """

    def _token(self, client):
        return client.get('/csrf-token').get_json()['token']

    def _patch_download_dir(self, monkeypatch, tmp_path):
        import helpers
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

    def test_download_file_zip_memory_below_threshold(self, client, clean_db, tmp_path, monkeypatch):
        """小包仍然走内存缓冲：行为不变，且不产生任何临时文件。"""
        created = self._spy_tempfile(monkeypatch)
        self._make_illust(clean_db, tmp_path, 88001, [100, 200, 300])

        resp = client.get('/download_file/88001')

        assert resp.status_code == 200
        assert resp.mimetype == 'application/zip'
        assert 'zip-me.zip' in resp.headers['Content-Disposition']
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            assert sorted(zf.namelist()) == ['zip-me_p0.jpg', 'zip-me_p1.jpg', 'zip-me_p2.jpg']
            assert zf.read('zip-me_p1.jpg') == b'x' * 200
        assert created == [], '阈值内不该落临时文件'

    def test_download_file_zip_tempfile_above_threshold(self, client, clean_db, tmp_path,
                                                        monkeypatch):
        """超过阈值改落临时文件：包内容一致，且只有一份临时文件。"""
        created = self._spy_tempfile(monkeypatch)
        monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
        self._make_illust(clean_db, tmp_path, 88002, [100, 200, 300])

        resp = client.get('/download_file/88002')

        assert resp.status_code == 200
        assert resp.mimetype == 'application/zip'
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            assert sorted(zf.namelist()) == ['zip-me_p0.jpg', 'zip-me_p1.jpg', 'zip-me_p2.jpg']
            assert zf.read('zip-me_p2.jpg') == b'x' * 300
        assert len(created) == 1, f'应当只建一份临时文件：{created}'
        resp.close()

    def test_download_file_tempfile_removed_after_request(self, client, clean_db, tmp_path,
                                                          monkeypatch):
        """响应结束（body 包装器 close）后临时文件必须消失，不留几百 MB 垃圾。"""
        created = self._spy_tempfile(monkeypatch)
        monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
        self._make_illust(clean_db, tmp_path, 88003, [100, 200])

        resp = client.get('/download_file/88003')
        assert resp.status_code == 200
        assert len(created) == 1
        resp.close()

        assert not os.path.exists(created[0]), '响应关闭后临时文件应被删除'

    def test_download_file_tempfile_removed_after_full_read(self, client, clean_db, tmp_path,
                                                            monkeypatch):
        """正常读完整包（没走 close 路径）也必须删掉临时文件。

        清理挂在 body 包装器上，两条路径都要覆盖：读完 EOF 与 close。
        """
        created = self._spy_tempfile(monkeypatch)
        monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
        self._make_illust(clean_db, tmp_path, 88007, [100, 200])

        resp = client.get('/download_file/88007')
        assert len(created) == 1
        with zipfile.ZipFile(io.BytesIO(resp.get_data())) as zf:
            assert len(zf.namelist()) == 2

        assert not os.path.exists(created[0]), '读完整包后临时文件应被删除'

    def test_download_file_head_request_cleans_tempfile(self, client, clean_db, tmp_path,
                                                        monkeypatch):
        """HEAD 不发 body（根本不会迭代），清理必须靠 close 路径兜住。"""
        created = self._spy_tempfile(monkeypatch)
        monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
        self._make_illust(clean_db, tmp_path, 88008, [100, 200])

        resp = client.head('/download_file/88008')
        assert resp.status_code == 200
        assert len(created) == 1
        resp.close()

        assert not os.path.exists(created[0]), 'HEAD 请求也不能漏临时文件'

    def test_download_file_unsatisfiable_range_cleans_tempfile(self, client, clean_db,
                                                               tmp_path, monkeypatch):
        """Range 不可满足 → send_file 抛异常（416），仍不能漏临时文件。"""
        created = self._spy_tempfile(monkeypatch)
        monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
        self._make_illust(clean_db, tmp_path, 88009, [100, 200])

        resp = client.get('/download_file/88009',
                          headers={'Range': 'bytes=999999999-'})

        assert resp.status_code == 416
        assert len(created) == 1
        assert not os.path.exists(created[0]), '416 也得清掉临时文件'

    def test_download_file_partial_range_still_works_and_cleans(self, client, clean_db,
                                                                tmp_path, monkeypatch):
        """Range 正常时返回 206，body 走 _RangeWrapper —— 包装器不得破坏它。"""
        created = self._spy_tempfile(monkeypatch)
        monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
        self._make_illust(clean_db, tmp_path, 88010, [100, 200])

        resp = client.get('/download_file/88010', headers={'Range': 'bytes=0-99'})

        assert resp.status_code == 206
        assert len(resp.get_data()) == 100
        assert len(created) == 1
        resp.close()
        assert not os.path.exists(created[0])

    def test_download_file_no_content_status_cleans_tempfile(self, client, clean_db, tmp_path,
                                                             monkeypatch):
        """非内容响应（304 类）：body 为空、清理挂不上，必须当场删且释放句柄。

        304 在真实路径上很难自然触发（每次请求都重建临时文件，ETag 必然不同），所以
        这里用替身把 send_file 的返回码改成 304，专门盯住这条兜底分支：既不能漏文件，
        也不能因为句柄还开着而删不掉。
        """
        created = self._spy_tempfile(monkeypatch)
        monkeypatch.setattr(routes_download, 'ZIP_MEMORY_THRESHOLD_BYTES', 10)
        self._make_illust(clean_db, tmp_path, 88011, [100, 200])

        real_send_file = routes_download.send_file

        def _not_modified(path, **kwargs):
            resp = real_send_file(path, **kwargs)
            resp.status_code = 304
            return resp

        monkeypatch.setattr(routes_download, 'send_file', _not_modified)

        resp = client.get('/download_file/88011')

        assert resp.status_code == 304
        assert len(created) == 1
        assert not os.path.exists(created[0]), '无内容响应也必须清掉临时文件'

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


class TestDetailApiMediumUrls:
    def test_detail_api_includes_medium_urls(self, client, clean_db):
        """/api/detail 返回 medium_urls：master1200 中图代理地址，数量与原图 URL 一致。"""
        illust = models.Illust(pixiv_id=92001, title='multi-page', page_count=2)
        illust.original_urls_list = [
            'https://i.pximg.net/img-original/img/0001/01/15/00/00/00/92001_p0.jpg',
            'https://i.pximg.net/img-original/img/0001/01/15/00/00/00/92001_p1.jpg',
        ]
        clean_db.add(illust)
        clean_db.commit()

        r = client.get('/api/detail/92001')
        assert r.status_code == 200
        data = r.get_json()
        assert data['pixiv_id'] == 92001
        assert 'medium_urls' in data
        assert len(data['medium_urls']) == len(illust.original_urls_list) == 2
        for u in data['medium_urls']:
            assert u.startswith('/thumb/')
            encoded = u[len('/thumb/'):]
            decoded = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode()
            assert '/img-master/' in decoded and decoded.endswith('_master1200.jpg')
