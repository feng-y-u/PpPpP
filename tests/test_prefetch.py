import json
import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest

import app
import background
import config
import fetcher
from models import SearchCache, Illust, Collection, CollectionItem, safe_commit


class TestPrefetchOneTag:
    def test_prefetch_one_tag_marks_source(self, clean_db, monkeypatch):
        clean_db.add_all([Illust(pixiv_id=1, title='a'), Illust(pixiv_id=2, title='b')])
        safe_commit(clean_db)

        calls = []

        def _fake_search(tag, **kwargs):
            calls.append(tag)
            if len(calls) == 1:
                return [{'pixiv_id': 1}, {'pixiv_id': 2}], True
            return [], False

        monkeypatch.setattr(app, 'search_by_tag', _fake_search)
        app._prefetch_one_tag('テスト')

        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'テスト').first()
        assert row is not None
        assert row.status == 'done'
        assert row.total == 2
        assert json.loads(row.illust_ids) == [1, 2]
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 1).first().prefetch_source == 1
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 2).first().prefetch_source == 1

    def test_prefetch_one_tag_error_sets_status(self, clean_db, monkeypatch):
        def _boom(tag, **kwargs):
            raise RuntimeError('network down')

        monkeypatch.setattr(app, 'search_by_tag', _boom)
        app._prefetch_one_tag('x')

        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'x').first()
        assert row is not None
        assert row.status == 'error'
        assert row.error

    def test_prefetch_one_tag_accumulates(self, clean_db, monkeypatch):
        # 已有缓存 [1, 2]，第二次预取抓 [2, 3, 4] → 合并去重为 [2, 3, 4, 1]
        clean_db.add(SearchCache(tag='acc', illust_ids='[1, 2]', status='done'))
        safe_commit(clean_db)

        calls = []

        def _fake_search(tag, **kwargs):
            calls.append(tag)
            if len(calls) == 1:
                return [{'pixiv_id': 2}, {'pixiv_id': 3}, {'pixiv_id': 4}], False
            return [], False

        monkeypatch.setattr(app, 'search_by_tag', _fake_search)
        app._prefetch_one_tag('acc')

        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'acc').first()
        assert row.status == 'done'
        assert json.loads(row.illust_ids) == [2, 3, 4, 1]
        assert row.total == 4

    def test_prefetch_one_tag_error_keeps_old_ids(self, clean_db, monkeypatch):
        # 预取失败时保持上次成功的 illust_ids 不变
        clean_db.add(SearchCache(tag='keep', illust_ids='[1, 2]', status='done'))
        safe_commit(clean_db)

        def _boom(tag, **kwargs):
            raise RuntimeError('network down')

        monkeypatch.setattr(app, 'search_by_tag', _boom)
        app._prefetch_one_tag('keep')

        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'keep').first()
        assert row.status == 'error'
        assert json.loads(row.illust_ids) == [1, 2]

    def test_prefetch_one_tag_skips_when_fetching(self, clean_db, monkeypatch):
        clean_db.add(SearchCache(tag='y', status='fetching'))
        safe_commit(clean_db)

        called = []

        def _fake_search(tag, **kwargs):
            called.append(tag)
            return [], False

        monkeypatch.setattr(app, 'search_by_tag', _fake_search)
        app._prefetch_one_tag('y')

        assert not called
        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'y').first()
        assert row.status == 'fetching'

    def test_prefetch_one_tag_does_not_mark_downloaded(self, clean_db, monkeypatch):
        clean_db.add(Illust(pixiv_id=42, title='dl', download_status='done'))
        safe_commit(clean_db)

        calls = []

        def _fake_search(tag, **kwargs):
            calls.append(tag)
            if len(calls) == 1:
                return [{'pixiv_id': 42}], True
            return [], False

        monkeypatch.setattr(app, 'search_by_tag', _fake_search)
        app._prefetch_one_tag('dl_tag')

        row = clean_db.query(Illust).filter(Illust.pixiv_id == 42).first()
        assert row is not None
        assert row.download_status == 'done'
        assert row.prefetch_source == 0


