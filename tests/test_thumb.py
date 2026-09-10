"""`/thumb` 越界重定向：拒绝 + 自动发现（审计 S7b）。

背景：`fetcher.build_pixiv_session()` 挂的是**会话级** Cookie 头，requests 会把它
发给任意主机 —— 所以"跟随到白名单外主机"等价于把 PHPSESSID 交给第三方，重定向
目标必须按凭据分级；同时 SSRF 真正危险的目标（内网、云元数据端点）必须硬性封死。
"""
import base64
import json
import os

import pytest
import requests

import config
import runtime
import routes_gallery

THUMB = 'https://i.pximg.net/c/250x250/img/test.jpg'
CROSS_HOST = 'img-cdn.example.net'
CROSS_TARGET = f'https://{CROSS_HOST}/img/new.jpg'
IMAGE_BYTES = b'\xff\xd8\xff\xe0fake-jpeg-bytes'


def _b64(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, body=IMAGE_BYTES):
        self.status_code = status_code
        self.headers = headers if headers is not None else {'Content-Type': 'image/jpeg'}
        self._body = body
        self.closed = False

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400 and 'Location' in self.headers

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code}')

    def iter_content(self, chunk_size=8192):
        yield self._body

    def close(self):
        self.closed = True


class _FakeSession:
    """替身 session：按 URL 出牌，并记录每次出站请求的凭据档位与跟随开关。"""

    def __init__(self, registry, with_cookie: bool):
        self.registry = registry
        self.with_cookie = with_cookie
        self.headers = {'Cookie': 'PHPSESSID=secret'} if with_cookie else {}

    def get(self, url, timeout=None, stream=False, allow_redirects=True):
        self.registry.calls.append({
            'url': url,
            'with_cookie': self.with_cookie,
            'cookie': self.headers.get('Cookie'),
            'allow_redirects': allow_redirects,
        })
        resp = self.registry.routes.get(url)
        if resp is None:
            raise requests.ConnectionError(f'未预置响应: {url}')
        return resp


class _Registry:
    def __init__(self):
        self.routes: dict = {}
        self.calls: list[dict] = []

    def add(self, url, status_code=200, headers=None, body=IMAGE_BYTES):
        self.routes[url] = _FakeResponse(status_code, headers, body)

    def redirect(self, url, target: str, status_code=302):
        self.add(url, status_code=status_code, headers={'Location': target})

    def session(self, with_cookie: bool = True):
        return _FakeSession(self, with_cookie)


@pytest.fixture
def thumb_env(monkeypatch, tmp_path):
    """隔离磁盘缓存、发现表落盘路径与进程级失败冷却。"""
    registry = _Registry()
    monkeypatch.setattr(routes_gallery, 'CACHE_DIR', str(tmp_path / 'cache'))
    os.makedirs(routes_gallery.CACHE_DIR, exist_ok=True)
    monkeypatch.setattr(runtime, 'thumb_redirect_state_path',
                        lambda: str(tmp_path / 'thumb_redirect_hosts.json'))
    monkeypatch.setattr(routes_gallery, 'get_pooled_session', registry.session)
    monkeypatch.setattr(routes_gallery, 'reset_pooled_session', lambda: None)
    runtime._thumb_failed.clear()
    with runtime._thumb_redirect_lock:
        runtime._thumb_redirect_hosts.clear()
        runtime._thumb_redirect_rejected.clear()
    yield {'registry': registry, 'tmp': tmp_path, 'state': tmp_path / 'thumb_redirect_hosts.json'}
    runtime._thumb_failed.clear()


def _rejected(host: str) -> int:
    return runtime.thumb_redirect_snapshot()['rejected'].get(host, 0)


# ── 正常路径 ──

def test_thumb_serves_image_and_never_follows_automatically(thumb_env, client):
    """无重定向：正常出图，且请求显式关闭自动跟随。"""
    thumb_env['registry'].add(THUMB)

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 200
    assert resp.data == IMAGE_BYTES
    calls = thumb_env['registry'].calls
    assert [c['url'] for c in calls] == [THUMB]
    assert calls[0]['allow_redirects'] is False, '必须自己处理重定向，不能交给 requests'


