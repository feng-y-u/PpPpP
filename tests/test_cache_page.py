import json
from datetime import datetime, timezone

from pixivviewer.models import SearchCache, Illust, safe_commit


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

    def test_items_envelope_and_status_passthrough(self, clean_db, client):
        """items 的响应信封 + `status` 如实回传。

        done 与 fetching 两种状态都必须返回累积缓存数据（不要求 status='done'），只有
        缓存行状态这一个输入不同，故合为一条用例；第二段直接改状态，避免重复 `_seed`
        撞 illusts 主键。原 `test_items_not_done_status` 的断言逐条保留。
        """
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试')
        assert r.status_code == 200
        data = r.get_json()
        assert data['tag'] == '测试'
        assert data['status'] == 'done'
        assert data['total'] == 4
        assert data['filtered_total'] == 4
        assert data['page_size'] == 24
        assert len(data['results']) == 4
        assert data['has_more'] is False
        dates = [x['upload_date'][:10] for x in data['results']]
        assert dates == ['2026-08-12', '2026-08-11', '2026-08-10', '2026-08-01']

        # fetching 状态下仍返回累积缓存数据，status 如实返回
        row = clean_db.query(SearchCache).filter(SearchCache.tag == '测试').one()
        row.status = 'fetching'
        safe_commit(clean_db)
        r = client.get('/api/cache/items?tag=测试')
        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'fetching'
        assert len(data['results']) == 4
        assert data['filtered_total'] == 4

    def test_items_min_bookmarks_filter(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试&min_bookmarks=50')
        data = r.get_json()
        ids = {x['pixiv_id'] for x in data['results']}
        assert ids == {101, 103}
        assert data['filtered_total'] == 2

    def test_items_popular_sort(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试&sort=popular_d')
        data = r.get_json()
        bookmarks = [x['bookmark_count'] for x in data['results']]
        assert bookmarks == sorted(bookmarks, reverse=True)
        assert bookmarks[0] == 500

    def test_items_pagination_and_has_more(self, clean_db, client):
        """分页信封的两端：不足一页 / 超过一页（第一页 has_more=True、第二页取剩余）。

        两次请求共用同一套分页断言，只有数据集大小不同（都是"offset + has_more"），
        故合为一条用例，逐段保留原断言。
        """
        # ① 4 条 < page_size 24：偏移到第 3 条后取完全部
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试&offset=2')
        data = r.get_json()
        assert len(data['results']) == 2
        assert data['offset'] == 2
        assert data['has_more'] is False

        # ② 25 条 > page_size 24 → 第一页 has_more=True，第二页取到剩余 1 条
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

    def test_items_param_contract(self, clean_db, client):
        """查询参数的健壮性契约：缺 tag=400、未知 tag=404、非法值一律退回默认。

        三个输入变体共用同一套（几乎是空的）准备，断言形状同构（状态码 / 结果条数），
        故合为一条用例。
        """
        # ① 缺 tag 参数
        r = client.get('/api/cache/items')
        assert r.status_code == 400, '缺 tag 必须是 400'

        # ② 库里没有这个标签
        r = client.get('/api/cache/items?tag=不存在')
        assert r.status_code == 404, '未知 tag 必须是 404'

        # ③ 非法值全部退回默认，不影响结果集
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