class TestCapacityCleanup:
    def test_capacity_cleanup_skips_user_download_logged(self, clean_db, monkeypatch):
        """用户点过下载的作品（DownloadLog）不当缓存垃圾清掉。"""
        from models import DownloadLog
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            Illust(pixiv_id=1, title='low', prefetch_source=1, bookmark_count=1,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='touched', prefetch_source=1, bookmark_count=2,
                   prefetch_refresh_at=refreshed),
            DownloadLog(pixiv_id=2, action='failed', message='下载失败待重试'),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 1)

        app._prefetch_capacity_cleanup()

        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2}  # 只有无下载日志的低收藏作品被淘汰

    def test_capacity_cleanup_keeps_queued_download(self, clean_db, monkeypatch):
        """排队中（worker 还没把状态写成 downloading）的作品不能被容量清理删掉。

        审计 S3：trigger 入队 → worker 首个 commit 之间，行状态仍是 None 且没有
        任何 DownloadLog（`_is_user_owned` 也判不出），旧实现会把它当缓存垃圾淘汰，
        worker 随后查不到行 → 下载静默消失。
        """
        import runtime
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            Illust(pixiv_id=1, title='low', prefetch_source=1, bookmark_count=10,
                   prefetch_refresh_at=refreshed),
            # 收藏数为 0：不设保护时它才是被淘汰的那个（保证用例能否证伪）
            Illust(pixiv_id=2, title='queued', prefetch_source=1, bookmark_count=0,
                   prefetch_refresh_at=refreshed),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 1)
        with runtime._download_queue_lock:
            runtime._queued_downloads.add(2)
        try:
            app._prefetch_capacity_cleanup()
        finally:
            with runtime._download_queue_lock:
                runtime._queued_downloads.discard(2)

        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2}, '排队中的下载不能被容量清理淘汰'

    def test_capacity_cleanup_ignores_prefetch_deleted_log(self, clean_db, monkeypatch):
        """缓存清理自己写的 prefetch_deleted 不算"用户拥有"，否则会永久保护。"""
        from models import DownloadLog
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            Illust(pixiv_id=1, title='low', prefetch_source=1, bookmark_count=1,
                   prefetch_refresh_at=refreshed),
            DownloadLog(pixiv_id=1, action='prefetch_deleted', message='之前被清理过'),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 0)

        app._prefetch_capacity_cleanup()

        assert clean_db.query(Illust).filter(Illust.pixiv_id == 1).first() is None

    def test_capacity_cleanup_low_bookmark_first(self, clean_db):
        # 超过上限时优先删除最终收藏数最低的预取作品，并从所有标签列表移除
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            SearchCache(tag='tag_a', illust_ids='[1, 2, 3]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            SearchCache(tag='tag_b', illust_ids='[3]',
                        cached_at=datetime(2021, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='low', prefetch_source=1, bookmark_count=10,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='high', prefetch_source=1, bookmark_count=500,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=3, title='mid', prefetch_source=1, bookmark_count=100,
                   prefetch_refresh_at=refreshed),
        ])
        safe_commit(clean_db)

        old = app._prefetch_state['max_illusts']
        try:
            app._prefetch_state['max_illusts'] = 1
            app._prefetch_capacity_cleanup()
        finally:
            app._prefetch_state['max_illusts'] = old

        # 收藏数最低的 pid1(10) 和 pid3(100) 被删，pid2(500) 保留
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2}
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_a').first().illust_ids) == [2]
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_b').first().illust_ids) == []

    def test_capacity_cleanup_skips_downloaded_and_collected(self, clean_db):
        coll = Collection(name='test-coll')
        clean_db.add(coll)
        clean_db.commit()
        clean_db.add(CollectionItem(collection_id=coll.id, pixiv_id=2, position=1000.0))
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            SearchCache(tag='tag_old', illust_ids='[1, 2]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='dl', prefetch_source=1, download_status='done',
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='col', prefetch_source=1, prefetch_refresh_at=refreshed),
        ])
        safe_commit(clean_db)

        old = app._prefetch_state['max_illusts']
        try:
            app._prefetch_state['max_illusts'] = 0
            app._prefetch_capacity_cleanup()
        finally:
            app._prefetch_state['max_illusts'] = old

        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {1, 2}
        # 受保护（已下载/已收藏）的作品保留在标签列表中
        ids = json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_old').first().illust_ids)
        assert ids == [1, 2]

    def test_capacity_cleanup_keeps_protected_in_list(self, clean_db):
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            SearchCache(tag='tag_a', illust_ids='[1, 2, 3]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='dl', prefetch_source=1, download_status='done',
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='b', prefetch_source=1, prefetch_refresh_at=refreshed),
            Illust(pixiv_id=3, title='c', prefetch_source=1, prefetch_refresh_at=refreshed),
        ])
        safe_commit(clean_db)

        old = app._prefetch_state['max_illusts']
        try:
            app._prefetch_state['max_illusts'] = 0
            app._prefetch_capacity_cleanup()
        finally:
            app._prefetch_state['max_illusts'] = old

        # 可删的 pid 2、3 被删除并从列表移除；已下载的 pid 1 保留在列表中
        ids = json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_a').first().illust_ids)
        assert ids == [1]
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {1}

    def test_capacity_cleanup_removes_from_all_tag_lists(self, clean_db):
        # 被多个标签引用的低收藏作品也会被删，且从所有标签的列表移除
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            SearchCache(tag='tag_a', illust_ids='[1, 2]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            SearchCache(tag='tag_b', illust_ids='[2]',
                        cached_at=datetime(2021, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='a', prefetch_source=1, bookmark_count=10,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='b', prefetch_source=1, bookmark_count=100,
                   prefetch_refresh_at=refreshed),
        ])
        safe_commit(clean_db)

        old = app._prefetch_state['max_illusts']
        try:
            app._prefetch_state['max_illusts'] = 1
            app._prefetch_capacity_cleanup()
        finally:
            app._prefetch_state['max_illusts'] = old

        # pid1(10) 收藏最低被删；pid2(100) 保留
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 1).first() is None
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 2).first() is not None
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_a').first().illust_ids) == [2]
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_b').first().illust_ids) == [2]

    def test_capacity_cleanup_prefers_refreshed_rows(self, clean_db, monkeypatch):
        """三层淘汰：已刷新作品优先淘汰，未刷新作品在够用时不动。"""
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            SearchCache(tag='tag_u', illust_ids='[1, 2, 3]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='finalized-low', prefetch_source=1, bookmark_count=10,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='unfinalized', prefetch_source=1, bookmark_count=1),
            Illust(pixiv_id=3, title='unfinalized2', prefetch_source=1, bookmark_count=2),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 2)  # need_free = 1

        app._prefetch_capacity_cleanup()

        # 只删已刷新的 pid1；未刷新的 pid2/pid3 原样保留
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2, 3}
        ids = json.loads(clean_db.query(SearchCache).filter(
            SearchCache.tag == 'tag_u').first().illust_ids)
        assert ids == [2, 3]

    def test_capacity_cleanup_evicts_failed_unrefreshed_as_tier2(self, clean_db, monkeypatch):
        """第二层：已刷新不够时，淘汰"刷新失败过"的未刷新作品（队列推不动就别占容量）。"""
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        failed_at = datetime.now(timezone.utc)
        clean_db.add_all([
            Illust(pixiv_id=1, title='refreshed', prefetch_source=1, bookmark_count=5,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='failed', prefetch_source=1, bookmark_count=1,
                   refresh_failed_at=failed_at),
            Illust(pixiv_id=3, title='fresh', prefetch_source=1, bookmark_count=2),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 1)  # need_free = 2

        app._prefetch_capacity_cleanup()

        # tier1 删 pid1，tier2 删 pid2；全新的 pid3 保留（还没到兜底层）
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {3}

    def test_capacity_cleanup_last_resort_enforces_cap(self, clean_db, monkeypatch):
        """第三层兜底：全是全新未刷新作品时也把上限压回去（不靠暂停入库）。"""
        clean_db.add_all([
            Illust(pixiv_id=i, title=f'new{i}', prefetch_source=1, bookmark_count=i)
            for i in (1, 2, 3, 4, 5)
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 2)  # need_free = 3

        app._prefetch_capacity_cleanup()

        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert len(remaining) == 2
        assert remaining == {4, 5}  # 收藏数高的留下

    def test_capacity_cleanup_never_evicts_user_owned(self, clean_db, monkeypatch):
        """兜底层同样不能碰用户拥有过的作品，哪怕它是唯一候选。"""
        from models import DownloadLog
        clean_db.add_all([
            Illust(pixiv_id=1, title='touched', prefetch_source=1, bookmark_count=1),
            DownloadLog(pixiv_id=1, action='failed', message='下载失败待重试'),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 0)

        app._prefetch_capacity_cleanup()

        assert clean_db.query(Illust).filter(Illust.pixiv_id == 1).first() is not None

    def test_prefetch_loop_survives_cleanup_error(self, clean_db, monkeypatch):
        clean_db.add(SearchCache(tag='t'))
        safe_commit(clean_db)
        monkeypatch.setattr(app, 'search_by_tag', lambda tag, **kwargs: ([], False))

        def _boom():
            raise RuntimeError('cleanup failed')

        monkeypatch.setattr(app, '_prefetch_capacity_cleanup', _boom)
        # 容量清理异常不应逃逸出 _prefetch_loop（守护线程靠它继续存活）
        app._prefetch_loop()
        assert app._prefetch_state['running'] is False

    def test_prefetch_loop_survives_tags_query_error(self, clean_db, monkeypatch):
        def _boom_session():
            raise RuntimeError('database is locked')

        monkeypatch.setattr(app, 'get_session', _boom_session)
        # 标签列表查询异常不应逃逸出 _prefetch_loop（守护线程靠它继续存活）
        app._prefetch_loop()
        assert app._prefetch_state['running'] is False

    def test_prefetch_loop_always_ingests(self, clean_db, monkeypatch):
        """入库永不停：积压再大也照常预取（容量由三层淘汰压住，不靠暂停入库）。"""
        clean_db.add(SearchCache(tag='t'))
        clean_db.add_all([
            Illust(pixiv_id=7000 + i, title='x', prefetch_source=1)
            for i in range(50)
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 10)
        fetched = []
        monkeypatch.setattr(app, 'search_by_tag',
                            lambda tag, **kwargs: (fetched.append(tag) or ([], False)))
        monkeypatch.setattr(background, '_prefetch_refresh_bookmarks', lambda: None)
        cleaned = []
        monkeypatch.setattr(app, '_prefetch_capacity_cleanup', lambda: cleaned.append(1))

        app._prefetch_loop()

        assert fetched == ['t']
        assert cleaned == [1]


class TestQueryCachedTagSort:
    def test_date_d_sort_with_multiple_none_upload_date(self, clean_db):
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done'),
            Illust(pixiv_id=1, title='a', bookmark_count=5),
            Illust(pixiv_id=2, title='b', bookmark_count=5),
        ])
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag(
            'x', 0, 'date_d', 'or', 'all')

        assert {r['pixiv_id'] for r in results} == {1, 2}
        assert has_more is False
        assert next_offset == 0
        assert filtered_total == 2


class TestResetStuckPrefetch:
    def test_reset_stuck_fetching_tags(self, clean_db):
        """fetching 残留（进程重启打断预取）应被启动重置为 done。"""
        clean_db.add_all([
            SearchCache(tag='stuck', status='fetching', illust_ids='[1, 2]'),
            SearchCache(tag='normal', status='done', illust_ids='[3]'),
        ])
        safe_commit(clean_db)

        app._reset_stuck_prefetch()

        rows = {sc.tag: sc for sc in clean_db.query(SearchCache).all()}
        assert rows['stuck'].status == 'done'
        assert rows['stuck'].error == '上次预取被中断，已重置'
        assert rows['stuck'].illust_ids == '[1, 2]'  # 累积数据保留
        assert rows['normal'].status == 'done'  # done 状态不受影响
        assert rows['normal'].error == ''

    def test_no_stuck_tags_is_noop(self, clean_db):
        clean_db.add(SearchCache(tag='x', status='done', illust_ids='[]'))
        safe_commit(clean_db)

        app._reset_stuck_prefetch()

        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'x').first()
        assert row.status == 'done'
        assert row.error == ''


class TestPrefetchThreadLiveness:
    def test_interval_zero_pauses_thread_but_allows_rerun(self, monkeypatch):
        """interval=0 应暂停循环（睡 60s 继续检查）而非永久退出；重新置>0 后仍能执行预取。"""
        captured = {}

        class _FakeThread:
            def __init__(self, **kwargs):
                captured['target'] = kwargs.get('target')
                captured['daemon'] = kwargs.get('daemon')

            def start(self):
                pass

        monkeypatch.setattr(app.threading, 'Thread', _FakeThread)

        sleeps = []
        loops = []

        def _fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                app._prefetch_state['interval'] = 5  # 暂停期间重新启用
            if len(sleeps) >= 3:
                raise RuntimeError('stop-loop')

        monkeypatch.setattr(app.time, 'sleep', _fake_sleep)
        monkeypatch.setattr(app, '_prefetch_loop', lambda: loops.append(1))

        old_interval = app._prefetch_state['interval']
        try:
            app._prefetch_state['interval'] = 60
            app._start_prefetch_thread()
            assert captured.get('target') is not None
            _run = captured['target']

            app._prefetch_state['interval'] = 0
            with pytest.raises(RuntimeError, match='stop-loop'):
                _run()
        finally:
            app._prefetch_state['interval'] = old_interval

        # interval=0 时走了 sleep(60) 暂停路径而非 break，随后重启用 interval=5 成功执行了 _prefetch_loop
        assert 60 in sleeps
        assert loops == [1]


class TestPrefetchRefreshBookmarks:
    """最终收藏数刷新：满 1 天刷新一次，<10 删除，保护已下载/已收藏。"""

    def _old_illust(self, clean_db, pid, days=2, **kw):
        import models
        old = datetime.now(timezone.utc) - timedelta(days=days)
        illust = Illust(pixiv_id=pid, title=f'p{pid}', prefetch_source=1,
                        bookmark_count=0, created_at=old, **kw)
        clean_db.add(illust)
        return illust

    def _mock_detail(self, monkeypatch, bookmark_count):
        class _FakeSession:
            def close(self):
                pass

        calls = []
        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail',
                            lambda s, pid, limiter=None, return_dead=False:
                            (calls.append(pid) or {'bookmark_count': bookmark_count}))
        return calls

    def _mock_dead_detail(self, monkeypatch):
        class _FakeSession:
            def close(self):
                pass

        calls = []
        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail',
                            lambda s, pid, limiter=None, return_dead=False:
                            (calls.append(pid) or fetcher.DEAD_DETAIL))
        return calls

    def test_refresh_updates_bookmark_once(self, clean_db, monkeypatch):
        from datetime import timedelta
        self._old_illust(clean_db, 5001)
        clean_db.add(SearchCache(tag='t', illust_ids='[5001]', status='done'))
        safe_commit(clean_db)
        calls = self._mock_detail(monkeypatch, 250)

        app._prefetch_refresh_bookmarks()

        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5001).first()
        assert illust.bookmark_count == 250
        assert illust.prefetch_refresh_at is not None
        assert calls == [5001]

        # 只刷新一次：第二次调用不再请求详情
        app._prefetch_refresh_bookmarks()
        assert calls == [5001]

    def test_refresh_deletes_low_bookmark(self, clean_db, monkeypatch):
        self._old_illust(clean_db, 5002)
        clean_db.add(SearchCache(tag='t', illust_ids='[5002]', status='done'))
        safe_commit(clean_db)
        self._mock_detail(monkeypatch, 3)

        app._prefetch_refresh_bookmarks()

        assert clean_db.query(Illust).filter(Illust.pixiv_id == 5002).first() is None
        sc = clean_db.query(SearchCache).filter(SearchCache.tag == 't').first()
        assert json.loads(sc.illust_ids) == []

    def test_refresh_keeps_downloaded_low_bookmark(self, clean_db, monkeypatch):
        self._old_illust(clean_db, 5003, download_status='done')
        clean_db.add(SearchCache(tag='t', illust_ids='[5003]', status='done'))
        safe_commit(clean_db)
        self._mock_detail(monkeypatch, 3)

        app._prefetch_refresh_bookmarks()

        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5003).first()
        assert illust is not None  # 已下载保护，不删
        assert illust.bookmark_count == 3
        assert illust.prefetch_refresh_at is not None

    def test_refresh_pass_keeps_queued_download(self, clean_db, monkeypatch):
        """排队中的作品即使详情已永久失效（DEAD_DETAIL）也不能被刷新清理删掉。

        审计 S3：这个窗口里行状态是 None、也没有 DownloadLog，`_is_user_owned`
        判不出来，旧实现会直接删行 → 排队中的下载静默消失。
        """
        import runtime
        self._old_illust(clean_db, 5011)
        clean_db.add(SearchCache(tag='t', illust_ids='[5011]', status='done'))
        safe_commit(clean_db)
        self._mock_dead_detail(monkeypatch)
        with runtime._download_queue_lock:
            runtime._queued_downloads.add(5011)
        try:
            app._prefetch_refresh_bookmarks()
        finally:
            with runtime._download_queue_lock:
                runtime._queued_downloads.discard(5011)

        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5011).first()
        assert illust is not None, '排队中的下载不能被最终收藏数刷新删掉'
        assert illust.prefetch_refresh_at is not None  # kept_dead：保留并标记完成

    def test_refresh_generic_exception_does_not_bubble(self, clean_db, monkeypatch):
        """未分类异常（如详情解析 KeyError）不许冒泡：否则容量清理被跳过、上限失效。"""
        self._old_illust(clean_db, 5012)
        clean_db.add(SearchCache(tag='t', illust_ids='[5012]', status='done'))
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())

        def _boom(session, pid, limiter=None, return_dead=False):
            raise KeyError('unexpected payload')

        monkeypatch.setattr(app.fetcher, '_get_illust_detail', _boom)

        app._prefetch_refresh_bookmarks()          # 关键：不抛

        assert app._prefetch_state['refresh_stats']['aborted'] == 'unknown'
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 5012).first() is not None

    def test_refresh_skips_fresh_illusts(self, clean_db, monkeypatch):
        from datetime import timedelta
        self._old_illust(clean_db, 5004, days=0)  # 今天入库 → 不满足满 1 天
        safe_commit(clean_db)
        calls = self._mock_detail(monkeypatch, 100)

        app._prefetch_refresh_bookmarks()

        assert calls == []
        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5004).first()
        assert illust.prefetch_refresh_at is None

    def test_refresh_failure_writes_backoff_marker(self, clean_db, monkeypatch):
        """暂时性失败：写 refresh_failed_at，退避期内下一轮不再入选。

        回归背景：旧实现失败静默 continue、无任何标记，永久失败的死作品
        每轮占满 100 个名额（head-of-line blocking），后续作品被饿死。
        """
        self._old_illust(clean_db, 5005)
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        calls = []
        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail',
                            lambda s, pid, limiter=None, return_dead=False:
                            (calls.append(pid) or None))  # 暂时性失败

        app._prefetch_refresh_bookmarks()

        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5005).first()
        assert illust is not None
        assert illust.prefetch_refresh_at is None
        assert illust.refresh_failed_at is not None  # 退避标记已写

        # 退避期内第二轮：不再尝试该作品
        app._prefetch_refresh_bookmarks()
        assert calls == [5005]

    def test_refresh_failure_within_backoff_skipped(self, clean_db, monkeypatch):
        """1 小时前刚失败 → 本轮不入选（不浪费名额）。"""
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        self._old_illust(clean_db, 5006)
        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5006).first()
        illust.refresh_failed_at = recent
        safe_commit(clean_db)
        calls = self._mock_detail(monkeypatch, 100)

        app._prefetch_refresh_bookmarks()

        assert calls == []

    def test_refresh_retries_after_backoff_expired(self, clean_db, monkeypatch):
        """退避过期（2 天前失败）→ 重新入选并重试，成功后清掉失败标记。"""
        old = datetime.now(timezone.utc) - timedelta(days=2)
        self._old_illust(clean_db, 5007)
        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5007).first()
        illust.refresh_failed_at = old
        safe_commit(clean_db)
        calls = self._mock_detail(monkeypatch, 200)

        app._prefetch_refresh_bookmarks()

        assert calls == [5007]
        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5007).first()
        assert illust.prefetch_refresh_at is not None
        assert illust.refresh_failed_at is None  # 成功清标记

    def test_refresh_deletes_permanently_dead(self, clean_db, monkeypatch):
        """永久失败（DEAD_DETAIL，404/删除类）：未下载未收藏 → 删行 + 摘除全部缓存引用。"""
        self._old_illust(clean_db, 5008)
        clean_db.add(SearchCache(tag='t', illust_ids='[5008]', status='done'))
        clean_db.add(SearchCache(tag='t2', illust_ids='[5008, 900]', status='done'))
        safe_commit(clean_db)
        self._mock_dead_detail(monkeypatch)

        app._prefetch_refresh_bookmarks()

        assert clean_db.query(Illust).filter(Illust.pixiv_id == 5008).first() is None
        sc = clean_db.query(SearchCache).filter(SearchCache.tag == 't').first()
        assert json.loads(sc.illust_ids) == []
        sc2 = clean_db.query(SearchCache).filter(SearchCache.tag == 't2').first()
        assert json.loads(sc2.illust_ids) == [900]

    def test_refresh_keeps_dead_downloaded(self, clean_db, monkeypatch):
        """永久失败但已下载 → 保留行、标记刷新完成退出队列。"""
        self._old_illust(clean_db, 5009, download_status='done')
        safe_commit(clean_db)
        self._mock_dead_detail(monkeypatch)

        app._prefetch_refresh_bookmarks()

        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5009).first()
        assert illust is not None
        assert illust.prefetch_refresh_at is not None
        assert illust.refresh_failed_at is None

    def test_refresh_keeps_dead_collected(self, clean_db, monkeypatch):
        """永久失败但已收藏 → 保留行、标记刷新完成退出队列。"""
        self._old_illust(clean_db, 5010)
        fav = Collection(name='我的收藏')
        clean_db.add(fav)
        safe_commit(clean_db)
        clean_db.add(CollectionItem(collection_id=fav.id, pixiv_id=5010))
        safe_commit(clean_db)
        self._mock_dead_detail(monkeypatch)

        app._prefetch_refresh_bookmarks()

        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5010).first()
        assert illust is not None
        assert illust.prefetch_refresh_at is not None

    def test_refresh_keeps_dead_with_download_log(self, clean_db, monkeypatch):
        """永久失败但用户点过下载（DownloadLog）→ 保留行、标记完成。"""
        from models import DownloadLog
        self._old_illust(clean_db, 5012)
        clean_db.add(DownloadLog(pixiv_id=5012, action='failed', message='下载失败'))
        safe_commit(clean_db)
        self._mock_dead_detail(monkeypatch)

        app._prefetch_refresh_bookmarks()

        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5012).first()
        assert illust is not None
        assert illust.prefetch_refresh_at is not None

    def test_dead_deletion_writes_prefetch_deleted_log(self, clean_db, monkeypatch):
        """死作品被清理时写 DownloadLog(action='prefetch_deleted')，且该 action 不构成保护。"""
        from models import DownloadLog
        self._old_illust(clean_db, 5013)
        safe_commit(clean_db)
        self._mock_dead_detail(monkeypatch)

        app._prefetch_refresh_bookmarks()

        assert clean_db.query(Illust).filter(Illust.pixiv_id == 5013).first() is None
        log = clean_db.query(DownloadLog).filter(DownloadLog.pixiv_id == 5013).first()
        assert log is not None
        assert log.action == 'prefetch_deleted'
        assert background._is_user_owned(clean_db, 5013) is False

    def test_refresh_force_done_after_long_failure(self, clean_db, monkeypatch):
        """失败超过 FORCE_DONE（14 天）→ 无网络请求，直接强制标记完成。

        兜底意义：未刷新的作品豁免容量淘汰，长期失败不兜底会无限累积、
        单独顶破容量上限。
        """
        long_ago = datetime.now(timezone.utc) - timedelta(days=15)
        self._old_illust(clean_db, 5011)
        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5011).first()
        illust.refresh_failed_at = long_ago
        safe_commit(clean_db)
        calls = self._mock_detail(monkeypatch, 100)

        app._prefetch_refresh_bookmarks()

        assert calls == []  # 未发起任何详情请求
        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5011).first()
        assert illust.prefetch_refresh_at is not None
        assert illust.refresh_failed_at is None

    def test_refresh_auth_error_aborts_without_backoff_marker(self, clean_db, monkeypatch):
        """认证失效（PixivAuthError）：中止本轮、不写退避标记、异常不逃逸。

        回归背景：① 异常冒泡到 `_prefetch_loop` 会让容量清理被跳过 → 上限彻底失效；
        ② 若给作品写退避标记，Cookie 修好后这批作品还要白等 24h。
        """
        self._old_illust(clean_db, 5020)
        self._old_illust(clean_db, 5021)
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        calls = []

        def _raise_auth(s, pid, limiter=None, return_dead=False):
            calls.append(pid)
            raise fetcher.PixivAuthError('HTTP 401')

        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail', _raise_auth)

        app._prefetch_refresh_bookmarks()  # 不应抛异常

        assert calls == [5020]  # 第一条即认证失败 → 中止，不再请求第二条
        for pid in (5020, 5021):
            illust = clean_db.query(Illust).filter(Illust.pixiv_id == pid).first()
            assert illust is not None
            assert illust.prefetch_refresh_at is None
            assert illust.refresh_failed_at is None  # 全局问题不记在作品头上

    def test_prefetch_loop_runs_cleanup_after_auth_error(self, clean_db, monkeypatch):
        """认证失效只中止刷新，容量清理必须照常执行（否则 10000 上限失效）。"""
        clean_db.add(SearchCache(tag='t'))
        safe_commit(clean_db)
        self._old_illust(clean_db, 5022)
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        def _raise_auth(s, pid, limiter=None, return_dead=False):
            raise fetcher.PixivAuthError('HTTP 401')

        monkeypatch.setattr(app, 'search_by_tag', lambda tag, **kwargs: ([], False))
        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail', _raise_auth)
        cleaned = []
        monkeypatch.setattr(app, '_prefetch_capacity_cleanup',
                            lambda: cleaned.append(1))

        app._prefetch_loop()

        assert cleaned == [1]

    def test_refresh_aborts_on_consecutive_global_failures(self, clean_db, monkeypatch):
        """连续 N 条限流/连接失败 → 中止本轮，且不给这些作品写退避标记。

        限流是账户级状态：按单作品记退避会把整队列刷上 24h，Pixiv 恢复后还要多等一天。
        """
        for pid in range(5030, 5036):
            self._old_illust(clean_db, pid)
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        calls = []
        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail',
                            lambda s, pid, limiter=None, return_dead=False:
                            (calls.append(pid) or fetcher.RETRYABLE_GLOBAL_DETAIL))

        app._prefetch_refresh_bookmarks()

        assert len(calls) == config.PREFETCH_REFRESH_ABORT_STREAK  # 凑满即熔断
        for pid in range(5030, 5036):
            illust = clean_db.query(Illust).filter(Illust.pixiv_id == pid).first()
            assert illust.refresh_failed_at is None
            assert illust.prefetch_refresh_at is None

    def test_refresh_global_failure_resets_streak(self, clean_db, monkeypatch):
        """普通失败会重置连续计数：偶发失败不会导致误熔断。"""
        for pid in range(5040, 5046):
            self._old_illust(clean_db, pid)
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        seq = [fetcher.RETRYABLE_GLOBAL_DETAIL, None,
               fetcher.RETRYABLE_GLOBAL_DETAIL, fetcher.RETRYABLE_GLOBAL_DETAIL,
               fetcher.RETRYABLE_GLOBAL_DETAIL, {'bookmark_count': 50}]
        calls = []

        def _fake(s, pid, limiter=None, return_dead=False):
            calls.append(pid)
            return seq[len(calls) - 1]

        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail', _fake)

        app._prefetch_refresh_bookmarks()

        # 第 2 条普通失败重置计数 → 第 5 条才凑满 3 连击，第 6 条不再尝试
        assert calls == [5040, 5041, 5042, 5043, 5044]
        marked = clean_db.query(Illust).filter(Illust.pixiv_id == 5041).first()
        assert marked.refresh_failed_at is not None  # 普通失败照常写退避

    def test_refresh_stats_recorded(self, clean_db, monkeypatch):
        """每轮刷新写入结构化统计（供 /api/prefetch/status 与设置页展示）。"""
        self._old_illust(clean_db, 5060)                          # 成功
        self._old_illust(clean_db, 5061)                          # 低收藏 → 删除
        self._old_illust(clean_db, 5062)                          # 暂时性失败
        self._old_illust(clean_db, 5063)                          # 永久失败 → 删除
        self._old_illust(clean_db, 5064, download_status='done')  # 永久失败但已下载 → 保留
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        seq = [{'bookmark_count': 100}, {'bookmark_count': 3}, None,
               fetcher.DEAD_DETAIL, fetcher.DEAD_DETAIL]
        calls = []

        def _fake(s, pid, limiter=None, return_dead=False):
            calls.append(pid)
            return seq[len(calls) - 1]

        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail', _fake)

        app._prefetch_refresh_bookmarks()

        stats = app._prefetch_state['refresh_stats']
        assert stats['processed'] == 5
        assert stats['ok'] == 2            # 5060 + 5061（5061 随后按低收藏删除）
        assert stats['deleted_low'] == 1
        assert stats['deleted_dead'] == 1
        assert stats['kept_dead'] == 1
        assert stats['failed_transient'] == 1
        assert stats['failed_global'] == 0
        assert stats['aborted'] == ''
        assert stats['at'] is not None

    def test_refresh_stats_records_abort_reason(self, clean_db, monkeypatch):
        """中止原因写进统计（认证失效/限流熔断要能在设置页看见）。"""
        self._old_illust(clean_db, 5070)
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        def _raise_auth(s, pid, limiter=None, return_dead=False):
            raise fetcher.PixivAuthError('HTTP 401')

        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail', _raise_auth)

        app._prefetch_refresh_bookmarks()

        stats = app._prefetch_state['refresh_stats']
        assert stats['aborted'] == 'auth'
        assert stats['processed'] == 1


