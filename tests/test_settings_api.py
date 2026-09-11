"""设置读写 API：`GET/POST /api/settings`（审计 S18 补齐）。

历史缺口：`POST /api/settings` 此前**零覆盖**，而它是设置页唯一的写入口，同时也是
`cookies.txt` 唯一的 Web 写入口 —— 也就是说"Cookie 注入剔除"这条防线此前没有用例盯着。
"""
import json
import os

import pytest

import app
import fetcher
import helpers
import routes_settings


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """settings.json 落点重定向到临时文件（路由经 app 命名空间读取该路径）。"""
    path = tmp_path / 'settings.json'
    monkeypatch.setattr(app, '_SETTINGS_PATH', str(path))
    return path


# patch 前的 COOKIE_PATH 绑定（app / fetcher 两侧），由下面这个 autouse 夹具在改之前记录。
_ORIGINAL_COOKIE_PATHS: dict = {}


@pytest.fixture(autouse=True)
def _isolate_cookies_txt(monkeypatch, tmp_path):
    """把 Cookie 落点重定向到临时文件，并**兜底断言没碰仓库真实文件**。

    落点就是 `app.COOKIE_PATH`（config.COOKIE_PATH 的再导出，fetcher 读的同一个值），
    所以直接 patch 它即可；同时把 `fetcher.COOKIE_PATH` 指向同一文件，读写两端才在
    一起（生产里它们本来就是同一个值）。
    仓库根目录下可能存在真实的 cookies.txt（.gitignore 里，但开发者机器上通常有），
    所以这里既要重定向，也要在收尾时证明它逐字节没变。
    """
    real_cookie = os.path.join(os.path.dirname(os.path.abspath(app.__file__)), 'cookies.txt')
    before = None
    if os.path.isfile(real_cookie):
        with open(real_cookie, 'rb') as f:
            before = f.read()

    target = tmp_path / 'cookies.txt'
    # patch 前的真实绑定，供 test_cookie_path_is_the_config_value 校验"两处同源"
    _ORIGINAL_COOKIE_PATHS['app'] = app.COOKIE_PATH
    _ORIGINAL_COOKIE_PATHS['fetcher'] = fetcher.COOKIE_PATH
    monkeypatch.setattr(app, 'COOKIE_PATH', str(target))
    monkeypatch.setattr(fetcher, 'COOKIE_PATH', str(target))
    original_cookie_value = fetcher._cookie_value
    original_cookie_mtime = fetcher._cookie_mtime
    yield target

    fetcher._cookie_value = original_cookie_value
    fetcher._cookie_mtime = original_cookie_mtime
    # 先**无条件还原**仓库根的真实 cookies.txt 再报告违规：该文件在 .gitignore 里，
    # 一旦被测试写坏就再没有任何副本可恢复（旧版"只断言不还原"的夹具真在证伪跑动中
    # 把它覆盖成了测试 token，只能人工重新贴 Cookie）。
    if before is None:
        created = os.path.isfile(real_cookie)
        if created:
            os.remove(real_cookie)
        assert not created, '测试不得在仓库根目录创建 cookies.txt（已清理）'
    else:
        with open(real_cookie, 'rb') as f:
            after = f.read()
        if after != before:
            with open(real_cookie, 'wb') as f:
                f.write(before)
        assert after == before, '测试不得改写仓库根目录的真实 cookies.txt（已还原）'


def _token(client):
    return client.get('/csrf-token').get_json()['token']


def _post_settings(client, payload):
    return client.post('/api/settings',
                       data=json.dumps(payload),
                       content_type='application/json',
                       headers={'X-CSRF-Token': _token(client)})


