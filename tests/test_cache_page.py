import json
from datetime import datetime, timezone

from models import SearchCache, Illust, safe_commit


class TestCacheItemsApi:
    def _seed(self, db, tag='测试', status='done', ids=None):
        if ids is None:
            ids = [101, 102, 103, 104]
        db.add(SearchCache(tag=tag, illust_ids=json.dumps(ids), status=status,
                           cached_at=datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc), total=len(ids)))
        for pid, bm, d in [(101, 100, '2026-08-10'), (102, 10, '2026-08-12'), (103, 500, '2026-08-01'), (104, 1, '2026-08-11')]:
            i = Illust(pixiv_id=pid, title=f't{pid}', bookmark_count=bm,
                       upload_date=datetime.fromisoformat(d + 'T00:00:00'), thumb_url='https://i.pximg.net/x.jpg')
            i.tags_list = ['tag']
            db.add(i)
        safe_commit(db)

    def test_items_basic(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试')
        assert r.status_code == 200
        data = r.get_json()
        assert data['tag'] == '测试'
        assert data['status'] == 'done'
        assert data['total'] == 4
        assert data['page_size'] == 24
        assert len(data['results']) == 4
        assert data['has_more'] is False
        dates = [x['upload_date'][:10] for x in data['results']]
        assert dates == ['2026-08-12', '2026-08-11', '2026-08-10', '2026-08-01']

    def test_items_min_bookmarks_filter(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试&min_bookmarks=50')
        data = r.get_json()
        ids = {x['pixiv_id'] for x in data['results']}
        assert ids == {101, 103}

    def test_items_popular_sort(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试&sort=popular_d')
        data = r.get_json()
        bookmarks = [x['bookmark_count'] for x in data['results']]
        assert bookmarks == sorted(bookmarks, reverse=True)
        assert bookmarks[0] == 500

    def test_items_pagination(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试&offset=2')
        data = r.get_json()
        assert len(data['results']) == 2
        assert data['offset'] == 2
        assert data['has_more'] is False

    def test_items_has_more_pagination(self, clean_db, client):
        # 25 条 > page_size 24 → 第一页 has_more=True，第二页取到剩余 1 条
        ids = list(range(1, 26))
        db = clean_db
        db.add(SearchCache(tag='大标签', illust_ids=json.dumps(ids), status='done',
                           cached_at=datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc), total=len(ids)))
        for pid in ids:
            i = Illust(pixiv_id=pid, title=f't{pid}', bookmark_count=pid,
                       upload_date=datetime.fromisoformat('2026-08-01T00:00:00'), thumb_url='https://i.pximg.net/x.jpg')
            i.tags_list = ['tag']
            db.add(i)
        safe_commit(db)

        r1 = client.get('/api/cache/items?tag=大标签')
        d1 = r1.get_json()
        assert d1['has_more'] is True
        assert len(d1['results']) == 24

        r2 = client.get('/api/cache/items?tag=大标签&offset=24')
        d2 = r2.get_json()
        assert d2['has_more'] is False
        assert len(d2['results']) == 1
        assert d2['offset'] == 24

    def test_items_missing_tag_param(self, clean_db, client):
        r = client.get('/api/cache/items')
        assert r.status_code == 400

    def test_items_unknown_tag(self, clean_db, client):
        r = client.get('/api/cache/items?tag=不存在')
        assert r.status_code == 404

    def test_items_not_done_status(self, clean_db, client):
        self._seed(clean_db, status='fetching')
        r = client.get('/api/cache/items?tag=测试')
        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'fetching'
        assert data['results'] == []

    def test_items_invalid_params_use_defaults(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试&min_bookmarks=abc&sort=xxx&offset=yyy')
        assert r.status_code == 200
        data = r.get_json()
        assert len(data['results']) == 4


class TestCachePage:
    def test_cache_page_renders(self, client):
        r = client.get('/cache')
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert 'cacheTagSelect' in html
        assert '缓存' in html
