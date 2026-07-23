from models import Illust, safe_commit


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
            'description', 'original_urls', 'local_paths', 'download_status',
            'downloaded_at', 'file_size', 'is_favorite', 'favorited_at', 'created_at',
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
        assert d['local_paths'] is None
        assert d['is_favorite'] is False

    def test_to_dict_includes_local_paths_when_set(self, clean_db, sample_illust):
        sample_illust.local_paths_list = ['/data/a.jpg']
        sample_illust.download_status = 'done'
        safe_commit(clean_db)
        d = sample_illust.to_dict()
        assert d['local_paths'] == ['/data/a.jpg']
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