def test_thumb_follows_redirect_within_static_whitelist_with_cookie(thumb_env, client):
    """A 级：目标仍在白名单内 → 带凭据跟随一次（官方图床内部跳转属正常）。"""
    target = 'https://i.pximg.net/img-original/img/2026/01/01/x_p0.jpg'
    thumb_env['registry'].redirect(THUMB, target)
    thumb_env['registry'].add(target)

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 200
    calls = thumb_env['registry'].calls
    assert [c['url'] for c in calls] == [THUMB, target]
    assert all(c['with_cookie'] for c in calls), '白名单内重定向仍应携带凭据'


def test_thumb_resolves_relative_location(thumb_env, client):
    """相对 Location 必须按原 URL 解析（否则会当成非法目标拒掉）。"""
    relative = '/img-original/img/2026/01/01/rel_p0.jpg'
    target = f'https://i.pximg.net{relative}'
    thumb_env['registry'].redirect(THUMB, relative, status_code=301)
    thumb_env['registry'].add(target)

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 200
    assert [c['url'] for c in thumb_env['registry'].calls] == [THUMB, target]


def test_thumb_follows_cross_host_redirect_without_cookie(thumb_env, client):
    """B 级：白名单外的公网 https 目标 → 跟随成功，但**不带任何凭据**，且记入发现表。"""
    thumb_env['registry'].redirect(THUMB, CROSS_TARGET)
    thumb_env['registry'].add(CROSS_TARGET)

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 200
    calls = thumb_env['registry'].calls
    assert [c['url'] for c in calls] == [THUMB, CROSS_TARGET]
    assert calls[0]['with_cookie'] is True
    assert calls[1]['with_cookie'] is False, '跨域跟随必须改用无凭据会话'
    assert calls[1]['cookie'] is None, 'PHPSESSID 绝不能发给白名单外主机'

    snapshot = runtime.thumb_redirect_snapshot()
    assert [e['host'] for e in snapshot['discovered']] == [CROSS_HOST]
    entry = snapshot['discovered'][0]
    assert entry['count'] == 1
    assert entry['last_content_type'] == 'image/jpeg'
    assert entry['first_seen'] and entry['last_seen']
    assert snapshot['rejected'] == {}


# ── 拒绝路径 ──

@pytest.mark.parametrize('name,target', [
    ('metadata', 'https://169.254.169.254/latest/meta-data/'),
    ('loopback', 'https://127.0.0.1/x.jpg'),
    ('loopback-v6', 'https://[::1]/x.jpg'),
    ('private', 'https://10.1.2.3/x.jpg'),
    ('userinfo', 'https://user:pw@evil.example/x.jpg'),
    ('bad-port', 'https://evil.example:8080/x.jpg'),
    ('insecure', 'http://pub.example/x.jpg'),
    ('localhost', 'https://localhost/x.jpg'),
])
def test_thumb_rejects_redirect_private_or_insecure_targets(thumb_env, client, name, target):
    """硬性非法目标：502、不发第二次请求、计入 rejected、**不**写发现表。"""
    url = f'https://i.pximg.net/c/250x250/img/{name}.jpg'
    thumb_env['registry'].redirect(url, target)

    resp = client.get(f'/thumb/{_b64(url)}')

    assert resp.status_code == 502
    assert [c['url'] for c in thumb_env['registry'].calls] == [url], '非法目标不得发起请求'
    snapshot = runtime.thumb_redirect_snapshot()
    assert snapshot['discovered'] == [], '被拒目标绝不能进发现表'
    assert sum(snapshot['rejected'].values()) == 1


def test_thumb_rejects_cross_host_non_image_content_type(thumb_env, client):
    """B 级目标必须是图片：返回 text/html 时拒绝落盘（防第三方内容以本站缓存形式落地）。"""
    thumb_env['registry'].redirect(THUMB, CROSS_TARGET)
    thumb_env['registry'].add(CROSS_TARGET, headers={'Content-Type': 'text/html; charset=utf-8'})

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 502
    snapshot = runtime.thumb_redirect_snapshot()
    assert snapshot['discovered'] == []
    assert _rejected(CROSS_HOST) == 1


def test_thumb_redirect_initial_url_whitelist_unchanged(thumb_env, client):
    """自动发现只作用于重定向目标，入口白名单不放松。"""
    resp = client.get(f'/thumb/{_b64("https://evil.example/x.jpg")}')

    assert resp.status_code == 403
    assert thumb_env['registry'].calls == []