class TestResetPrefetchRefresh:
    """手动把作品放回刷新队列：必须指定范围，只影响预取来源作品。"""

    def test_reset_requires_scope(self, clean_db):
        clean_db.add(Illust(pixiv_id=9200, title='a', prefetch_source=1,
                            refresh_failed_at=datetime.now(timezone.utc)))
        safe_commit(clean_db)

        assert app.reset_prefetch_refresh() == 0
        clean_db.expire_all()
        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 9200).first()
        assert illust.refresh_failed_at is not None

    def test_reset_missing_tag_returns_zero(self, clean_db):
        assert app.reset_prefetch_refresh(tag='nope') == 0

    def test_reset_tag_ignores_non_prefetch_and_other_tags(self, clean_db):
        clean_db.add_all([
            SearchCache(tag='t', illust_ids='[9201, 9202]'),
            Illust(pixiv_id=9201, title='a', prefetch_source=1,
                   refresh_failed_at=datetime.now(timezone.utc)),
            Illust(pixiv_id=9202, title='b',
                   refresh_failed_at=datetime.now(timezone.utc)),  # 非预取来源
            Illust(pixiv_id=9203, title='c', prefetch_source=1,
                   refresh_failed_at=datetime.now(timezone.utc)),  # 不在该标签列表
        ])
        safe_commit(clean_db)

        assert app.reset_prefetch_refresh(tag='t') == 1
        clean_db.expire_all()
        assert clean_db.query(Illust).filter(
            Illust.pixiv_id == 9201).first().refresh_failed_at is None
        assert clean_db.query(Illust).filter(
            Illust.pixiv_id == 9202).first().refresh_failed_at is not None
        assert clean_db.query(Illust).filter(
            Illust.pixiv_id == 9203).first().refresh_failed_at is not None

    def test_reset_tag_with_large_id_list_uses_json_each(self, clean_db):
        """整条 id 数组走 json_each 下推（单个绑定参数），不拼上万参数的 IN。"""
        ids = list(range(9300, 9400))
        clean_db.add(SearchCache(tag='big', illust_ids=json.dumps(ids)))
        clean_db.add_all([
            Illust(pixiv_id=i, title='x', prefetch_source=1,
                   refresh_failed_at=datetime.now(timezone.utc)) for i in ids
        ])
        safe_commit(clean_db)

        assert app.reset_prefetch_refresh(tag='big') == 100


