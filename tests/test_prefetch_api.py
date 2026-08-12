import json
import os
import time
from datetime import datetime, timezone

import pytest

import app
from models import SearchCache, Illust, Collection, CollectionItem, safe_commit


def _get_token(client):
    return client.get('/csrf-token').get_json()['token']


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """将 settings.json 写入重定向到临时文件，避免污染生产 instance/settings.json。"""
    settings_path = tmp_path / 'settings.json'
    monkeypatch.setattr(app, '_SETTINGS_PATH', str(settings_path))
    return settings_path


@pytest.fixture(autouse=True)
def _restore_prefetch_state():
    original = dict(app._prefetch_state)
    yield
    app._prefetch_state.clear()
    app._prefetch_state.update(original)


class TestPrefetchConfigAPI:
    def test_get_returns_defaults(self, client):
        resp = client.get('/api/prefetch/config')
        assert resp.status_code == 200
        data = resp.get_json()
        assert set(data) == {'interval', 'pages', 'max_illusts'}
        assert data == {
            'interval': app._prefetch_state['interval'],
            'pages': app._prefetch_state['pages'],
            'max_illusts': app._prefetch_state['max_illusts'],
        }

    def test_post_updates_state_and_persists(self, client, _isolate_settings):
        token = _get_token(client)
        resp = client.post('/api/prefetch/config',
                           data=json.dumps({'interval': 123, 'pages': 5, 'max_illusts': 42}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['interval'] == 123
        assert data['pages'] == 5
        assert data['max_illusts'] == 42
        assert app._prefetch_state['interval'] == 123
        assert app._prefetch_state['pages'] == 5
        assert app._prefetch_state['max_illusts'] == 42

        with open(_isolate_settings, encoding='utf-8') as f:
            saved = json.load(f)
        assert saved['prefetch_interval'] == 123
        assert saved['prefetch_pages'] == 5
        assert saved['prefetch_max_illusts'] == 42

    def test_post_clamps_negative_to_zero(self, client):
        token = _get_token(client)
        resp = client.post('/api/prefetch/config',
                           data=json.dumps({'interval': -5}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json()['interval'] == 0

    def test_post_partial_update(self, client):
        token = _get_token(client)
        client.post('/api/prefetch/config',
                    data=json.dumps({'pages': 7}),
                    content_type='application/json',
                    headers={'X-CSRF-Token': token})
        resp = client.get('/api/prefetch/config')
        assert resp.get_json()['pages'] == 7
        assert resp.get_json()['interval'] == app._prefetch_state['interval']

    def test_post_invalid_type_400(self, client):
        token = _get_token(client)
        resp = client.post('/api/prefetch/config',
                           data=json.dumps({'interval': 'abc'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 400
        assert 'error' in resp.get_json()

    def test_post_partial_invalid_does_not_mutate_state(self, client, _isolate_settings):
        """部分字段非法时返回 400，且合法字段不得先于校验整体写入内存/磁盘。"""
        token = _get_token(client)
        before = dict(app._prefetch_state)
        resp = client.post('/api/prefetch/config',
                           data=json.dumps({'interval': 5, 'pages': 'abc'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 400
        assert 'error' in resp.get_json()
        assert app._prefetch_state['interval'] == before['interval']
        assert app._prefetch_state['pages'] == before['pages']
        assert app._prefetch_state['max_illusts'] == before['max_illusts']
        # settings.json 未被写入（文件不应存在）
        assert not os.path.exists(_isolate_settings)

    def test_post_without_csrf_403(self, client):
        resp = client.post('/api/prefetch/config',
                           data=json.dumps({'interval': 1}),
                           content_type='application/json')
        assert resp.status_code == 403
        assert resp.get_json()['error'] == 'CSRF校验失败'


class TestPrefetchTagsAPI:
    def test_post_adds_tag(self, client, clean_db):
        token = _get_token(client)
        resp = client.post('/api/prefetch/tags',
                           data=json.dumps({'tag': '初音ミク'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 201
        assert resp.get_json()['tag'] == '初音ミク'
        assert clean_db.query(SearchCache).filter(SearchCache.tag == '初音ミク').first() is not None

    def test_post_empty_tag_400(self, client, clean_db):
        token = _get_token(client)
        resp = client.post('/api/prefetch/tags',
                           data=json.dumps({'tag': '   '}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 400

    def test_post_duplicate_409(self, client, clean_db):
        clean_db.add(SearchCache(tag='dup'))
        safe_commit(clean_db)
        token = _get_token(client)
        resp = client.post('/api/prefetch/tags',
                           data=json.dumps({'tag': 'dup'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 409
        assert resp.get_json()['error'] == '标签已存在'

    def test_get_lists_tags_newest_first(self, client, clean_db):
        clean_db.add_all([
            SearchCache(tag='a', status='done', total=5, error='',
                        cached_at=datetime(2025, 1, 2, tzinfo=timezone.utc)),
            SearchCache(tag='b', status='idle', total=0, error=''),
            SearchCache(tag='c', status='error', total=0, error='boom',
                        cached_at=datetime(2025, 1, 3, tzinfo=timezone.utc)),
        ])
        safe_commit(clean_db)

        resp = client.get('/api/prefetch/tags')
        assert resp.status_code == 200
        data = resp.get_json()
        # cached_at DESC：c 最新；b 从未抓取（NULL）排最后
        assert [d['tag'] for d in data] == ['c', 'a', 'b']
        assert data[0]['total'] == 0
        assert data[0]['status'] == 'error'
        assert data[0]['cached_at'] == '2025-01-03T00:00:00'
        assert data[2]['cached_at'] is None

    def test_delete_removes_tag(self, client, clean_db):
        clean_db.add(SearchCache(tag='gone', illust_ids='[]'))
        safe_commit(clean_db)
        token = _get_token(client)
        resp = client.delete('/api/prefetch/tags/gone', headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json()['tag'] == 'gone'
        assert clean_db.query(SearchCache).filter(SearchCache.tag == 'gone').first() is None

    def test_delete_missing_404(self, client, clean_db):
        token = _get_token(client)
        resp = client.delete('/api/prefetch/tags/nope', headers={'X-CSRF-Token': token})
        assert resp.status_code == 404
        assert resp.get_json()['error'] == '标签不存在'

    def test_delete_deletes_unreferenced_prefetch_illusts(self, client, clean_db):
        clean_db.add_all([
            SearchCache(tag='t', illust_ids='[1, 2, 3]'),
            SearchCache(tag='other', illust_ids='[2]'),
            Illust(pixiv_id=1, title='free', prefetch_source=1),
            Illust(pixiv_id=2, title='referenced', prefetch_source=1),
            Illust(pixiv_id=3, title='downloaded', prefetch_source=1, download_status='done'),
        ])
        safe_commit(clean_db)
        token = _get_token(client)
        resp = client.delete('/api/prefetch/tags/t', headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        # pid1 未被引用且可删 → 删除；pid2 被 other 引用 → 保留；pid3 已下载 → 保留
        assert remaining == {2, 3}

    def test_delete_keeps_collected_illusts(self, client, clean_db):
        coll = Collection(name='c')
        clean_db.add(coll)
        clean_db.commit()
        clean_db.add(CollectionItem(collection_id=coll.id, pixiv_id=5, position=1000.0))
        clean_db.add_all([
            SearchCache(tag='t', illust_ids='[5]'),
            Illust(pixiv_id=5, title='collected', prefetch_source=1),
        ])
        safe_commit(clean_db)
        token = _get_token(client)
        resp = client.delete('/api/prefetch/tags/t', headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 5).first() is not None

    def test_delete_keeps_non_prefetch_illusts(self, client, clean_db):
        clean_db.add_all([
            SearchCache(tag='t', illust_ids='[7]'),
            Illust(pixiv_id=7, title='normal', prefetch_source=0),
        ])
        safe_commit(clean_db)
        token = _get_token(client)
        resp = client.delete('/api/prefetch/tags/t', headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 7).first() is not None

    def test_post_without_csrf_403(self, client):
        resp = client.post('/api/prefetch/tags', data=json.dumps({'tag': 'x'}),
                           content_type='application/json')
        assert resp.status_code == 403

    def test_delete_without_csrf_403(self, client):
        resp = client.delete('/api/prefetch/tags/x')
        assert resp.status_code == 403


class TestPrefetchStatusAPI:
    def test_status_returns_fields(self, client):
        resp = client.get('/api/prefetch/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert set(data) == {'running', 'last_check', 'interval'}
        assert data['running'] == app._prefetch_state['running']
        assert data['last_check'] == app._prefetch_state['last_check']
        assert data['interval'] == app._prefetch_state['interval']


class TestPrefetchRefreshAPI:
    def test_refresh_done_tag_spawns_thread(self, client, clean_db, monkeypatch):
        clean_db.add(SearchCache(tag='t', status='done', illust_ids='[]'))
        safe_commit(clean_db)
        called = []
        monkeypatch.setattr(app, '_prefetch_one_tag', lambda tag: called.append(tag))

        token = _get_token(client)
        resp = client.post('/api/prefetch/refresh',
                           data=json.dumps({'tag': 't'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json() == {'tag': 't', 'status': 'refreshing'}
        time.sleep(0.1)  # 后台线程异步执行
        assert called == ['t']

    def test_refresh_fetching_409(self, client, clean_db):
        clean_db.add(SearchCache(tag='t', status='fetching'))
        safe_commit(clean_db)
        token = _get_token(client)
        resp = client.post('/api/prefetch/refresh',
                           data=json.dumps({'tag': 't'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 409
        assert resp.get_json()['error'] == '该标签正在刷新中'

    def test_refresh_missing_404(self, client, clean_db):
        token = _get_token(client)
        resp = client.post('/api/prefetch/refresh',
                           data=json.dumps({'tag': 'nope'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 404
        assert resp.get_json()['error'] == '标签不存在'

    def test_refresh_empty_400(self, client, clean_db):
        token = _get_token(client)
        resp = client.post('/api/prefetch/refresh',
                           data=json.dumps({'tag': '   '}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 400

    def test_refresh_without_csrf_403(self, client):
        resp = client.post('/api/prefetch/refresh', data=json.dumps({'tag': 'x'}),
                           content_type='application/json')
        assert resp.status_code == 403
