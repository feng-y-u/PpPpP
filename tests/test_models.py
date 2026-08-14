from models import Illust, DownloadLog, safe_commit


class TestIllustCreate:
    def test_create_and_query(self, clean_db, sample_illust):
        fetched = clean_db.query(Illust).filter(Illust.pixiv_id == 12345678).first()
        assert fetched is not None
        assert fetched.title == 'テスト作品'
        assert fetched.user_name == 'テスト画師'
        assert fetched.bookmark_count == 1500

    def test_unique_pixiv_id(self, clean_db):
        i1 = Illust(pixiv_id=999, title='a')
        clean_db.add(i1)
        safe_commit(clean_db)
        i2 = Illust(pixiv_id=999, title='b')
        clean_db.add(i2)
        import sqlalchemy.exc
        import pytest
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            safe_commit(clean_db)
        clean_db.rollback()
        clean_db.query(Illust).filter(Illust.pixiv_id == 999).delete()
        clean_db.commit()


class TestIllustJsonProperties:
    def test_tags_list_roundtrip(self, clean_db, sample_illust):
        assert sample_illust.tags_list == ['test', 'sample', 'original']
        sample_illust.tags_list = ['new', 'tags']
        safe_commit(clean_db)
        clean_db.refresh(sample_illust)
        assert sample_illust.tags_list == ['new', 'tags']

    def test_original_urls_list_roundtrip(self, clean_db, sample_illust):
        urls = sample_illust.original_urls_list
        assert len(urls) == 2
        assert urls[0].startswith('https://i.pximg.net/')

    def test_local_paths_null_by_default(self, clean_db, sample_illust):
        assert sample_illust.local_paths_list is None

    def test_local_paths_roundtrip(self, clean_db, sample_illust):
        paths = [r'C:\downloads\test_p0.jpg', r'C:\downloads\test_p1.jpg']
        sample_illust.local_paths_list = paths
        safe_commit(clean_db)
        clean_db.refresh(sample_illust)
        assert sample_illust.local_paths_list == paths

    def test_local_paths_set_to_null(self, clean_db, sample_illust):
        sample_illust.local_paths_list = ['a.jpg']
        safe_commit(clean_db)
        sample_illust.local_paths_list = None
        safe_commit(clean_db)
        clean_db.refresh(sample_illust)
        assert sample_illust.local_paths_list is None


