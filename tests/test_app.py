import json
from unittest.mock import patch

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
    @patch('app.browse_discovery')
    def test_empty_query_calls_discovery(self, mock_discovery, client):
        mock_discovery.return_value = ([], False)
        resp = client.get('/search')
        data = resp.get_json()
        assert resp.status_code == 200
        mock_discovery.assert_called_once()

    @patch('app.search_by_tag')
    def test_search_by_tag_called(self, mock_search, client):
        mock_search.return_value = ([], False)
        resp = client.get('/search?type=tag&query=初音ミク')
        assert resp.status_code == 200
        mock_search.assert_called_once()
        args, kwargs = mock_search.call_args
        assert '初音ミク' in args

    @patch('app.search_by_user')
    def test_search_by_user_called(self, mock_search, client):
        mock_search.return_value = ([], False)
        resp = client.get('/search?type=user&query=12345')
        assert resp.status_code == 200
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
        data = resp.get_json()
        assert data['has_more'] is True
        assert data['cursor'] == 'cursor_abc'
        assert len(data['results']) == 1

    @patch('app.browse_discovery')
    def test_invalid_sort_fallback(self, mock_discovery, client):
        mock_discovery.return_value = ([], False)
        resp = client.get('/search?sort=invalid')
        assert resp.status_code == 200
        args, kwargs = mock_discovery.call_args
        assert args[1] == 'date_d'


class TestRoutes:
    def test_csrf_token(self, client):
        resp = client.get('/csrf-token')
        assert resp.status_code == 200
        assert 'token' in resp.get_json()

    def test_settings_page(self, client):
        resp = client.get('/settings')
        assert resp.status_code == 200

    def test_downloads_page(self, client):
        resp = client.get('/downloads')
        assert resp.status_code == 200

    def test_bulk_page(self, client):
        resp = client.get('/bulk')
        assert resp.status_code == 200
