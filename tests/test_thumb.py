"""`/thumb` 越界重定向：拒绝 + 自动发现（审计 S7b）。

背景：`fetcher.build_pixiv_session()` 挂的是**会话级** Cookie 头，requests 会把它
发给任意主机 —— 所以"跟随到白名单外主机"等价于把 PHPSESSID 交给第三方，重定向
目标必须按凭据分级；同时 SSRF 真正危险的目标（内网、云元数据端点）必须硬性封死。
"""
import base64
import json
import os
import threading
import time

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


# ── 并发槽位纪律（审计 S11）──

class _NoSlotSemaphore:
    """永不给名额的信号量替身。

    `acquire` 强制要求 timeout：旧实现用 `with _thumb_sem`，在这里会直接抛断言
    而不是把整个用例挂死（挂死的话失败信息只有"超时"，看不出根因）。`__enter__`
    同样拒绝 —— 那正是"没有等待上限"的写法。
    """

    def __init__(self):
        self.acquire_timeouts: list = []

    def acquire(self, blocking=True, timeout=None):
        assert timeout is not None, 'acquire 必须带 timeout：无条件阻塞会把整页缩略图挂死'
        self.acquire_timeouts.append(timeout)
        return False

    def release(self):
        raise AssertionError('没拿到名额就不该调用 release')

    def __enter__(self):
        raise AssertionError('不得用 with 获取信号量（没有等待上限）')

    def __exit__(self, *exc):
        return False


def test_thumb_returns_503_when_semaphore_exhausted(thumb_env, client, monkeypatch):
    """等不到槽位就快速失败：503、不发起取图请求、等待上限取自 runtime 常量。"""
    thumb_env['registry'].add(THUMB)
    sem = _NoSlotSemaphore()
    monkeypatch.setattr(routes_gallery, '_thumb_sem', sem)
    # raising=False：常量不存在时也要走到"真的发一次请求"再失败（否则证伪跑只会在
    # 夹具接线处报错，看不出是行为差异）
    monkeypatch.setattr(runtime, 'THUMB_SEM_TIMEOUT', 0.25, raising=False)

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 503
    assert sem.acquire_timeouts == [0.25], '等待上限必须取自 runtime.THUMB_SEM_TIMEOUT'
    assert thumb_env['registry'].calls == [], '没拿到名额就不该发起取图请求'
    assert runtime.thumb_redirect_snapshot()['rejected'] == {}, '槽位超时不是重定向拒绝'


def test_thumb_releases_semaphore_after_success_and_failure(thumb_env, client, monkeypatch):
    """成功与失败两条路径都必须归还槽位（否则名额会一张张漏光）。"""
    sem = threading.Semaphore(1)
    monkeypatch.setattr(routes_gallery, '_thumb_sem', sem)

    ok_url = f'{THUMB}?release=ok'
    thumb_env['registry'].add(ok_url)
    assert client.get(f'/thumb/{_b64(ok_url)}').status_code == 200

    bad_url = f'{THUMB}?release=bad'      # registry 里没有 → 连接错误 → 502
    assert client.get(f'/thumb/{_b64(bad_url)}').status_code == 502

    assert sem.acquire(blocking=False) is True, '两条路径都该归还槽位'
    sem.release()


