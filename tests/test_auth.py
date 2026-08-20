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


class TestSafeNext:
    def test_relative_path_allowed(self):
        import app
        assert app._safe_next('/detail/123') == '/detail/123'

    def test_protocol_relative_rejected(self):
        import app
        assert app._safe_next('//evil.com') == '/'

    def test_backslash_variant_rejected(self):
        """回归：/\\evil.com 会被浏览器规整为 //evil.com（开放重定向）。"""
        import app
        assert app._safe_next('/\\evil.com') == '/'
        assert app._safe_next('\\evil.com') == '/'

    def test_control_chars_rejected(self):
        import app
        assert app._safe_next('/a\r\nb') == '/'
        assert app._safe_next('') == '/'


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


class TestOpenDir:
    def test_non_localhost_forbidden(self, client):
        token = _get_token(client)
        resp = client.post('/api/open-dir', json={'path': '/tmp'},
                           headers={'X-CSRF-Token': token,
                                    'X-Forwarded-For': '8.8.8.8'})
        assert resp.status_code == 403

    def test_localhost_allowed(self, client, monkeypatch, tmp_path):
        import app as app_module
        monkeypatch.setattr(app_module.platform, 'system', lambda: 'Windows')
        called = []
        monkeypatch.setattr(app_module.os, 'startfile',
                            lambda p: called.append(p), raising=False)
        token = _get_token(client)
        resp = client.post('/api/open-dir', json={'path': str(tmp_path)},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert called == [str(tmp_path)]


class TestSecurityHeaders:
    def test_headers_present(self, client):
        resp = client.get('/')
        assert resp.headers['X-Content-Type-Options'] == 'nosniff'
        assert resp.headers['X-Frame-Options'] == 'DENY'
        assert resp.headers['Referrer-Policy'] == 'no-referrer'
        # 脚本已全部抽离到 static/，script-src 收紧为 'self'（不含 unsafe-inline）
        csp = resp.headers['Content-Security-Policy']
        assert "script-src 'self';" in csp
        assert "'unsafe-inline'" not in csp.split('script-src')[1].split(';')[0]

    def test_csp_allows_self_and_data_images(self, client):
        resp = client.get('/')
        csp = resp.headers['Content-Security-Policy']
        assert "img-src 'self' data:" in csp
