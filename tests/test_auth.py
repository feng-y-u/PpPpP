import logging
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

    def test_unlock_failure_applies_delay(self, client, monkeypatch):
        """解锁失败要像登录失败一样延迟 1 秒，减缓爆破。"""
        import app as app_module
        import routes_settings
        monkeypatch.setattr(app_module, 'SETTINGS_PASSWORD', 'settings-pw')
        sleeps = []

        class _FakeTime:
            def sleep(self, seconds):
                sleeps.append(seconds)

        monkeypatch.setattr(routes_settings, 'time', _FakeTime())
        token = _get_token(client)
        resp = client.post('/api/settings/unlock', json={'password': 'wrong'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 403
        assert sleeps == [1]


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

    def test_rejects_spoofed_loopback_via_xff(self, client, monkeypatch, tmp_path):
        """伪造 `X-Forwarded-For: 127.0.0.1` 让 ProxyFix 还原出本机地址 —— 仍必须拒绝。

        旧实现只看 `remote_addr`：XFF 被 ProxyFix 还原成 127.0.0.1 后即放行，
        于是任何"反代原样透传 XFF"的部署都能远程打开服务器本地目录。
        """
        import app as app_module
        called = []
        monkeypatch.setattr(app_module.os, 'startfile',
                            lambda p: called.append(p), raising=False)
        token = _get_token(client)
        resp = client.post('/api/open-dir', json={'path': str(tmp_path)},
                           headers={'X-CSRF-Token': token,
                                    'X-Forwarded-For': '127.0.0.1'})
        assert resp.status_code == 403
        assert called == [], '被拒绝的请求不能产生任何本地副作用'


class TestPublicDeploymentPosture:
    """审计 S6：公网/反代部署下的默认姿态必须是安全的。"""

    def test_dev_server_bind_defaults_to_loopback(self, monkeypatch):
        """`python app.py` 默认只监听 loopback（旧实现硬编码 0.0.0.0）。"""
        import app as app_module
        monkeypatch.delenv('HOST', raising=False)
        monkeypatch.delenv('PORT', raising=False)
        assert app_module._dev_server_bind() == ('127.0.0.1', 5000)

        monkeypatch.setenv('HOST', '0.0.0.0')     # 显式要求时才放开
        monkeypatch.setenv('PORT', '9000')
        assert app_module._dev_server_bind() == ('0.0.0.0', 9000)

    def test_main_block_does_not_hardcode_public_bind(self):
        """`__main__` 块必须走 `_dev_server_bind()`，不能再硬编码对外监听。

        `__main__` 块没有可 patch 的 seam（执行它等于起服务器），所以这里直接看源码：
        旧实现是 `app.run(debug=False, host='0.0.0.0', port=5000)` 一行字面量。
        """
        import inspect
        import app as app_module

        source = inspect.getsource(app_module)
        main_block = source.split("if __name__ == '__main__':", 1)
        assert len(main_block) == 2, 'app.py 必须有 __main__ 块'
        assert '_dev_server_bind()' in main_block[1]
        assert '0.0.0.0' not in main_block[1]

    def test_startup_path_actually_calls_the_warning(self):
        """只有函数体、没人调用 = 生产上不会出现告警，故检查模块级确实调用了它。"""
        import ast
        import pathlib
        import app as app_module

        tree = ast.parse(pathlib.Path(app_module.__file__).read_text(encoding='utf-8'))
        module_level_calls = [
            node.value.func.id
            for node in tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ]
        assert '_warn_if_unprotected' in module_level_calls

    def test_warns_when_access_password_missing(self, monkeypatch, caplog):
        """未设访问密码 = 全站免认证，启动必须留下告警痕迹。"""
        import app as app_module
        monkeypatch.setattr(app_module, 'ACCESS_PASSWORD', '')
        with caplog.at_level(logging.WARNING, logger='app'):
            app_module._warn_if_unprotected()
        assert 'ACCESS_PASSWORD 未设置' in caplog.text

    def test_silent_when_access_password_set(self, monkeypatch, caplog):
        import app as app_module
        monkeypatch.setattr(app_module, 'ACCESS_PASSWORD', 'secret-pw')
        with caplog.at_level(logging.WARNING, logger='app'):
            app_module._warn_if_unprotected()
        assert 'ACCESS_PASSWORD 未设置' not in caplog.text

    def test_warns_when_tls_verify_disabled(self, monkeypatch, caplog):
        """关掉 TLS 校验必须留下告警（默认已开启，只有显式设 false 才走到这里）。"""
        import app as app_module
        monkeypatch.setattr(app_module, 'SSL_VERIFY', False)
        with caplog.at_level(logging.WARNING, logger='app'):
            app_module._warn_if_tls_unverified()
        assert 'TLS 校验已关闭' in caplog.text

    def test_silent_when_tls_verify_enabled(self, monkeypatch, caplog):
        import app as app_module
        monkeypatch.setattr(app_module, 'SSL_VERIFY', True)
        with caplog.at_level(logging.WARNING, logger='app'):
            app_module._warn_if_tls_unverified()
        assert 'TLS 校验已关闭' not in caplog.text


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
