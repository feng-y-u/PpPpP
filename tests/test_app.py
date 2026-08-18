import json
from unittest.mock import patch

from sqlalchemy import text

import models


class TestIndexRoute:
    def test_get_returns_200(self, client):
        resp = client.get('/')
        assert resp.status_code == 200
        assert b'Pixiv' in resp.data or b'\xe6\x90\x9c\xe7\xb4\xa2' in resp.data


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

    def test_search_user_non_digit_returns_400(self, client):
        resp = client.get('/search?type=user&query=abc')
        assert resp.status_code == 400

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
        assert b'pvCache' in resp.data or b'loadGallery' in resp.data

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
        clean_db.add(models.Illust(pixiv_id=90060, title='fav-item', download_status='done'))
        clean_db.add(models.Illust(pixiv_id=90061, title='plain-item', download_status='done'))
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
