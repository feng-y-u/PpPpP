import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

import app
import background
import helpers
from models import SearchCache, Illust, safe_commit


def _get_token(client):
    return client.get('/csrf-token').get_json()['token']


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """将 settings.json 写入重定向到临时文件，避免污染生产 instance/settings.json。"""
    settings_path = tmp_path / 'settings.json'
    monkeypatch.setattr(app, '_SETTINGS_PATH', str(settings_path))
    return settings_path


@pytest.fixture(autouse=True)
def _restore_prefetch_state():
    original = dict(app._prefetch_state)
    yield
    app._prefetch_state.clear()
    app._prefetch_state.update(original)


def _mutate_without_csrf(client, method, path, payload=None):
    """不带 `X-CSRF-Token` 发一次修改型请求（CSRF 矩阵专用）。"""
    return client.open(path, method=method,
                       data=json.dumps(payload) if payload is not None else None,
                       content_type='application/json')


def test_all_mutating_endpoints_reject_missing_csrf(client):
    """预取管理 API 的**全量修改型端点**都必须拒收缺 CSRF 的请求。

    六个端点逐个列出、各自一条断言（而不是写成一处 for 循环）：循环能把 6 条压成 1 条，
    但 `Select-String` 数的是断言行数，压掉之后就再没人看得出"哪些端点被钉住了" ——
    新增修改型端点时必须显式补一行。
    """
    resp = _mutate_without_csrf(client, 'POST', '/api/prefetch/config', {'interval': 1})
    assert resp.status_code == 403
    assert resp.get_json()['error'] == 'CSRF校验失败'
    assert _mutate_without_csrf(
        client, 'POST', '/api/prefetch/tags', {'tag': 'x'}).status_code == 403
    assert _mutate_without_csrf(
        client, 'DELETE', '/api/prefetch/tags/x').status_code == 403
    assert _mutate_without_csrf(
        client, 'POST', '/api/prefetch/refresh', {'tag': 'x'}).status_code == 403
    assert _mutate_without_csrf(
        client, 'POST', '/api/prefetch/refresh-reset', {'pixiv_id': 1}).status_code == 403
    assert _mutate_without_csrf(
        client, 'POST', '/api/cache/items/6001/delete').status_code == 403


