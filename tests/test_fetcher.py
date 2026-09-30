from unittest.mock import patch

import os
import time
import json
import logging
import threading
from datetime import datetime, timezone, timedelta

import pytest
import requests

import fetcher
import pixiv_client
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


def _detail_batch(details=None, attempted=0, *, rate_limited=False):
    """构造 `_fetch_details_parallel` 的返回对象（替代 tuple 的显式类型）。"""
    return fetcher._DetailFetchBatch(dict(details or {}), attempted, rate_limited=rate_limited)


def _detail(pid: int, bookmark_count: int = 600, tags=('a',)) -> dict:
    """一条规范化详情（`pixiv_client.parse_illust_detail` 的输出形状）。"""
    return {
        'title': f't{pid}', 'user_id': 1, 'user_name': 'u', 'page_count': 1,
        'bookmark_count': bookmark_count, 'thumb_url': f'https://x/{pid}.jpg',
        'upload_date': '2026-01-01T00:00:00+09:00',
        'original_urls': [f'https://i.pximg.net/{pid}_p0.jpg'],
        'tags': list(tags),
    }


class _FakeClock:
    """可手动推进的假时钟。

    只替换 `fetcher.time`（模块全局），真正的 `time` 模块不受影响 —— 真实 sleep /
    其它线程的计时照常工作。缓存的 TTL 判定全部走 `time.time()`，替换它就能在
    不睡眠的前提下验证"599 秒还算命中、601 秒已失效"。
    """

    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def _cookie_file(tmp_path, monkeypatch):
    """给走**真实** `_fetch_details_parallel` 的用例一个临时 Cookie 文件。

    那条路径会经 `get_pooled_session()` → `build_pixiv_session()` → `_load_cookie()` 读
    `COOKIE_PATH`，而仓库根不一定有 `cookies.txt`（干净 checkout / 别人机器上就没有），
    缺文件会抛 `FileNotFoundError` 把拉取打断 —— 于是这些用例此前**默默依赖开发者本机
    存在真实 Cookie**。用一个临时文件解耦，顺带保证跑测试不会读/写真实凭据。
    """
    cookie = tmp_path / 'cookies.txt'
    cookie.write_text('PHPSESSID=test-token\n', encoding='utf-8')
    monkeypatch.setattr(pixiv_client, 'COOKIE_PATH', str(cookie))
    return cookie


@pytest.fixture(autouse=True)
def _fresh_detail_gate(monkeypatch):
    """每个用例一条全新的详情熔断闸——闸是有状态的，开路状态会**跨用例泄漏**。

    `fetch_illust_detail` 读的是 `pixiv_client` 的模块级单例：某个用例把它开到 60 秒
    冷却后，后续用例的详情调用会直接收到 `PixivRateLimitedError`，红灯与自身断言无关、
    顺序一变又"自己好了"。要造"闸已开路"的场景用 `_open_detail_gate()` 自行替换实例。
    """
    monkeypatch.setattr(pixiv_client, '_detail_gate', pixiv_client._DetailRequestGate())


