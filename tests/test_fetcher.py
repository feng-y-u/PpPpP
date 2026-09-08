from unittest.mock import patch

import os
import time
import json
import threading
from datetime import datetime, timezone, timedelta

import pytest
import requests

import fetcher
from config import ITEMS_PER_PAGE, PER_PAGE
from models import BlockedTag, Illust


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
             patch('fetcher.build_pixiv_session', side_effect=RuntimeError('no net')):
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


class TestDetailRetryPolicy:
    """详情拉取的重试必须分类：连接错误 fail fast，限流才退避重试。

    回归背景：urllib3 的 Retry 与应用层的 DETAIL_MAX_RETRIES 曾同时放开，
    把 10s 的连接超时放大成 62s（3 次 attempt × 2 次连接 × 10s ＋ 退避），
    断网时详情页要干等一分钟才降级为"无原图"。
    """

    class _FakeSession:
        def __init__(self, exc):
            self.exc = exc
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            raise self.exc

    @staticmethod
    def _http_error(status):
        resp = requests.Response()
        resp.status_code = status
        return requests.HTTPError(response=resp)

    def _run(self, monkeypatch, exc, return_dead=False):
        # 旁路令牌桶，让用例只测重试语义、不受全局限速器残留状态影响
        monkeypatch.setattr(fetcher, '_total_limiter', fetcher._TokenBucket(6000))
        session = self._FakeSession(exc)
        with patch('fetcher.time.sleep') as mock_sleep:
            try:
                result = fetcher._get_illust_detail(
                    session, 123, limiter=fetcher._TokenBucket(6000),
                    return_dead=return_dead)
            except fetcher.PixivAuthError as e:
                result = e
        return session.calls, mock_sleep, result

    def test_connect_error_fails_fast(self, monkeypatch):
        """连接类错误不重试：重试几乎必然重复失败，每次还要空等满超时。"""
        calls, mock_sleep, result = self._run(
            monkeypatch, requests.ConnectTimeout('connect timeout'))
        assert result is None
        assert calls == 1
        mock_sleep.assert_not_called()

    def test_rate_limit_retries_with_backoff(self, monkeypatch):
        """429 限流是暂时性的，应退避后重试 DETAIL_MAX_RETRIES 次。"""
        calls, mock_sleep, result = self._run(monkeypatch, self._http_error(429))
        assert result is None
        assert calls == fetcher.DETAIL_MAX_RETRIES + 1
        assert mock_sleep.call_count == fetcher.DETAIL_MAX_RETRIES

    def test_401_raises_auth_error_immediately(self, monkeypatch):
        """认证失效重试无意义，必须直接上报 PixivAuthError。"""
        calls, mock_sleep, result = self._run(monkeypatch, self._http_error(401))
        assert isinstance(result, fetcher.PixivAuthError)
        assert calls == 1
        mock_sleep.assert_not_called()

    # ── 永久失败（404 / 删除类报错）→ 哨兵 / 立即返回 ──

    class _StaticSession:
        """get() 返回固定响应（用于 404 与 error:true JSON 分支）。"""

        def __init__(self, resp):
            self.resp = resp
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return self.resp

    @staticmethod
    def _http_resp(status):
        resp = requests.Response()
        resp.status_code = status
        resp._content = b'{}'
        return resp

    @staticmethod
    def _json_resp(payload):
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps(payload).encode()
        return resp

    def _run_static(self, monkeypatch, resp, return_dead=False):
        monkeypatch.setattr(fetcher, '_total_limiter', fetcher._TokenBucket(6000))
        session = self._StaticSession(resp)
        with patch('fetcher.time.sleep') as mock_sleep:
            result = fetcher._get_illust_detail(
                session, 123, limiter=fetcher._TokenBucket(6000),
                return_dead=return_dead)
        return session.calls, mock_sleep, result

    def test_404_returns_none_immediately(self, monkeypatch):
        """404 = 确定性永久失败：默认调用方收到 None，且不重试不退避。"""
        calls, mock_sleep, result = self._run_static(
            monkeypatch, self._http_resp(404))
        assert result is None
        assert calls == 1
        mock_sleep.assert_not_called()

    def test_404_returns_dead_sentinel_when_requested(self, monkeypatch):
        """return_dead=True 时 404 → DEAD_DETAIL 哨兵（供刷新路径直接出清）。"""
        calls, mock_sleep, result = self._run_static(
            monkeypatch, self._http_resp(404), return_dead=True)
        assert result is fetcher.DEAD_DETAIL
        assert calls == 1
        mock_sleep.assert_not_called()

    def test_deleted_message_returns_dead_sentinel(self, monkeypatch):
        """error:true 且 message 命中删除类关键词 → DEAD_DETAIL。"""
        resp = self._json_resp({'error': True, 'message': '作品已被删除，或作品ID不正确。'})
        _calls, _sleep, result = self._run_static(monkeypatch, resp, return_dead=True)
        assert result is fetcher.DEAD_DETAIL

    def test_age_check_message_is_transient(self, monkeypatch):
        """权限类 message（年龄确认）不判死 → None（暂时性，走退避重试）。"""
        resp = self._json_resp({'error': True, 'message': '年齢確認が必要です。'})
        _calls, _sleep, result = self._run_static(monkeypatch, resp, return_dead=True)
        assert result is None

    # ── 全局性暂时失败（限流 / 连接错误）→ 哨兵，供刷新侧熔断 ──

    def test_rate_limit_exhausted_returns_global_sentinel(self, monkeypatch):
        """403 重试耗尽 = 全局性失败：刷新路径收到哨兵以便中止本轮。"""
        calls, _sleep, result = self._run(
            monkeypatch, self._http_error(403), return_dead=True)
        assert result is fetcher.RETRYABLE_GLOBAL_DETAIL
        assert calls == fetcher.DETAIL_MAX_RETRIES + 1

    def test_connect_error_returns_global_sentinel_when_requested(self, monkeypatch):
        """连接错误同样是全局性失败（网络/代理问题，不是单个作品的问题）。"""
        calls, mock_sleep, result = self._run(
            monkeypatch, requests.ConnectTimeout('connect timeout'), return_dead=True)
        assert result is fetcher.RETRYABLE_GLOBAL_DETAIL
        assert calls == 1
        mock_sleep.assert_not_called()

    def test_server_error_exhausted_returns_none_even_with_return_dead(self, monkeypatch):
        """5xx 等非限流失败仍按单作品暂时性失败处理（写退避，不触发熔断）。"""
        _calls, _sleep, result = self._run(
            monkeypatch, self._http_error(500), return_dead=True)
        assert result is None

    # ── 未识别报错采样（供设置页核对删除关键词清单）──

    def test_unmatched_error_message_recorded(self, monkeypatch):
        monkeypatch.setattr(fetcher, '_detail_error_samples', {})
        resp = self._json_resp({'error': True, 'message': '謎のエラー'})
        self._run_static(monkeypatch, resp, return_dead=True)
        assert fetcher.get_detail_error_samples() == {'謎のエラー': 1}

    def test_unmatched_error_message_counted_once_per_message(self, monkeypatch):
        monkeypatch.setattr(fetcher, '_detail_error_samples', {})
        resp = self._json_resp({'error': True, 'message': '謎のエラー'})
        self._run_static(monkeypatch, resp, return_dead=True)
        self._run_static(monkeypatch, resp, return_dead=True)
        assert fetcher.get_detail_error_samples() == {'謎のエラー': 2}

    def test_permanent_and_auth_messages_not_sampled(self, monkeypatch):
        """已判死/认证类报文不进样本（前者已处理，后者另有上报路径）。"""
        monkeypatch.setattr(fetcher, '_detail_error_samples', {})
        dead = self._json_resp({'error': True, 'message': '作品已被删除'})
        self._run_static(monkeypatch, dead, return_dead=True)
        assert fetcher.get_detail_error_samples() == {}

    def test_session_does_not_retry_connect_errors(self, monkeypatch):
        """传输层同样不能重试连接错误（Retry(connect=0)），否则又叠回一层。"""
        monkeypatch.setattr(fetcher, '_load_cookie', lambda: None)
        monkeypatch.setattr(fetcher, '_cookie_value', 'test')
        session = fetcher.build_pixiv_session()
        retry = session.get_adapter('https://www.pixiv.net').max_retries
        assert retry.connect == 0
        assert retry.total == 1


