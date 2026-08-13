import json
from datetime import datetime, timezone

import pytest

import app
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
    def test_capacity_cleanup_deletes_oldest(self, clean_db):
        clean_db.add_all([
            SearchCache(tag='tag_old', illust_ids='[1, 2]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            SearchCache(tag='tag_new', illust_ids='[2, 3]',
                        cached_at=datetime(2021, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='a', prefetch_source=1),
            Illust(pixiv_id=2, title='b', prefetch_source=1),
            Illust(pixiv_id=3, title='c', prefetch_source=1),
        ])
        safe_commit(clean_db)

        old = app._prefetch_state['max_illusts']
        try:
            app._prefetch_state['max_illusts'] = 1
            app._prefetch_capacity_cleanup()
        finally:
            app._prefetch_state['max_illusts'] = old

        # 最旧标签的末尾（pid 1）与 tag_new 的 pid 3 被删除；
        # pid 2 因被 tag_new 引用而保留在两个标签的列表中
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2}
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_old').first().illust_ids) == [2]
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_new').first().illust_ids) == [2]

    def test_capacity_cleanup_skips_downloaded_and_collected(self, clean_db):
        coll = Collection(name='test-coll')
        clean_db.add(coll)
        clean_db.commit()
        clean_db.add(CollectionItem(collection_id=coll.id, pixiv_id=2, position=1000.0))
        clean_db.add_all([
            SearchCache(tag='tag_old', illust_ids='[1, 2]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='dl', prefetch_source=1, download_status='done'),
            Illust(pixiv_id=2, title='col', prefetch_source=1),
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
        clean_db.add_all([
            SearchCache(tag='tag_a', illust_ids='[1, 2, 3]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='dl', prefetch_source=1, download_status='done'),
            Illust(pixiv_id=2, title='b', prefetch_source=1),
            Illust(pixiv_id=3, title='c', prefetch_source=1),
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

    def test_capacity_cleanup_exact_membership_no_like_false_positive(self, clean_db):
        # LIKE 子串误判：pid 1234567 不应被 tag_b 的 12345678 误判为“仍被引用”
        clean_db.add_all([
            SearchCache(tag='tag_a', illust_ids='[1234567]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            SearchCache(tag='tag_b', illust_ids='[12345678]',
                        cached_at=datetime(2021, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1234567, title='a', prefetch_source=1),
            Illust(pixiv_id=12345678, title='b', prefetch_source=1),
        ])
        safe_commit(clean_db)

        old = app._prefetch_state['max_illusts']
        try:
            app._prefetch_state['max_illusts'] = 1
            app._prefetch_capacity_cleanup()
        finally:
            app._prefetch_state['max_illusts'] = old

        assert clean_db.query(Illust).filter(Illust.pixiv_id == 1234567).first() is None
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 12345678).first() is not None
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_a').first().illust_ids) == []
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_b').first().illust_ids) == [12345678]

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
