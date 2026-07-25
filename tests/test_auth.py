import pytest


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    import app as app_module
    app_module._rate_limit_store.clear()
    yield


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setattr('app.ACCESS_PASSWORD', 'test-secret')


def _get_token(client):
    return client.get('/csrf-token').get_json()['token']


class TestAuthRequired:
    def test_page_redirects_to_login(self, client, auth_enabled):
        resp = client.get('/')
        assert resp.status_code == 302
        assert resp.headers['Location'].startswith('/login')

    def test_api_returns_401(self, client, auth_enabled):
        resp = client.get('/api/blocked-tags')
        assert resp.status_code == 401
        assert resp.get_json()['error_code'] == 'AUTH_REQUIRED'

    def test_post_returns_401_not_redirect(self, client, auth_enabled):
        resp = client.post('/api/blocked-tags', json={'tag': 'x'})
        assert resp.status_code == 401

    def test_login_page_exempt(self, client, auth_enabled):
        assert client.get('/login').status_code == 200

    def test_static_exempt(self, client, auth_enabled):
        assert client.get('/static/app.js').status_code == 200

    def test_no_password_means_open_access(self, client):
        # ACCESS_PASSWORD 默认空 → 免认证
        assert client.get('/').status_code == 200


class TestLogin:
    def test_wrong_password_403(self, client, auth_enabled):
        token = _get_token(client)
        resp = client.post('/login', json={'password': 'wrong'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 403

    def test_login_success_then_access(self, client, auth_enabled):
        token = _get_token(client)
        resp = client.post('/login', json={'password': 'test-secret'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json()['ok'] is True
        assert client.get('/').status_code == 200

    def test_open_redirect_blocked(self, client, auth_enabled):
        token = _get_token(client)
        resp = client.post('/login', json={'password': 'test-secret', 'next': '//evil.com'},
                           headers={'X-CSRF-Token': token})
        assert resp.get_json()['next'] == '/'

    def test_rate_limit_after_5_attempts(self, client, auth_enabled):
        for _ in range(5):
            token = _get_token(client)
            client.post('/login', json={'password': 'wrong'},
                        headers={'X-CSRF-Token': token})
        token = _get_token(client)
        resp = client.post('/login', json={'password': 'wrong'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 429

    def test_login_requires_csrf(self, client, auth_enabled):
        resp = client.post('/login', json={'password': 'test-secret'})
        assert resp.status_code == 403


class TestSettingsCompat:
    def test_authed_session_skips_settings_lock(self, client, auth_enabled, monkeypatch):
        monkeypatch.setattr('app.SETTINGS_PASSWORD', 'settings-pw')
        token = _get_token(client)
        client.post('/login', json={'password': 'test-secret'},
                    headers={'X-CSRF-Token': token})
        assert client.get('/settings').status_code == 200
        assert client.get('/api/settings').status_code == 200

    def test_unlock_passthrough_when_authed(self, client, auth_enabled):
        token = _get_token(client)
        client.post('/login', json={'password': 'test-secret'},
                    headers={'X-CSRF-Token': token})
        token = _get_token(client)
        resp = client.post('/api/settings/unlock', json={},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
