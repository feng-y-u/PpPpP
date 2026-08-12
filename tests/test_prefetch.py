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

        # 最旧标签的末尾（pid 1、3）被删除；pid 2 因被 tag_new 引用而保留
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2}
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_old').first().illust_ids) == []
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