class TestPooledSession:
    """图片代理与并发详情共用的线程内连接池。

    回归背景：`/thumb` 与 `_fetch_details_parallel` 曾对每张图 / 每个作品都新建
    Session 再 close()，等于每次请求都重做 TCP + TLS 握手（实测 30 次请求 =
    30 条连接，复用后 = 1 条），是图库首屏、灯箱与搜索变慢的主因。
    """

    @pytest.fixture(autouse=True)
    def _isolate_cookie(self, monkeypatch, tmp_path):
        cookie = tmp_path / 'cookies.txt'
        cookie.write_text('PHPSESSID=test-cookie\n')
        monkeypatch.setattr(fetcher, 'COOKIE_PATH', str(cookie))
        monkeypatch.setattr(fetcher, '_cookie_mtime', 0)
        monkeypatch.setattr(fetcher, '_cookie_value', '')
        fetcher.reset_pooled_session()
        yield
        fetcher.reset_pooled_session()

    def test_same_thread_reuses_session(self):
        """同线程跨请求复用同一个 Session，即复用同一条 keep-alive 连接。"""
        assert fetcher.get_pooled_session() is fetcher.get_pooled_session()

    def test_different_threads_get_own_session(self):
        """不跨线程共享：requests.Session 不保证线程安全。"""
        main_session = fetcher.get_pooled_session()
        box = []
        t = threading.Thread(target=lambda: box.append(fetcher.get_pooled_session()))
        t.start()
        t.join()
        assert box, '子线程应拿到自己的 session'
        assert box[0] is not main_session
        box[0].close()

    def test_cookie_change_rebuilds_session(self):
        """设置页改写 cookies.txt 后必须立即生效，不能沿用旧连接的旧 Cookie。"""
        first = fetcher.get_pooled_session()
        os.utime(fetcher.COOKIE_PATH, (time.time() + 60, time.time() + 60))
        assert fetcher.get_pooled_session() is not first

    def test_reset_drops_pool(self):
        """复用连接被对端关闭后，reset 必须让下次取到全新的 Session。"""
        first = fetcher.get_pooled_session()
        fetcher.reset_pooled_session()
        assert fetcher.get_pooled_session() is not first