def test_thumb_redirect_no_recursive_follow(thumb_env, client):
    """只跟随一次：跟随后仍是 3xx → 502，不再请求第三个地址。"""
    second = 'https://i.pximg.net/img-original/img/second.jpg'
    third = 'https://i.pximg.net/img-original/img/third.jpg'
    thumb_env['registry'].redirect(THUMB, second)
    thumb_env['registry'].redirect(second, third)
    thumb_env['registry'].add(third)

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 502
    assert [c['url'] for c in thumb_env['registry'].calls] == [THUMB, second]


def test_thumb_redirect_missing_location_is_rejected(thumb_env, client):
    """3xx 但没有 Location：视为异常重定向，直接失败。"""
    thumb_env['registry'].add(THUMB, status_code=302, headers={'Content-Type': 'text/html'})

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 502
    assert [c['url'] for c in thumb_env['registry'].calls] == [THUMB]


def test_thumb_redirect_discovery_disabled_never_follows_cross_host(thumb_env, client,
                                                                   monkeypatch):
    """`THUMB_REDIRECT_DISCOVERY=False` → 回到"只允许白名单内重定向"的纯拒绝行为。"""
    monkeypatch.setattr(routes_gallery, 'THUMB_REDIRECT_DISCOVERY', False)
    thumb_env['registry'].redirect(THUMB, CROSS_TARGET)
    thumb_env['registry'].add(CROSS_TARGET)

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 502
    assert [c['url'] for c in thumb_env['registry'].calls] == [THUMB]
    snapshot = runtime.thumb_redirect_snapshot()
    assert snapshot['discovered'] == []
    assert _rejected(CROSS_HOST) == 1


# ── 观测与人工控制 ──

def test_redirect_hosts_discovered_persisted_and_reset(thumb_env, client):
    """发现表：落盘 → 重启可恢复；GET 可查；DELETE 需 CSRF 且清空内存与磁盘。"""
    thumb_env['registry'].redirect(THUMB, CROSS_TARGET)
    thumb_env['registry'].add(CROSS_TARGET)
    assert client.get(f'/thumb/{_b64(THUMB)}').status_code == 200

    state_file = thumb_env['state']
    assert state_file.is_file(), '发现表必须落盘（重启后仍可观测）'
    payload = json.loads(state_file.read_text(encoding='utf-8'))
    assert list(payload['hosts']) == [CROSS_HOST]

    listing = client.get('/api/thumb/redirect-hosts').get_json()
    assert listing['static'] == ['i.pximg.net']
    assert listing['discovery_enabled'] is True
    assert [e['host'] for e in listing['discovered']] == [CROSS_HOST]
    assert listing['rejected'] == {}

    # 模拟重启：清空内存后从磁盘恢复
    with runtime._thumb_redirect_lock:
        runtime._thumb_redirect_hosts.clear()
    assert runtime.load_thumb_redirect_hosts() == 1
    assert runtime.thumb_redirect_snapshot()['discovered'][0]['count'] == 1

    # DELETE 需要 CSRF
    assert client.delete('/api/thumb/redirect-hosts').status_code == 403
    token = client.get('/csrf-token').get_json()['token']
    resp = client.delete('/api/thumb/redirect-hosts', headers={'X-CSRF-Token': token})
    assert resp.status_code == 200
    assert runtime.thumb_redirect_snapshot()['discovered'] == []
    assert json.loads(state_file.read_text(encoding='utf-8'))['hosts'] == {}


def test_redirect_state_missing_or_corrupt_file_is_tolerated(thumb_env):
    """文件缺失/损坏都不能影响启动（观测数据而已）。"""
    assert runtime.load_thumb_redirect_hosts() == 0        # 缺失
    thumb_env['state'].write_text('{ not json', encoding='utf-8')
    assert runtime.load_thumb_redirect_hosts() == 0        # 损坏
    assert runtime.thumb_redirect_snapshot()['discovered'] == []


def test_rejected_counter_counts_each_attempt(thumb_env, client):
    """拒绝计数按目标主机累加（便于发现有人在扫内网）。"""
    for index, target in enumerate(('https://10.0.0.1/a.jpg', 'https://10.0.0.1/b.jpg')):
        url = f'https://i.pximg.net/c/250x250/img/{index}.jpg'
        thumb_env['registry'].redirect(url, target)
        assert client.get(f'/thumb/{_b64(url)}').status_code == 502

    assert _rejected('10.0.0.1') == 2