class TestSettingsGet:
    def test_masks_passwords_and_cookie(self, client, _isolate_settings):
        """密码类与 Cookie 字段绝不回传明文（纵深防御）。"""
        _isolate_settings.write_text(json.dumps({
            'access_password': 'super-secret',
            'settings_password': 'another-secret',
            'cookie': 'PHPSESSID=leaked-token',
            'per_page': 30,
        }), encoding='utf-8')

        resp = client.get('/api/settings')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['access_password'] == ''
        assert data['settings_password'] == ''
        assert data['cookie'] == ''
        assert data['per_page'] == 30, '普通配置项必须照常回传'
        body = json.dumps(data, ensure_ascii=False)
        assert 'super-secret' not in body
        assert 'leaked-token' not in body

    def test_defaults_when_file_missing(self, client, _isolate_settings):
        """没有 settings.json 时回默认值；密码类不在设置页可编辑键里。"""
        data = client.get('/api/settings').get_json()

        assert data['per_page'] == routes_settings._SETTINGS_DEFAULTS['per_page']
        assert 'access_password' not in data
        assert 'cookie_secure' not in data

    def test_corrupt_file_falls_back_to_defaults(self, client, _isolate_settings):
        """损坏文件不能让设置页 500（否则用户连改回默认值的机会都没有）。"""
        _isolate_settings.write_text('{broken json', encoding='utf-8')

        resp = client.get('/api/settings')

        assert resp.status_code == 200
        assert resp.get_json()['per_page'] == routes_settings._SETTINGS_DEFAULTS['per_page']


class TestSettingsLock:
    """`SETTINGS_PASSWORD` 门禁：读与写都必须拦住（写更关键）。"""

    def test_locked_get_and_post_403(self, client, monkeypatch, _isolate_settings):
        monkeypatch.setattr(app, 'SETTINGS_PASSWORD', 'pw')

        assert client.get('/api/settings').status_code == 403
        resp = _post_settings(client, {'per_page': 30})

        assert resp.status_code == 403
        assert not _isolate_settings.exists(), '锁定状态下不得写盘'


class TestSettingsPost:
    def test_write_merges_known_keys_only(self, client, _isolate_settings):
        resp = _post_settings(client, {
            'per_page': 30,
            'prefetch_pages': 4,
            'unknown_key': 'ignored',
            'access_password': 'should-not-be-writable-through-the-ui',
        })

        assert resp.status_code == 200
        saved = json.loads(_isolate_settings.read_text(encoding='utf-8'))
        assert saved['per_page'] == 30
        assert saved['prefetch_pages'] == 4
        assert 'unknown_key' not in saved
        assert saved.get('access_password') != 'should-not-be-writable-through-the-ui'

    def test_write_is_atomic_and_leaves_no_tmp(self, client, _isolate_settings):
        resp = _post_settings(client, {'per_page': 30, 'prefetch_pages': 4})

        assert resp.status_code == 200
        saved = json.loads(_isolate_settings.read_text(encoding='utf-8'))
        assert saved['per_page'] == 30
        assert saved['prefetch_pages'] == 4
        assert [p.name for p in _isolate_settings.parent.iterdir()] == ['settings.json'], \
            '同目录不得残留 .tmp'

    def test_write_failure_keeps_old_bytes(self, client, _isolate_settings, monkeypatch):
        """替换失败 → 500，旧文件逐字节不变、不留半份文件、内存态不漂移。"""
        original = json.dumps({'per_page': 17}, ensure_ascii=False)
        _isolate_settings.write_text(original, encoding='utf-8')
        before_state = dict(app._prefetch_state)

        def _boom(src, dst, *a, **kw):
            raise OSError('模拟磁盘写入失败')

        monkeypatch.setattr(helpers.os, 'replace', _boom)

        resp = _post_settings(client, {'per_page': 99, 'prefetch_interval': 77})

        assert resp.status_code == 500
        assert '保存失败' in resp.get_json()['error']
        assert _isolate_settings.read_text(encoding='utf-8') == original, '旧内容必须完整保留'
        assert [p.name for p in _isolate_settings.parent.iterdir()] == ['settings.json']
        assert dict(app._prefetch_state) == before_state, '写盘失败不得更新内存态'

    def test_prefetch_keys_apply_immediately(self, client, _isolate_settings):
        """prefetch_* 是唯一保存即生效（不需重启）的一组键。

        回归守卫：内存态（`_prefetch_state`）用短键，settings.json 用长键。历史上设置页
        保存把长键直接写进内存态，`background` 的预取循环读 `_prefetch_state['interval']`
        读到的仍是旧值 —— "立即生效"实际从未生效（审计 S18 补测时发现）。
        """
        resp = _post_settings(client, {
            'prefetch_interval': 321,
            'prefetch_pages': 3,
            'prefetch_max_illusts': 555,
        })

        assert resp.status_code == 200
        assert app._prefetch_state['interval'] == 321
        assert app._prefetch_state['pages'] == 3
        assert app._prefetch_state['max_illusts'] == 555
        saved = json.loads(_isolate_settings.read_text(encoding='utf-8'))
        assert saved['prefetch_interval'] == 321, '落盘仍用长键'
        # 内存态不得被塞进长键（写进去也没人读，只会掩盖问题）
        assert 'prefetch_interval' not in app._prefetch_state
        # 与 /api/prefetch/config 读到的必须是同一个值
        assert client.get('/api/prefetch/config').get_json()['interval'] == 321


