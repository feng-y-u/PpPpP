import time
from datetime import datetime, timezone

import app
from models import SearchCache, Illust, BlockedTag, safe_commit


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

    def test_safe_mode_filters_r18_variants_and_min_bookmarks(self, clean_db):
        """safe 模式的过滤口径：min_bookmarks 剔除低收藏，R-18 / R-18G 一律剔除。

        R-18 与 R-18G 是同一条不变式（safe 模式按标签剔除）的两个输入变体，且两次
        查询共用同一套"seed 缓存行 + 作品行"的准备，故合为一条用例（逐段保留各自断言）。
        """
        # ① min_bookmarks 剔除低收藏 + R-18 剔除
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

        assert [r['pixiv_id'] for r in results] == [3], '低收藏与 R-18 都必须被剔除'
        assert has_more is False
        assert next_offset == 0
        assert filtered_total == 1

        # ② R-18G 同样被 safe 模式剔除
        r18g = Illust(pixiv_id=11, title='g', bookmark_count=50)
        r18g.tags_list = ['R-18G']
        clean_db.add_all([
            SearchCache(tag='g', illust_ids='[11, 12]', status='done'),
            r18g,
            Illust(pixiv_id=12, title='ok', bookmark_count=50),
        ])
        safe_commit(clean_db)

        results, _, _, _ = app.query_cached_tag('g', 0, 'date_d', 'or', 'safe')
        assert [r['pixiv_id'] for r in results] == [12], 'R-18G 也必须被 safe 模式剔除'

    def test_empty_result_envelope(self, clean_db):
        """两种"没有可返回的作品"：根本没有缓存行 / 缓存行存在但 illust_ids 为空。

        期望的信封完全同构（空结果 + has_more / next_offset / filtered_total 归零），
        只有前置输入不同，故合为一条用例。
        """
        # ① 无缓存行
        results, has_more, next_offset, filtered_total = app.query_cached_tag(
            'nope', 0, 'date_d', 'or', 'all')
        assert results == [], '没有缓存行时必须返回空结果'
        assert has_more is False
        assert next_offset == 0
        assert filtered_total == 0

        # ② 有缓存行但 illust_ids 为空
        clean_db.add(SearchCache(tag='x', illust_ids='[]', status='done'))
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag(
            'x', 0, 'date_d', 'or', 'all')
        assert results == [], '空 illust_ids 同样必须返回空结果'
        assert has_more is False
        assert next_offset == 0
        assert filtered_total == 0

    def test_non_done_status_and_missing_rows_still_return_data(self, clean_db):
        """有多少能返回就返回多少：不要求 status='done'，illust_ids 引用的缺失行跳过。

        三个输入变体（fetching 状态 / error 状态含缺失行 / done 状态含缺失行）共用
        同一条不变式，故合为一条用例，逐段保留原本的注释与断言。
        """
        # ① fetching/error 状态下也应能查看累积缓存（不要求 status='done'）
        clean_db.add_all([
            SearchCache(tag='fetching', illust_ids='[1, 2]', status='fetching'),
            Illust(pixiv_id=1, title='a', bookmark_count=5),
            Illust(pixiv_id=2, title='b', bookmark_count=10),
        ])
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag(
            'fetching', 0, 'date_d', 'or', 'all')
        assert {r['pixiv_id'] for r in results} == {1, 2}
        assert filtered_total == 2

        # ② status=error 时返回已有数据；illust_ids 引用的缺失行跳过
        clean_db.add(SearchCache(tag='err', illust_ids='[99, 1]', status='error'))
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag(
            'err', 0, 'date_d', 'or', 'all')
        assert [r['pixiv_id'] for r in results] == [1], '缺失的 99 必须被跳过，不报错'
        assert filtered_total == 1

        # ③ illust_ids 引用了不存在的行（已被容量清理删除）→ 跳过，不报错
        clean_db.add(SearchCache(tag='x', illust_ids='[99, 1]', status='done'))
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag(
            'x', 0, 'date_d', 'or', 'all')
        assert [r['pixiv_id'] for r in results] == [1]
        assert has_more is False
        assert filtered_total == 1

    def test_sort_orders(self, clean_db):
        """排序口径的两个输入：popular_d 按收藏数降序、date_d 把无上传日期的排最后。

        两次查询共用同一套"seed 两行 → 比对 pixiv_id 顺序"的准备，故合为一条用例。
        """
        # ① popular_d：收藏数高者在前
        clean_db.add_all([
            SearchCache(tag='pop', illust_ids='[1, 2]', status='done'),
            Illust(pixiv_id=1, title='a', bookmark_count=5),
            Illust(pixiv_id=2, title='b', bookmark_count=100),
        ])
        safe_commit(clean_db)

        results, _, _, _ = app.query_cached_tag('pop', 0, 'popular_d', 'or', 'all')
        assert [r['pixiv_id'] for r in results] == [2, 1], 'popular_d 必须按收藏数降序'

        # ② date_d：无 upload_date 的沉底（不能因为 NULL 冒到最前）
        clean_db.add_all([
            SearchCache(tag='dt', illust_ids='[3, 4]', status='done'),
            Illust(pixiv_id=3, title='no-date', bookmark_count=5),
            Illust(pixiv_id=4, title='dated', bookmark_count=5,
                   upload_date=datetime(2023, 1, 1, tzinfo=timezone.utc)),
        ])
        safe_commit(clean_db)

        results, _, _, _ = app.query_cached_tag('dt', 0, 'date_d', 'or', 'all')
        assert [r['pixiv_id'] for r in results] == [4, 3], '无日期的必须排在有日期的后面'

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

    def test_cache_items_filters(self, client, clean_db):
        """缓存页的三个过滤口径（同一接口、同一断言形状：结果集 + filtered_total）。

        ① filter_tag 只留含该标签的；② r18 缺省 = safe，显式 all 才放 R18；
        ③ 全局屏蔽标签照常生效 —— 与搜索/图库一致。
        三者只是查询参数不同，故合为一条用例，各自的数据准备逐段保留（作品 id 不重叠）。
        """
        # ① filter_tag 命中
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

        # ② 回归：缓存页默认不含 R18（r18 参数缺省 = safe）
        r18_illust = Illust(pixiv_id=3, title='r', prefetch_source=1)
        r18_illust.tags_list = ['R-18', 'original']
        safe_illust = Illust(pixiv_id=4, title='s', prefetch_source=1)
        safe_illust.tags_list = ['original']
        clean_db.add_all([
            SearchCache(tag='r18', illust_ids='[3, 4]', status='done'),
            r18_illust, safe_illust,
        ])
        safe_commit(clean_db)

        resp = client.get('/api/cache/items?tag=r18')
        assert resp.status_code == 200
        data = resp.get_json()
        assert [r['pixiv_id'] for r in data['results']] == [4]
        assert data['filtered_total'] == 1

        # 显式 r18=all 时包含 R18
        resp = client.get('/api/cache/items?tag=r18&r18=all')
        assert resp.status_code == 200
        data = resp.get_json()
        assert [r['pixiv_id'] for r in data['results']] == [3, 4]
        assert data['filtered_total'] == 2

        # ③ 回归：全局屏蔽标签应在缓存页生效（与搜索/图库一致）
        blocked_a = Illust(pixiv_id=5, title='a', prefetch_source=1)
        blocked_a.tags_list = ['猫']
        blocked_b = Illust(pixiv_id=6, title='b', prefetch_source=1)
        blocked_b.tags_list = ['猫', 'R-18']
        clean_db.add_all([
            SearchCache(tag='blk', illust_ids='[5, 6]', status='done'),
            blocked_a, blocked_b,
            BlockedTag(tag='猫'),
        ])
        safe_commit(clean_db)

        resp = client.get('/api/cache/items?tag=blk&r18=all')
        assert resp.status_code == 200
        data = resp.get_json()
        # 两个作品都含被屏蔽标签'猫' → 全部被过滤
        assert data['results'] == []
        assert data['filtered_total'] == 0


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