class TestIllustToDict:
    def test_to_dict_keys(self, sample_illust):
        d = sample_illust.to_dict()
        expected_keys = {
            'id', 'pixiv_id', 'title', 'user_id', 'user_name', 'tags',
            'page_count', 'bookmark_count', 'upload_date', 'thumb_url',
            'original_urls', 'download_status',
            'downloaded_at', 'file_size', 'is_favorite', 'created_at',
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values(self, sample_illust):
        d = sample_illust.to_dict()
        assert d['pixiv_id'] == 12345678
        assert d['title'] == 'テスト作品'
        assert d['tags'] == ['test', 'sample', 'original']
        assert d['page_count'] == 3
        assert d['bookmark_count'] == 1500
        assert d['download_status'] is None
        assert d['is_favorite'] is False

    def test_to_dict_omits_local_paths(self, clean_db, sample_illust):
        """回归：磁盘绝对路径不应出现在序列化输出中（仅模型属性保留）。"""
        sample_illust.local_paths_list = ['/data/a.jpg']
        sample_illust.download_status = 'done'
        safe_commit(clean_db)
        d = sample_illust.to_dict()
        assert 'local_paths' not in d
        assert 'local_dir' not in d
        assert d['download_status'] == 'done'


class TestIllustDownloadStatus:
    def test_default_status(self, clean_db, sample_illust):
        assert sample_illust.download_status is None

    def test_transitions(self, clean_db, sample_illust):
        sample_illust.download_status = 'downloading'
        safe_commit(clean_db)
        assert sample_illust.download_status == 'downloading'

        sample_illust.download_status = 'done'
        safe_commit(clean_db)
        assert sample_illust.download_status == 'done'

        sample_illust.download_status = None
        safe_commit(clean_db)
        assert sample_illust.download_status is None


class TestPositionMigration:
    def test_position_column_exists(self, clean_db):
        import models
        with models.engine.connect() as conn:
            cols = [r[1] for r in conn.exec_driver_sql('PRAGMA table_info(collection_items)').fetchall()]
        assert 'position' in cols

    def test_migration_assigns_positions_by_created_at(self, clean_db):
        import models as m
        from datetime import datetime, timezone, timedelta
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        coll = m.Collection(name='test-coll-mig')
        clean_db.add(coll); clean_db.commit()
        for i in range(3):
            ci = m.CollectionItem(collection_id=coll.id, pixiv_id=10000 + i)
            ci.created_at = base + timedelta(seconds=i)
            ci.position = 0.0
            clean_db.add(ci)
        clean_db.commit()
        with m.engine.connect() as conn:
            conn.exec_driver_sql('PRAGMA user_version = 0')
            conn.commit()
        m.init_db()
        with m.get_session() as s:
            items = s.query(m.CollectionItem).filter(
                m.CollectionItem.collection_id == coll.id
            ).order_by(m.CollectionItem.position).all()
        assert [it.position for it in items] == [1000.0, 2000.0, 3000.0]
        assert [it.pixiv_id for it in items] == [10000, 10001, 10002]

    def test_migration_is_idempotent_on_restart(self, clean_db):
        import models as m
        coll = m.Collection(name='test-coll-idem')
        clean_db.add(coll); clean_db.commit()
        ci = m.CollectionItem(collection_id=coll.id, pixiv_id=20000, position=42.0)
        clean_db.add(ci); clean_db.commit()
        m.init_db()
        with m.get_session() as s:
            it = s.query(m.CollectionItem).filter(m.CollectionItem.pixiv_id == 20000).one()
        assert it.position == 42.0


class TestIllustToDictFavoriteOverride:
    def test_to_dict_uses_provided_favorite(self, clean_db):
        from models import Illust
        il = Illust(pixiv_id=808, title='t')
        clean_db.add(il); clean_db.commit()
        d = il.to_dict(favorite=True)
        assert d['is_favorite'] is True
        d2 = il.to_dict(favorite=False)
        assert d2['is_favorite'] is False


class TestIsRemoved:
    def test_illusts_no_is_favorite_column(self, clean_db):
        import models
        with models.engine.connect() as conn:
            cols = [r[1] for r in conn.exec_driver_sql('PRAGMA table_info(illusts)').fetchall()]
        assert 'is_favorite' not in cols
        assert 'favorited_at' not in cols

    def test_no_sync_is_favorite_symbol(self):
        import app
        assert not hasattr(app, '_sync_is_favorite')

    def test_illust_model_has_no_is_favorite_attr(self, clean_db):
        from models import Illust
        il = Illust(pixiv_id=99999, title='t')
        clean_db.add(il); clean_db.commit()
        assert not hasattr(il, 'is_favorite')
        assert not hasattr(il, 'favorited_at')


from models import SearchCache, safe_commit


class TestSearchCache:
    def test_create_and_query(self, clean_db):
        sc = SearchCache(
            tag='初音ミク',
            illust_ids='[123, 456, 789]',
            status='done',
            total=3,
        )
        clean_db.add(sc)
        safe_commit(clean_db)
        fetched = clean_db.query(SearchCache).filter(SearchCache.tag == '初音ミク').first()
        assert fetched is not None
        assert fetched.tag == '初音ミク'
        assert fetched.illust_ids == '[123, 456, 789]'
        assert fetched.status == 'done'
        assert fetched.total == 3

    def test_primary_key_is_tag(self, clean_db):
        clean_db.add(SearchCache(tag='tag1'))
        safe_commit(clean_db)
        import pytest
        from sqlalchemy.exc import IntegrityError
        clean_db.add(SearchCache(tag='tag1'))
        with pytest.raises(IntegrityError):
            safe_commit(clean_db)
        clean_db.rollback()
        clean_db.query(SearchCache).filter(SearchCache.tag == 'tag1').delete()
        clean_db.commit()

    def test_defaults(self, clean_db):
        sc = SearchCache(tag='test')
        clean_db.add(sc)
        safe_commit(clean_db)
        assert sc.illust_ids == '[]'
        assert sc.status == 'idle'
        assert sc.error == ''
        assert sc.total == 0

    def test_prefetch_source_column(self, clean_db, sample_illust):
        sample_illust.prefetch_source = 1
        safe_commit(clean_db)
        clean_db.refresh(sample_illust)
        assert sample_illust.prefetch_source == 1

    def test_description_removed(self, clean_db, sample_illust):
        assert not hasattr(sample_illust, 'description')
        import models
        with models.engine.connect() as conn:
            cols = [r[1] for r in conn.exec_driver_sql('PRAGMA table_info(illusts)').fetchall()]
        assert 'description' not in cols


class TestDescriptionPrefetchMigration:
    def test_migration_drops_description_adds_prefetch_source(self, clean_db):
        import models as m
        with m.engine.connect() as conn:
            conn.exec_driver_sql('ALTER TABLE illusts ADD COLUMN description TEXT DEFAULT ""')
            conn.exec_driver_sql('ALTER TABLE illusts DROP COLUMN prefetch_source')
            conn.commit()
        m.init_db()
        with m.engine.connect() as conn:
            cols = [r[1] for r in conn.exec_driver_sql('PRAGMA table_info(illusts)').fetchall()]
        assert 'description' not in cols
        assert 'prefetch_source' in cols


class TestSafeCommitLocked:
    def test_locked_raises_and_rolls_back(self, clean_db, monkeypatch):
        """回归：commit 遇 database is locked 必须 rollback 恢复 session（曾抛 PendingRollbackError）。"""
        import models
        import pytest
        from sqlalchemy.exc import OperationalError
        rollback_called = []

        def fake_commit():
            raise OperationalError('stmt', {}, Exception('database is locked'))

        monkeypatch.setattr(clean_db, 'commit', fake_commit)
        monkeypatch.setattr(clean_db, 'rollback', lambda: rollback_called.append(1))
        with pytest.raises(OperationalError):
            models.safe_commit(clean_db)
        assert rollback_called == [1]

    def test_session_usable_after_locked(self, clean_db, monkeypatch):
        """回归：locked 失败后 session 已恢复可用，不再抛 PendingRollbackError。"""
        import models
        import pytest
        from sqlalchemy import text
        from sqlalchemy.exc import OperationalError

        def fake_commit():
            raise OperationalError('stmt', {}, Exception('database is locked'))

        monkeypatch.setattr(clean_db, 'commit', fake_commit)
        with pytest.raises(OperationalError):
            models.safe_commit(clean_db)
        assert clean_db.execute(text('SELECT 1')).scalar() == 1


class TestDownloadLog:
    def test_defaults_and_to_dict(self, clean_db):
        log = DownloadLog(pixiv_id=123, action='start', message='测试')
        clean_db.add(log)
        clean_db.commit()
        d = log.to_dict()
        assert d['pixiv_id'] == 123
        assert d['action'] == 'start'
        assert d['message'] == '测试'
        assert d['created_at'] is not None


class TestRebuildIllustsTable:
    def test_rebuild_preserves_data_and_schema(self, clean_db, sample_illust):
        """SQLite<3.35 的重建路径（高危未测）：数据与索引必须保留。"""
        import models as m
        from sqlalchemy import text
        m._rebuild_illusts_table(set())
        with m.get_session() as s:
            row = s.query(Illust).filter(Illust.pixiv_id == 12345678).first()
            assert row is not None
            assert row.title == 'テスト作品'
            assert row.bookmark_count == 1500
        with m.engine.connect() as conn:
            idx = [r[1] for r in conn.exec_driver_sql('PRAGMA index_list(illusts)').fetchall()]
        assert 'ix_illusts_pixiv_id' in idx
        assert 'ix_illusts_dl_status_created' in idx
