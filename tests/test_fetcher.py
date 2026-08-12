from unittest.mock import patch

import time
import threading
from datetime import datetime, timezone, timedelta

import fetcher
from models import Illust


class TestDetailRateLimiter:
    def test_global_rate_limit_across_threads(self):
        """3 个并发线程共享限速器时，整体速率被压到配置值。"""
        limiter = fetcher._TokenBucket(rate_per_minute=120)  # 0.5s 间隔
        start = time.time()
        threads = [threading.Thread(target=limiter.wait) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.time() - start
        assert elapsed >= 0.9, f'3 次请求应被限速到约 1.0s，实际 {elapsed:.2f}s'


def _item(pid: int, bookmark_count=None, tags=('a', 'b')):
    item = {
        'id': str(pid),
        'title': 'テスト',
        'userId': 1,
        'userName': 'u',
        'pageCount': 1,
        'url': f'https://i.pximg.net/thumb/{pid}.jpg',
        'updateDate': '2026-01-01T00:00:00+09:00',
        'tags': [{'tag': t} for t in tags],
    }
    if bookmark_count is not None:
        item['bookmarkCount'] = bookmark_count
    return item


class TestProcessItemsBookmarkFill:
    @patch('fetcher._kick_background_fill')
    @patch('fetcher._fetch_details_parallel')
    def test_existing_zero_bookmark_updated_from_item_in_defer_path(
            self, mock_fetch, mock_fill, clean_db):
        """defer + min_bookmarks=0：已有 0 收藏记录应被条目自带 bookmarkCount 更新，
        而不是直接显示 0（fetch_following / 浏览页路径）。"""
        clean_db.add(Illust(pixiv_id=1001, title='old', bookmark_count=0))
        clean_db.commit()
        mock_fetch.return_value = {}

        results = fetcher._process_items(
            clean_db, [_item(1001, 500)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=0,
            defer_details=True,
        )

        assert len(results) == 1
        assert results[0]['bookmark_count'] == 500
        row = clean_db.query(Illust).filter(Illust.pixiv_id == 1001).first()
        assert row.bookmark_count == 500

    @patch('fetcher._kick_background_fill')
    @patch('fetcher._fetch_details_parallel')
    def test_refetch_detail_failure_enqueues_background_fill(
            self, mock_fetch, mock_fill, clean_db):
        """min>0 + 条目无 bookmarkCount + 详情拉取失败：
        记录应排入后台补全队列，而不是被永久静默丢弃。"""
        clean_db.add(Illust(pixiv_id=1002, title='old', bookmark_count=0))
        clean_db.commit()
        mock_fetch.return_value = ({}, 1)

        results = fetcher._process_items(
            clean_db, [_item(1002)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=500,
            defer_details=True,
        )

        assert results == []
        mock_fill.assert_called_once_with([1002])

    @patch('fetcher._kick_background_fill')
    @patch('fetcher._fetch_details_parallel')
    def test_refetch_success_updates_bookmark(
            self, mock_fetch, mock_fill, clean_db):
        """min>0 + 条目无 bookmarkCount：详情拉取成功时更新 DB 并显示真实值。"""
        clean_db.add(Illust(pixiv_id=1003, title='old', bookmark_count=0))
        clean_db.commit()
        mock_fetch.return_value = ({1003: {
            'title': 't', 'user_id': 1, 'user_name': 'u', 'page_count': 1,
            'bookmark_count': 900, 'thumb_url': 'https://x.jpg',
            'upload_date': '2026-01-01T00:00:00+09:00',
            'original_urls': ['https://i.pximg.net/1003_p0.jpg'],
            'tags': ['a'],
        }}, 1)

        results = fetcher._process_items(
            clean_db, [_item(1003)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=500,
            defer_details=True,
        )

        assert len(results) == 1
        assert results[0]['bookmark_count'] == 900
        row = clean_db.query(Illust).filter(Illust.pixiv_id == 1003).first()
        assert row.bookmark_count == 900

    @patch('fetcher._kick_background_fill')
    @patch('fetcher._fetch_details_parallel')
    def test_new_item_defer_writes_zero_and_background_fills(self, mock_fetch, mock_fill, clean_db):
        """defer 路径：新记录列表接口无 bookmarkCount（恒缺失），写入 0 并排入后台补全。"""
        mock_fetch.return_value = ({}, 0)

        results = fetcher._process_items(
            clean_db, [_item(1004, 1200)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=0,
            defer_details=True,
        )

        assert len(results) == 1
        assert results[0]['bookmark_count'] == 0
        mock_fill.assert_called_once_with([1004])
        row = clean_db.query(Illust).filter(Illust.pixiv_id == 1004).first()
        assert row.bookmark_count == 0

    @patch('fetcher._fetch_details_parallel')
    def test_max_results_stops_after_enough_passed(self, mock_fetch, clean_db):
        """max_results>0 时，_process_items 传入的 early_stop 应按过滤条件计数。"""
        d500 = {'title': 't', 'user_id': 1, 'user_name': 'u', 'page_count': 1,
                'bookmark_count': 500, 'thumb_url': 'https://x.jpg',
                'upload_date': '2026-01-01T00:00:00+09:00',
                'original_urls': ['https://i.pximg.net/2001_p0.jpg'],
                'tags': ['a']}
        d300 = {**d500, 'bookmark_count': 300}
        mock_fetch.return_value = ({2001: d500, 2002: d300, 2003: d500, 2004: d500}, 4)

        items = [_item(2001), _item(2002), _item(2003), _item(2004)]
        results = fetcher._process_items(
            clean_db, items,
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=400,
            defer_details=False,
            max_results=2,
        )

        early_stop = mock_fetch.call_args.kwargs.get('early_stop')
        assert early_stop is not None
        # 通过过滤的详情才计数：2 个通过后返回 True（提前终止）
        assert early_stop(d500) is False
        assert early_stop(d300) is False   # 不过滤（收藏 300 < 400）不计入
        assert early_stop(d500) is True
        # mock 不做早停，过滤后 3 条照常处理
        assert len(results) == 3

    @patch('fetcher._fetch_details_parallel')
    def test_max_results_zero_does_not_pass_early_stop(self, mock_fetch, clean_db):
        """max_results=0（默认，批量下载）时不传 early_stop，全量拉取。"""
        mock_fetch.return_value = ({}, 0)
        fetcher._process_items(
            clean_db, [_item(3001, 500)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=0,
            defer_details=False,
        )
        assert mock_fetch.call_args.kwargs.get('early_stop') is None

    def test_early_stop_returns_immediately(self):
        """early_stop 返回 True 后，_fetch_details_parallel 取消未启动的拉取；
        已启动的请求仍处理完（不 break 丢弃），返回其全部结果。"""
        with patch('fetcher._get_illust_detail', return_value=None), \
             patch('fetcher._build_session', side_effect=RuntimeError('no net')):
            results, attempted = fetcher._fetch_details_parallel(
                [4001, 4002, 4003, 4004],
                early_stop=lambda detail: True,
            )
            # shutdown(wait=False) 不等待后台线程；sleep 让残留线程在 patch
            # 恢复前结束，避免其调用真实 _get_illust_detail 联网
            time.sleep(0.5)
        assert results == {}
        assert attempted >= 1  # 已启动的请求（全部失败）均被处理，未启动的已取消

    def test_fetch_stats_accurate_failure_count(self, clean_db):
        """统计准确性：早停取消的请求不计入失败；仅实际发起的请求统计失败数。"""
        def _fake_detail(session, pid, limiter=None):
            if pid in (6002, 6003):  # 两个失败
                return None
            return {
                'title': f't{pid}', 'user_id': 1, 'user_name': 'u', 'page_count': 1,
                'bookmark_count': 500, 'thumb_url': 'https://x.jpg',
                'upload_date': '2026-01-01T00:00:00+09:00',
                'original_urls': [f'https://i.pximg.net/{pid}_p0.jpg'],
                'tags': ['a'],
            }

        with patch('fetcher._get_illust_detail', side_effect=_fake_detail):
            results = fetcher._process_items(
                clean_db,
                [_item(6001, 500), _item(6002, 300), _item(6003, 300),
                 _item(6004, 500), _item(6005, 500)],
                id_extractor=lambda item: int(item['id']),
                illust_factory=fetcher._illust_from_item,
                blocked=set(),
                min_bookmarks=400,
                defer_details=False,
                max_results=3,
            )
            time.sleep(0.5)
        stats = fetcher.get_last_fetch_stats()
        assert stats['detail_fetched'] >= 3
        assert stats['detail_failed'] == 2
        assert len(results) == 3

    def test_early_stop_fires_after_enough_passed(self, clean_db):
        """流式过滤端到端：真实 _fetch_details_parallel 下，凑够 max_results 条
        通过过滤的结果后取消未启动的拉取，但已启动的照常返回（不丢弃）。"""
        def _fake_detail(session, pid, limiter=None):
            return {
                'title': f't{pid}', 'user_id': 1, 'user_name': 'u', 'page_count': 1,
                'bookmark_count': 500, 'thumb_url': 'https://x.jpg',
                'upload_date': '2026-01-01T00:00:00+09:00',
                'original_urls': [f'https://i.pximg.net/{pid}_p0.jpg'],
                'tags': ['a'],
            }

        with patch('fetcher._get_illust_detail', side_effect=_fake_detail):
            results = fetcher._process_items(
                clean_db,
                [_item(5001, 500), _item(5002, 300), _item(5003, 500), _item(5004, 500)],
                id_extractor=lambda item: int(item['id']),
                illust_factory=fetcher._illust_from_item,
                blocked=set(),
                min_bookmarks=400,
                defer_details=False,
                max_results=2,
            )
            # 等后台线程在 patch 恢复前结束
            time.sleep(0.5)
        # 至少凑够 2 条；已启动的全部返回（4 个 worker 全部启动时为 4 条）
        assert 2 <= len(results) <= 4
        assert all(r['bookmark_count'] == 500 for r in results)


class TestPaginatedSearchRemaining:
    def test_remaining_decreases_across_pages(self):
        """跨页累计：paginated_search 每页把"还需收集的条数"传给 search_fn。"""
        calls = []

        def fake_fn(page, remaining=None):
            calls.append((page, remaining))
            return ([{'id': str(1000 + page), 'bookmarkCount': 999}], True)

        results, cursor, has_more = fetcher.paginated_search(
            fake_fn, {'type': 'tag'}, items_per_page=3, cursor_data=None)

        assert len(results) == 3
        assert calls == [(1, 3), (2, 2), (3, 1)]
        assert has_more is True


class TestUserProfileCache:
    def test_second_call_hits_cache(self):
        """同一画师两次搜索：profile 只拉一次，翻页直接切片。"""
        from unittest.mock import Mock
        fetcher._USER_PROFILE_CACHE.clear()
        try:
            mock_session = Mock()
            resp = Mock()
            resp.raise_for_status = lambda: None
            resp.json.return_value = {'error': False,
                                      'body': {'illusts': {'1': {}, '2': {}, '3': {}}}}
            mock_session.get.return_value = resp

            ids1 = fetcher._get_user_profile_ids(mock_session, '12345')
            ids2 = fetcher._get_user_profile_ids(mock_session, '12345')
            assert ids1 == ids2 == [3, 2, 1]
            assert mock_session.get.call_count == 1
        finally:
            fetcher._USER_PROFILE_CACHE.clear()


class TestBookmarkStaleness:
    def _old_illust(self, clean_db, pid, bookmark_count, days_ago):
        illust = Illust(pixiv_id=pid, title='old', bookmark_count=bookmark_count)
        illust.bookmark_updated_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
        clean_db.add(illust)
        clean_db.commit()
        return illust

    def test_null_updated_at_not_stale(self):
        illust = Illust(bookmark_count=500)
        assert fetcher._is_bookmark_stale(illust) is False

    def test_recent_updated_at_not_stale(self):
        illust = Illust(bookmark_count=500)
        illust.bookmark_updated_at = datetime.now(timezone.utc)
        assert fetcher._is_bookmark_stale(illust) is False

    def test_old_updated_at_stale(self):
        illust = Illust(bookmark_count=500)
        illust.bookmark_updated_at = datetime.now(timezone.utc) - timedelta(days=8)
        assert fetcher._is_bookmark_stale(illust) is True

    @patch('fetcher._kick_background_fill')
    def test_stale_record_enqueued_for_background_refresh(self, mock_fill, clean_db):
        """defer 路径：收藏数过期的记录应排入后台补全刷新。"""
        self._old_illust(clean_db, 7001, 500, days_ago=8)

        results = fetcher._process_items(
            clean_db, [_item(7001)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=0,
            defer_details=True,
        )

        assert len(results) == 1
        assert results[0]['bookmark_count'] == 500
        mock_fill.assert_called_once_with([7001])

    @patch('fetcher._fetch_details_parallel')
    def test_stale_record_sync_refetch_and_timestamp_update(self, mock_fetch, clean_db):
        """min>0：收藏数过期的记录同步拉详情刷新，并更新时间戳。"""
        self._old_illust(clean_db, 7002, 500, days_ago=8)
        mock_fetch.return_value = ({7002: {
            'title': 't', 'user_id': 1, 'user_name': 'u', 'page_count': 1,
            'bookmark_count': 800, 'thumb_url': 'https://x.jpg',
            'upload_date': '2026-01-01T00:00:00+09:00',
            'original_urls': ['https://i.pximg.net/7002_p0.jpg'],
            'tags': ['a'],
        }}, 1)

        results = fetcher._process_items(
            clean_db, [_item(7002)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=600,
            defer_details=True,
        )

        assert len(results) == 1
        assert results[0]['bookmark_count'] == 800
        row = clean_db.query(Illust).filter(Illust.pixiv_id == 7002).first()
        assert row.bookmark_count == 800
        assert row.bookmark_updated_at is not None
        updated = row.bookmark_updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        assert (datetime.now(timezone.utc) - updated).total_seconds() < 60

    @patch('fetcher._fetch_details_parallel')
    def test_non_defer_new_record_stamps_timestamp(self, mock_fetch, clean_db):
        """非 defer 插入（同步拉详情成功）：新记录应带 bookmark_updated_at，
        否则 7 天 TTL 刷新永远不会作用于它。"""
        mock_fetch.return_value = ({8001: {
            'title': 't', 'user_id': 1, 'user_name': 'u', 'page_count': 1,
            'bookmark_count': 900, 'thumb_url': 'https://x.jpg',
            'upload_date': '2026-01-01T00:00:00+09:00',
            'original_urls': ['https://i.pximg.net/8001_p0.jpg'],
            'tags': ['a'],
        }}, 1)

        results = fetcher._process_items(
            clean_db, [_item(8001)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=0,
            defer_details=False,
        )

        assert len(results) == 1
        row = clean_db.query(Illust).filter(Illust.pixiv_id == 8001).first()
        assert row.bookmark_updated_at is not None

    @patch('fetcher._kick_background_fill')
    def test_user_search_stale_record_gets_background_refresh(self, mock_fill, clean_db):
        """search_by_user 场景（非 defer + min=0）：收藏数过期的记录应排入后台补全。"""
        self._old_illust(clean_db, 8002, 500, days_ago=8)

        results = fetcher._process_items(
            clean_db, [_item(8002)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=0,
            defer_details=False,
        )

        assert len(results) == 1
        mock_fill.assert_called_once_with([8002])

    @patch('fetcher._kick_background_fill')
    @patch('fetcher._fetch_details_parallel')
    def test_non_defer_refetch_failure_still_background_filled(self, mock_fetch, mock_fill, clean_db):
        """非 defer（批量下载等 min>0 场景）：同步拉详情失败的记录仍应排入后台补全，
        不再静默丢弃。"""
        self._old_illust(clean_db, 8003, 0, days_ago=0)
        mock_fetch.return_value = ({}, 1)  # 拉取失败

        results = fetcher._process_items(
            clean_db, [_item(8003)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=500,
            defer_details=False,
        )

        assert results == []
        mock_fill.assert_called_once_with([8003])