class TestDownloadLockRegistry:
    """同一作品的下载锁：收尾时只能注销自己那把。

    回归：`_download_illust` 的 finally 曾无条件 pop。gunicorn --threads 下，
    任务 A 在 release 之后、pop 之前若被抢占，任务 B 会拿到 A 那把已释放的锁并
    开始下载，A 随后把它 pop 掉 —— 任务 C 再进来就拿到一把全新锁，同一作品被
    并发下载两次。
    """

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        import background
        background.download_locks.clear()
        yield
        background.download_locks.clear()

    def test_release_removes_own_lock(self):
        import background
        lock = threading.Lock()
        background.download_locks[555] = lock
        background._release_download_lock(555, lock)
        assert 555 not in background.download_locks

    def test_release_keeps_superseding_lock(self):
        import background
        old, new = threading.Lock(), threading.Lock()
        background.download_locks[555] = old
        background.download_locks[555] = new      # 新任务已顶替
        background._release_download_lock(555, old)
        assert background.download_locks[555] is new, '新任务的锁不能被旧任务误删'

    def test_release_without_entry_is_noop(self):
        import background
        background._release_download_lock(999, threading.Lock())   # 不应抛异常
        assert 999 not in background.download_locks


class TestPrefetchRoundResilience:
    """审计 S4：单轮里任何一段异常都不许让本轮的容量清理被跳过。

    背景：容量清理是 `prefetch_max_illusts` 上限的**唯一**执行者（"暂停入库"
    方案已否决），一旦被异常跳过，入库就没人压得住，库会无限增长。
    """

    def _one_tag(self, clean_db):
        clean_db.add(SearchCache(tag='t'))
        safe_commit(clean_db)

    def test_prefetch_loop_runs_cleanup_when_refresh_raises(self, clean_db, monkeypatch):
        self._one_tag(clean_db)
        monkeypatch.setattr(app, 'search_by_tag', lambda tag, **kwargs: ([], False))

        def _boom():
            raise RuntimeError('refresh exploded')

        # 循环里用的是裸名，解析发生在 background 的全局命名空间 → 补丁必须打在
        # background（打 app 命名空间对裸名调用无效）
        monkeypatch.setattr(background, '_prefetch_refresh_bookmarks', _boom)
        cleaned = []
        monkeypatch.setattr(app, '_prefetch_capacity_cleanup', lambda: cleaned.append(1))

        app._prefetch_loop()

        assert cleaned == [1], '刷新阶段抛异常时容量清理仍必须执行'

    def test_prefetch_loop_runs_cleanup_when_tag_raises(self, clean_db, monkeypatch):
        self._one_tag(clean_db)

        def _boom_tag(tag):
            raise RuntimeError('tag exploded')

        monkeypatch.setattr(background, '_prefetch_one_tag', _boom_tag)
        monkeypatch.setattr(background, '_prefetch_refresh_bookmarks', lambda: None)
        cleaned = []
        monkeypatch.setattr(app, '_prefetch_capacity_cleanup', lambda: cleaned.append(1))

        app._prefetch_loop()

        assert cleaned == [1], '单个标签抛异常时容量清理仍必须执行'

    def test_prefetch_one_tag_status_write_failure_does_not_raise(self, clean_db, monkeypatch,
                                                                 caplog):
        """失败状态回写再失败也不许冒泡（否则整轮中止、且 fetching 残留无人处理）。"""
        from sqlalchemy.exc import OperationalError
        clean_db.add(SearchCache(tag='t', status='done'))
        safe_commit(clean_db)

        def _boom_search(tag, **kwargs):
            raise RuntimeError('search exploded')

        monkeypatch.setattr(app, 'search_by_tag', _boom_search)
        real_commit = background.safe_commit
        calls = {'n': 0}

        def _flaky_commit(db, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                return real_commit(db, *args, **kwargs)   # 抢占 fetching 成功
            db.rollback()                                 # 与真实 safe_commit 同款语义
            raise OperationalError('UPDATE search_cache', {},
                                   Exception('database is locked'))

        monkeypatch.setattr(background, 'safe_commit', _flaky_commit)

        with caplog.at_level(logging.ERROR, logger='background'):
            background._prefetch_one_tag('t')             # 关键：不抛

        assert calls['n'] == 2
        assert '失败状态回写失败' in caplog.text
        row = clean_db.query(SearchCache).filter(SearchCache.tag == 't').first()
        assert row.status == 'fetching', '回写失败的残留状态如实保留（由日志暴露）'
