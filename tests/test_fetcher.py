from unittest.mock import patch

import fetcher
from models import Illust


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
        mock_fetch.return_value = {}

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
        mock_fetch.return_value = {1003: {
            'title': 't', 'user_id': 1, 'user_name': 'u', 'page_count': 1,
            'bookmark_count': 900, 'thumb_url': 'https://x.jpg',
            'upload_date': '2026-01-01T00:00:00+09:00',
            'original_urls': ['https://i.pximg.net/1003_p0.jpg'],
            'tags': ['a'], 'description': '',
        }}

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
    def test_new_item_bookmark_written_from_item(self, mock_fetch, mock_fill, clean_db):
        """defer 路径：新记录写入条目自带 bookmarkCount，不写 0。"""
        mock_fetch.return_value = {}

        results = fetcher._process_items(
            clean_db, [_item(1004, 1200)],
            id_extractor=lambda item: int(item['id']),
            illust_factory=fetcher._illust_from_item,
            blocked=set(),
            min_bookmarks=0,
            defer_details=True,
        )

        assert len(results) == 1
        assert results[0]['bookmark_count'] == 1200
        row = clean_db.query(Illust).filter(Illust.pixiv_id == 1004).first()
        assert row.bookmark_count == 1200