class TestFetchFollowingR18Filter:
    """关注列表 R18 过滤：safe 模式必须按标签过滤 R-18/R-18G。

    回归背景：fetch_following 曾只依赖 Pixiv follow_latest 的 mode 参数；
    账号开启 R18 显示后该接口 safe/all 可能返回相同结果，"隐藏R18"失效。
    本地 hide_r18 按标签兜底过滤（与搜索路径一致）。
    """

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _FakeSession:
        def __init__(self, payload):
            self._payload = payload

        def get(self, *args, **kwargs):
            return TestFetchFollowingR18Filter._FakeResponse(self._payload)

    @staticmethod
    def _payload(items):
        return {
            'error': False,
            'body': {
                'thumbnails': {'illust': items},
                'page': {'isLastPage': True},
            },
        }

    def _run(self, monkeypatch, r18_mode, items):
        fetcher._SEARCH_CACHE.clear()
        session = self._FakeSession(self._payload(items))
        monkeypatch.setattr(fetcher, 'build_pixiv_session', lambda: session)
        with patch('fetcher._kick_background_fill'):
            return fetcher.fetch_following(1, r18_mode=r18_mode)

    def test_safe_filters_r18_by_tag(self, clean_db, monkeypatch):
        results, has_more = self._run(
            monkeypatch, 'safe',
            [_item(6001, tags=['少女']), _item(6002, tags=['R-18', 'original'])],
        )
        assert [r['pixiv_id'] for r in results] == [6001]
        assert has_more is False

    def test_safe_filters_r18g(self, clean_db, monkeypatch):
        results, _ = self._run(monkeypatch, 'safe', [_item(6003, tags=['R-18G'])])
        assert results == []

    def test_all_keeps_r18(self, clean_db, monkeypatch):
        results, _ = self._run(
            monkeypatch, 'all',
            [_item(6004, tags=['少女']), _item(6005, tags=['R-18'])],
        )
        assert [r['pixiv_id'] for r in results] == [6004, 6005]



