import json
from datetime import datetime, timezone

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
