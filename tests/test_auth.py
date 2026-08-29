import threading
import time

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


class TestRateLimitConcurrency:
    """限流是安全控制，并发下不能被绕过。

    回归：限流曾把"读窗口 → 判定 → 记录"拆成无锁的多条语句，并发请求各自读到
    未计入对方的 records，于是同时通过判定——gunicorn --threads 下每次登录请求
    都在不同线程，爆破成本从 5 次/分钟变成 5 次/批。

    注意：这里直接压测 _check_rate_limit 而不是走 HTTP。走完整请求时每个线程
    都要先穿过路由/session 等大量代码，真正撞进临界区的时刻被自然错开，
    竞态窗口几乎打不中（实测走 HTTP 的用例在拆掉锁的情况下依然全绿——
    那样的测试是无效的）。直接压测才让线程真正重叠在临界区上。
    """

    class _YieldList(list):
        """读出长度后主动让出 GIL，把线程切换强制推进限流的判定窗口内。

        限流的临界区（判定 → 记录）只有几微秒。默认 5ms 的 GIL 切换间隔下线程
        根本来不及在窗口内被抢占，竞态永远打不中——实测走完整 HTTP 请求、压到
        100 线程、把 switchinterval 降到 1µs 都依然全绿。只有在这里主动 yield，
        线程才会真正重叠在"已读到旧长度、尚未 append"的那一瞬。
        """

        def __len__(self):
            n = super().__len__()
            time.sleep(0)
            return n

    def test_concurrent_burst_cannot_exceed_limit(self):
        import middleware

        # 单轮复现率约 2/3（CPython 线程调度决定），跑 40 轮取最大值：
        # 有 bug 时几乎必然被抓到，无 bug 时每轮都恰好放行 limit 个。
        total, limit, rounds = 8, 1, 40
        seen = set()

        for _ in range(rounds):
            middleware._rate_limit_store.clear()
            # 预置成 _YieldList：setdefault 会命中它，从而接管 len() 的时机
            middleware._rate_limit_store['1.2.3.4'] = self._YieldList()
            barrier = threading.Barrier(total)
            allowed = []
            guard = threading.Lock()

            def attempt():
                barrier.wait()
                if not middleware._check_rate_limit('1.2.3.4', limit, 60):
                    with guard:
                        allowed.append(1)

            threads = [threading.Thread(target=attempt) for _ in range(total)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            assert not any(t.is_alive() for t in threads), '有线程挂住'
            seen.add(len(allowed))

        assert max(seen) == limit, (
            f'限流被绕过：{rounds} 轮并发里最多放行了 {max(seen)} 个'
            f'（上限 {limit}），观察到 {sorted(seen)}')

    def test_http_login_still_capped_at_5(self, client, auth_enabled):
        """端到端冒烟：装饰器确实接到了限流上（用正确密码，避免失败延迟）。"""
        token = _get_token(client)
        codes = [client.post('/login', json={'password': 'test-secret'},
                             headers={'X-CSRF-Token': token}).status_code
                 for _ in range(7)]
        assert codes == [200] * 5 + [429, 429]

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