def test_thumb_concurrent_requests_never_exceed_semaphore(thumb_env, app, monkeypatch):
    """并发取图不超过槽位数，且结束后名额全部归还（finally 释放的竞态防线）。"""
    limit = 2
    urls = [f'{THUMB}?conc={i}' for i in range(6)]
    sem = threading.Semaphore(limit)
    monkeypatch.setattr(routes_gallery, '_thumb_sem', sem)
    for url in urls:
        thumb_env['registry'].add(url)

    registry = thumb_env['registry']
    gate = threading.Lock()
    state = {'in_flight': 0, 'peak': 0}

    def _instrumented_session(with_cookie: bool = True):
        session = registry.session(with_cookie)
        plain_get = session.get

        def _get(url, **kwargs):
            with gate:
                state['in_flight'] += 1
                state['peak'] = max(state['peak'], state['in_flight'])
            try:
                time.sleep(0.05)          # 放大窗口：让并发真的叠起来
                return plain_get(url, **kwargs)
            finally:
                with gate:
                    state['in_flight'] -= 1

        session.get = _get
        return session

    monkeypatch.setattr(routes_gallery, 'get_pooled_session', _instrumented_session)

    results: list[int] = []
    errors: list[BaseException] = []

    def _hit(url: str):
        try:
            results.append(app.test_client().get(f'/thumb/{_b64(url)}').status_code)
        except BaseException as e:      # noqa: BLE001 —— 汇总后统一断言
            errors.append(e)

    threads = [threading.Thread(target=_hit, args=(url,), daemon=True) for url in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert not errors, f'并发取图出错：{errors!r}'
    assert not any(t.is_alive() for t in threads)
    assert sorted(results) == [200] * len(urls)
    assert state['peak'] <= limit, f'并发峰值 {state["peak"]} 超过槽位数 {limit}'
    for _ in range(limit):                # 名额必须全部归还
        assert sem.acquire(blocking=False) is True, '有槽位没被归还'
    for _ in range(limit):
        sem.release()


# ── 磁盘缓存、失败冷却、原子写降级（审计 S18 补齐）──

def _cache_paths(url: str) -> tuple[str, str]:
    """复现路由推导缓存文件名的规则（md5(url) + 扩展名 + 同名 .meta）。"""
    import hashlib
    key = hashlib.md5(url.encode()).hexdigest()
    cache_path = os.path.join(routes_gallery.CACHE_DIR, f'{key}.{routes_gallery._extract_ext(url)}')
    return cache_path, cache_path + '.meta'


def test_thumb_cache_hit_serves_from_disk_without_network(thumb_env, client):
    """命中磁盘缓存：不发网络请求、mtime 不变、带 7 天 max_age。

    mtime 必须不变 —— 命中时刷新 mtime 会改 ETag，让浏览器那 7 天的本地缓存整体失效
    （`image_cache` 的"最旧写入优先"淘汰也依赖 mtime 不被读操作污染）。
    """
    cache_path, meta_path = _cache_paths(THUMB)
    with open(cache_path, 'wb') as f:
        f.write(b'cached-jpeg-bytes')
    with open(meta_path, 'w') as f:
        f.write('image/png')
    before = os.stat(cache_path).st_mtime_ns

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 200
    assert resp.data == b'cached-jpeg-bytes'
    assert resp.mimetype == 'image/png', '.meta 里的原始 Content-Type 必须回放'
    assert 'max-age=604800' in resp.headers['Cache-Control']
    assert thumb_env['registry'].calls == [], '命中缓存不得再打图床'
    assert os.stat(cache_path).st_mtime_ns == before, '读缓存不得改 mtime'


def test_thumb_cache_hit_without_meta_assumes_jpeg(thumb_env, client):
    """`.meta` 缺失（旧版本缓存）时按 jpeg 回放，不能因此 500。"""
    cache_path, _ = _cache_paths(THUMB)
    with open(cache_path, 'wb') as f:
        f.write(b'no-meta')

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 200
    assert resp.mimetype == 'image/jpeg'
    assert thumb_env['registry'].calls == []


def test_thumb_failure_cooldown_skips_network_until_expiry(thumb_env, client):
    """失败 URL 在冷却期内直接 502 且不再发请求；冷却过期后恢复尝试。

    这是防"图库刷新时同一批坏图反复打满图床 → 整批 502 → 前端全消失"的关键。
    注意断言的是**请求数不再增长**而不是"总共 1 次"：`_thumb_request` 对非超时的连接
    失败会重建连接池重试一次（keep-alive 被对端关掉的快失败场景），所以首次失败本身
    就是 2 次出站调用。
    """
    url = f'{THUMB}?cooldown=1'
    # registry 里没有该 URL → 替身 session 抛 ConnectionError → 502 并记冷却
    first = client.get(f'/thumb/{_b64(url)}')

    assert first.status_code == 502
    calls_after_first = len(thumb_env['registry'].calls)
    assert calls_after_first >= 1
    assert runtime._thumb_failed[url] > 0

    second = client.get(f'/thumb/{_b64(url)}')

    assert second.status_code == 502
    assert len(thumb_env['registry'].calls) == calls_after_first, '冷却期内不得再发起请求'

    runtime._thumb_failed[url] = time.time() - runtime._THUMB_FAIL_COOLDOWN - 1   # 冷却过期
    third = client.get(f'/thumb/{_b64(url)}')

    assert third.status_code == 502
    assert len(thumb_env['registry'].calls) > calls_after_first, '冷却过期后必须重新尝试'


def test_thumb_success_clears_failure_cooldown(thumb_env, client):
    """取图成功后要清掉冷却记录：否则一次抖动会让这张图 30 秒内都用不了。"""
    url = f'{THUMB}?recover=1'
    assert client.get(f'/thumb/{_b64(url)}').status_code == 502
    assert url in runtime._thumb_failed

    thumb_env['registry'].add(url)
    assert client.get(f'/thumb/{_b64(url)}').status_code == 502, '仍在冷却期内'

    runtime._thumb_failed.pop(url, None)          # 模拟冷却到期后的成功
    assert client.get(f'/thumb/{_b64(url)}').status_code == 200
    assert url not in runtime._thumb_failed


def test_thumb_atomic_write_failure_streams_response_without_caching(thumb_env, client,
                                                                    monkeypatch):
    """原子写失败（磁盘满/权限）时要降级为直接转发响应，且不留半份缓存与 .tmp。

    降级的意义：图还是得给用户看，缓存写不进去不该变成 502；但**绝不能**留下一个
    截断的缓存文件 —— 下次命中会拿它当完整图片返回。
    """
    thumb_env['registry'].add(THUMB)

    def _boom(src, dst, *a, **kw):
        raise OSError('模拟 os.replace 失败')

    monkeypatch.setattr(routes_gallery.os, 'replace', _boom)

    resp = client.get(f'/thumb/{_b64(THUMB)}')

    assert resp.status_code == 200
    assert resp.data == IMAGE_BYTES, '降级路径必须把原图完整给出去'
    assert os.listdir(routes_gallery.CACHE_DIR) == [], '不得留下缓存或 .tmp 残留'


# ── /api/image 三分支（审计 S18 补齐）──

def _make_done_illust(pixiv_id: int, paths: list[str]):
    from models import Illust, get_session, safe_commit
    with get_session() as db:
        db.add(Illust(pixiv_id=pixiv_id, title='t', download_status='done',
                      local_paths_list=paths))
        safe_commit(db)
    return paths


def test_api_image_serves_db_path(client, clean_db, tmp_path, monkeypatch):
    """DB 命中且文件在盘上：直接回文件（带 7 天 max_age，灯箱不再每张发 304）。"""
    img = tmp_path / 'page1.jpg'
    img.write_bytes(b'db-jpeg')
    _make_done_illust(70001, [str(img)])

    resp = client.get('/api/image/70001/0')

    assert resp.status_code == 200
    assert resp.data == b'db-jpeg'
    assert 'max-age=604800' in resp.headers['Cache-Control']


def test_api_image_falls_back_to_download_dir(client, clean_db, tmp_path, monkeypatch):
    """DB 无记录（或状态不对）时从 downloads/<pid>/ 兜底，按页号排序取第 index 张。"""
    import helpers

    ddir = tmp_path / 'downloads' / '70002'
    ddir.mkdir(parents=True)
    (ddir / '70002_p1.jpg').write_bytes(b'page-one')
    (ddir / '70002_p2.jpg').write_bytes(b'page-two')
    (ddir / '70002_p10.jpg').write_bytes(b'page-ten')
    monkeypatch.setattr(helpers, 'DOWNLOAD_DIR', str(tmp_path / 'downloads'))

    first = client.get('/api/image/70002/0')
    third = client.get('/api/image/70002/2')

    assert first.status_code == 200 and first.data == b'page-one'
    # 页号排序：_p10 必须排在 _p2 之后（字典序会排错）
    assert third.status_code == 200 and third.data == b'page-ten'


def test_api_image_404_paths(client, clean_db, tmp_path, monkeypatch):
    """三分支的兜底：目录不存在、index 越界、DB 行在但文件被删 —— 一律 404。"""
    import helpers

    monkeypatch.setattr(helpers, 'DOWNLOAD_DIR', str(tmp_path / 'downloads'))
    assert client.get('/api/image/70003/0').status_code == 404, '目录不存在'

    ddir = tmp_path / 'downloads' / '70004'
    ddir.mkdir(parents=True)
    (ddir / '70004_p1.jpg').write_bytes(b'only-page')
    assert client.get('/api/image/70004/5').status_code == 404, 'index 越界'

    missing = tmp_path / 'gone.jpg'
    _make_done_illust(70005, [str(missing)])
    assert client.get('/api/image/70005/0').status_code == 404, 'DB 有记录但文件已删除'