class TestUserSearchPageSize:
    """作者搜索按 ITEMS_PER_PAGE 切片，而不是标签搜索那个 PER_PAGE。

    PER_PAGE=60 是标签搜索从 Pixiv 上游"白拿"的页大小（一次 HTTP 回来 60 条，
    多拿不花额外请求）。作者搜索每多切一条就多一次详情请求，沿用 60 是纯浪费。
    """

    @staticmethod
    def _call(clean_db, mock_ids, mock_fetch, n_works=100, page=1):
        mock_ids.return_value = list(range(1, n_works + 1))
        mock_fetch.return_value = ({}, 0)
        return fetcher.search_by_user('12345', page=page, max_results=ITEMS_PER_PAGE)

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_first_page_slices_items_per_page(self, mock_fetch, mock_ids, mock_sess, clean_db):
        self._call(clean_db, mock_ids, mock_fetch)
        requested = mock_fetch.call_args[0][0]
        assert len(requested) == ITEMS_PER_PAGE
        assert len(requested) < PER_PAGE

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_last_partial_page_is_clipped(self, mock_fetch, mock_ids, mock_sess, clean_db):
        """作品不足一页时按实际剩余切片，不越界。"""
        self._call(clean_db, mock_ids, mock_fetch, n_works=30, page=2)
        assert len(mock_fetch.call_args[0][0]) == 30 - ITEMS_PER_PAGE

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_has_more_uses_new_stride(self, mock_fetch, mock_ids, mock_sess, clean_db):
        """max_pages 跟着新步长算：100 件 / 24 = 5 页。"""
        _, has_more = self._call(clean_db, mock_ids, mock_fetch, n_works=100, page=5)
        assert has_more is False
        _, has_more = self._call(clean_db, mock_ids, mock_fetch, n_works=100, page=4)
        assert has_more is True


class TestUserSearchResultCache:
    """作者搜索的结果缓存。

    标签搜索的缓存只活 30 秒，那对它够用（成本是 1 次 HTTP）。作者搜索一页要发
    整页详情请求，30 秒会在用户看完这一屏之前就失效，所以用独立的长 TTL。
    """

    @staticmethod
    def _call(mock_fetch, page=1):
        mock_fetch.return_value = ({}, 0)
        return fetcher.search_by_user('12345', page=page, max_results=ITEMS_PER_PAGE)

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_repeat_search_is_served_from_cache(self, mock_fetch, mock_ids, mock_sess, clean_db):
        mock_ids.return_value = list(range(1, 61))
        fetcher.clear_search_cache()
        try:
            self._call(mock_fetch)
            after_first = mock_fetch.call_count
            assert after_first == 1

            self._call(mock_fetch)
            assert mock_fetch.call_count == after_first, '第二次应命中缓存，不再拉详情'
        finally:
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_different_page_is_separate_entry(self, mock_fetch, mock_ids, mock_sess, clean_db):
        mock_ids.return_value = list(range(1, 61))
        fetcher.clear_search_cache()
        try:
            self._call(mock_fetch, page=1)
            self._call(mock_fetch, page=2)
            assert mock_fetch.call_count == 2, '不同页不应共用缓存条目'
        finally:
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_blocked_tag_change_invalidates_cache(self, mock_fetch, mock_ids, mock_sess, clean_db):
        """TTL 长达 10 分钟，屏蔽标签必须计入缓存键，否则改完要等十分钟才见效。"""
        mock_ids.return_value = list(range(1, 61))
        fetcher.clear_search_cache()
        try:
            self._call(mock_fetch)
            assert mock_fetch.call_count == 1

            clean_db.add(BlockedTag(tag='a'))
            clean_db.commit()

            self._call(mock_fetch)
            assert mock_fetch.call_count == 2, '屏蔽标签变了应重新搜索，不能吃旧缓存'
        finally:
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_cache_hit_zeroes_fetch_stats(self, mock_fetch, mock_ids, mock_sess, clean_db):
        """命中缓存时清零统计，否则上次搜索的耗时会张冠李戴到本次。"""
        mock_ids.return_value = list(range(1, 61))
        fetcher.clear_search_cache()
        try:
            self._call(mock_fetch)
            fetcher._last_fetch_stats.update({'detail_fetched': 24, 'seconds': 32.0})
            self._call(mock_fetch)
            assert fetcher.get_last_fetch_stats()['detail_fetched'] == 0
            assert fetcher.get_last_fetch_stats()['seconds'] == 0.0
        finally:
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_empty_profile_not_cached(self, mock_fetch, mock_ids, mock_sess, clean_db):
        """拉不到作品列表（Cookie 失效/网络故障）不缓存，否则刷新也一直空。"""
        mock_ids.return_value = []
        fetcher.clear_search_cache()
        try:
            assert self._call(mock_fetch) == ([], False)
            assert fetcher._SEARCH_CACHE == {}, '空结果不应写入缓存'
            assert mock_fetch.call_count == 0
        finally:
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_budget_exhausted_result_not_cached(self, mock_fetch, mock_ids, mock_sess, clean_db):
        """预算中途耗尽的结果是残缺的，缓存它等于把残缺固化到 TTL 结束。"""
        mock_ids.return_value = list(range(1, 61))
        fetcher.clear_search_cache()
        try:
            fetcher._budget_begin(1)
            fetcher._budget_consume(1)
            assert fetcher.budget_exhausted() is True

            self._call(mock_fetch)
            assert not any(k.startswith('user|q=12345') for k in fetcher._SEARCH_CACHE)
        finally:
            fetcher._budget_end()
            fetcher.clear_search_cache()