class TestSettingsCookie:
    """Cookie 字段：唯一会落盘到 cookies.txt 的入口，注入剔除必须守住。"""

    def test_cookie_written_as_single_line(self, client, _isolate_cookies_txt):
        resp = _post_settings(client, {'cookie': 'abc123def456'})

        assert resp.status_code == 200
        assert _isolate_cookies_txt.read_text(encoding='utf-8') == 'PHPSESSID=abc123def456\n'
        assert fetcher._cookie_value == 'abc123def456'

    def test_control_chars_cannot_inject_extra_lines(self, client, _isolate_cookies_txt):
        """换行/回车/NUL 必须被剔除：否则可写入第二行伪造其它 Cookie。"""
        resp = _post_settings(client, {'cookie': 'abc\r\nINJECTED=1\t\x00\n'})

        assert resp.status_code == 200
        written = _isolate_cookies_txt.read_text(encoding='utf-8')
        assert written.endswith('\n')
        assert written.count('\n') == 1, '只能有一行（末尾换行）'
        assert '\r' not in written and '\t' not in written and '\x00' not in written
        assert written.startswith('PHPSESSID=abcINJECTED=1')
        assert fetcher._cookie_value == 'abcINJECTED=1'

    def test_cookie_of_only_control_chars_is_rejected(self, client, _isolate_cookies_txt):
        """剔除后为空说明用户根本没填有效内容 → 400，且不碰文件。"""
        resp = _post_settings(client, {'cookie': '\r\n\t\x00'})

        assert resp.status_code == 400
        assert '无效' in resp.get_json()['error']
        assert not _isolate_cookies_txt.exists()

    def test_empty_cookie_leaves_file_alone(self, client, _isolate_cookies_txt):
        """空 Cookie 字段 = 不改 Cookie（设置页其它改动照常保存）。"""
        resp = _post_settings(client, {'cookie': '', 'per_page': 25})

        assert resp.status_code == 200
        assert not _isolate_cookies_txt.exists()

    def test_cookie_write_failure_500_and_settings_untouched(self, client,
                                                            _isolate_cookies_txt,
                                                            _isolate_settings):
        """cookies.txt 写不进去 → 500，且 settings.json 一个字节都不写。

        写序是先 Cookie 后设置，失败必须整体放弃：否则会出现"设置存了、Cookie 没存"
        的半完成状态，用户看到 200 却仍在用旧 Cookie。
        """
        os.makedirs(_isolate_cookies_txt)     # 同名目录 → open(..., 'w') 必然 OSError

        resp = _post_settings(client, {'cookie': 'will-fail', 'per_page': 31})

        assert resp.status_code == 500
        assert 'cookies.txt 写入失败' in resp.get_json()['error']
        assert str(_isolate_cookies_txt) in resp.get_json()['error'], '错误信息要给出实际落点'
        assert not _isolate_settings.exists()
        assert fetcher._cookie_value != 'will-fail'

    def test_cookie_lands_where_the_fetcher_reads_it(self, client, _isolate_cookies_txt):
        """写盘落点必须是 fetcher 读的那个文件，而不是"项目根下的同名文件"。

        审计 §20.2 的 Medium 发现：路由此前硬编码项目根目录（`__file__` 推导），而
        fetcher 读 `config.COOKIE_PATH`（Linux 上存在 `/etc/pixiv-viewer/cookies.txt`
        时就是它）。那种部署里设置页等于在写一个没人读的文件 —— 进程内靠直接赋值
        `_cookie_value` 看着生效，**重启后旧 Cookie 复辟**。

        本用例把落点与读侧分开验证：文件写在 COOKIE_PATH；写完之后真的能被 `_load_cookie`
        从磁盘读回来（而不是只看内存赋值这层伪装）。
        """
        resp = _post_settings(client, {'cookie': 'roundtrip-token'})

        assert resp.status_code == 200
        assert _isolate_cookies_txt.read_text(encoding='utf-8') == 'PHPSESSID=roundtrip-token\n'

        # 读侧真链路：抹掉内存态强制回读磁盘（跑的是 fetcher 自己的读取逻辑）
        fetcher._cookie_value = ''
        fetcher._cookie_mtime = 0.0
        fetcher._load_cookie()
        assert fetcher._cookie_value == 'roundtrip-token', '设置页写的 Cookie 必须能被读回来'

    def test_cookie_path_is_the_config_value(self):
        """落点就是 config.COOKIE_PATH（经 app 命名空间再导出），不是第二份路径推导。"""
        import config

        assert _ORIGINAL_COOKIE_PATHS['app'] == config.COOKIE_PATH
        assert _ORIGINAL_COOKIE_PATHS['fetcher'] == config.COOKIE_PATH

    def test_cookie_write_is_an_atomic_swap(self, client, _isolate_cookies_txt, monkeypatch):
        """写 Cookie 必须是"同目录 tmp → os.replace"，**不能先把目标文件截断再写**。

        为什么这在这里是必需品而不是洁癖：`fetcher._load_cookie()` 在其它线程里读同一
        路径（生产是 `gunicorn -w 1 --threads 8`），而 `open(path, 'w')` 会**先截断**。
        读侧读到空串时会把 `_cookie_value` 置空并**连同 mtime 一起缓存**（见
        `fetcher._load_cookie`），之后除非文件 mtime 再变，那条线程/那个连接池会一直
        用空 Cookie —— 症状就是"设置页明明保存成功了，搜索仍 401/空结果，重启才好"。

        本用例在 `os.replace` 被调用的**那一刻**取证：目标文件必须仍是完整的旧值
        （说明它从未被提前截断），tmp 里已是完整的新值。
        """
        target = _isolate_cookies_txt
        target.write_text('PHPSESSID=old-token\n', encoding='utf-8')
        real_replace = os.replace
        observed = []

        def spy(src, dst, *args, **kwargs):
            if os.path.abspath(dst) == os.path.abspath(str(target)):
                with open(dst, encoding='utf-8') as f:
                    old_seen = f.read()
                with open(src, encoding='utf-8') as f:
                    new_seen = f.read()
                observed.append((old_seen, new_seen))
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, 'replace', spy)

        resp = _post_settings(client, {'cookie': 'new-token'})

        assert resp.status_code == 200
        assert observed == [('PHPSESSID=old-token\n', 'PHPSESSID=new-token\n')], (
            '替换瞬间应当是"旧值仍完整、新值已完整落在 tmp"；'
            '观测为空说明没有走 tmp+replace 原子替换')
        assert target.read_text(encoding='utf-8') == 'PHPSESSID=new-token\n'
        assert not os.path.exists(str(target) + '.tmp'), '成功路径不得残留 tmp'

    def test_failed_cookie_write_keeps_the_previous_cookie(
            self, client, _isolate_cookies_txt, monkeypatch):
        """交换失败时必须保住原来那个能用的 Cookie，并清掉 tmp。

        旧实现"先截断再写"会把"保存失败"直接升级成"立刻断网"：中途任何失败都留下
        一个空文件，而读侧还会把空值缓存住。
        """
        target = _isolate_cookies_txt
        target.write_text('PHPSESSID=old-token\n', encoding='utf-8')
        real_replace = os.replace

        def explode(src, dst, *args, **kwargs):
            if os.path.abspath(dst) == os.path.abspath(str(target)):
                raise OSError('disk full（模拟替换失败）')
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, 'replace', explode)

        resp = _post_settings(client, {'cookie': 'new-token'})

        assert resp.status_code == 500
        assert str(target) in resp.get_json()['error'], '错误信息要给出实际路径'
        assert target.read_text(encoding='utf-8') == 'PHPSESSID=old-token\n', (
            '写入失败不得破坏原来可用的 Cookie')
        assert not os.path.exists(str(target) + '.tmp'), '失败路径必须清掉 tmp'

    def test_concurrent_reader_never_sees_a_truncated_cookie(
            self, client, _isolate_cookies_txt):
        """读侧并发（`--threads 8` 的真实情形）：读到的值必须是旧的或新的，**绝不能是空串**。

        这是症状级断言：空串一旦被 `_load_cookie` 缓存住（它读到空就把 `_cookie_value`
        置空并连 mtime 一起记下），那条线程会一直用空 Cookie，直到 mtime 再变。
        读线程每次强制把 `_cookie_mtime` 归零，保证它真的回读磁盘（模拟"刚建连接池/
        换了线程"），否则 mtime 缓存会把问题挡住。

        平台差异（如实断言，不放宽核心保证）：POSIX 的 `rename` 是原子的，读侧既不会
        读到半截内容、也不会报错；Windows 上 `os.replace` 期间目标名会短暂处于"删除
        待定"状态，读侧 `open` 可能抛 **PermissionError** —— 那仍不是读到脏内容。所以
        这里对"绝不出现空/半截值"在两端都断言（这才是被审计的缺陷），对"读侧不报错"
        只在 POSIX 上断言。生产是 Linux（systemd + gunicorn），Windows 只是开发机。

        读侧刻意 sleep 2ms 而不是死循环空转：真实读侧是请求线程在 mtime 变化时 open
        一次（`fetcher._load_cookie`），不是自旋。自旋会把 Windows 的共享冲突放大成
        "目标文件永远开着"，那是测试造出来的现象、不是产品现象。2ms 的轮询仍然比写侧
        的截断窗口快几个数量级 —— 旧实现上照样能复现读到空串（2026-09-11 实测 120 轮
        内必现）。
        """
        import threading
        import time

        target = _isolate_cookies_txt
        target.write_text('PHPSESSID=old-token\n', encoding='utf-8')
        rounds = 120
        seen_values = []
        read_errors = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    fetcher._cookie_mtime = 0.0
                    fetcher._load_cookie()
                    seen_values.append(fetcher._cookie_value)
                except PermissionError as exc:
                    read_errors.append(exc)   # 仅 Windows 的共享冲突，见 docstring
                except Exception as exc:
                    read_errors.append(exc)
                time.sleep(0.002)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for i in range(rounds):
                resp = _post_settings(client, {'cookie': f'new-token-{i}'})
                assert resp.status_code == 200, f'第 {i} 次写入失败: {resp.get_json()}'
        finally:
            stop.set()
            thread.join(timeout=5)

        if os.name == 'posix':
            assert not read_errors, f'POSIX 下读侧不该报错: {read_errors[:3]}'
        else:
            unexpected = [e for e in read_errors if not isinstance(e, PermissionError)]
            assert not unexpected, f'Windows 下只允许共享冲突型 PermissionError: {unexpected[:3]}'

        allowed = {'old-token'} | {f'new-token-{i}' for i in range(rounds)}
        bad = [v for v in seen_values if v not in allowed]
        assert not bad, (
            f'读到非完整值（空/截断）{bad[:5]}，共读取 {len(seen_values)} 次；'
            '写侧必须先写 tmp 再原子替换')

        # 收尾一致性：磁盘上是最后一次写进去的值，且能被读回来
        fetcher._cookie_mtime = 0.0
        fetcher._load_cookie()
        assert fetcher._cookie_value == f'new-token-{rounds - 1}'

    @pytest.mark.skipif(os.name != 'posix', reason='POSIX 权限位（Windows 的 chmod 语义不同）')
    def test_cookie_write_preserves_existing_file_mode(self, client, _isolate_cookies_txt):
        """已加固过的 Cookie 文件权限不得被设置页悄悄放宽。

        旧实现 `open(path, 'w')` 对**已存在**的文件是保留原权限的；换成 tmp+replace 后
        新文件的权限来自 tmp（默认 umask），所以必须显式把原模式挪到 tmp 上。
        """
        target = _isolate_cookies_txt
        target.write_text('PHPSESSID=old-token\n', encoding='utf-8')
        os.chmod(target, 0o600)

        assert _post_settings(client, {'cookie': 'new-token'}).status_code == 200

        assert oct(os.stat(target).st_mode & 0o777) == oct(0o600)