class TestProcessItemsBookmarkFill:
    @patch('fetcher._kick_background_fill')
    @patch('fetcher._fetch_details_parallel')
    def test_existing_zero_bookmark_updated_from_item_in_defer_path(
            self, mock_fetch, mock_fill, clean_db):
        """defer + min_bookmarks=0：已有 0 收藏记录应被条目自带 bookmarkCount 更新，
        而不是直接显示 0（fetch_following / 浏览页路径）。"""
        clean_db.add(Illust(pixiv_id=1001, title='old', bookmark_count=0))
        clean_db.commit()
        mock_fetch.return_value = _detail_batch()

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
        mock_fetch.return_value = _detail_batch(attempted=1)

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
        mock_fetch.return_value = _detail_batch({1003: {
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
        mock_fetch.return_value = _detail_batch()

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
        mock_fetch.return_value = _detail_batch(
            {2001: d500, 2002: d300, 2003: d500, 2004: d500}, 4)

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
        mock_fetch.return_value = _detail_batch()
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
            batch = fetcher._fetch_details_parallel(
                [4001, 4002, 4003, 4004],
                early_stop=lambda detail: True,
            )
            # shutdown(wait=False) 不等待后台线程；sleep 让残留线程在 patch
            # 恢复前结束，避免其调用真实 _get_illust_detail 联网
            time.sleep(0.5)
        assert batch.details == {}
        assert batch.attempted >= 1  # 已启动的请求（全部失败）均被处理，未启动的已取消

    def test_fetch_stats_accurate_failure_count(self, clean_db, _cookie_file):
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

    def test_early_stop_fires_after_enough_passed(self, clean_db, _cookie_file):
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


class TestInsertNewIllusts:
    """INSERT ... ON CONFLICT DO NOTHING：并发/重复 pid 不再炸整批。

    回归：服务器曾出现 `UNIQUE constraint failed: illusts.pixiv_id` ——
    `_process_items` 的查重→拉详情（网络耗时）→INSERT 之间有并发窗口，
    同一 pid 被其他线程或本批重复条目插入两次，普通 flush 撞 UNIQUE 且整批作废。
    """

    def test_conflict_with_existing_row_returns_existing(self, clean_db):
        """已存在 pid（模拟并发线程先入库）→ 静默跳过并返回既有行。"""
        clean_db.add(Illust(pixiv_id=3001, title='existing'))
        clean_db.commit()

        winners = fetcher._insert_new_illusts(clean_db, [
            Illust(pixiv_id=3001, title='dup'),
            Illust(pixiv_id=3002, title='fresh'),
        ])

        assert set(winners) == {3001, 3002}
        assert winners[3001].title == 'existing'   # 赢家是现有行
        clean_db.commit()
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 3002).first() is not None

    def test_duplicate_within_batch_merges_to_one_row(self, clean_db):
        winners = fetcher._insert_new_illusts(clean_db, [
            Illust(pixiv_id=3003, title='a'),
            Illust(pixiv_id=3003, title='b'),
        ])
        clean_db.commit()
        assert len(winners) == 1
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 3003).count() == 1


class TestProcessItemsDuplicateInsert:
    """同一批输入含重复 pixiv_id 时：结果去重、库内一行、不抛 IntegrityError。"""

    @patch('fetcher._kick_background_fill')
    @patch('fetcher._fetch_details_parallel')
    def test_duplicate_items_in_non_defer_batch(self, mock_fetch, mock_fill, clean_db):
        mock_fetch.return_value = _detail_batch({2001: {'bookmark_count': 100, 'tags': ['a']}}, 1)

        results = fetcher._process_items(
            clean_db, [_item(2001), _item(2001)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=1,
            defer_details=False,
        )

        assert len(results) == 1
        assert results[0]['pixiv_id'] == 2001
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 2001).count() == 1

    @patch('fetcher._kick_background_fill')
    def test_duplicate_items_in_defer_batch(self, mock_fill, clean_db):
        results = fetcher._process_items(
            clean_db, [_item(2002), _item(2002)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=0,
            defer_details=True,
        )

        assert len(results) == 1
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 2002).count() == 1


class TestDetailProgressPublication:
    """详情流水线逐条发布已通过过滤的结果，并把全局限流标成批次状态。

    进度回调必须在**搜索任务自己的线程**里跑 —— 也就是 `_fetch_details_parallel`
    中消费 `as_completed` 的那个 collector 线程（它只是碰巧由调用方线程执行，
    而不是线程池 worker）。理由有两条：① `_cancelled()` 是 threading.local，
    worker 线程读不到任务线程的取消状态；② 回调要发布的是"已确认"的结果，
    与调用方共享同一份事务语义，丢到 worker 里就需要给共享状态另加锁。
    """

    def test_detail_progress_callback_runs_in_collector_thread(self, _cookie_file):
        collector_thread = threading.get_ident()
        first_done = threading.Event()
        seen: list[tuple[int, int, dict | None]] = []

        def fake_detail(session, pixiv_id, limiter=None):
            if pixiv_id == 99:
                first_done.set()            # 99 先完成，另外两条才放行
            elif not first_done.wait(timeout=5):
                raise AssertionError('先完成的详情始终没跑完，用例死锁')
            return {'pixiv_id': pixiv_id, 'title': f'p{pixiv_id}'}

        with patch('fetcher._get_illust_detail', side_effect=fake_detail):
            batch = fetcher._fetch_details_parallel(
                [1, 2, 99],
                on_detail=lambda pid, detail: seen.append(
                    (threading.get_ident(), pid, detail)))

        assert seen, '三条详情都完成了，on_detail 一次都没被调用'
        assert [pid for _tid, pid, _d in seen][0] == 99, '回调顺序 = future 完成顺序'
        assert sorted(pid for _tid, pid, _d in seen) == [1, 2, 99]
        assert all(tid == collector_thread for tid, _pid, _d in seen), \
            'on_detail 必须在消费 as_completed 的 collector 线程回调'
        assert batch.attempted == 3

    def test_process_items_publishes_only_filter_matches(self, clean_db, _cookie_file):
        """result 事件只发通过屏蔽标签/收藏数/R18 的记录；详情失败只发 detail_failed。"""
        def fake_detail(session, pixiv_id, limiter=None):
            if pixiv_id == 15:                              # 详情失败：不该发 result
                return None
            return _detail(
                pixiv_id,
                bookmark_count={11: 600, 12: 100, 13: 600, 14: 600}[pixiv_id],
                tags={11: ['a'], 12: ['a'], 13: ['ng'], 14: ['R-18']}[pixiv_id])

        events: list[dict] = []
        with patch('fetcher._get_illust_detail', side_effect=fake_detail):
            results = fetcher._process_items(
                clean_db, [_item(pid) for pid in (11, 12, 13, 14, 15)],
                id_extractor=lambda item: int(item['id']),
                illust_factory=fetcher._illust_from_item,
                blocked={'ng'}, min_bookmarks=500, hide_r18=True,
                progress=events.append)

        assert [e['pixiv_id'] for e in events if e['type'] == 'examined'] == [11, 12, 13, 14, 15]
        assert [e['pixiv_id'] for e in events if e['type'] == 'detail_failed'] == [15]
        assert [e['result']['pixiv_id'] for e in events if e['type'] == 'result'] == [11], \
            '每个 PID 的 result 只发一次，且只有通过全部过滤的记录才发'
        assert [r['pixiv_id'] for r in results] == [11]
        assert results.rate_limited is False
        published = [e['result'] for e in events if e['type'] == 'result'][0]
        assert published['bookmark_count'] == 600
        assert 'is_favorite' not in published, '本地收藏已移除，结果里不该再出现 is_favorite'

    def test_defer_path_publishes_summary_without_detail_requests(self, clean_db):
        """defer 路径（标签/发现/关注）直接用条目摘要发布，不为进度多拉一次详情。"""
        events: list[dict] = []
        with patch('fetcher._kick_background_fill'), \
             patch('fetcher._fetch_details_parallel') as mock_fetch:
            results = fetcher._process_items(
                clean_db, [_item(31), _item(32, tags=('ng',))],
                id_extractor=lambda item: int(item['id']),
                illust_factory=fetcher._illust_from_item,
                blocked={'ng'}, min_bookmarks=0, defer_details=True,
                progress=events.append)

        mock_fetch.assert_not_called()
        assert [r['pixiv_id'] for r in results] == [31]
        assert [e['pixiv_id'] for e in events if e['type'] == 'examined'] == [31, 32]
        assert [e['result']['pixiv_id'] for e in events if e['type'] == 'result'] == [31]
        assert [e['result']['bookmark_count'] for e in events if e['type'] == 'result'] == [0], \
            '摘要路径的收藏数为 0（列表接口不带 bookmarkCount），由后台补全刷新'

    def test_cancelled_search_does_not_publish_progress(self, clean_db):
        """取消后不再发布旧任务进度，但已通过过滤的作品仍按原事务语义收尾。"""
        clean_db.add(Illust(pixiv_id=41, title='t', bookmark_count=700))
        clean_db.commit()
        events: list[dict] = []
        ev = threading.Event()
        ev.set()
        fetcher._cancel_begin(ev.is_set)
        try:
            with patch('fetcher._kick_background_fill'):   # 这条记录缺原图，会触发后台补全
                results = fetcher._process_items(
                    clean_db, [_item(41)],
                    id_extractor=lambda item: int(item['id']),
                    illust_factory=fetcher._illust_from_item,
                    blocked=set(), min_bookmarks=0, defer_details=False,
                    progress=events.append)
        finally:
            fetcher._cancel_end()

        assert events == [], '旧任务已被新搜索取代，不能再刷它的进度'
        assert [r['pixiv_id'] for r in results] == [41], '入库/返回语义不因取消而改变'

    def test_rate_limited_batch_keeps_completed_details(self, clean_db, _cookie_file):
        """限流（闸开路）截断批次：已完成详情照常保留并入库，批次标 rate_limited。

        被闸拒绝的请求**没有发出**，不能计入 attempted，也不能算成"详情失败"——
        否则限流会被误报成"这些作品不匹配"，已完成的部分也一起丢掉。

        13/14/15 必须等 11/12 都完成才被闸拒绝（用 Event 同步、带超时上限）：否则
        "11/12 已完成"这件事就只是 `FETCH_DETAIL_WORKERS == 5 == len(batch)` 与假实现
        同样便宜带来的巧合 —— 拒绝路径会对每个 future 调 `cancel()`（只对 PENDING 有效），
        工作线程数一变，11/12 就可能在启动前被取消，用例为一个无关原因变红。
        """
        both_done = threading.Event()
        done: set[int] = set()
        done_lock = threading.Lock()

        def fake_detail(session, pixiv_id, limiter=None):
            if pixiv_id in (11, 12):
                with done_lock:
                    done.add(pixiv_id)
                    if done == {11, 12}:
                        both_done.set()
                return _detail(pixiv_id)
            # 闸拒绝的必须是"11/12 已完成之后"才发起的请求
            if not both_done.wait(timeout=5):
                raise AssertionError('11/12 始终没跑完，用例死锁')
            raise fetcher.PixivRateLimitedError('详情熔断闸开路')

        # 抓 `_process_items` 内部那一批，而不是另起一次 `_fetch_details_parallel`：
        # 跑两轮会让 13/14/15 的第二个闸在第二轮一开场就拒绝，11/12 反而被取消。
        batches: list = []
        real_batch = fetcher._fetch_details_parallel

        def spy_batch(pixiv_ids, **kwargs):
            batch = real_batch(pixiv_ids, **kwargs)
            batches.append(batch)
            return batch

        events: list[dict] = []
        with patch('fetcher._get_illust_detail', side_effect=fake_detail), \
             patch('fetcher._fetch_details_parallel', side_effect=spy_batch):
            results = fetcher._process_items(
                clean_db, [_item(pid) for pid in (11, 12, 13, 14, 15)],
                id_extractor=lambda item: int(item['id']),
                illust_factory=fetcher._illust_from_item,
                blocked=set(), min_bookmarks=0, defer_details=False,
                progress=events.append)

        (batch,) = batches
        assert done == {11, 12}, '11/12 必须都已发起并完成，才谈得上"保留已完成部分"'
        assert batch.rate_limited is True
        assert set(batch.details) == {11, 12}
        assert batch.attempted == 2, '只有真正发出的请求才计 attempted'
        assert results.rate_limited is True, '批次限流必须传到 _ProcessedItems'
        assert sorted(r['pixiv_id'] for r in results) == [11, 12], '已完成的详情仍要照常入库/返回'
        assert [e['pixiv_id'] for e in events if e['type'] == 'detail_failed'] == [], \
            '被闸拒绝不是"这件作品详情失败"'

    def test_parallel_detail_propagates_auth_error(self, _cookie_file):
        """PixivAuthError 必须原样抛出，不能被宽泛 except 吞成"详情失败"。

        吞掉的结果是整页静默变空：用户看到 0 条结果，而不是"Cookie 已失效"。
        """
        seen: list[tuple[int, dict | None]] = []

        def fake_detail(session, pixiv_id, limiter=None):
            raise fetcher.PixivAuthError('Pixiv API returned HTTP 401')

        with patch('fetcher._get_illust_detail', side_effect=fake_detail):
            with pytest.raises(fetcher.PixivAuthError):
                fetcher._fetch_details_parallel(
                    [21, 22, 23],
                    on_detail=lambda pid, detail: seen.append((pid, detail)))

        assert seen == [], '认证失效不是"这件作品详情失败"，不能回调成 detail=None'

    def test_paginated_search_propagates_rate_limited(self):
        """限流是全局状态，不是"这页没搜到"：不能落入宽泛 except 当空页完成。"""
        def fake_fn(page, remaining=None):
            raise fetcher.SearchRateLimitedError('详情请求被限流')

        with pytest.raises(fetcher.SearchRateLimitedError):
            fetcher.paginated_search(fake_fn, {'type': 'user'}, 24)

    def test_existing_record_published_before_detail_batch_completes(self, clean_db):
        """已入库且通过全部过滤的记录，必须在**详情批次还在途中**就发布。

        这是作者搜索的典型形态：一页 24 条里命中已入库记录的概率很高，而它不需要
        任何网络请求就能定稿。若只在收尾的统一兜底里发布，它就得排在一整页无关
        作品的详情请求后面（约 30 秒）才出现在前端 —— 渐进显示要消除的正是这种
        等待。这里让详情批次卡在 `threading.Event` 上，另开观察线程在"批次在途"
        那一刻断言 result 事件已经发出（收尾兜底此时根本还没执行）。
        """
        row = Illust(pixiv_id=5001, title='cached', bookmark_count=700)
        row.original_urls_list = ['https://i.pximg.net/5001_p0.jpg']
        clean_db.add(row)
        clean_db.commit()

        events: list[dict] = []
        batch_started = threading.Event()
        batch_release = threading.Event()
        seen_in_flight: list[list[int]] = []
        failures: list[BaseException] = []

        def blocking_batch(pixiv_ids, **kwargs):
            """替身详情批次：卡住不放，模拟作者搜索那约 30 秒。"""
            batch_started.set()
            if not batch_release.wait(timeout=10):
                raise AssertionError('详情批次始终没被放行，用例死锁')
            return _detail_batch()

        def observer():
            try:
                assert batch_started.wait(timeout=10), '详情批次始终没开始'
                seen_in_flight.append(
                    [e['result']['pixiv_id'] for e in events if e['type'] == 'result'])
            except BaseException as e:      # 线程里的断言不会让用例失败，带回主线程
                failures.append(e)
            finally:
                batch_release.set()

        with patch('fetcher._fetch_details_parallel', side_effect=blocking_batch), \
             patch('fetcher._kick_background_fill'):
            watcher = threading.Thread(target=observer)
            watcher.start()
            try:
                fetcher._process_items(
                    clean_db, [5001, 5002],
                    id_extractor=lambda pid: pid,
                    illust_factory=fetcher._illust_from_detail,
                    blocked=set(), min_bookmarks=0,
                    progress=events.append)
            finally:
                watcher.join(timeout=10)

        assert failures == [], f'详情批次还在途，已入库记录却没有发布：{failures}'
        assert not watcher.is_alive()
        assert seen_in_flight == [[5001]], \
            '批次尚未结束，已入库记录就该出现在 result 事件里'
        assert [e['pixiv_id'] for e in events if e['type'] == 'examined'] == [5001, 5002]

    @patch('fetcher._kick_background_fill')
    @patch('fetcher._fetch_details_parallel')
    def test_duplicate_input_pid_examined_and_published_once(
            self, mock_fetch, mock_fill, clean_db):
        """输入里同一 pixiv_id 出现两次时：examined 只发一次、result 只发布一次。

        重复输入不是纸面假设：Pixiv 分页会漂移（两次翻页之间作品被删除/新增，向后
        分页整体错位），同一作品因此可能跨页、跨批次重复出现。前端把重复项当成两件
        作品就会多算进度、多插一张卡，所以 `_process_items` 必须自己把重复吃下去 ——
        这里钉住 examined 的 once-per-PID 去重（连同 published_pids 的单次发布）。
        """
        mock_fetch.return_value = _detail_batch({3001: _detail(3001, bookmark_count=600)}, 1)
        events: list[dict] = []

        results = fetcher._process_items(
            clean_db, [_item(3001), _item(3001)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(), min_bookmarks=500, defer_details=False,
            progress=events.append)

        assert [e['pixiv_id'] for e in events if e['type'] == 'examined'] == [3001], \
            '同一 PID 被检查两次，只应发一次 examined'
        assert [r['pixiv_id'] for r in results] == [3001]
        assert [e['result']['pixiv_id'] for e in events if e['type'] == 'result'] == [3001], \
            '同一 PID 的结果只应发布一次'


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


class TestPaginatedSearchNoProgressParam:
    """分页层不参与逐条发布：progress 只能注入 `search_by_*` / `_process_items`。

    留着这个形参的代价不是"多一个没用的参数"，而是它**看起来接得上**：下一任务按
    计划字面"沿 `app.paginated_search` 和对应 `app.search_by_*` 闭包传入"时，接线
    通过、测试全绿，端到端却零事件。所以这里钉住它必须直接不存在。
    """

    def test_progress_keyword_is_rejected(self):
        with pytest.raises(TypeError):
            fetcher.paginated_search(
                lambda page, remaining=None: ([], False), {'type': 'user'}, 24,
                progress=[].append)


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


class TestHttpErrorClassification:
    """401 = 认证失效，403 = 限流/风控（审计 S13）。

    403 被当成认证失效会把"换个节奏重试就能成功"的限流误报成 Cookie 失效：预取
    整轮中止（连容量清理一起跳过）、前端提示重新登录，而重登修不好限流。
    """

    @staticmethod
    def _failing_session(status: int):
        class _Session:
            def __init__(self):
                self.calls = 0

            def get(self, *args, **kwargs):
                self.calls += 1
                resp = requests.Response()
                resp.status_code = status
                raise requests.HTTPError(f'HTTP {status}', response=resp)

        return _Session()

    def _patched(self, monkeypatch, status: int):
        session = self._failing_session(status)
        monkeypatch.setattr(fetcher, 'build_pixiv_session', lambda: session)
        # 旁路令牌桶：只测分类语义，不受全局限速器残留状态影响
        monkeypatch.setattr(pixiv_client, '_total_limiter', pixiv_client._TokenBucket(6000))
        return session

    # ── search_by_tag ──

    def test_search_tag_401_raises_auth_error(self, monkeypatch):
        """401 才是认证失效：必须上报 PixivAuthError，让用户去更新 Cookie。"""
        session = self._patched(monkeypatch, 401)
        with pytest.raises(fetcher.PixivAuthError):
            fetcher.search_by_tag('s13-401')
        assert session.calls == 1

    def test_search_tag_403_returns_empty_not_auth_error(self, monkeypatch, caplog):
        """403 按失败形态返回空结果，并且留下可检索的告警。"""
        session = self._patched(monkeypatch, 403)
        with caplog.at_level(logging.WARNING, logger='pixiv_client'):
            result = fetcher.search_by_tag('s13-403')

        assert result == ([], False)
        assert session.calls == 1, '不重试（重试策略未改）'
        assert '403' in caplog.text and '限流' in caplog.text

    # ── browse_discovery ──

    def test_browse_discovery_403_returns_empty(self, monkeypatch):
        session = self._patched(monkeypatch, 403)
        assert fetcher.browse_discovery(page=91) == ([], False)
        assert session.calls == 1

    def test_browse_discovery_401_raises_auth_error(self, monkeypatch):
        self._patched(monkeypatch, 401)
        with pytest.raises(fetcher.PixivAuthError):
            fetcher.browse_discovery(page=92)

    # ── _get_user_profile_ids ──

    def test_user_profile_403_returns_empty(self, monkeypatch):
        session = self._failing_session(403)
        monkeypatch.setattr(pixiv_client, '_total_limiter', pixiv_client._TokenBucket(6000))
        assert fetcher._get_user_profile_ids(session, 's13-user-403') == []
        assert session.calls == 1

    def test_user_profile_401_raises_auth_error(self, monkeypatch):
        session = self._failing_session(401)
        monkeypatch.setattr(pixiv_client, '_total_limiter', pixiv_client._TokenBucket(6000))
        with pytest.raises(fetcher.PixivAuthError):
            fetcher._get_user_profile_ids(session, 's13-user-401')

    # ── fetch_following ──

    def test_fetch_following_403_returns_empty(self, monkeypatch):
        session = self._patched(monkeypatch, 403)
        assert fetcher.fetch_following(page=93) == ([], False)
        assert session.calls == 1

    def test_fetch_following_401_raises_auth_error(self, monkeypatch):
        self._patched(monkeypatch, 401)
        with pytest.raises(fetcher.PixivAuthError):
            fetcher.fetch_following(page=94)

    # ── 详情路径的 403 语义不变（退避重试，仍不上报认证失效）──

    def test_detail_403_still_retries_and_never_auth_error(self, monkeypatch):
        """回归：详情 API 的 403 = 限流，走退避重试（S13 不动这条语义）。"""
        session = self._failing_session(403)
        monkeypatch.setattr(pixiv_client, '_total_limiter', pixiv_client._TokenBucket(6000))
        with patch('pixiv_client.time.sleep'):
            result = fetcher._get_illust_detail(
                session, 123, limiter=fetcher._TokenBucket(6000))

        assert result is None
        assert session.calls == fetcher.DETAIL_MAX_RETRIES + 1


class TestFillAttemptMapPruning:
    """后台补全的节流表不能只增不减（审计 S14）。

    表里每个"补全过一次"的作品留一条 int→float，大库 + 反复翻页会攒到几万条且
    永不释放。清理只允许发生在**远超节流窗口**的条目上 —— 否则就会把节流放宽。
    """

    def _install_map(self, monkeypatch, entries: dict[int, float]):
        monkeypatch.setattr(fetcher, '_fill_last_attempt', dict(entries))
        monkeypatch.setattr(fetcher, '_filling_ids', set())
        calls: list[list[int]] = []
        monkeypatch.setattr(fetcher, '_fetch_details_parallel',
                            lambda ids, **kwargs: (calls.append(list(ids)), _detail_batch())[1])
        return calls

    def test_fill_attempt_map_pruned_when_large(self, monkeypatch):
        """表超过上限时清掉早已过期的条目；本次请求的条目照常写入。"""
        now = time.time()
        entries = {pid: now - 10000 for pid in range(1, 1301)}   # 全部远超窗口
        entries[999999] = now - 10                               # 窗口内，必须保留
        calls = self._install_map(monkeypatch, entries)

        fetcher._background_fill_details([900001])

        remaining = fetcher._fill_last_attempt
        assert set(remaining) == {999999, 900001}, '过期条目必须清掉、窗口内条目必须保留'
        assert remaining[900001] == pytest.approx(now, abs=5), '本次尝试要记进节流表'
        assert calls == [[900001]]

    def test_fill_attempt_recent_kept(self, monkeypatch):
        """未超上限时一条都不清（清理是有代价的，不该每轮都扫全表）。"""
        now = time.time()
        entries = {pid: now - 10000 for pid in range(1, 501)}   # 全部过期但表不大
        calls = self._install_map(monkeypatch, entries)

        fetcher._background_fill_details([900002])

        assert len(fetcher._fill_last_attempt) == 501
        assert calls == [[900002]]

    def test_fill_attempt_throttle_survives_pruning(self, monkeypatch):
        """清理不能放宽节流：窗口内的作品依然被跳过。"""
        now = time.time()
        entries = {pid: now - 10000 for pid in range(1, 1301)}
        entries[900003] = now - 10                               # 刚补过
        calls = self._install_map(monkeypatch, entries)

        fetcher._background_fill_details([900003])

        assert calls == [], '窗口内的作品必须继续被节流跳过'
        assert fetcher._fill_last_attempt[900003] == now - 10, '跳过时不该刷新时间戳'

    def test_fill_attempt_pruning_is_thread_safe(self, monkeypatch):
        """多线程同时补全时清理与判定互斥：不得抛 dictionary changed size。"""
        now = time.time()
        entries = {pid: now - 10000 for pid in range(1, 2501)}
        calls = self._install_map(monkeypatch, entries)
        errors: list[BaseException] = []

        def _run(pid: int):
            try:
                fetcher._background_fill_details([pid])
            except BaseException as e:      # noqa: BLE001 —— 汇总后统一断言
                errors.append(e)

        threads = [threading.Thread(target=_run, args=(910000 + i,), daemon=True)
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)

        assert not errors, f'并发补全出错：{errors!r}'
        assert not any(t.is_alive() for t in threads)
        assert len(calls) == 8
        assert len(fetcher._fill_last_attempt) == 8, '清理后不该残留陈年条目'


class TestBackgroundFillRateLimited:
    """后台补全批次被限流截断时的行为。"""

    def test_rate_limited_fill_still_writes_completed_details(self, monkeypatch, clean_db):
        """已拿到的详情照常写库（刷新是幂等的，拿到不写才亏），且去重集合必须释放 ——
        否则这些作品在整个进程生命周期内都不会再被补全。"""
        monkeypatch.setattr(fetcher, '_filling_ids', set())
        monkeypatch.setattr(fetcher, '_fill_last_attempt', {})
        monkeypatch.setattr(fetcher, '_fetch_details_parallel', lambda ids, **kwargs: _detail_batch(
            {7001: {'bookmark_count': 777,
                    'original_urls': ['https://i.pximg.net/7001_p0.jpg']}},
            2, rate_limited=True))
        clean_db.add(Illust(pixiv_id=7001, title='t', bookmark_count=1))
        clean_db.commit()

        fetcher._background_fill_details([7001, 7002])   # 不应抛异常打断预取循环

        clean_db.expire_all()   # 补全用自己的事务提交，过期后重读才是库里真实值
        row = clean_db.query(Illust).filter(Illust.pixiv_id == 7001).first()
        assert row.bookmark_count == 777
        assert row.original_urls_list == ['https://i.pximg.net/7001_p0.jpg']
        assert fetcher._filling_ids == set(), '限流截断也必须释放 _filling_ids'


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
        mock_fetch.return_value = _detail_batch({7002: {
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
        mock_fetch.return_value = _detail_batch({8001: {
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
        mock_fetch.return_value = _detail_batch(attempted=1)  # 拉取失败

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
        monkeypatch.setattr(pixiv_client, '_total_limiter', pixiv_client._TokenBucket(6000))
        session = self._FakeSession(exc)
        with patch('pixiv_client.time.sleep') as mock_sleep:
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
        # 只数**退避**睡眠：每次重试都要重取令牌，而旁路桶（6000/分钟）自己也会
        # sleep 0.01s —— 混在一起数会把"重取令牌"错判成"多退避了一次"。
        backoffs = [c.args[0] for c in mock_sleep.call_args_list if c.args[0] >= 1]
        assert backoffs == [3, 9], '403/429 的退避是不可变的 3s/9s'

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
        monkeypatch.setattr(pixiv_client, '_total_limiter', pixiv_client._TokenBucket(6000))
        session = self._StaticSession(resp)
        with patch('pixiv_client.time.sleep') as mock_sleep:
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
        monkeypatch.setattr(pixiv_client, '_detail_error_samples', {})
        resp = self._json_resp({'error': True, 'message': '謎のエラー'})
        self._run_static(monkeypatch, resp, return_dead=True)
        assert fetcher.get_detail_error_samples() == {'謎のエラー': 1}

    def test_unmatched_error_message_counted_once_per_message(self, monkeypatch):
        monkeypatch.setattr(pixiv_client, '_detail_error_samples', {})
        resp = self._json_resp({'error': True, 'message': '謎のエラー'})
        self._run_static(monkeypatch, resp, return_dead=True)
        self._run_static(monkeypatch, resp, return_dead=True)
        assert fetcher.get_detail_error_samples() == {'謎のエラー': 2}

    def test_permanent_and_auth_messages_not_sampled(self, monkeypatch):
        """已判死/认证类报文不进样本（前者已处理，后者另有上报路径）。"""
        monkeypatch.setattr(pixiv_client, '_detail_error_samples', {})
        dead = self._json_resp({'error': True, 'message': '作品已被删除'})
        self._run_static(monkeypatch, dead, return_dead=True)
        assert fetcher.get_detail_error_samples() == {}

    def test_hostile_error_message_is_truncated_in_log(self, monkeypatch, caplog):
        """外部 message 只能以截断后的形态进日志：换行能伪造日志行，超长能刷屏。

        两条报错日志（判死 / 未识别）与 429 的 `Retry-After` 同源风险，走同一个
        `_log_safe_header`。
        """
        hostile = 'A' * 500 + '\n伪造日志行'
        monkeypatch.setattr(pixiv_client, '_detail_error_samples', {})
        with caplog.at_level(logging.WARNING, logger='pixiv_client'):
            self._run_static(
                monkeypatch,
                self._json_resp({'error': True, 'message': '作品已被删除' + hostile}),
                return_dead=True)
            self._run_static(
                monkeypatch,
                self._json_resp({'error': True, 'message': '謎のエラー' + hostile}),
                return_dead=True)

        lines = [r.getMessage() for r in caplog.records
                 if r.name == 'pixiv_client'
                 and ('Detail API 永久失败' in r.getMessage()
                      or 'Detail API error for' in r.getMessage())]
        assert len(lines) == 2, '两条报错日志都要覆盖'
        for line in lines:
            assert '\n' not in line, '外部报文不得换行伪造日志'
            assert 'A' * 41 not in line, '外部报文必须截断'
        assert all('123' in line for line in lines), 'pixiv_id 仍要保留'

    def test_session_does_not_retry_connect_errors(self, monkeypatch):
        """传输层同样不能重试连接错误（Retry(connect=0)），否则又叠回一层。

        后三条断言把传输层配置**钉死**：它们不是复述实现，而是决定 429 走哪条路的
        分水岭。429 在 `status_forcelist` 里 → urllib3 先重试一次，耗尽后 requests 抛
        **不带 `.response` 的 `RetryError`**，`fetch_illust_detail` 拿不到状态码，
        只能归入"其他"→ 1s，熔断闸的 `observe_response(429)` 也收不到它；真正的
        限流信号是 403（不在 forcelist，响应原样返回）。`respect_retry_after_header`
        决定这段时间由传输层睡（睡在闸槽位内）还是由应用层退避。适配器只
        `mount('https://', ...)`：`PIXIV_BASE_URL` 为 `http://` 的镜像走默认适配器
        （`max_retries=0`、不重试），429 响应原样回来并**到达闸** —— 闸的 429 分支
        因此才不是死代码。改动这三项里任何一项都会改变上述路径，故在此显式固化。
        """
        monkeypatch.setattr(pixiv_client, '_load_cookie', lambda: None)
        monkeypatch.setattr(pixiv_client, '_cookie_value', 'test')
        session = pixiv_client.build_pixiv_session()
        https_adapter = session.get_adapter('https://www.pixiv.net')
        retry = https_adapter.max_retries
        assert retry.connect == 0
        assert retry.total == 1
        assert retry.status_forcelist == [429, 500, 502, 503]
        assert retry.respect_retry_after_header is True
        http_adapter = session.get_adapter('http://www.pixiv.net')
        assert http_adapter is not https_adapter, '适配器只应挂在 https:// 上'
        assert http_adapter.max_retries.total == 0, 'http:// 镜像不重试，429 会到达闸'


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
        monkeypatch.setattr(pixiv_client, 'COOKIE_PATH', str(cookie))
        monkeypatch.setattr(pixiv_client, '_cookie_mtime', 0)
        monkeypatch.setattr(pixiv_client, '_cookie_value', '')
        pixiv_client.reset_pooled_session()
        yield
        pixiv_client.reset_pooled_session()

    def test_same_thread_reuses_session(self):
        """同线程跨请求复用同一个 Session，即复用同一条 keep-alive 连接。"""
        assert pixiv_client.get_pooled_session() is pixiv_client.get_pooled_session()

    def test_different_threads_get_own_session(self):
        """不跨线程共享：requests.Session 不保证线程安全。"""
        main_session = pixiv_client.get_pooled_session()
        box = []
        t = threading.Thread(target=lambda: box.append(pixiv_client.get_pooled_session()))
        t.start()
        t.join()
        assert box, '子线程应拿到自己的 session'
        assert box[0] is not main_session
        box[0].close()

    def test_cookie_change_rebuilds_session(self):
        """设置页改写 cookies.txt 后必须立即生效，不能沿用旧连接的旧 Cookie。"""
        first = pixiv_client.get_pooled_session()
        os.utime(pixiv_client.COOKIE_PATH, (time.time() + 60, time.time() + 60))
        assert pixiv_client.get_pooled_session() is not first

    def test_reset_drops_pool(self):
        """复用连接被对端关闭后，reset 必须让下次取到全新的 Session。"""
        first = pixiv_client.get_pooled_session()
        pixiv_client.reset_pooled_session()
        assert pixiv_client.get_pooled_session() is not first


class TestCredentiallessSession:
    """白名单外主机必须用无凭据会话（审计 S7a）。

    `build_pixiv_session()` 挂的是**会话级** `Cookie` 头，requests 会把它发给任意
    主机 —— 访问非 Pixiv 图床域名时必须显式摘掉，否则等于交出 PHPSESSID。
    """

    @pytest.fixture(autouse=True)
    def _isolate_cookie(self, monkeypatch, tmp_path):
        cookie = tmp_path / 'cookies.txt'
        cookie.write_text('PHPSESSID=test-cookie\n')
        monkeypatch.setattr(pixiv_client, 'COOKIE_PATH', str(cookie))
        monkeypatch.setattr(pixiv_client, '_cookie_mtime', 0)
        monkeypatch.setattr(pixiv_client, '_cookie_value', '')

    def test_build_session_carries_cookie(self):
        s = pixiv_client.build_pixiv_session()
        try:
            assert 'PHPSESSID=test-cookie' in s.headers.get('Cookie', '')
        finally:
            s.close()

    def test_credentialless_session_has_no_cookie_at_all(self):
        s = pixiv_client.build_credentialless_session()
        try:
            assert 'Cookie' not in s.headers, '会话级 Cookie 头必须摘掉（否则会发给任意主机）'
            assert s.cookies.get_dict() == {}, '域级 Cookie 也要清掉，做到名副其实'
        finally:
            s.close()

    def test_session_verify_follows_config(self, monkeypatch):
        """SSL_VERIFY 必须在建 session 时就生效（默认 True 才有意义）。"""
        monkeypatch.setattr(pixiv_client, 'SSL_VERIFY', False)
        s = pixiv_client.build_pixiv_session()
        try:
            assert s.verify is False
        finally:
            s.close()

        monkeypatch.setattr(pixiv_client, 'SSL_VERIFY', True)
        s = pixiv_client.build_pixiv_session()
        try:
            assert s.verify is True
        finally:
            s.close()


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
        mock_fetch.return_value = _detail_batch()
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
        mock_fetch.return_value = _detail_batch()
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
    def test_rate_limited_result_not_cached(self, mock_fetch, mock_ids, mock_sess, clean_db):
        """限流截断的页是残缺的：必须抛 SearchRateLimitedError 且不写成功缓存。

        否则下一次搜索会命中这份"看起来完整"的残缺页，直到 600 秒 TTL 结束 ——
        用户根本不知道还有作品没被检查过。
        """
        mock_ids.return_value = list(range(1, 61))
        # 不用 self._call：它会把 return_value 重置成默认批次，盖掉这里的限流标记
        mock_fetch.return_value = _detail_batch({1: _detail(1)}, 24, rate_limited=True)
        fetcher.clear_search_cache()
        try:
            with pytest.raises(fetcher.SearchRateLimitedError):
                fetcher.search_by_user('12345', page=1, max_results=ITEMS_PER_PAGE)
            assert not any(k.startswith('user|q=12345') for k in fetcher._SEARCH_CACHE)
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

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_cache_ttl_is_user_search_600s_not_tag_30s(
            self, mock_fetch, mock_ids, mock_sess, clean_db, monkeypatch):
        """钉住作者搜索缓存的 TTL：就是 600 秒，不是标签搜索那条 30 秒。

        用假时钟验证"599 秒仍命中、601 秒失效"：若误用标签搜索的 30 秒 TTL，
        599 秒那一步就会重新拉详情（`_fetch_details_parallel` / `_get_user_profile_ids`
        再被调用），用例即失败 —— 这正是要钉住的差异，也补上了"完整画师搜索重复
        执行仍命中 600 秒缓存"这条规范契约此前只能间接推知的缺口。
        """
        clock = _FakeClock()
        monkeypatch.setattr(fetcher, 'time', clock)
        mock_ids.return_value = list(range(1, 61))
        mock_fetch.return_value = _detail_batch()
        fetcher.clear_search_cache()
        try:
            self._call(mock_fetch)
            assert (mock_fetch.call_count, mock_ids.call_count) == (1, 1)

            # 直接钉住写进缓存的那条 TTL（索引 1 = entry_ttl），而不只是"行为像"，
            # 免得日后改了常量却仍然通过时间窗口断言
            key = next(iter(fetcher._SEARCH_CACHE))
            assert key.startswith('user|q=12345')
            assert fetcher._SEARCH_CACHE[key][1] == fetcher._USER_SEARCH_CACHE_TTL == 600.0

            clock.advance(599.0)
            self._call(mock_fetch)
            assert (mock_fetch.call_count, mock_ids.call_count) == (1, 1), \
                '599 秒（< 600）必须仍命中缓存，既不重拉详情也不重拉 profile'

            clock.advance(2.0)      # 累计 601 秒
            self._call(mock_fetch)
            assert mock_fetch.call_count == 2, '601 秒（> 600）缓存必须失效并重新搜索'
        finally:
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_blocked_fingerprint_is_part_of_cache_key(
            self, mock_fetch, mock_ids, mock_sess, clean_db):
        """屏蔽标签指纹必须参与作者搜索缓存键：同一批输入、只改屏蔽集合就要换条目。

        TTL 长达 600 秒，指纹漏掉就意味着"改完屏蔽标签还得等十分钟才见效"。这里
        把键里的 `bt=` 分量直接钉出来（只断言"重新搜索了"的话，TTL 过期后的重搜
        也能满足，测不出指纹的作用）。
        """
        mock_ids.return_value = list(range(1, 61))
        mock_fetch.return_value = _detail_batch()
        fetcher.clear_search_cache()
        try:
            self._call(mock_fetch)
            first_key = next(iter(fetcher._SEARCH_CACHE))
            assert fetcher._blocked_fingerprint(set()) in first_key
            assert mock_fetch.call_count == 1

            clean_db.add(BlockedTag(tag='a'))
            clean_db.commit()

            self._call(mock_fetch)
            keys = list(fetcher._SEARCH_CACHE)
            assert len(keys) == 2, '屏蔽标签集合变了就该是另一条缓存条目'
            assert fetcher._blocked_fingerprint({'a'}) in keys[1]
            assert fetcher._blocked_fingerprint({'a'}) != fetcher._blocked_fingerprint(set())
            assert mock_fetch.call_count == 2, '指纹变了必须重新搜索，不能吃旧缓存'
        finally:
            fetcher.clear_search_cache()


class TestTagSearchResultCacheTTL:
    """标签搜索结果缓存的 TTL：专用 120 秒，且**只有**标签路径用它。

    标签搜索的成本是 1 次 HTTP，原来的 30 秒缓存常在用户"翻回上一页"时已经过期，
    白等一次上游往返；120 秒覆盖了"看完一屏再回退"的实际节奏。
    发现页/关注页的成本同样是 1 次 HTTP，但它们的缓存是另一套语义，必须继续用
    `_SEARCH_CACHE_TTL`（30 秒）—— 下面把两条路径**各自写进缓存条目的 TTL** 都钉出来，
    而不是只断言"行为像"（改了常量但仍落在时间窗口里也能蒙过去）。
    """

    @staticmethod
    def _tag_search(mock_fetch, mock_items, keyword='ttl'):
        mock_items.return_value = ([_item(901)], 1)
        mock_fetch.return_value = _detail_batch({901: _detail(901)}, 1)
        return fetcher.search_by_tag(keyword, min_bookmarks=500)

    @patch('fetcher.build_pixiv_session')
    @patch('pixiv_client.fetch_search_illusts')
    @patch('fetcher._fetch_details_parallel')
    def test_tag_cache_ttl_is_120s(
            self, mock_fetch, mock_items, mock_sess, clean_db, monkeypatch):
        clock = _FakeClock()
        monkeypatch.setattr(fetcher, 'time', clock)
        fetcher.clear_search_cache()
        try:
            self._tag_search(mock_fetch, mock_items)
            assert mock_items.call_count == 1

            key = next(iter(fetcher._SEARCH_CACHE))
            assert key.startswith('tag|q=ttl')
            assert fetcher._SEARCH_CACHE[key][1] == fetcher._TAG_SEARCH_CACHE_TTL == 120.0

            clock.advance(119.0)
            self._tag_search(mock_fetch, mock_items)
            assert mock_items.call_count == 1, \
                '119 秒（< 120）必须仍命中缓存：30 秒 TTL 会在这里重打一次上游'

            clock.advance(2.0)      # 累计 121 秒
            self._tag_search(mock_fetch, mock_items)
            assert mock_items.call_count == 2, '121 秒（> 120）缓存必须失效并重新请求'
        finally:
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('pixiv_client.fetch_search_illusts')
    def test_empty_tag_page_keeps_short_ttl(
            self, mock_items, mock_sess, clean_db, monkeypatch):
        """空结果页**不**跟随 120 秒：它必须留在 30 秒窗口里。

        为什么单独钉住：Cookie 过期时 Pixiv **静默返回空页**，而同条件重试命中同一个
        缓存键 —— 若空页也按 120 秒缓存，"凭据失效"会被伪装成"这个标签真的没作品"
        长达两分钟，用户只会反复换关键词而不是去检查 Cookie。空结果继续按
        `_SEARCH_CACHE_TTL`（30 秒）缓存，`search_by_user` 更是干脆不缓存空结果。
        """
        clock = _FakeClock()
        monkeypatch.setattr(fetcher, 'time', clock)
        fetcher.clear_search_cache()
        try:
            mock_items.return_value = ([], 0)
            assert fetcher.search_by_tag('empty-ttl') == ([], False)

            key = next(iter(fetcher._SEARCH_CACHE))
            assert key.startswith('tag|q=empty-ttl')
            assert fetcher._SEARCH_CACHE[key][1] == fetcher._SEARCH_CACHE_TTL == 30.0, \
                '空结果页必须留在 30 秒窗口，不能继承 120 秒的标签 TTL'

            clock.advance(31.0)
            assert fetcher.search_by_tag('empty-ttl') == ([], False)
            assert mock_items.call_count == 2, \
                '31 秒（> 30）后空结果缓存必须失效并重新请求上游'
        finally:
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('pixiv_client.fetch_discovery_artworks')
    @patch('fetcher._fetch_details_parallel')
    def test_discovery_cache_keeps_30s_ttl(
            self, mock_fetch, mock_items, mock_sess, clean_db, monkeypatch):
        """发现页不得被顺手改成 120 秒：它是另一条路径，TTL 仍是 `_SEARCH_CACHE_TTL`。"""
        clock = _FakeClock()
        monkeypatch.setattr(fetcher, 'time', clock)
        mock_items.return_value = ([_item(911)], 1)
        mock_fetch.return_value = _detail_batch({911: _detail(911)}, 1)
        fetcher.clear_search_cache()
        try:
            fetcher.browse_discovery(min_bookmarks=500)
            key = next(iter(fetcher._SEARCH_CACHE))
            assert key.startswith('disc|')
            assert fetcher._SEARCH_CACHE[key][1] == fetcher._SEARCH_CACHE_TTL == 30.0

            clock.advance(31.0)
            fetcher.browse_discovery(min_bookmarks=500)
            assert mock_items.call_count == 2, '发现页 31 秒后必须重新请求（仍是 30 秒 TTL）'
        finally:
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('pixiv_client.fetch_following_latest')
    @patch('fetcher._kick_background_fill')
    def test_following_cache_keeps_30s_ttl(
            self, mock_kick, mock_items, mock_sess, clean_db, monkeypatch):
        """关注页同样保持 30 秒（`fetch_following` 恒走 defer 路径，不拉详情）。"""
        clock = _FakeClock()
        monkeypatch.setattr(fetcher, 'time', clock)
        mock_items.return_value = ([_item(921)], False)
        fetcher.clear_search_cache()
        try:
            fetcher.fetch_following(1, r18_mode='all')
            key = next(iter(fetcher._SEARCH_CACHE))
            assert key.startswith('follow|')
            assert fetcher._SEARCH_CACHE[key][1] == fetcher._SEARCH_CACHE_TTL == 30.0

            clock.advance(31.0)
            fetcher.fetch_following(1, r18_mode='all')
            assert mock_items.call_count == 2, '关注页 31 秒后必须重新请求（仍是 30 秒 TTL）'
        finally:
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('pixiv_client.fetch_search_illusts')
    @patch('fetcher._fetch_details_parallel')
    def test_budget_exhausted_tag_page_not_cached(self, mock_fetch, mock_items, mock_sess, clean_db):
        """预算中途耗尽的标签页是残缺的（本页还有条目没判定），不得写成功缓存。

        当前只有作者搜索会启用详情预算，这条守的是"哪天给标签路径也开预算"时
        不静默退化成"残缺页被 120 秒 TTL 固化"。
        """
        mock_items.return_value = ([_item(931)], 1)
        mock_fetch.return_value = _detail_batch({931: _detail(931)}, 1)
        fetcher.clear_search_cache()
        try:
            fetcher._budget_begin(1)
            fetcher._budget_consume(1)
            assert fetcher.budget_exhausted() is True

            fetcher.search_by_tag('budget', min_bookmarks=500)
            assert not any(k.startswith('tag|q=budget') for k in fetcher._SEARCH_CACHE)
        finally:
            fetcher._budget_end()
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('pixiv_client.fetch_discovery_artworks')
    @patch('fetcher._fetch_details_parallel')
    def test_budget_exhausted_discovery_page_not_cached(
            self, mock_fetch, mock_items, mock_sess, clean_db):
        """发现页同样不得把预算截断的残缺页写进缓存（与标签路径同款守卫）。"""
        mock_items.return_value = ([_item(941)], 1)
        mock_fetch.return_value = _detail_batch({941: _detail(941)}, 1)
        fetcher.clear_search_cache()
        try:
            fetcher._budget_begin(1)
            fetcher._budget_consume(1)
            assert fetcher.budget_exhausted() is True

            fetcher.browse_discovery(min_bookmarks=500)
            assert not any(k.startswith('disc|') for k in fetcher._SEARCH_CACHE)
        finally:
            fetcher._budget_end()
            fetcher.clear_search_cache()

    @patch('fetcher.build_pixiv_session')
    @patch('pixiv_client.fetch_search_illusts')
    def test_upstream_auth_error_not_cached(self, mock_items, mock_sess, clean_db):
        """认证/上游异常必须原样抛出，绝不能顺手写一条空结果的"成功"缓存。

        否则 Cookie 失效后的第一次失败搜索会留下一份"没有作品"的空页，用户在 TTL
        内怎么重试都看不到真实结果。
        """
        mock_items.side_effect = pixiv_client.PixivAuthError('cookie 已失效')
        fetcher.clear_search_cache()
        try:
            with pytest.raises(pixiv_client.PixivAuthError):
                fetcher.search_by_tag('auth', min_bookmarks=500)
            assert fetcher._SEARCH_CACHE == {}
        finally:
            fetcher.clear_search_cache()


class TestRateLimitedSearchCache:
    """限流截断的搜索不得写成功缓存：残缺结果被 TTL 固化后，用户重试也拿不到补全。"""

    @patch('fetcher.build_pixiv_session')
    @patch('pixiv_client.fetch_search_illusts')
    @patch('fetcher._fetch_details_parallel')
    def test_tag_search_rate_limited_not_cached(self, mock_fetch, mock_items, mock_sess, clean_db):
        mock_items.return_value = ([_item(51)], 1)
        mock_fetch.return_value = _detail_batch({51: _detail(51)}, 1, rate_limited=True)
        fetcher.clear_search_cache()
        try:
            with pytest.raises(fetcher.SearchRateLimitedError):
                fetcher.search_by_tag('lim', min_bookmarks=500)
            assert fetcher._SEARCH_CACHE == {}, '限流页不能进成功缓存'
        finally:
            fetcher.clear_search_cache()


class TestSearchFunctionProgressPassThrough:
    """`search_by_*` 必须把 publisher 原样转交给 `_process_items`。

    这条链路此前完全没有用例覆盖：删掉任何一个 `progress=progress`，整套用例依然
    全绿（进度用例都直接调 `_process_items`）。下一任务按计划把路由的 publisher
    沿 `search_by_*` 注入，接错就是"接线通过、测试全绿、端到端零事件"——所以三个
    入口各钉一条，属于 `search_by_*` 的形参而不是 `_process_items` 的形参。
    """

    @patch('fetcher.build_pixiv_session')
    @patch('pixiv_client.fetch_search_illusts')
    @patch('fetcher._fetch_details_parallel')
    def test_tag_search_forwards_progress(self, mock_fetch, mock_items, mock_sess, clean_db):
        mock_items.return_value = ([_item(61)], 1)
        mock_fetch.return_value = _detail_batch({61: _detail(61)}, 1)
        fetcher.clear_search_cache()
        events: list[dict] = []
        try:
            results, _has_more = fetcher.search_by_tag(
                'prog', min_bookmarks=500, progress=events.append)
        finally:
            fetcher.clear_search_cache()

        assert [r['pixiv_id'] for r in results] == [61]
        assert [e['pixiv_id'] for e in events if e['type'] == 'examined'] == [61], \
            'search_by_tag 收下的 publisher 必须一路传到 _process_items'
        assert [e['result']['pixiv_id'] for e in events if e['type'] == 'result'] == [61]

    @patch('fetcher.build_pixiv_session')
    @patch('fetcher._get_user_profile_ids')
    @patch('fetcher._fetch_details_parallel')
    def test_user_search_forwards_progress(self, mock_fetch, mock_ids, mock_sess, clean_db):
        mock_ids.return_value = [71]
        mock_fetch.return_value = _detail_batch({71: _detail(71)}, 1)
        fetcher.clear_search_cache()
        events: list[dict] = []
        try:
            results, _has_more = fetcher.search_by_user('12345', progress=events.append)
        finally:
            fetcher.clear_search_cache()

        assert [r['pixiv_id'] for r in results] == [71]
        assert [e['pixiv_id'] for e in events if e['type'] == 'examined'] == [71], \
            'search_by_user 收下的 publisher 必须一路传到 _process_items'
        assert [e['result']['pixiv_id'] for e in events if e['type'] == 'result'] == [71]

    @patch('fetcher.build_pixiv_session')
    @patch('pixiv_client.fetch_discovery_artworks')
    @patch('fetcher._fetch_details_parallel')
    def test_discovery_forwards_progress(self, mock_fetch, mock_items, mock_sess, clean_db):
        mock_items.return_value = ([_item(81)], 1)
        mock_fetch.return_value = _detail_batch({81: _detail(81)}, 1)
        fetcher.clear_search_cache()
        events: list[dict] = []
        try:
            results, _has_more = fetcher.browse_discovery(
                min_bookmarks=500, progress=events.append)
        finally:
            fetcher.clear_search_cache()

        assert [r['pixiv_id'] for r in results] == [81]
        assert [e['pixiv_id'] for e in events if e['type'] == 'examined'] == [81], \
            'browse_discovery 收下的 publisher 必须一路传到 _process_items'
        assert [e['result']['pixiv_id'] for e in events if e['type'] == 'result'] == [81]


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
                batch = fetcher._fetch_details_parallel([1, 2, 3, 4, 5])
            finally:
                fetcher._cancel_end()
        assert batch.details == {}
        assert batch.attempted == 0
        mock_detail.assert_not_called()

    def test_cancel_keeps_in_flight_results(self, _cookie_file):
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
                batch = fetcher._fetch_details_parallel(ids)
            finally:
                fetcher._cancel_end()
        assert len(batch.details) == first_batch
        assert batch.attempted == first_batch, '取消的不计 attempted，在途的才计'


class TestSplitTags:
    """标签切分：中文逗号与英文逗号必须等价。

    用户分不清该打哪个逗号（输入法状态不同，打出来就是不同的字符），
    不该让他记这件事 —— `_split_tags` 是标签搜索唯一的切分入口
    （`search_by_tag` 调用），故把两种逗号钉在同一个用例里。
    """

    @pytest.mark.parametrize('raw, expected', [
        ('初音ミク,オリジナル', ['初音ミク', 'オリジナル']),
        ('初音ミク，オリジナル', ['初音ミク', 'オリジナル']),                # 中文逗号
        ('初音ミク，オリジナル, 風景', ['初音ミク', 'オリジナル', '風景']),  # 混用
        ('  初音ミク ， オリジナル  ', ['初音ミク', 'オリジナル']),          # 逗号两侧空格
    ])
    def test_both_comma_forms_split_identically(self, raw, expected):
        assert fetcher._split_tags(raw) == expected

    def test_single_tag_is_kept_whole(self):
        # 无逗号时整体作为一个标签：标签本身可能含空格
        #（如「アイドルマスター シンデレラガールズ」），不能被切碎
        assert fetcher._split_tags('  アイドルマスター シンデレラガールズ ') == \
            ['アイドルマスター シンデレラガールズ']

    def test_only_separators_falls_back_to_raw(self):
        # 既有语义：全是分隔符 → 切出的 parts 为空 → 回退成**归一化后**的原始串
        #（中文逗号在上一行 replace 里已转成英文逗号），而不是空列表 ——
        # 返回空列表会让上游拿不到任何关键词。此断言按实测行为固定。
        assert fetcher._split_tags('，,') == [',,']


# ── 熔断闸接进详情路径后的两条跨模块语义 ──


def _open_detail_gate(monkeypatch, pids=(901, 902, 903)):
    """把 `pixiv_client` 的模块级闸换成新实例并**按真实调用顺序**开路。

    用替换实例（而不是导入期的真实单例）：闸是有状态的，把它开在真实单例上会污染
    同文件/其它文件的详情用例（`_fresh_detail_gate` 已经预置了一条新闸，这里再换一条
    只是为了拿到它的引用）。
    """
    gate = pixiv_client._DetailRequestGate()
    monkeypatch.setattr(pixiv_client, '_detail_gate', gate)
    for pid in pids:
        with gate.request_slot(pid):
            gate.observe_response(pid, 403)
    assert gate.is_open, '用例前置条件：闸必须已开路'
    return gate


def _bypass_detail_limiters(monkeypatch):
    """旁路三级令牌桶：离线用例只关心接线，不该为真实限速付等待时间。"""
    monkeypatch.setattr(pixiv_client, '_detail_limiter', pixiv_client._TokenBucket(6000))
    monkeypatch.setattr(pixiv_client, '_fill_limiter', pixiv_client._TokenBucket(6000))
    monkeypatch.setattr(pixiv_client, '_total_limiter', pixiv_client._TokenBucket(6000))


class TestDetailGateDoesNotEscapeToRoutes:
    """闸拒绝必须被 `_fetch_details_parallel` 收成 `rate_limited`，绝不冒泡。

    一旦 `PixivRateLimitedError` 逃到 `routes_search`，那里的宽 `except Exception`
    会把它变成 HTTP 502 `error` —— 可重试的限流被静默降级成"搜索失败"，
    `partial` 态（保留已确认结果、以可重试状态收尾）直接失效。
    """

    def test_open_gate_surfaces_as_rate_limited_without_http(self, monkeypatch):
        _bypass_detail_limiters(monkeypatch)
        _open_detail_gate(monkeypatch)

        class _NoHttpSession:
            def __init__(self):
                self.calls = 0

            def get(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError('闸开路期间不得发出任何详情 HTTP 请求')

        session = _NoHttpSession()
        # 这条路径用的是线程内连接池，换成"一用就炸"的哨兵来证明 HTTP 没发出
        monkeypatch.setattr(fetcher, 'get_pooled_session', lambda *a, **k: session)

        batch = fetcher._fetch_details_parallel([11, 12, 13])   # 关键：不抛

        assert session.calls == 0
        assert batch.rate_limited is True
        assert batch.attempted == 0, '被闸拒绝的请求没发出，不计 attempted'
        assert batch.details == {}, '被拒不算"这件作品详情失败"'


class TestPrefetchTagWedgeOnGateOpen:
    """闸开路时预取轮次算失败，但标签必须可重试、容量清理照跑、已入库作品不丢。

    决策（见 plans/2026-09-29-search-throughput-execution-notes.md「Task 2 必读」）：
    `_prefetch_one_tag` 的宽 `except Exception` 会把 `SearchRateLimitedError` 变成
    "该标签本轮失败" —— 这是**预期语义**：标签记 `error`、下轮重试，已入库作品
    下轮从库里命中 existing 记录，不会丢也不会重拉。
    """

    def test_tag_recovers_next_round_and_cleanup_still_runs(self, clean_db, monkeypatch,
                                                             _cookie_file):
        import app
        import background
        from models import SearchCache

        # 上一轮已入库的作品 + 已缓存的标签列表（本轮失败不得把它们清掉）
        clean_db.add(Illust(pixiv_id=7, title='cached', bookmark_count=600))
        clean_db.add(SearchCache(tag='t', illust_ids='[7]', status='done'))
        clean_db.commit()

        class _CountingSession:
            """任何详情请求都回 404：既证明"该发的发了"，也证明"不该发的没发"。"""

            def __init__(self):
                self.calls = 0

            def get(self, *args, **kwargs):
                self.calls += 1
                resp = requests.Response()
                resp.status_code = 404
                return resp

        session = _CountingSession()
        # 详情走线程内连接池，它读的是 pixiv_client 的全局 build_pixiv_session
        monkeypatch.setattr(pixiv_client, 'build_pixiv_session', lambda: session)
        # search_by_tag 用的是 fetcher 自己 from-import 的绑定（再导出一份），两处都要换
        monkeypatch.setattr(fetcher, 'build_pixiv_session', lambda: session)
        _bypass_detail_limiters(monkeypatch)
        monkeypatch.setattr(fetcher, '_kick_background_fill', lambda pids: None)
        # 两轮的上游列表完全相同：7 已入库、8 从未入库（闸开路那轮没写成）
        monkeypatch.setattr(pixiv_client, 'fetch_search_illusts',
                            lambda session, query, **kwargs: ([_item(7), _item(8)], 2))
        monkeypatch.setattr(background, '_prefetch_refresh_bookmarks', lambda: None)
        cleaned = []
        monkeypatch.setattr(app, '_prefetch_capacity_cleanup', lambda: cleaned.append(1))

        gate = _open_detail_gate(monkeypatch)
        app._prefetch_loop()
        clean_db.expire_all()   # 预取用自己的事务提交，过期后重读才是库里的真实值

        row = clean_db.query(SearchCache).filter(SearchCache.tag == 't').first()
        assert gate.is_open
        assert row.status == 'error', '本轮算失败，但状态必须可重试（不是 fetching 残留）'
        assert row.illust_ids == '[7]', '本轮失败不得清掉已入库作品的缓存列表'
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 7).count() == 1
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 8).count() == 0
        assert cleaned == [1], '单标签失败不得跳过容量清理'
        assert session.calls == 0, '闸开路时连一发详情请求都不该发出'

        # 第二轮：闸换成全新实例（等价于冷却到期/重启后恢复）→ 同一标签继续预取
        monkeypatch.setattr(pixiv_client, '_detail_gate', pixiv_client._DetailRequestGate())
        app._prefetch_loop()
        clean_db.expire_all()

        row = clean_db.query(SearchCache).filter(SearchCache.tag == 't').first()
        assert row.status == 'done', '标签不得被永久卡死'
        assert json.loads(row.illust_ids) == [7], '已入库作品被重新推导为 existing，不重复'
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 7).count() == 1
        assert cleaned == [1, 1], '第二轮同样要跑到容量清理'
        assert session.calls == 1, \
            '第二轮只该为未入库的 8 发一次详情；已入库的 7 从库里命中，不重拉'


class TestRefreshPathMapsGateRefusalToGlobalFailure:
    """`return_dead=True` 的最终收藏数刷新必须把闸拒绝当成**全局**暂时失败。

    这是接闸后最容易踩坏的一条：按单作品写 `refresh_failed_at` 会把整个刷新队列刷上
    24h 退避（Pixiv 恢复后还要多等一天），让异常冒泡又会跳过 `_prefetch_loop` 的
    容量清理、`prefetch_max_illusts` 上限随之失效。
    """

    def test_open_gate_aborts_refresh_without_per_work_backoff(self, clean_db, monkeypatch):
        import app
        import background

        old = datetime.now(timezone.utc) - timedelta(days=2)
        for pid in (61, 62, 63):
            clean_db.add(Illust(pixiv_id=pid, title=f'p{pid}', prefetch_source=1,
                                bookmark_count=0, created_at=old))
        clean_db.commit()

        class _NoHttpSession:
            def __init__(self):
                self.calls = 0

            def get(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError('闸开路期间不得发出详情 HTTP 请求')

            def close(self):
                pass

        session = _NoHttpSession()
        monkeypatch.setattr(app, 'build_pixiv_session', lambda: session)
        _bypass_detail_limiters(monkeypatch)
        _open_detail_gate(monkeypatch)

        app._prefetch_refresh_bookmarks()   # 关键：不抛
        clean_db.expire_all()

        stats = app._prefetch_state['refresh_stats']
        assert stats['processed'] == 3
        assert stats['failed_global'] == 3, '闸拒绝必须记成全局性失败'
        assert stats['aborted'] == 'rate_limit', \
            f'连续 {background.PREFETCH_REFRESH_ABORT_STREAK} 条全局失败即中止本轮'
        assert session.calls == 0
        for pid in (61, 62, 63):
            illust = clean_db.query(Illust).filter(Illust.pixiv_id == pid).first()
            assert illust.refresh_failed_at is None, '全局限流不得写成该作品的退避'
            assert illust.prefetch_refresh_at is None, '作品仍留在刷新队列里（下轮再试）'