class TestSearchDetailBudget:
    """单次搜索的详情拉取总预算。

    early_stop 只数"通过过滤"的条数：筛选严格时一页可能一条都不通过，
    paginated_search 会一直翻页，最坏扫满 _MAX_SCAN_PAGES 页。
    """

    @staticmethod
    def _scanning_fn(calls, cost_per_page):
        def fake_fn(page, remaining=None):
            calls.append(page)
            fetcher._budget_consume(cost_per_page)
            return ([], True)   # 一页都没通过过滤，永远凑不满
        return fake_fn

    def test_unlimited_by_default(self):
        """不传 detail_budget 时行为不变 —— 标签/发现/关注路径不受影响。"""
        calls = []
        fetcher.paginated_search(
            self._scanning_fn(calls, cost_per_page=5), {'type': 'tag'},
            items_per_page=24, cursor_data=None)
        assert len(calls) == fetcher._MAX_SCAN_PAGES

    def test_budget_stops_scanning(self):
        calls = []
        fetcher.paginated_search(
            self._scanning_fn(calls, cost_per_page=12), {'type': 'user'},
            items_per_page=24, cursor_data=None, detail_budget=24)
        assert calls == [1, 2], '预算 24 条、每页耗 12 条 → 只应扫两页'

    def test_budget_not_consumed_by_productive_pages(self):
        """过滤宽松、一页就凑满时预算不该被触发。"""
        calls = []

        def fake_fn(page, remaining=None):
            calls.append(page)
            return ([{'id': str(1000 + page), 'bookmarkCount': 999}], True)

        results, _cursor, has_more = fetcher.paginated_search(
            fake_fn, {'type': 'user'}, items_per_page=1, cursor_data=None,
            detail_budget=2)
        assert results and len(calls) == 1
        assert has_more is True

    def test_budget_state_does_not_leak_out(self):
        fetcher.paginated_search(
            self._scanning_fn([], cost_per_page=100), {'type': 'user'},
            items_per_page=24, cursor_data=None, detail_budget=24)
        assert fetcher.budget_exhausted() is False, '预算必须随搜索结束而释放'

    def test_budget_is_thread_local(self):
        """并发的两次搜索不能互相污染预算额度。"""
        seen = {}

        def run():
            fetcher._budget_begin(1)
            fetcher._budget_consume(1)
            seen['exhausted_in_thread'] = fetcher.budget_exhausted()
            fetcher._budget_end()

        t = threading.Thread(target=run)
        t.start()
        t.join()
        assert seen['exhausted_in_thread'] is True
        assert fetcher.budget_exhausted() is False, '主线程不应看到子线程的预算'


