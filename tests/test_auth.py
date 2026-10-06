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
    def test_unauthenticated_requests_are_blocked(self, client, auth_enabled):
        """登录墙的三种形态：页面 302 到 /login，API 与 POST 一律 401（不重定向）。

        同一个不变式（未认证请求的处置），只有请求类型与期望形态不同，故合成一条。
        """
        page = client.get('/')
        assert page.status_code == 302
        assert page.headers['Location'].startswith('/login')

        api = client.get('/api/blocked-tags')
        assert api.status_code == 401
        assert api.get_json()['error_code'] == 'AUTH_REQUIRED'

        assert client.post('/api/blocked-tags', json={'tag': 'x'}).status_code == 401

    def test_exempt_paths_reach_their_handlers(self, client, auth_enabled):
        """豁免矩阵：登录页与 `/static` 前缀（含 vendor 子目录）照旧免认证。"""
        for path in ('/login',
                     '/static/app.js',
                     '/static/vendor/bootstrap-5.3.3/bootstrap.min.css'):
            assert client.get(path).status_code == 200, f'{path} 必须免认证'

    def test_static_prefix_requires_path_boundary(self, client, auth_enabled):
        # 前缀豁免必须以 '/' 为边界。裸 startswith('/static') 会让
        # '/static../.git/config' 这类路径也跳过登录墙 —— 真实部署的 nginx
        # 日志里扫描器正是这么打的（该请求当时绕过认证墙落到静态路由返回 404）。
        resp = client.get('/static../.git/config')
        assert resp.status_code == 302
        assert resp.headers['Location'].startswith('/login')

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
        import hashlib
        import hmac
        import app

        token = _get_token(client)
        with client.session_transaction() as sess:
            sess['pre_login_marker'] = 'stale'
        resp = client.post('/login', json={'password': 'test-secret'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json()['ok'] is True
        expected_version = hmac.new(
            app.app.config['SECRET_KEY'].encode(),
            b'test-secret',
            hashlib.sha256,
        ).hexdigest()
        with client.session_transaction() as sess:
            assert sess['authed'] is True
            assert sess['credential_version'] == expected_version
            assert 'pre_login_marker' not in sess
            assert 'test-secret' not in sess.values()
            assert app.app.config['SECRET_KEY'] not in sess.values()
        assert client.get('/').status_code == 200

    def test_password_rotation_revokes_existing_session(self, client, auth_enabled, monkeypatch):
        token = _get_token(client)
        resp = client.post('/login', json={'password': 'test-secret'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200

        monkeypatch.setattr('app.ACCESS_PASSWORD', 'rotated-secret')
        assert client.get('/api/blocked-tags').status_code == 401

    def test_legacy_session_without_credential_version_is_rejected(
            self, client, auth_enabled):
        with client.session_transaction() as sess:
            sess['authed'] = True
            sess.pop('credential_version', None)

        resp = client.get('/api/blocked-tags')
        assert resp.status_code == 401

    def test_settings_gates_reject_stale_session_version(
            self, client, monkeypatch):
        import hashlib
        import hmac
        import app
        import routes_settings

        monkeypatch.setattr('app.ACCESS_PASSWORD', '')
        monkeypatch.setattr('app.SETTINGS_PASSWORD', 'settings-secret')
        monkeypatch.setattr(routes_settings.time, 'sleep', lambda _seconds: None)
        stale_version = hmac.new(
            app.app.config['SECRET_KEY'].encode(),
            b'previous-secret',
            hashlib.sha256,
        ).hexdigest()
        with client.session_transaction() as sess:
            sess['authed'] = True
            sess['credential_version'] = stale_version
            sess['_csrf_token'] = 'csrf-test-token'

        assert client.get('/api/settings').status_code == 403
        resp = client.post('/api/settings/unlock', json={},
                           headers={'X-CSRF-Token': 'csrf-test-token'})
        assert resp.status_code == 403

    def test_open_redirect_blocked(self, client, auth_enabled):
        token = _get_token(client)
        resp = client.post('/login', json={'password': 'test-secret', 'next': '//evil.com'},
                           headers={'X-CSRF-Token': token})
        assert resp.get_json()['next'] == '/'

    def test_http_login_is_capped_at_5_per_minute(self, client, auth_enabled):
        """端到端：限流装饰器确实接在 `/login` 上，阈值就是 5 次/分钟。

        两种密码各跑一遍（两遍之间清空限流存储）。正确密码那一遍证明"限额不是失败
        延迟凑出来的"（这条路径不睡，5 次成功登录照常计数）；错误密码那一遍证明失败
        尝试同样计入窗口 —— 否则爆破可以靠 403 无限重试。
        """
        import app as app_module

        for password, first_five in (('test-secret', [200] * 5), ('wrong', [403] * 5)):
            app_module._rate_limit_store.clear()
            codes = [client.post('/login', json={'password': password},
                                 headers={'X-CSRF-Token': _get_token(client)}).status_code
                     for _ in range(7)]
            assert codes == first_five + [429, 429], \
                f'密码 {password!r}：第 6、7 次必须被限流，实际 {codes}'


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

    def test_login_requires_csrf(self, client, auth_enabled):
        resp = client.post('/login', json={'password': 'test-secret'})
        assert resp.status_code == 403


class TestSafeNext:
    def test_unsafe_values_fall_back_to_root(self):
        """`_safe_next` 的拒绝矩阵：只有站内相对路径放行，其余一律回退到 '/'。

        两个容易漏的规整：`/\\evil.com` 会被浏览器规整为 `//evil.com`（开放重定向）；
        控制字符（回车换行）是往 `Location` 里注入的老手法，空串则无人可跳。
        """
        import app
        for value in ('//evil.com', '/\\evil.com', '\\evil.com', '/a\r\nb', ''):
            assert app._safe_next(value) == '/', f'{value!r} 必须回退到 /（开放重定向）'
        assert app._safe_next('/detail/123') == '/detail/123'


class TestSettingsCompat:
    def test_authed_session_skips_settings_gates(self, client, auth_enabled, monkeypatch):
        """已全局登录 → 设置页与设置 API 直通；解锁接口同样直接放行（旧流程兼容）。

        设了 `SETTINGS_PASSWORD` 也拦不住已登录会话，这正是"直通"要比"没设密码所以
        放行"更强的地方，故两个接口与解锁接口共用这一次登录。
        """
        monkeypatch.setattr('app.SETTINGS_PASSWORD', 'settings-pw')
        token = _get_token(client)
        client.post('/login', json={'password': 'test-secret'},
                    headers={'X-CSRF-Token': token})

        assert client.get('/settings').status_code == 200
        assert client.get('/api/settings').status_code == 200

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
    def test_forwarded_for_always_rejected(self, client, monkeypatch, tmp_path):
        """只要请求带 `X-Forwarded-For` 就直接 403（fail-closed），不管它声称是谁。

        两类 XFF 合成一条：外部地址（8.8.8.8）与伪造的 `127.0.0.1`。旧实现只看
        `remote_addr`：XFF 被 ProxyFix 还原成 127.0.0.1 后即放行，于是任何
        "反代原样透传 XFF"的部署都能远程打开服务器本地目录。
        """
        import app as app_module
        called = []
        monkeypatch.setattr(app_module.os, 'startfile',
                            lambda p: called.append(p), raising=False)
        token = _get_token(client)

        for xff in ('8.8.8.8', '127.0.0.1'):
            resp = client.post('/api/open-dir', json={'path': str(tmp_path)},
                               headers={'X-CSRF-Token': token, 'X-Forwarded-For': xff})
            assert resp.status_code == 403, f'XFF={xff} 必须拒绝'

        assert called == [], '被拒绝的请求不能产生任何本地副作用'

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


class TestPublicDeploymentPosture:
    """审计 S6：公网/反代部署下的默认姿态必须是安全的。"""

    def test_dev_server_bind_defaults_to_loopback(self, monkeypatch):
        """`python app.py` 默认只监听 loopback（旧实现硬编码 0.0.0.0）。

        本应用默认无访问密码，且 `/api/open-dir` 能在服务器上打开本地目录 —— 直跑
        等于把这些能力暴露给整个局域网。
        """
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
        与上一条分开：那条测运行期默认值，这条测源码接线（机制不同）。
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

    def test_warns_only_when_access_password_missing(self, monkeypatch, caplog):
        """未设访问密码 = 全站免认证，启动必须留下告警痕迹；设了就不能有噪声。"""
        import app as app_module

        for password, expected in (('', True), ('secret-pw', False)):
            caplog.clear()
            monkeypatch.setattr(app_module, 'ACCESS_PASSWORD', password)
            with caplog.at_level(logging.WARNING, logger='app'):
                app_module._warn_if_unprotected()
            assert ('ACCESS_PASSWORD 未设置' in caplog.text) is expected, \
                f'ACCESS_PASSWORD={password!r} 时告警状态不对：{caplog.text!r}'

    def test_warns_only_when_tls_verify_disabled(self, monkeypatch, caplog):
        """关掉 TLS 校验必须留下告警；默认已开启，只有显式设 false 才走到这里。"""
        import app as app_module

        for verify, expected in ((False, True), (True, False)):
            caplog.clear()
            monkeypatch.setattr(app_module, 'SSL_VERIFY', verify)
            with caplog.at_level(logging.WARNING, logger='app'):
                app_module._warn_if_tls_unverified()
            assert ('TLS 校验已关闭' in caplog.text) is expected, \
                f'SSL_VERIFY={verify} 时告警状态不对：{caplog.text!r}'


class TestSecurityHeaders:
    def test_headers_present_and_csp_locked_down(self, client):
        """一次响应上钉住全部安全头：三个固定头 + CSP 的 script/img 指令。

        脚本已全部抽离到 `static/`，`script-src` 收紧为 `'self'`（不含 unsafe-inline）；
        `img-src 'self' data:` 是占位图所需。它们同属"安全头字段"这一个不变式。
        """
        resp = client.get('/')
        assert resp.headers['X-Content-Type-Options'] == 'nosniff'
        assert resp.headers['X-Frame-Options'] == 'DENY'
        assert resp.headers['Referrer-Policy'] == 'no-referrer'
        csp = resp.headers['Content-Security-Policy']
        assert "script-src 'self';" in csp
        assert "'unsafe-inline'" not in csp.split('script-src')[1].split(';')[0]
        assert "img-src 'self' data:" in csp


class TestSecretFiles:
    """密钥文件的强度与权限（审计 S17）。

    两种失败模式：① 截断/空文件被直接当密钥用 —— 空 key 的 HMAC 等于没有签名，
    游标可伪造、会话可伪造；② 文件是 0644 —— 同机其他用户能读走密钥。旧实现里
    `.cursor_secret` 连长度都不检查，`.secret_key` 只检查"是否为空"。
    """

    def test_secret_file_content_validation_matrix(self, tmp_path, caplog):
        """内容校验矩阵：缺失 / 截断 / 空白 / 够长四种输入 → 重新生成或原样沿用。

        四种输入的期望不同（是否告警、是否沿用），但前置准备与判定入口完全一致
        （同一个文件 + 同一个 `_load_or_create_secret`），故共用一次循环：
          - 缺失：首次生成是正常路径，不该产生"内容过短"这类告警噪声；
          - 截断（'abc'）：必须重新生成，且真出现截断文件要能看见（否则只会在密钥被
            换掉后百思不得其解）；
          - 空白（'   \\n'）：回归 —— 空密钥文件必须重新生成（原 app.py 的行为，现在两条
            密钥共用）；空 key 的 HMAC 等于没有签名，游标/会话都可伪造，而旧
            `.secret_key` 只检查"是否为空"；
          - 够长：必须原样使用，每次启动换密钥会让所有会话/游标失效。
        """
        import config

        cases = (
            ('缺失', '.cursor_secret', None, False, False),
            ('截断', '.cursor_secret', 'abc', True, False),
            ('空白', '.secret_key', '   \n', False, False),
            ('够长', '.cursor_secret', 'a' * 64, False, True),
        )
        for label, name, raw, expect_warning, expect_preserved in cases:
            path = tmp_path / name
            if raw is not None:
                path.write_text(raw, encoding='utf-8')

            caplog.clear()
            with caplog.at_level(logging.WARNING, logger='config'):
                secret = config._load_or_create_secret(str(path))

            assert len(secret) >= 32, f'{label}：密钥长度必须 ≥32，实际 {len(secret)}'
            assert path.is_file(), f'{label}：密钥文件必须存在'
            assert path.read_text(encoding='utf-8') == secret, \
                f'{label}：文件内容必须与返回的密钥一致（新密钥必须写回）'
            assert (secret == raw) is expect_preserved, \
                f'{label}：{"必须原样沿用" if expect_preserved else "必须重新生成"}，实际 {secret!r}'
            assert ('内容过短' in caplog.text) is expect_warning, \
                f'{label}：告警状态不对（真出现截断文件必须能看见）：{caplog.text!r}'

    def test_secret_file_mode_0600(self, tmp_path):
        """写入后权限必须是 0600（Windows 上 POSIX 权限语义不适用，跳过）。"""
        import os
        import stat
        import sys
        import config

        if sys.platform == 'win32':
            pytest.skip('Windows 的 chmod 只映射只读位，没有 POSIX 权限语义')

        path = tmp_path / '.cursor_secret'

        config._load_or_create_secret(str(path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

        os.chmod(path, 0o644)                       # 已有文件被放宽过 → 下次启动收紧
        config._load_or_create_secret(str(path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_chmod_is_requested_with_0600(self, tmp_path, monkeypatch):
        """平台无关的证据：无论读写哪条分支，都要对密钥文件请求 0600。

        上一条用例在 Windows 上会 skip，这条用替身盯住实际调用的权限位，保证
        "收紧权限"这件事在任何平台上都被执行（不是"只在 Linux 上想过")。
        """
        import config
        calls = []
        monkeypatch.setattr(config.os, 'chmod', lambda p, mode: calls.append((str(p), mode)))
        path = tmp_path / '.cursor_secret'

        config._load_or_create_secret(str(path))            # 新建分支
        path.write_text('short', encoding='utf-8')
        config._load_or_create_secret(str(path))            # 重生成分支
        path.write_text('b' * 64, encoding='utf-8')
        config._load_or_create_secret(str(path))            # 复用已有分支

        assert len(calls) == 3
        assert {mode for _, mode in calls} == {0o600}

    def test_chmod_failure_does_not_break_startup(self, tmp_path, monkeypatch):
        """权限收紧失败（Windows / 文件属主不是本用户）不能让启动炸掉。"""
        import config

        def _boom(path, mode):
            raise OSError('模拟 chmod 不被支持')

        monkeypatch.setattr(config.os, 'chmod', _boom)
        path = tmp_path / '.cursor_secret'

        secret = config._load_or_create_secret(str(path))

        assert len(secret) >= 32 and path.is_file()

    def test_app_and_config_use_the_same_helper(self, tmp_path):
        """两条密钥路径都必须经过同一助手（源码级：模块级调用确实存在）。

        `.secret_key` 没有可在测试里重跑的 seam（重跑 app.py 等于建第二个 Flask 应用并
        起后台线程），所以这里用 AST 确认模块级真的调用了助手 —— 与
        `TestPublicDeploymentPosture.test_startup_path_actually_calls_the_warning` 同款做法。
        """
        import ast
        import pathlib

        import app as app_module
        import config

        tree = ast.parse(pathlib.Path(app_module.__file__).read_text(encoding='utf-8'))
        # 只看模块级语句里的调用：app.py 这里是赋值语句（app.config['SECRET_KEY'] = ...），
        # 不是裸表达式；同时不能把函数体里的同名调用算进来（那样等于没验证启动路径）。
        called = [
            sub.func.id
            for node in tree.body
            if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign))
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
        ]
        assert '_load_or_create_secret' in called, 'app.py 模块级必须用它设置 SECRET_KEY'

        # 真实运行态：当前进程的 SECRET_KEY 必须来自那个文件，且足够长
        secret_path = pathlib.Path(app_module._secret_path)
        assert secret_path.is_file()
        assert app_module.app.config['SECRET_KEY'] == secret_path.read_text(encoding='utf-8')
        assert len(app_module.app.config['SECRET_KEY']) >= 32
        assert len(config.CURSOR_SECRET) >= 32
