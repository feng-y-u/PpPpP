import time
from datetime import datetime, timezone

import app
from models import SearchCache, Illust, safe_commit


class TestQueryCachedTag:
    def test_basic_filter_and_sort(self, clean_db):
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2, 3, 4]', status='done',
                        cached_at=datetime(2025, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='a', bookmark_count=10,
                   upload_date=datetime(2023, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=2, title='b', bookmark_count=20,
                   upload_date=datetime(2023, 2, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=3, title='c', bookmark_count=30,
                   upload_date=datetime(2023, 3, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=4, title='d', bookmark_count=40,
                   upload_date=datetime(2023, 4, 1, tzinfo=timezone.utc)),
        ])
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag(
            'x', 0, 'date_d', 'or', 'all', offset=0, limit=2)

        assert [r['pixiv_id'] for r in results] == [4, 3]
        assert has_more is True
        assert next_offset == 2
        assert filtered_total == 4

    def test_min_bookmarks_and_r18(self, clean_db):
        r18 = Illust(pixiv_id=2, title='r18', bookmark_count=100)
        r18.tags_list = ['R-18', 'original']
        safe_illust = Illust(pixiv_id=3, title='ok', bookmark_count=100)
        safe_illust.tags_list = ['original']
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2, 3]', status='done'),
            Illust(pixiv_id=1, title='low', bookmark_count=5),
            r18,
            safe_illust,
        ])
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag(
            'x', 10, 'date_d', 'or', 'safe')

        assert [r['pixiv_id'] for r in results] == [3]
        assert has_more is False
        assert next_offset == 0
        assert filtered_total == 1

    def test_r18g_filtered_in_safe_mode(self, clean_db):
        r18g = Illust(pixiv_id=1, title='g', bookmark_count=50)
        r18g.tags_list = ['R-18G']
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done'),
            r18g,
            Illust(pixiv_id=2, title='ok', bookmark_count=50),
        ])
        safe_commit(clean_db)

        results, _, _, _ = app.query_cached_tag('x', 0, 'date_d', 'or', 'safe')
        assert [r['pixiv_id'] for r in results] == [2]

    def test_missing_row_returns_empty(self, clean_db):
        # 无缓存行
        results, has_more, next_offset, filtered_total = app.query_cached_tag('nope', 0, 'date_d', 'or', 'all')
        assert results == []
        assert has_more is False
        assert next_offset == 0
        assert filtered_total == 0

    def test_non_done_status_still_returns_data(self, clean_db):
        # fetching/error 状态下也应能查看累积缓存（不要求 status='done'）
        clean_db.add_all([
            SearchCache(tag='fetching', illust_ids='[1, 2]', status='fetching'),
            Illust(pixiv_id=1, title='a', bookmark_count=5),
            Illust(pixiv_id=2, title='b', bookmark_count=10),
        ])
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag('fetching', 0, 'date_d', 'or', 'all')
        assert {r['pixiv_id'] for r in results} == {1, 2}
        assert filtered_total == 2

    def test_status_error_with_missing_illust_skipped(self, clean_db):
        # status=error 时返回已有数据；illust_ids 引用的缺失行跳过
        clean_db.add_all([
            SearchCache(tag='err', illust_ids='[99, 1]', status='error'),
            Illust(pixiv_id=1, title='exists', bookmark_count=5),
        ])
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag('err', 0, 'date_d', 'or', 'all')
        assert [r['pixiv_id'] for r in results] == [1]
        assert filtered_total == 1

    def test_empty_ids(self, clean_db):
        clean_db.add(SearchCache(tag='x', illust_ids='[]', status='done'))
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag('x', 0, 'date_d', 'or', 'all')
        assert results == []
        assert has_more is False
        assert next_offset == 0
        assert filtered_total == 0

    def test_missing_illust_skipped(self, clean_db):
        # illust_ids 引用了不存在的行（已被容量清理删除）→ 跳过，不报错
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[99, 1]', status='done'),
            Illust(pixiv_id=1, title='exists', bookmark_count=5),
        ])
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag('x', 0, 'date_d', 'or', 'all')
        assert [r['pixiv_id'] for r in results] == [1]
        assert has_more is False
        assert filtered_total == 1

    def test_popular_sort(self, clean_db):
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done'),
            Illust(pixiv_id=1, title='a', bookmark_count=5),
            Illust(pixiv_id=2, title='b', bookmark_count=100),
        ])
        safe_commit(clean_db)

        results, _, _, _ = app.query_cached_tag('x', 0, 'popular_d', 'or', 'all')
        assert [r['pixiv_id'] for r in results] == [2, 1]

    def test_none_upload_date_sorts_last(self, clean_db):
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done'),
            Illust(pixiv_id=1, title='no-date', bookmark_count=5),
            Illust(pixiv_id=2, title='dated', bookmark_count=5,
                   upload_date=datetime(2023, 1, 1, tzinfo=timezone.utc)),
        ])
        safe_commit(clean_db)

        results, _, _, _ = app.query_cached_tag('x', 0, 'date_d', 'or', 'all')
        assert [r['pixiv_id'] for r in results] == [2, 1]

    def test_filter_tag(self, clean_db):
        a = Illust(pixiv_id=1, title='a', bookmark_count=5)
        a.tags_list = ['猫', '日常']
        b = Illust(pixiv_id=2, title='b', bookmark_count=5)
        b.tags_list = ['风景']
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done'),
            a, b,
        ])
        safe_commit(clean_db)

        # 命中：只返回含该标签的作品
        results, _, _, filtered_total = app.query_cached_tag(
            'x', 0, 'date_d', 'or', 'all', filter_tag='猫')
        assert [r['pixiv_id'] for r in results] == [1]
        assert filtered_total == 1

        # 未命中：空结果
        results, _, _, filtered_total = app.query_cached_tag(
            'x', 0, 'date_d', 'or', 'all', filter_tag='不存在')
        assert results == []
        assert filtered_total == 0

        # 空 filter_tag = 不过滤
        results, _, _, filtered_total = app.query_cached_tag(
            'x', 0, 'date_d', 'or', 'all', filter_tag='')
        assert [r['pixiv_id'] for r in results] == [1, 2]