class TestSearchCancellation:
    """搜索任务取消：用户改条件重搜时中止在途搜索（routes_search 提交新任务
    时把旧任务的取消事件置位，fetcher 在检查点抛 SearchCancelledError）。
    """

    def test_cancel_before_first_page_raises(self):
        """提交前就置位（用户手速快于线程启动）：一页都不拉。"""
        ev = threading.Event()
        ev.set()
        fetcher._cancel_begin(ev.is_set)
        try:
            with pytest.raises(fetcher.SearchCancelledError):
                fetcher.paginated_search(
                    lambda page, remaining=None: ([], True), {'type': 'user'}, 24)
        finally:
            fetcher._cancel_end()

    def test_cancel_between_pages_raises(self):
        """第 2 页拉取期间取消：第 2 页已入库（search_fn 内部提交），此后中止。"""
        ev = threading.Event()
        calls = []

        def fn(page, remaining=None):
            calls.append(page)
            if page >= 2:
                ev.set()
            return ([{'pixiv_id': 100 + page, 'bookmarkCount': 1}], True)

        fetcher._cancel_begin(ev.is_set)
        try:
            with pytest.raises(fetcher.SearchCancelledError):
                fetcher.paginated_search(fn, {'type': 'user'}, 24)
        finally:
            fetcher._cancel_end()
        assert calls == [1, 2], '第 1 页正常拉取，第 2 页后中止，不应有第 3 页'

    def test_no_cancel_state_never_cancelled(self):
        """预取/后台补全线程没有取消状态：恒为未取消，行为不受影响。"""
        assert fetcher._cancelled() is False
        fetcher._cancel_begin(None)
        try:
            assert fetcher._cancelled() is False
        finally:
            fetcher._cancel_end()

    def test_cancel_is_thread_local(self):
        """并发的两个搜索任务各自持各自的取消事件，互不可见。"""
        ev = threading.Event()
        ev.set()
        seen = {}

        def child():
            fetcher._cancel_begin(ev.is_set)
            seen['child'] = fetcher._cancelled()
            fetcher._cancel_end()

        t = threading.Thread(target=child)
        t.start()
        t.join()
        assert seen['child'] is True
        assert fetcher._cancelled() is False, '主线程不应看到子线程的取消状态'

    def test_cancelled_fetch_skips_all_requests(self):
        """取消后再进 _fetch_details_parallel：一个请求都不发、不计 attempted。"""
        ev = threading.Event()
        ev.set()
        with patch('fetcher._get_illust_detail') as mock_detail:
            fetcher._cancel_begin(ev.is_set)
            try:
                details, attempted = fetcher._fetch_details_parallel([1, 2, 3, 4, 5])
            finally:
                fetcher._cancel_end()
        assert details == {}
        assert attempted == 0
        mock_detail.assert_not_called()

    def test_cancel_keeps_in_flight_results(self):
        """在途请求照常处理完并保留（与 early_stop 同款语义：已付出的请求
        结果入库，下次同条件搜索命中 existing_map 免重拉）。用 Barrier 保证
        第一批 worker 全部处于在途状态时才触发取消，之后的任务全部跳过。
        """
        ids = list(range(1, 13))
        first_batch = min(fetcher.FETCH_DETAIL_WORKERS, len(ids))
        barrier = threading.Barrier(first_batch)
        ev = threading.Event()

        def fake_detail(session, pixiv_id, limiter=None):
            barrier.wait(timeout=5)
            ev.set()   # 第一批全部在途时用户取消
            return {'id': pixiv_id, 'title': f'p{pixiv_id}'}

        with patch('fetcher._get_illust_detail', side_effect=fake_detail):
            fetcher._cancel_begin(ev.is_set)
            try:
                details, attempted = fetcher._fetch_details_parallel(ids)
            finally:
                fetcher._cancel_end()
        assert len(details) == first_batch
        assert attempted == first_batch, '取消的不计 attempted，在途的才计'
