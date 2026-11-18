"""X 抓图路由（`pixivviewer/routes/xfeed.py`）的接口测试。

不联网：标签抓取、图片代理、X 搜索全部在这层被 mock 掉；这里只钉接口契约
（分页/过滤/CSRF/字段形状/下载入队/同作品标记）。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from pixivviewer import config, runtime, x_client
from pixivviewer.models import WorkLink, XTag, XWork, get_session


@pytest.fixture
def x_client_iso(monkeypatch, tmp_path):
    """把 Cookie / api 配置指到临时文件，避免读到真实实例目录。"""
    monkeypatch.setattr(config, 'X_COOKIE_PATH', str(tmp_path / 'x_cookies.txt'))
    monkeypatch.setattr(config, 'X_API_CONFIG_PATH', str(tmp_path / 'x_api.json'))
    monkeypatch.setattr(config, 'X_SEARCH_QUERY_ID', 'QID')
    x_client.clear_cookie_cache()
    yield tmp_path
    x_client.clear_cookie_cache()


def _token(client):
    return client.get('/csrf-token').get_json()['token']


def _add_work(session, tweet_id, tag='#tag', dup_status=None, media_count=1):
    work = XWork(tweet_id=tweet_id, tag=tag, author_name='A', author_screen_name='a',
                 text='t', media_count=media_count, dup_status=dup_status)
    work.media_list = [{'url': f'https://pbs.twimg.com/media/{tweet_id}.jpg?name=orig',
                        'w': 100, 'h': 100}]
    session.add(work)
    session.commit()
    return work


class TestXPage:
    def test_page_renders_with_script_and_nav(self, client, clean_db):
        resp = client.get('/x')
        assert resp.status_code == 200
        assert b'page-x.js' in resp.data
        assert b'/api/x/items' not in resp.data      # 数据走 JS，不在模板里内联
        html = resp.get_data(as_text=True)
        assert 'href="/x"' in html


class TestXTags:
    def test_add_list_delete(self, client, clean_db):
        token = _token(client)
        resp = client.post('/api/x/tags', json={'tag': '  #原神  '},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 201
        assert resp.get_json()['tag']['tag'] == '#原神'

        tags = client.get('/api/x/tags').get_json()
        assert [t['tag'] for t in tags] == ['#原神']

        # 标签下放一条作品，删除标签应连带清掉记录
        _add_work(clean_db, '1800000000000000009', tag='#原神')
        resp = client.delete('/api/x/tags/%23%E5%8E%9F%E7%A5%9E',
                             headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json()['removed_works'] == 1
        assert clean_db.query(XWork).count() == 0

    def test_empty_tag_rejected(self, client, clean_db):
        token = _token(client)
        resp = client.post('/api/x/tags', json={'tag': '   '},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 400

    def test_delete_missing_tag_404(self, client, clean_db):
        token = _token(client)
        assert client.delete('/api/x/tags/nope', headers={'X-CSRF-Token': token}).status_code == 404


class TestXItems:
    def test_pagination_and_tag_filter(self, client, clean_db):
        for i in range(5):
            _add_work(clean_db, f'18000000000000000{i}', tag='#a')
        _add_work(clean_db, '1800000000000000100', tag='#b')

        data = client.get('/api/x/items?limit=2&offset=0').get_json()
        assert data['total'] == 6 and len(data['data']) == 2

        filtered = client.get('/api/x/items?tag=%23a').get_json()
        assert filtered['total'] == 5
        assert all(row['tag'] == '#a' for row in filtered['data'])

    def test_dup_filter(self, client, clean_db):
        _add_work(clean_db, '1800000000000000201', dup_status='confirmed')
        _add_work(clean_db, '1800000000000000202', dup_status='suspected')
        _add_work(clean_db, '1800000000000000203')

        hide = client.get('/api/x/items?dup=hide').get_json()
        assert hide['total'] == 2   # 藏 confirmed，保留 suspected 与无标记
        only = client.get('/api/x/items?dup=only').get_json()
        assert only['total'] == 2
        assert {row['dup_status'] for row in only['data']} == {'confirmed', 'suspected'}

    def test_item_shape(self, client, clean_db):
        work = _add_work(clean_db, '1800000000000000301', media_count=2)
        row = client.get('/api/x/items').get_json()['data'][0]
        for key in ('id', 'tweet_id', 'tag', 'author_name', 'author_screen_name', 'text',
                    'tweeted_at', 'media', 'media_count', 'saved_status', 'saved_count',
                    'link_pixiv_id', 'dup_status', 'dup_reason', 'dup_pixiv_id'):
            assert key in row, f'缺少字段 {key}'
        assert row['tweet_id'] == '1800000000000000301'   # 字符串，不是数字
        assert row['media'][0]['url'].startswith('https://pbs.twimg.com/')


class TestXCookie:
    def test_save_validates_and_writes(self, client, clean_db, x_client_iso):
        token = _token(client)
        bad = client.post('/api/x/cookie', json={'cookie': 'auth_token=only'},
                          headers={'X-CSRF-Token': token})
        assert bad.status_code == 400

        ok = client.post('/api/x/cookie',
                         json={'cookie': 'auth_token=AAA; ct0=BBB; other=x'},
                         headers={'X-CSRF-Token': token})
        assert ok.status_code == 200
        written = pathlib.Path(config.X_COOKIE_PATH).read_text(encoding='utf-8')
        assert written == 'auth_token=AAA; ct0=BBB'
        assert client.get('/api/x/cookie').get_json()['configured'] is True

    def test_cookie_value_never_returned(self, client, clean_db, x_client_iso):
        token = _token(client)
        client.post('/api/x/cookie', json={'cookie': 'auth_token=SECRET; ct0=CT0'},
                    headers={'X-CSRF-Token': token})
        body = client.get('/api/x/cookie').get_data(as_text=True)
        assert 'SECRET' not in body and 'CT0' not in body


class TestXStatus:
    def test_status_shape(self, client, clean_db, x_client_iso):
        data = client.get('/api/x/status').get_json()
        for key in ('state', 'works', 'tags', 'tag_errors',
                    'cookie_configured', 'query_id_configured', 'hash_available'):
            assert key in data
        for key in ('enabled', 'interval', 'running', 'last_check', 'last_error'):
            assert key in data['state']


class TestXWorkActions:
    def test_download_enqueues(self, client, clean_db, monkeypatch):
        work = _add_work(clean_db, '1800000000000000401')
        submitted = []
        monkeypatch.setattr(runtime.x_download_executor, 'submit',
                            lambda fn, *a, **kw: submitted.append((fn, a)))
        token = _token(client)
        resp = client.post(f'/api/x/works/{work.id}/download',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 202
        assert submitted and submitted[0][1] == (work.id,)

    def test_dup_confirm_writes_work_link(self, client, clean_db):
        work = _add_work(clean_db, '1800000000000000501', dup_status='suspected')
        token = _token(client)
        resp = client.post(f'/api/x/works/{work.id}/dup',
                           json={'action': 'confirm', 'pixiv_id': 8888},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json()['dup_status'] == 'confirmed'
        assert clean_db.query(WorkLink).filter_by(pixiv_id=8888).count() == 1

    def test_dup_ignore(self, client, clean_db):
        work = _add_work(clean_db, '1800000000000000502', dup_status='suspected')
        token = _token(client)
        resp = client.post(f'/api/x/works/{work.id}/dup', json={'action': 'ignore'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json()['dup_status'] == 'ignored'

    def test_dup_rejects_unknown_action(self, client, clean_db):
        work = _add_work(clean_db, '1800000000000000503')
        token = _token(client)
        resp = client.post(f'/api/x/works/{work.id}/dup', json={'action': 'maybe'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 400

    def test_delete_work_removes_row(self, client, clean_db):
        work = _add_work(clean_db, '1800000000000000601')
        token = _token(client)
        assert client.delete(f'/api/x/works/{work.id}',
                             headers={'X-CSRF-Token': token}).status_code == 200
        assert clean_db.query(XWork).count() == 0

    def test_artist_link_upsert(self, client, clean_db):
        token = _token(client)
        resp = client.post('/api/x/artist-links',
                           json={'pixiv_user_id': 42, 'x_screen_name': '@Artist_A'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        from pixivviewer.models import ArtistLink
        row = clean_db.get(ArtistLink, 42)
        assert row.x_screen_name == 'artist_a'