class _AliveThread:
    """最小替身：`get_background_health()` 只对线程对象调 `is_alive()`。"""

    def is_alive(self):
        return True


class _DeadThread:
    """真线程、真退出：比 `is_alive() → False` 的假对象更接近"线程死了"的现场。"""

    def __init__(self):
        import threading

        self._t = threading.Thread(target=lambda: None)
        self._t.start()
        self._t.join()

    def is_alive(self):
        return self._t.is_alive()


class TestAutoFollowStatus:
    """`/api/auto-follow/status`：state 之外还要回答"后台线程还在不在"。

    该字段此前**只**出现在 `/api/prefetch/status` 里，而设置页既不读那个路由的
    这个键、也不读本路由 —— 也就是说"自动关注静默停止"在界面上完全看不到。
    本组用例盯住两点：`alive` 反映**真实线程引用**（不是常量），以及它是派生值、
    **不得写进 `_auto_follow_state`**（那个 dict 由自动关注线程与 config 路由共用）。
    """

    def test_reports_running_thread_and_state(self, client, monkeypatch):
        import background
        import runtime

        monkeypatch.setattr(background, '_auto_follow_thread', _AliveThread())
        monkeypatch.setitem(runtime._auto_follow_state, 'interval', 600)
        monkeypatch.setitem(runtime._auto_follow_state, 'auto_download', True)
        monkeypatch.setitem(runtime._auto_follow_state, 'last_check', '2026-09-11T05:00:00+00:00')
        monkeypatch.setitem(runtime._auto_follow_state, 'last_count', 3)

        resp = client.get('/api/auto-follow/status')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['alive'] is True
        assert data['interval'] == 600
        assert data['auto_download'] is True
        assert data['last_check'] == '2026-09-11T05:00:00+00:00'
        assert data['last_count'] == 3, 'state 里的既有字段必须照常回传'
        assert {'last_check', 'last_count', 'interval', 'auto_download', 'alive'} <= set(data)

    @pytest.mark.parametrize('fake_thread', [None, _DeadThread()], ids=['never_started', 'dead'])
    def test_reports_stopped_thread(self, client, monkeypatch, fake_thread):
        """没启动（None）与启动后已退出（dead）都必须报 False —— 这正是"静默停止"的信号。"""
        import background

        monkeypatch.setattr(background, '_auto_follow_thread', fake_thread)

        data = client.get('/api/auto-follow/status').get_json()

        assert data['alive'] is False

    def test_derived_alive_is_not_written_into_runtime_state(self, client):
        """`alive` 是派生值：写进 `_auto_follow_state` 会污染运行态（config 路由会回传它）。"""
        import runtime

        before = dict(runtime._auto_follow_state)

        data = client.get('/api/auto-follow/status').get_json()

        assert 'alive' in data, '响应里要有这个字段'
        assert 'alive' not in runtime._auto_follow_state, '不得把派生值塞进运行态'
        assert runtime._auto_follow_state == before, '运行态必须逐键不变'

    def test_settings_page_actually_consumes_the_field(self):
        """前端接线静态核对：光有 JSON 字段、界面上没人看，就等于没修。

        仓库没有前端测试运行器（原生 JS 无构建），所以这里只能核对"容器存在、
        脚本真的请求这个路由、且函数被调用" —— 它挡的是真实回归：改个 id、漏掉
        fetch、定义完忘了调用，都会让这个字段重新变成"只在 JSON 里"。
        """
        root = os.path.dirname(os.path.abspath(app.__file__))  # app.py 就在仓库根
        with open(os.path.join(root, 'templates', 'settings.html'), encoding='utf-8') as f:
            tpl = f.read()
        with open(os.path.join(root, 'static', 'page-settings.js'), encoding='utf-8') as f:
            js = f.read()

        assert 'id="autoFollowStatus"' in tpl, '自动关注卡片里要有状态容器'
        assert "'/api/auto-follow/status'" in js, '脚本要请求本路由'
        assert '\nloadAutoFollowStatus();' in js, '页面加载时必须真的调用（顶层调用点）'
        assert js.count('loadAutoFollowStatus(') >= 3, '定义 + 页面加载 + 保存后刷新'

    def test_reports_last_error_of_the_most_recent_round(self, client, monkeypatch):
        """`last_error` 要经本路由暴露（审计 S20 遗留）。

        它是 worker 写的运行态字段（与 `alive` 相反：`alive` 是派生值、只在这里拼），
        所以直接随 `dict(_auto_follow_state)` 回传。语义：非空 = **最近一轮**失败
        （成功跑完一轮会清空）—— 这正是把"没有新作品"和"每轮都失败"分开的那个字段。
        worker 侧的写入/清空时机由 `tests/test_auto_follow.py` 盯住。
        """
        import background
        import runtime

        monkeypatch.setattr(background, '_auto_follow_thread', _AliveThread())
        monkeypatch.setitem(runtime._auto_follow_state, 'last_error', '自动关注异常: 模拟网络挂了')

        data = client.get('/api/auto-follow/status').get_json()

        assert data['last_error'] == '自动关注异常: 模拟网络挂了'
        assert data['alive'] is True, '线程活着与最近一轮失败可以同时成立 —— 界面不能只看 alive'
        assert 'last_error' in runtime._auto_follow_state, '它属于运行态（worker 写），不是派生值'

    def test_settings_page_shows_the_last_round_error(self):
        """前端接线静态核对：`last_error` 必须真的被渲染出来（否则等于没补这个字段）。"""
        root = os.path.dirname(os.path.abspath(app.__file__))
        with open(os.path.join(root, 'static', 'page-settings.js'), encoding='utf-8') as f:
            js = f.read()

        assert 's.last_error' in js, '脚本要消费这个字段'
        assert '最近一轮出错' in js, '要有一句人能读懂的文案'
        assert "el.classList.toggle('text-danger', !s.alive || !!s.last_error)" in js, \
            '线程活着但每轮都失败也要标红 —— 这正是这个字段存在的意义'