class TestPrefetchConfigAPI:
    def test_get_returns_defaults(self, client):
        resp = client.get('/api/prefetch/config')
        assert resp.status_code == 200
        data = resp.get_json()
        assert set(data) == {'interval', 'pages', 'max_illusts'}
        assert data == {
            'interval': app._prefetch_state['interval'],
            'pages': app._prefetch_state['pages'],
            'max_illusts': app._prefetch_state['max_illusts'],
        }

    def test_post_updates_state_and_persists_atomically(self, client, _isolate_settings):
        """一次成功写入要同时满足三件事：响应回显、内存态更新、settings.json 落盘。

        落盘还必须**原子**（审计 S16）：直接 open('w') 会在写盘失败/进程被杀时留下
        截断的半份文件，读取侧遇到损坏只能整体回退默认；实例目录里也**不得**残留
        `.tmp` 半成品（读侧可能正好读到它）。覆盖写与首次写是同一条路径，一并验。
        """
        token = _get_token(client)
        resp = client.post('/api/prefetch/config',
                           data=json.dumps({'interval': 123, 'pages': 5, 'max_illusts': 42}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['interval'] == 123
        assert data['pages'] == 5
        assert data['max_illusts'] == 42
        assert app._prefetch_state['interval'] == 123
        assert app._prefetch_state['pages'] == 5
        assert app._prefetch_state['max_illusts'] == 42

        with open(_isolate_settings, encoding='utf-8') as f:
            saved = json.load(f)
        assert saved['prefetch_interval'] == 123
        assert saved['prefetch_pages'] == 5
        assert saved['prefetch_max_illusts'] == 42

        # 再覆盖写一次（只带一个字段）：落盘内容随之更新，目录里仍只剩 settings.json
        again = client.post('/api/prefetch/config',
                            data=json.dumps({'interval': 55}),
                            content_type='application/json',
                            headers={'X-CSRF-Token': token})
        assert again.status_code == 200
        assert json.loads(_isolate_settings.read_text(encoding='utf-8'))['prefetch_interval'] == 55
        assert [p.name for p in _isolate_settings.parent.iterdir()] == ['settings.json']

    def test_post_write_failure_keeps_old_file_and_state(self, client, _isolate_settings,
                                                         monkeypatch):
        """替换失败 → 500，旧文件不变、不留 tmp，且**内存态不得漂移**。

        「先写盘成功再更新内存」是这个接口的既有约定：写盘失败却更新了内存，会导致
        运行参数与盘上配置不一致，重启后行为突变。
        """
        original = json.dumps({'prefetch_interval': 111}, ensure_ascii=False)
        _isolate_settings.write_text(original, encoding='utf-8')
        before = dict(app._prefetch_state)
        monkeypatch.setattr(helpers.os, 'replace',
                            lambda *a, **kw: (_ for _ in ()).throw(OSError('模拟写盘失败')))
        token = _get_token(client)

        resp = client.post('/api/prefetch/config',
                           data=json.dumps({'interval': 999}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})

        assert resp.status_code == 500
        assert _isolate_settings.read_text(encoding='utf-8') == original
        assert [p.name for p in _isolate_settings.parent.iterdir()] == ['settings.json']
        assert dict(app._prefetch_state) == before, '写盘失败不得更新内存态'

    def test_post_single_field_update_and_clamping(self, client):
        """只传一个字段时：该字段生效、其余字段保持原值；负数一律夹到 0。"""
        token = _get_token(client)
        negative = client.post('/api/prefetch/config',
                               data=json.dumps({'interval': -5}),
                               content_type='application/json',
                               headers={'X-CSRF-Token': token})
        assert negative.status_code == 200
        assert negative.get_json()['interval'] == 0

        client.post('/api/prefetch/config',
                    data=json.dumps({'pages': 7}),
                    content_type='application/json',
                    headers={'X-CSRF-Token': token})
        resp = client.get('/api/prefetch/config')
        assert resp.get_json()['pages'] == 7
        assert resp.get_json()['interval'] == app._prefetch_state['interval']

    def test_post_invalid_payload_400_leaves_state_untouched(self, client, _isolate_settings):
        """非法字段一律 400 + 报错，且**校验先于写入**。

        同一个请求里既有合法字段又有非法字段时，合法那个也不得先进内存/磁盘 ——
        否则用户看到"保存失败"，参数却已经变了。
        """
        token = _get_token(client)
        before = dict(app._prefetch_state)

        bad_type = client.post('/api/prefetch/config',
                               data=json.dumps({'interval': 'abc'}),
                               content_type='application/json',
                               headers={'X-CSRF-Token': token})
        assert bad_type.status_code == 400
        assert 'error' in bad_type.get_json()

        partial_invalid = client.post('/api/prefetch/config',
                                      data=json.dumps({'interval': 5, 'pages': 'abc'}),
                                      content_type='application/json',
                                      headers={'X-CSRF-Token': token})
        assert partial_invalid.status_code == 400
        assert 'error' in partial_invalid.get_json()
        assert app._prefetch_state['interval'] == before['interval']
        assert app._prefetch_state['pages'] == before['pages']
        assert app._prefetch_state['max_illusts'] == before['max_illusts']
        # settings.json 未被写入（文件不应存在）
        assert not os.path.exists(_isolate_settings)


class TestPrefetchTagsAPI:
    def test_post_adds_tag(self, client, clean_db):
        token = _get_token(client)
        resp = client.post('/api/prefetch/tags',
                           data=json.dumps({'tag': '初音ミク'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 201
        assert resp.get_json()['tag'] == '初音ミク'
        assert clean_db.query(SearchCache).filter(SearchCache.tag == '初音ミク').first() is not None

    def test_post_empty_tag_400(self, client, clean_db):
        token = _get_token(client)
        resp = client.post('/api/prefetch/tags',
                           data=json.dumps({'tag': '   '}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 400

    def test_post_duplicate_409(self, client, clean_db):
        clean_db.add(SearchCache(tag='dup'))
        safe_commit(clean_db)
        token = _get_token(client)
        resp = client.post('/api/prefetch/tags',
                           data=json.dumps({'tag': 'dup'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 409
        assert resp.get_json()['error'] == '标签已存在'

    def test_get_lists_tags_newest_first(self, client, clean_db):
        clean_db.add_all([
            SearchCache(tag='a', status='done', total=5, error='',
                        cached_at=datetime(2025, 1, 2, tzinfo=timezone.utc)),
            SearchCache(tag='b', status='idle', total=0, error=''),
            SearchCache(tag='c', status='error', total=0, error='boom',
                        cached_at=datetime(2025, 1, 3, tzinfo=timezone.utc)),
        ])
        safe_commit(clean_db)

        resp = client.get('/api/prefetch/tags')
        assert resp.status_code == 200
        data = resp.get_json()
        # cached_at DESC：c 最新；b 从未抓取（NULL）排最后
        assert [d['tag'] for d in data] == ['c', 'a', 'b']
        assert data[0]['total'] == 0
        assert data[0]['status'] == 'error'
        assert data[0]['cached_at'] == '2025-01-03T00:00:00'
        assert data[2]['cached_at'] is None

    def test_delete_removes_tag(self, client, clean_db):
        clean_db.add(SearchCache(tag='gone', illust_ids='[]'))
        safe_commit(clean_db)
        token = _get_token(client)
        resp = client.delete('/api/prefetch/tags/gone', headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json()['tag'] == 'gone'
        assert clean_db.query(SearchCache).filter(SearchCache.tag == 'gone').first() is None

    def test_delete_missing_404(self, client, clean_db):
        token = _get_token(client)
        resp = client.delete('/api/prefetch/tags/nope', headers={'X-CSRF-Token': token})
        assert resp.status_code == 404
        assert resp.get_json()['error'] == '标签不存在'

    def test_delete_tag_prunes_only_unreferenced_deletable_prefetch_illusts(
            self, client, clean_db):
        """删标签时的作品清理规则（同一套准备 × 三种作品，故合为一条）。

        只删"只被这个标签引用 + 是预取来源 + 没被下载/正在下载"的作品：
        - 仍被其他 SearchCache 引用的保留（删了会让别的标签列表指向空行）；
        - 已下载的保留（用户的资产，不是缓存垃圾）；
        - 正在下载的保留（删行会让在途下载静默消失，审计 S3）；
        - 非预取来源（`prefetch_source=0`）根本不在清理范围里。
        """
        token = _get_token(client)

        # ① 未被引用且可删 → 删；被 other 引用 / 已下载 → 留
        clean_db.add_all([
            SearchCache(tag='t', illust_ids='[1, 2, 3]'),
            SearchCache(tag='other', illust_ids='[2]'),
            Illust(pixiv_id=1, title='free', prefetch_source=1),
            Illust(pixiv_id=2, title='referenced', prefetch_source=1),
            Illust(pixiv_id=3, title='downloaded', prefetch_source=1, download_status='done'),
        ])
        safe_commit(clean_db)
        resp = client.delete('/api/prefetch/tags/t', headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2, 3}, f'只该删掉未被引用的 pid1，实际剩下 {remaining}'

        # ② 正在下载的也不能删（worker 还没把状态写成 done）
        clean_db.add_all([
            SearchCache(tag='t', illust_ids='[5, 6]'),
            Illust(pixiv_id=5, title='downloading', prefetch_source=1,
                   download_status='downloading'),
            Illust(pixiv_id=6, title='plain', prefetch_source=1),
        ])
        safe_commit(clean_db)
        resp = client.delete('/api/prefetch/tags/t', headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2, 3, 5}, f'下载中的 pid5 必须保留，实际剩下 {remaining}'

        # ③ 非预取来源的作品一律不动
        clean_db.add_all([
            SearchCache(tag='t', illust_ids='[7]'),
            Illust(pixiv_id=7, title='normal', prefetch_source=0),
        ])
        safe_commit(clean_db)
        resp = client.delete('/api/prefetch/tags/t', headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 7).first() is not None


class TestPrefetchStatusAPI:
    def test_status_returns_fields_and_health_counts(self, client, clean_db):
        now = datetime.now(timezone.utc)
        clean_db.add_all([
            Illust(pixiv_id=9001, title='a', prefetch_source=1),              # 未刷新
            Illust(pixiv_id=9002, title='b', prefetch_source=1,
                   prefetch_refresh_at=now),                                  # 已刷新
            Illust(pixiv_id=9003, title='c', prefetch_source=1,
                   refresh_failed_at=now),                                    # 退避中
            Illust(pixiv_id=9004, title='d'),                                 # 非预取来源
        ])
        safe_commit(clean_db)

        resp = client.get('/api/prefetch/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert set(data) == {'running', 'last_check', 'interval', 'refresh',
                             'pending_refresh', 'failed_backoff', 'detail_errors',
                             'alive', 'auto_follow_alive', 'stale', 'last_error'}
        assert data['running'] == app._prefetch_state['running']
        assert data['last_check'] == app._prefetch_state['last_check']
        assert data['interval'] == app._prefetch_state['interval']
        assert data['pending_refresh'] == 2   # 9001 未刷新 + 9003 退避中
        assert data['failed_backoff'] == 1    # 9003
        assert data['refresh'] == app._prefetch_state['refresh_stats']
        assert isinstance(data['detail_errors'], dict)
        assert isinstance(data['alive'], bool)
        assert isinstance(data['auto_follow_alive'], bool)
        assert isinstance(data['stale'], bool)
        assert data['last_error'] == app._prefetch_state['last_error']

    def test_status_reports_alive_and_stale(self, client, monkeypatch):
        """存活/陈旧/最近错误都要能直接读出来（审计 S12）。

        线程活着不代表在干活：线程静默死掉时数据只是越来越旧，所以把"线程还在不在"
        （alive）与"多久没跑完一轮"（stale）分开报。末尾一并覆盖"预取被显式关掉
        （interval=0）"这一合法配置：从没跑过也不算 stale。
        """
        class _ThreadStub:
            def __init__(self, alive):
                self._alive = alive

            def is_alive(self):
                return self._alive

        interval = 3600
        monkeypatch.setattr(background, '_prefetch_thread', _ThreadStub(True))
        monkeypatch.setitem(app._prefetch_state, 'interval', interval)
        monkeypatch.setitem(app._prefetch_state, 'last_error', '标签预取异常: boom')

        # ① 超过 2*interval+600 没跑完一轮 → stale
        old = datetime.now(timezone.utc) - timedelta(seconds=2 * interval + 601)
        monkeypatch.setitem(app._prefetch_state, 'last_check', old.isoformat())
        data = client.get('/api/prefetch/status').get_json()
        assert data['alive'] is True
        assert data['stale'] is True
        assert data['last_error'] == '标签预取异常: boom'

        # ② 阈值内（含"正好一轮刚跑完"）→ 不算 stale
        fresh = datetime.now(timezone.utc) - timedelta(seconds=2 * interval + 599)
        monkeypatch.setitem(app._prefetch_state, 'last_check', fresh.isoformat())
        assert client.get('/api/prefetch/status').get_json()['stale'] is False
        monkeypatch.setitem(app._prefetch_state, 'last_check',
                            datetime.now(timezone.utc).isoformat())
        assert client.get('/api/prefetch/status').get_json()['stale'] is False

        # ③ 线程死了但数据很新 → 只有 alive 能暴露它
        monkeypatch.setattr(background, '_prefetch_thread', _ThreadStub(False))
        data = client.get('/api/prefetch/status').get_json()
        assert data['alive'] is False and data['stale'] is False

        # ④ interval>0 但从没跑完过一轮 → stale（线程起来了但一直没成功）
        monkeypatch.setitem(app._prefetch_state, 'last_check', None)
        assert client.get('/api/prefetch/status').get_json()['stale'] is True

        # ⑤ interval=0（预取被显式关掉）是合法配置：从没跑过也不算 stale
        monkeypatch.setattr(background, '_prefetch_thread', None)
        monkeypatch.setitem(app._prefetch_state, 'interval', 0)
        monkeypatch.setitem(app._prefetch_state, 'last_check', None)
        data = client.get('/api/prefetch/status').get_json()
        assert data['stale'] is False
        assert data['alive'] is False


class TestPrefetchRefreshResetAPI:
    def _setup(self, clean_db):
        now = datetime.now(timezone.utc)
        clean_db.add_all([
            SearchCache(tag='t', illust_ids='[9101, 9102]', status='done'),
            Illust(pixiv_id=9101, title='a', prefetch_source=1,
                   prefetch_refresh_at=now),          # 已刷新（含 force-done）
            Illust(pixiv_id=9102, title='b', prefetch_source=1,
                   refresh_failed_at=now),            # 退避中
            Illust(pixiv_id=9103, title='c', prefetch_source=1,
                   refresh_failed_at=now),            # 不在该标签列表
        ])
        safe_commit(clean_db)

    def _rewrite_markers(self, clean_db):
        """把标记写回 `_setup` 的初始状态（第二段从同一套初始值重新开始）。"""
        now = datetime.now(timezone.utc)
        for pid, refresh_at, failed_at in ((9101, now, None), (9102, None, now),
                                          (9103, None, now)):
            row = clean_db.query(Illust).filter(Illust.pixiv_id == pid).first()
            row.prefetch_refresh_at = refresh_at
            row.refresh_failed_at = failed_at
        safe_commit(clean_db)

    def test_reset_clears_only_the_selected_scope(self, client, clean_db):
        """两种范围（整个标签 / 单个 pid）都只清范围内的标记，范围外一律不动。

        合并成一条的理由：两段共用同一套 `_setup` 数据，断言同构（范围内清空、
        范围外原样），只有"范围怎么指定"不同。范围外不动这条不能丢 —— 否则
        "救回一个作品"会顺手把整库的刷新队列重置掉。
        """
        self._setup(clean_db)
        token = _get_token(client)

        # ① 按标签：清该标签列表里的全部作品
        by_tag = client.post('/api/prefetch/refresh-reset',
                             data=json.dumps({'tag': 't'}),
                             content_type='application/json',
                             headers={'X-CSRF-Token': token})
        assert by_tag.status_code == 200
        assert by_tag.get_json() == {'status': 'reset', 'count': 2}
        clean_db.expire_all()
        a = clean_db.query(Illust).filter(Illust.pixiv_id == 9101).first()
        b = clean_db.query(Illust).filter(Illust.pixiv_id == 9102).first()
        c = clean_db.query(Illust).filter(Illust.pixiv_id == 9103).first()
        assert a.prefetch_refresh_at is None and a.refresh_failed_at is None
        assert b.prefetch_refresh_at is None and b.refresh_failed_at is None
        assert c.refresh_failed_at is not None, '不在该标签列表里的作品不动'

        # ② 按 pixiv_id：只清指定那一个（标记先写回去）
        self._rewrite_markers(clean_db)
        by_pid = client.post('/api/prefetch/refresh-reset',
                             data=json.dumps({'pixiv_id': 9102}),
                             content_type='application/json',
                             headers={'X-CSRF-Token': token})
        assert by_pid.status_code == 200
        assert by_pid.get_json()['count'] == 1
        clean_db.expire_all()
        b = clean_db.query(Illust).filter(Illust.pixiv_id == 9102).first()
        assert b.refresh_failed_at is None
        a = clean_db.query(Illust).filter(Illust.pixiv_id == 9101).first()
        assert a.prefetch_refresh_at is not None, '没指定范围的作品不动'

    def test_reset_without_scope_400(self, client, clean_db):
        token = _get_token(client)
        resp = client.post('/api/prefetch/refresh-reset',
                           data=json.dumps({}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 400

    def test_reset_missing_tag_404(self, client, clean_db):
        token = _get_token(client)
        resp = client.post('/api/prefetch/refresh-reset',
                           data=json.dumps({'tag': 'nope'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 404


class TestPrefetchRefreshAPI:
    def test_refresh_done_tag_spawns_thread(self, client, clean_db, monkeypatch):
        clean_db.add(SearchCache(tag='t', status='done', illust_ids='[]'))
        safe_commit(clean_db)
        called = []
        monkeypatch.setattr(app, '_prefetch_one_tag', lambda tag: called.append(tag))

        token = _get_token(client)
        resp = client.post('/api/prefetch/refresh',
                           data=json.dumps({'tag': 't'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json() == {'tag': 't', 'status': 'refreshing'}
        time.sleep(0.1)  # 后台线程异步执行
        assert called == ['t']

    def test_refresh_rejects_unusable_targets(self, client, clean_db):
        """不可刷新的目标各自的错误码与文案（同一入口的状态/参数矩阵）：

        - 正在刷新中 → 409（重复点不该排队第二次）；
        - 标签不存在 → 404；
        - 空标签 → 400。
        "正在刷新"与"标签不存在"必须能分辨，否则用户只会看到"没反应"。
        """
        clean_db.add(SearchCache(tag='busy', status='fetching'))
        safe_commit(clean_db)
        token = _get_token(client)

        busy = client.post('/api/prefetch/refresh', data=json.dumps({'tag': 'busy'}),
                           content_type='application/json',
                           headers={'X-CSRF-Token': token})
        assert busy.status_code == 409
        assert busy.get_json()['error'] == '该标签正在刷新中'

        missing = client.post('/api/prefetch/refresh', data=json.dumps({'tag': 'nope'}),
                              content_type='application/json',
                              headers={'X-CSRF-Token': token})
        assert missing.status_code == 404
        assert missing.get_json()['error'] == '标签不存在'

        empty = client.post('/api/prefetch/refresh', data=json.dumps({'tag': '   '}),
                            content_type='application/json',
                            headers={'X-CSRF-Token': token})
        assert empty.status_code == 400


class TestCacheItemDelete:
    def _setup(self, clean_db, pid=6001, **illust_kw):
        clean_db.add_all([
            Illust(pixiv_id=pid, title='x', prefetch_source=1, **illust_kw),
            Illust(pixiv_id=6002, title='y', prefetch_source=1),
            SearchCache(tag='t', illust_ids=json.dumps([pid, 6002]), status='done'),
        ])
        safe_commit(clean_db)

    def test_delete_removes_illust_and_cache_ref(self, client, clean_db):
        self._setup(clean_db)
        token = _get_token(client)
        resp = client.post('/api/cache/items/6001/delete',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'deleted'
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 6001).first() is None
        sc = clean_db.query(SearchCache).filter(SearchCache.tag == 't').first()
        assert json.loads(sc.illust_ids) == [6002]
        # 其他作品不受影响
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 6002).first() is not None

    def test_delete_downloaded_rejected(self, client, clean_db):
        self._setup(clean_db, download_status='done')
        token = _get_token(client)
        resp = client.post('/api/cache/items/6001/delete',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 400
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 6001).first() is not None

    def test_delete_non_prefetch_404(self, client, clean_db):
        clean_db.add(Illust(pixiv_id=6003, title='z', prefetch_source=0))
        safe_commit(clean_db)
        token = _get_token(client)
        resp = client.post('/api/cache/items/6003/delete',
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 404