class TestCacheTagsAPI:
    def test_cache_tags_lists_prefetch_illust_tags(self, client, clean_db):
        a = Illust(pixiv_id=1, title='a', prefetch_source=1)
        a.tags_list = ['猫', '日常']
        b = Illust(pixiv_id=2, title='b', prefetch_source=1)
        b.tags_list = ['猫', '风景']
        c = Illust(pixiv_id=3, title='c', prefetch_source=0)  # 非预取来源不列出
        c.tags_list = ['私人']
        clean_db.add_all([a, b, c])
        safe_commit(clean_db)

        resp = client.get('/api/cache/tags')
        assert resp.status_code == 200
        tags = resp.get_json()
        assert '猫' in tags and '日常' in tags and '风景' in tags
        assert '私人' not in tags

    def test_cache_items_filter_tag(self, client, clean_db):
        a = Illust(pixiv_id=1, title='a', prefetch_source=1)
        a.tags_list = ['猫']
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done'),
            a,
            Illust(pixiv_id=2, title='b', prefetch_source=1),
        ])
        safe_commit(clean_db)

        resp = client.get('/api/cache/items?tag=x&filter_tag=猫')
        assert resp.status_code == 200
        data = resp.get_json()
        assert [r['pixiv_id'] for r in data['results']] == [1]
        assert data['filtered_total'] == 1

    def test_cache_items_r18_default_safe(self, client, clean_db):
        """回归：缓存页默认不含 R18（r18 参数缺省 = safe）。"""
        r18_illust = Illust(pixiv_id=1, title='r', prefetch_source=1)
        r18_illust.tags_list = ['R-18', 'original']
        safe_illust = Illust(pixiv_id=2, title='s', prefetch_source=1)
        safe_illust.tags_list = ['original']
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done'),
            r18_illust, safe_illust,
        ])
        safe_commit(clean_db)

        resp = client.get('/api/cache/items?tag=x')
        assert resp.status_code == 200
        data = resp.get_json()
        assert [r['pixiv_id'] for r in data['results']] == [2]
        assert data['filtered_total'] == 1

        # 显式 r18=all 时包含 R18
        resp = client.get('/api/cache/items?tag=x&r18=all')
        assert resp.status_code == 200
        data = resp.get_json()
        assert [r['pixiv_id'] for r in data['results']] == [1, 2]
        assert data['filtered_total'] == 2


class TestSearchAlwaysLive:
    def test_search_route_always_live_even_for_cached_tag(self, clean_db, client, monkeypatch):
        clean_db.add(SearchCache(tag='缓存标签', status='done', illust_ids='[1,2,3]'))
        safe_commit(clean_db)

        called = []
        def fake_search_by_tag(*args, **kwargs):
            called.append(1)
            return [], False
        monkeypatch.setattr('app.search_by_tag', fake_search_by_tag)

        r = client.get('/search?type=tag&query=缓存标签')
        assert r.status_code == 200
        assert 'task_id' in r.get_json()
        deadline = time.time() + 3
        while not called and time.time() < deadline:
            time.sleep(0.02)
        assert called, '预取标签搜索必须走实时 search_by_tag'
