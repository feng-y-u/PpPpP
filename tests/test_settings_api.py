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
