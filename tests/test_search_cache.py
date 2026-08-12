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

        results, has_more, next_offset = app.query_cached_tag(
            'x', 0, 'date_d', 'or', 'all', offset=0, limit=2)

        assert [r['pixiv_id'] for r in results] == [4, 3]
        assert has_more is True
        assert next_offset == 2

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

        results, has_more, next_offset = app.query_cached_tag(
            'x', 10, 'date_d', 'or', 'safe')

        assert [r['pixiv_id'] for r in results] == [3]
        assert has_more is False
        assert next_offset == 0

    def test_r18g_filtered_in_safe_mode(self, clean_db):
        r18g = Illust(pixiv_id=1, title='g', bookmark_count=50)
        r18g.tags_list = ['R-18G']
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done'),
            r18g,
            Illust(pixiv_id=2, title='ok', bookmark_count=50),
        ])
        safe_commit(clean_db)

        results, _, _ = app.query_cached_tag('x', 0, 'date_d', 'or', 'safe')
        assert [r['pixiv_id'] for r in results] == [2]

    def test_missing_or_not_done(self, clean_db):
        clean_db.add(SearchCache(tag='pending', illust_ids='[1]', status='pending'))
        safe_commit(clean_db)

        # 无缓存行
        results, has_more, next_offset = app.query_cached_tag('nope', 0, 'date_d', 'or', 'all')
        assert results == []
        assert has_more is False
        assert next_offset == 0

        # status 非 done
        results, has_more, next_offset = app.query_cached_tag('pending', 0, 'date_d', 'or', 'all')
        assert results == []
        assert has_more is False
        assert next_offset == 0

    def test_empty_ids(self, clean_db):
        clean_db.add(SearchCache(tag='x', illust_ids='[]', status='done'))
        safe_commit(clean_db)

        results, has_more, next_offset = app.query_cached_tag('x', 0, 'date_d', 'or', 'all')
        assert results == []
        assert has_more is False
        assert next_offset == 0

    def test_missing_illust_skipped(self, clean_db):
        # illust_ids 引用了不存在的行（已被容量清理删除）→ 跳过，不报错
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[99, 1]', status='done'),
            Illust(pixiv_id=1, title='exists', bookmark_count=5),
        ])
        safe_commit(clean_db)

        results, has_more, next_offset = app.query_cached_tag('x', 0, 'date_d', 'or', 'all')
        assert [r['pixiv_id'] for r in results] == [1]
        assert has_more is False

    def test_popular_sort(self, clean_db):
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done'),
            Illust(pixiv_id=1, title='a', bookmark_count=5),
            Illust(pixiv_id=2, title='b', bookmark_count=100),
        ])
        safe_commit(clean_db)

        results, _, _ = app.query_cached_tag('x', 0, 'popular_d', 'or', 'all')
        assert [r['pixiv_id'] for r in results] == [2, 1]

    def test_none_upload_date_sorts_last(self, clean_db):
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done'),
            Illust(pixiv_id=1, title='no-date', bookmark_count=5),
            Illust(pixiv_id=2, title='dated', bookmark_count=5,
                   upload_date=datetime(2023, 1, 1, tzinfo=timezone.utc)),
        ])
        safe_commit(clean_db)

        results, _, _ = app.query_cached_tag('x', 0, 'date_d', 'or', 'all')
        assert [r['pixiv_id'] for r in results] == [2, 1]


class TestSearchRouteCache:
    def _poll(self, client, task_id, timeout=50):
        """轮询异步搜索任务直到终态（与 test_app.py 的 TestSearch._poll 一致）。"""
        for _ in range(timeout):
            r = client.get(f'/api/search/status/{task_id}')
            if r.status_code == 404:
                return r
            data = r.get_json()
            if data and data.get('status') != 'running':
                return r
            time.sleep(0.05)
        raise AssertionError(f'搜索任务 {task_id} 超时未完成')

    def test_search_route_cache_hit(self, clean_db, client):
        # cached_at 用 naive datetime（SQLite DateTime 回读不带 tzinfo）
        cached_at = datetime(2025, 1, 1, 12, 0, 0)
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done', cached_at=cached_at),
            Illust(pixiv_id=1, title='a', bookmark_count=10,
                   upload_date=datetime(2023, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=2, title='b', bookmark_count=20,
                   upload_date=datetime(2023, 2, 1, tzinfo=timezone.utc)),
        ])
        safe_commit(clean_db)

        resp = client.get('/search?type=tag&query=x&min_bookmarks=0')
        assert resp.status_code == 200
        assert 'task_id' in resp.get_json()

        final = self._poll(client, resp.get_json()['task_id'])
        assert final.status_code == 200
        body = final.get_json()
        assert body['status'] == 'done'
        assert body['source'] == 'cache'
        assert body['cached_at'] == '2025-01-01T12:00:00'
        assert [r['pixiv_id'] for r in body['results']] == [2, 1]
        assert body['has_more'] is False
        assert body['cursor'] is None

    def test_search_route_cache_miss_still_async(self, clean_db, client, monkeypatch):
        # 未命中缓存 → 仍走异步任务（用 mock 避免真实网络请求）
        monkeypatch.setattr(app, 'search_by_tag', lambda *a, **k: ([], False))
        resp = client.get('/search?type=tag&query=not-cached')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'task_id' in data

        final = self._poll(client, data['task_id'])
        assert final.status_code == 200
        body = final.get_json()
        assert body['status'] == 'done'
        assert body.get('source') is None  # 非缓存路径不带 source

    def test_search_route_cache_cursor_pagination(self, clean_db, client):
        cached_at = datetime(2025, 1, 1, 12, 0, 0)
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2, 3]', status='done', cached_at=cached_at),
            Illust(pixiv_id=1, title='a', bookmark_count=10,
                   upload_date=datetime(2023, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=2, title='b', bookmark_count=20,
                   upload_date=datetime(2023, 2, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=3, title='c', bookmark_count=30,
                   upload_date=datetime(2023, 3, 1, tzinfo=timezone.utc)),
        ])
        safe_commit(clean_db)

        # 第一页：limit=ITEMS_PER_PAGE(24) > 3 条，全量返回
        resp = client.get('/search?type=tag&query=x&min_bookmarks=0')
        task_id = resp.get_json()['task_id']
        body = self._poll(client, task_id).get_json()
        assert body['source'] == 'cache'
        assert [r['pixiv_id'] for r in body['results']] == [3, 2, 1]
        assert body['has_more'] is False

        # 构造 cache_offset 游标模拟第二页（date_d 排序，offset=2 → 剩 [1]）
        cursor = app.encode_cursor({
            'type': 'tag', 'query': 'x', 'sort': 'date_d',
            'tag_mode': 'or', 'r18_mode': 'all', 'min_bookmarks': 0,
            'cache_offset': 2, 'created_at': int(time.time()),
        })
        resp = client.get(f'/search?type=tag&query=x&min_bookmarks=0&cursor={cursor}')
        assert resp.status_code == 200
        task_id = resp.get_json()['task_id']
        body = self._poll(client, task_id).get_json()
        assert body['source'] == 'cache'
        assert [r['pixiv_id'] for r in body['results']] == [1]
        assert body['has_more'] is False
