from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest

import app
from pixivviewer import background
from pixivviewer import config
from pixivviewer import fetcher
from pixivviewer.models import SearchCache, Illust, get_session, safe_commit


class TestPrefetchOneTag:
    def test_prefetch_one_tag_marks_source(self, clean_db, monkeypatch):
        """入库前的来源标记口径：搜索结果里的作品标 `prefetch_source=1`，
        **已下载的不标** —— 标了就会计入容量，而它按定义永远清不掉。

        同一轮里既有新作品又有已下载作品，故合并成一条（原先拆成两条时，
        两条的前置准备与断言同构，只有"搜索返回了什么"不同）。
        """
        clean_db.add_all([
            Illust(pixiv_id=1, title='a'),
            Illust(pixiv_id=2, title='b'),
            Illust(pixiv_id=42, title='dl', download_status='done'),
        ])
        safe_commit(clean_db)

        calls = []

        def _fake_search(tag, **kwargs):
            calls.append(tag)
            if len(calls) == 1:
                return [{'pixiv_id': 1}, {'pixiv_id': 2}, {'pixiv_id': 42}], True
            return [], False

        monkeypatch.setattr(app, 'search_by_tag', _fake_search)
        app._prefetch_one_tag('テスト')

        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'テスト').first()
        assert row is not None
        assert row.status == 'done'
        assert row.total == 3
        assert json.loads(row.illust_ids) == [1, 2, 42]
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 1).first().prefetch_source == 1
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 2).first().prefetch_source == 1
        downloaded = clean_db.query(Illust).filter(Illust.pixiv_id == 42).first()
        assert downloaded is not None
        assert downloaded.download_status == 'done'
        assert downloaded.prefetch_source == 0, '已下载的作品不该被标成预取来源'

    def test_prefetch_one_tag_error_sets_status(self, clean_db, monkeypatch):
        """搜索失败：状态转 error 并记下原因，但**上次成功累积的 illust_ids 不受损**。

        ① 全新标签的失败要留下一行 error（不能无声无息）；② 已有累积的标签失败时
        旧 id 原样保留（缓存列表只增不减，失败不能把已有作品一起抹掉）。两条同属
        "失败时的状态口径"、断言同构，故合为一条。
        """
        def _boom(tag, **kwargs):
            raise RuntimeError('network down')

        monkeypatch.setattr(app, 'search_by_tag', _boom)

        app._prefetch_one_tag('x')
        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'x').first()
        assert row is not None
        assert row.status == 'error'
        assert row.error

        clean_db.add(SearchCache(tag='keep', illust_ids='[1, 2]', status='done'))
        safe_commit(clean_db)
        app._prefetch_one_tag('keep')
        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'keep').first()
        assert row.status == 'error'
        assert json.loads(row.illust_ids) == [1, 2], '失败不该丢掉上次成功的累积'

    def test_prefetch_one_tag_accumulates(self, clean_db, monkeypatch):
        # 已有缓存 [1, 2]，第二次预取抓 [2, 3, 4] → 合并去重为 [2, 3, 4, 1]
        clean_db.add(SearchCache(tag='acc', illust_ids='[1, 2]', status='done'))
        safe_commit(clean_db)

        calls = []

        def _fake_search(tag, **kwargs):
            calls.append(tag)
            if len(calls) == 1:
                return [{'pixiv_id': 2}, {'pixiv_id': 3}, {'pixiv_id': 4}], False
            return [], False

        monkeypatch.setattr(app, 'search_by_tag', _fake_search)
        app._prefetch_one_tag('acc')

        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'acc').first()
        assert row.status == 'done'
        assert json.loads(row.illust_ids) == [2, 3, 4, 1]
        assert row.total == 4

    def test_alias_sources_merge_and_dedupe(self, clean_db, monkeypatch):
        """组名 + 同义词各搜一遍，重叠作品只出现一次（并集去重）。

        这是"同义标签大量重复作品"的核心不变式：直接拼接会让同一 pid 出现两次，
        缓存页出现重复卡片、`total` 与用户看到的数量翻倍。
        """
        clean_db.add(SearchCache(
            tag='吉星排队', alias_tags=json.dumps(['星引擎party'])))
        safe_commit(clean_db)

        calls = []

        def _fake_search(tag, **kwargs):
            calls.append(tag)
            if tag == '吉星排队':
                return [{'pixiv_id': 1}, {'pixiv_id': 2}], False
            return [{'pixiv_id': 2}, {'pixiv_id': 3}], False

        monkeypatch.setattr(app, 'search_by_tag', _fake_search)
        app._prefetch_one_tag('吉星排队')

        assert calls == ['吉星排队', '星引擎party'], '组名与每个同义词都必须被搜索'
        row = clean_db.query(SearchCache).filter(SearchCache.tag == '吉星排队').first()
        assert json.loads(row.illust_ids) == [1, 2, 3], '重叠的 pid 2 只能保留一次'
        assert row.total == 3
        assert row.status == 'done'
        assert row.error == ''
        assert row.cached_at is not None

    def test_partial_alias_failure_keeps_done_with_error_note(self, clean_db, monkeypatch):
        """一个同义词失败：成功那部分照常写回、组保持 done，失败原因写进 error。"""
        clean_db.add(SearchCache(tag='g', alias_tags=json.dumps(['bad'])))
        safe_commit(clean_db)

        def _fake_search(tag, **kwargs):
            if tag == 'bad':
                raise RuntimeError('boom')
            return [{'pixiv_id': 7}], False

        monkeypatch.setattr(app, 'search_by_tag', _fake_search)
        app._prefetch_one_tag('g')

        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'g').first()
        assert json.loads(row.illust_ids) == [7]
        assert row.status == 'done', '部分成功仍算 done：已抓到的数据有效'
        assert '部分同义词失败' in row.error and 'bad' in row.error

    def test_auth_failure_aborts_remaining_sources(self, clean_db, monkeypatch):
        """认证失效时剩余同义词不再请求（同一原因重复失败只是空耗限流）。"""
        clean_db.add(SearchCache(
            tag='g', alias_tags=json.dumps(['a1', 'a2'])))
        safe_commit(clean_db)

        calls = []

        def _fake_search(tag, **kwargs):
            calls.append(tag)
            raise fetcher.PixivAuthError('HTTP 401')

        monkeypatch.setattr(app, 'search_by_tag', _fake_search)
        app._prefetch_one_tag('g')

        assert calls == ['g'], '第一个 source 认证失效后就该中止'
        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'g').first()
        assert row.status == 'error'
        assert '认证失效' in row.error

    def test_tag_merged_into_group_is_not_recreated(self, clean_db, monkeypatch):
        """竞态：预取循环拿到的旧标签刚被合并成别组同义词，不能重建独立行。

        否则"合并两个标签"会在下一轮被循环重新建出第二条，重复条目原样复辟。
        """
        clean_db.add(SearchCache(
            tag='吉星排队', alias_tags=json.dumps(['星引擎party']), status='done'))
        safe_commit(clean_db)

        called = []

        def _fake_search(tag, **kwargs):
            called.append(tag)
            return [], False

        monkeypatch.setattr(app, 'search_by_tag', _fake_search)
        app._prefetch_one_tag('星引擎party')

        assert called == [], '已成为同义词的旧标签不该再独立预取'
        assert clean_db.query(SearchCache).filter(
            SearchCache.tag == '星引擎party').first() is None

    def test_prefetch_one_tag_skips_when_fetching(self, clean_db, monkeypatch):
        clean_db.add(SearchCache(tag='y', status='fetching'))
        safe_commit(clean_db)

        called = []

        def _fake_search(tag, **kwargs):
            called.append(tag)
            return [], False

        monkeypatch.setattr(app, 'search_by_tag', _fake_search)
        app._prefetch_one_tag('y')

        assert not called
        row = clean_db.query(SearchCache).filter(SearchCache.tag == 'y').first()
        assert row.status == 'fetching'


class TestIsUserOwned:
    """保护判定收窄后的语义：只认下载意图，收藏夹不再是保护来源。

    "已下载 → 受保护"由调用方（容量清理/最终收藏数刷新）的 `download_status`
    判定负责，本类只钉 `_is_user_owned` 自己的 DownloadLog 口径。
    """

    def test_download_log_scope_decides_ownership(self, clean_db):
        """`_is_user_owned` 的 DownloadLog 口径（四行一表，原先拆成三条）。

        - 没有任何下载日志 → 不保护；
        - `failed` / `cancelled`（失败/取消待重试）也是用户意图：用户点过下载，
          不能当缓存垃圾；
        - `prefetch_deleted` 不算 —— 那是缓存清理自己写的，认它等于给作品永久保护。
        """
        from pixivviewer.models import DownloadLog
        clean_db.add_all([
            Illust(pixiv_id=6001, title='plain'),
            Illust(pixiv_id=6002, title='failed'),
            Illust(pixiv_id=6003, title='cancelled'),
            Illust(pixiv_id=6004, title='evicted-before'),
            DownloadLog(pixiv_id=6002, action='failed', message='下载失败待重试'),
            DownloadLog(pixiv_id=6003, action='cancelled', message='用户取消待重试'),
            DownloadLog(pixiv_id=6004, action='prefetch_deleted', message='之前被清理过'),
        ])
        safe_commit(clean_db)

        assert background._is_user_owned(clean_db, 6001) is False, '没有任何下载日志 → 不保护'
        assert background._is_user_owned(clean_db, 6002) is True, '失败待重试也是用户意图'
        assert background._is_user_owned(clean_db, 6003) is True, '用户取消待重试也是用户意图'
        assert background._is_user_owned(clean_db, 6004) is False, \
            'prefetch_deleted 是缓存清理自己写的，不算用户拥有'


def _wipe_illusts_and_tags(clean_db):
    """清掉作品与标签行（合并用例里第二段要从同一套初始数据重新开始）。"""
    clean_db.query(Illust).delete()
    clean_db.query(SearchCache).delete()
    safe_commit(clean_db)


class TestCapacityCleanup:
    def test_capacity_cleanup_protects_user_download_log_only(self, clean_db, monkeypatch):
        """容量清理的保护口径 = 用户下载日志：`failed` 保护、`prefetch_deleted` 不保护。

        两者必须相反：认 `prefetch_deleted` 等于让"被清过一次"变成永久保护
        （同一 pid 再次入库后就再也清不掉了）。同一套造数据方式、断言同构，
        只有喂进去的 action 不同，故合为一条。
        """
        from pixivviewer.models import DownloadLog
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            Illust(pixiv_id=1, title='low', prefetch_source=1, bookmark_count=1,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='touched', prefetch_source=1, bookmark_count=2,
                   prefetch_refresh_at=refreshed),
            DownloadLog(pixiv_id=2, action='failed', message='下载失败待重试'),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 1)

        app._prefetch_capacity_cleanup()

        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2}, '只有无下载日志的低收藏作品被淘汰'

        # 换成缓存清理自己写的 prefetch_deleted：不构成保护，照样被淘汰
        clean_db.add_all([
            Illust(pixiv_id=3, title='low', prefetch_source=1, bookmark_count=1,
                   prefetch_refresh_at=refreshed),
            DownloadLog(pixiv_id=3, action='prefetch_deleted', message='之前被清理过'),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 1)

        app._prefetch_capacity_cleanup()

        assert clean_db.query(Illust).filter(Illust.pixiv_id == 3).first() is None, \
            'prefetch_deleted 不算"用户拥有"，否则会永久保护'

    def test_capacity_cleanup_keeps_queued_download(self, clean_db, monkeypatch):
        """排队中（worker 还没把状态写成 downloading）的作品不能被容量清理删掉。

        审计 S3：trigger 入队 → worker 首个 commit 之间，行状态仍是 None 且没有
        任何 DownloadLog（`_is_user_owned` 也判不出），旧实现会把它当缓存垃圾淘汰，
        worker 随后查不到行 → 下载静默消失。
        """
        from pixivviewer import runtime
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            Illust(pixiv_id=1, title='low', prefetch_source=1, bookmark_count=10,
                   prefetch_refresh_at=refreshed),
            # 收藏数为 0：不设保护时它才是被淘汰的那个（保证用例能否证伪）
            Illust(pixiv_id=2, title='queued', prefetch_source=1, bookmark_count=0,
                   prefetch_refresh_at=refreshed),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 1)
        with runtime._download_queue_lock:
            runtime._queued_downloads.add(2)
        try:
            app._prefetch_capacity_cleanup()
        finally:
            with runtime._download_queue_lock:
                runtime._queued_downloads.discard(2)

        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2}, '排队中的下载不能被容量清理淘汰'

    def test_capacity_cleanup_low_bookmark_first(self, clean_db):
        """超过上限时优先淘汰最终收藏数最低的作品，并从**所有**标签列表里摘掉。

        行删了列表还留着 id，缓存页就会指向一条不存在的记录（"幽灵引用"）。
        两段同属"淘汰顺序 + 列表同步"这一个不变式，只有数据不同，故合为一条。
        """
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            SearchCache(tag='tag_a', illust_ids='[1, 2, 3]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            SearchCache(tag='tag_b', illust_ids='[3]',
                        cached_at=datetime(2021, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='low', prefetch_source=1, bookmark_count=10,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='high', prefetch_source=1, bookmark_count=500,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=3, title='mid', prefetch_source=1, bookmark_count=100,
                   prefetch_refresh_at=refreshed),
        ])
        safe_commit(clean_db)

        old = app._prefetch_state['max_illusts']
        try:
            app._prefetch_state['max_illusts'] = 1
            app._prefetch_capacity_cleanup()
        finally:
            app._prefetch_state['max_illusts'] = old

        # 收藏数最低的 pid1(10) 和 pid3(100) 被删，pid2(500) 保留
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2}
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_a').first().illust_ids) == [2]
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_b').first().illust_ids) == []

        # 被多个标签引用的作品：淘汰后同样要从每一个标签的列表里摘掉
        _wipe_illusts_and_tags(clean_db)
        clean_db.add_all([
            SearchCache(tag='tag_a', illust_ids='[1, 2]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            SearchCache(tag='tag_b', illust_ids='[2]',
                        cached_at=datetime(2021, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='a', prefetch_source=1, bookmark_count=10,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='b', prefetch_source=1, bookmark_count=100,
                   prefetch_refresh_at=refreshed),
        ])
        safe_commit(clean_db)
        try:
            app._prefetch_state['max_illusts'] = 1
            app._prefetch_capacity_cleanup()
        finally:
            app._prefetch_state['max_illusts'] = old

        # pid1(10) 收藏最低被删；pid2(100) 保留，且在两个标签列表里都还在
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 1).first() is None
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 2).first() is not None
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_a').first().illust_ids) == [2]
        assert json.loads(clean_db.query(SearchCache).filter(SearchCache.tag == 'tag_b').first().illust_ids) == [2]

    def test_capacity_cleanup_protection_is_download_only(self, clean_db, monkeypatch):
        """保护只看"已下载 / 用户点过下载"：普通未刷新作品与它们一视同仁。

        受保护的作品不仅要留在库里，还要**留在标签列表里**（列表与行必须一起对齐）。
        两段共用同一套"再造两行未受保护作品"的准备，只有上限压得多狠不同，故合为一条。
        """
        from pixivviewer.models import DownloadLog
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            SearchCache(tag='tag_old', illust_ids='[1, 2, 3]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='dl', prefetch_source=1, download_status='done',
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='plain', prefetch_source=1,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=3, title='log', prefetch_source=1,
                   prefetch_refresh_at=refreshed),
            DownloadLog(pixiv_id=3, action='cancelled', message='用户取消待重试'),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 2)  # need_free = 1

        app._prefetch_capacity_cleanup()

        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {1, 3}, '已下载/点过下载的保留；无下载意图的普通作品被淘汰'
        # 被删除的作品也要从标签列表摘掉，未被删的保留
        ids = json.loads(clean_db.query(SearchCache).filter(
            SearchCache.tag == 'tag_old').first().illust_ids)
        assert ids == [1, 3]

        # 上限压到 0：受保护的照样留下，列表里也只剩它们
        clean_db.add_all([
            Illust(pixiv_id=4, title='b', prefetch_source=1, prefetch_refresh_at=refreshed),
            Illust(pixiv_id=5, title='c', prefetch_source=1, prefetch_refresh_at=refreshed),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 0)

        app._prefetch_capacity_cleanup()

        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {1, 3}, '受保护的作品哪怕上限为 0 也不能删'
        ids = json.loads(clean_db.query(SearchCache).filter(
            SearchCache.tag == 'tag_old').first().illust_ids)
        assert ids == [1, 3], '受保护的作品必须留在标签列表里'

    def test_capacity_cleanup_prefers_refreshed_rows(self, clean_db, monkeypatch):
        """三层淘汰：已刷新作品优先淘汰，未刷新作品在够用时不动。"""
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        clean_db.add_all([
            SearchCache(tag='tag_u', illust_ids='[1, 2, 3]',
                        cached_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
            Illust(pixiv_id=1, title='finalized-low', prefetch_source=1, bookmark_count=10,
                   prefetch_refresh_at=refreshed),
            Illust(pixiv_id=2, title='unfinalized', prefetch_source=1, bookmark_count=1),
            Illust(pixiv_id=3, title='unfinalized2', prefetch_source=1, bookmark_count=2),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 2)  # need_free = 1

        app._prefetch_capacity_cleanup()

        # 只删已刷新的 pid1；未刷新的 pid2/pid3 原样保留
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2, 3}
        ids = json.loads(clean_db.query(SearchCache).filter(
            SearchCache.tag == 'tag_u').first().illust_ids)
        assert ids == [2, 3]

    def test_capacity_cleanup_evicts_failed_unrefreshed_as_tier2(self, clean_db, monkeypatch):
        """层序：tier1（已最终刷新）不够时用 tier2（刷新失败过），且 tier1 **必须先于** tier2。

        两段是同一不变式（三层淘汰的**先后**）的两个输入：`need_free=2` 时两层都要动，
        `need_free=1` 时才区分得出"先删哪一层"。

        ⚠️ 第二段是变异验证挖出来的补钉：原用例只跑了 `need_free=2`，那时两层都要删，
        把实现里的 tier 顺序**整个反过来**（tier2 先删）用例依然全绿 —— 也就是说
        "已刷新的信号可信、先淘汰它"这条文档里的层序当时其实没人钉。别把第二段删掉。
        """
        refreshed = datetime(2021, 1, 1, tzinfo=timezone.utc)
        failed_at = datetime.now(timezone.utc)

        def seed():
            _wipe_illusts_and_tags(clean_db)
            clean_db.add_all([
                Illust(pixiv_id=1, title='refreshed', prefetch_source=1, bookmark_count=5,
                       prefetch_refresh_at=refreshed),
                Illust(pixiv_id=2, title='failed', prefetch_source=1, bookmark_count=1,
                       refresh_failed_at=failed_at),
                Illust(pixiv_id=3, title='fresh', prefetch_source=1, bookmark_count=2),
            ])
            safe_commit(clean_db)

        # ① tier1 不够 → 接着删 tier2；全新的 pid3 保留（还没到兜底层）
        seed()
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 1)  # need_free = 2
        app._prefetch_capacity_cleanup()
        assert {i.pixiv_id for i in clean_db.query(Illust).all()} == {3}

        # ② tier1 够用 → 只删 tier1，tier2 原样保留（层序反了这里就会留下 pid1、删掉 pid2）
        seed()
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 2)  # need_free = 1
        app._prefetch_capacity_cleanup()
        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert remaining == {2, 3}, \
            f'tier1（已刷新）必须先于 tier2 淘汰，实际留下 {remaining}'

    def test_capacity_cleanup_last_resort_enforces_cap(self, clean_db, monkeypatch):
        """第三层兜底：全是全新未刷新作品时也把上限压回去（不靠暂停入库）。"""
        clean_db.add_all([
            Illust(pixiv_id=i, title=f'new{i}', prefetch_source=1, bookmark_count=i)
            for i in (1, 2, 3, 4, 5)
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 2)  # need_free = 3

        app._prefetch_capacity_cleanup()

        remaining = {i.pixiv_id for i in clean_db.query(Illust).all()}
        assert len(remaining) == 2
        assert remaining == {4, 5}  # 收藏数高的留下

    def test_capacity_cleanup_never_evicts_user_owned(self, clean_db, monkeypatch):
        """兜底层同样不能碰用户拥有过的作品，哪怕它是唯一候选。"""
        from pixivviewer.models import DownloadLog
        clean_db.add_all([
            Illust(pixiv_id=1, title='touched', prefetch_source=1, bookmark_count=1),
            DownloadLog(pixiv_id=1, action='failed', message='下载失败待重试'),
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 0)

        app._prefetch_capacity_cleanup()

        assert clean_db.query(Illust).filter(Illust.pixiv_id == 1).first() is not None

    def test_prefetch_loop_always_ingests(self, clean_db, monkeypatch):
        """入库永不停：积压再大也照常预取（容量由三层淘汰压住，不靠暂停入库）。"""
        clean_db.add(SearchCache(tag='t'))
        clean_db.add_all([
            Illust(pixiv_id=7000 + i, title='x', prefetch_source=1)
            for i in range(50)
        ])
        safe_commit(clean_db)
        monkeypatch.setitem(app._prefetch_state, 'max_illusts', 10)
        fetched = []
        monkeypatch.setattr(app, 'search_by_tag',
                            lambda tag, **kwargs: (fetched.append(tag) or ([], False)))
        monkeypatch.setattr(background, '_prefetch_refresh_bookmarks', lambda: None)
        cleaned = []
        monkeypatch.setattr(app, '_prefetch_capacity_cleanup', lambda: cleaned.append(1))

        app._prefetch_loop()

        assert fetched == ['t']
        assert cleaned == [1]


class TestQueryCachedTagSort:
    def test_date_d_sort_with_multiple_none_upload_date(self, clean_db):
        clean_db.add_all([
            SearchCache(tag='x', illust_ids='[1, 2]', status='done'),
            Illust(pixiv_id=1, title='a', bookmark_count=5),
            Illust(pixiv_id=2, title='b', bookmark_count=5),
        ])
        safe_commit(clean_db)

        results, has_more, next_offset, filtered_total = app.query_cached_tag(
            'x', 0, 'date_d', 'or', 'all')

        assert {r['pixiv_id'] for r in results} == {1, 2}
        assert has_more is False
        assert next_offset == 0
        assert filtered_total == 2


class TestResetStuckPrefetch:
    def test_reset_stuck_fetching_tags(self, clean_db):
        """fetching 残留（进程重启打断预取）应被启动重置为 done。

        末尾一并覆盖"没有残留时是纯 no-op"：`done` 行与它的 error 都不许被动到
        —— 两者是同一个函数的两种输入，断言同构，故合为一条。
        """
        clean_db.add_all([
            SearchCache(tag='stuck', status='fetching', illust_ids='[1, 2]'),
            SearchCache(tag='normal', status='done', illust_ids='[3]'),
        ])
        safe_commit(clean_db)

        app._reset_stuck_prefetch()

        rows = {sc.tag: sc for sc in clean_db.query(SearchCache).all()}
        assert rows['stuck'].status == 'done'
        assert rows['stuck'].error == '上次预取被中断，已重置'
        assert rows['stuck'].illust_ids == '[1, 2]'  # 累积数据保留
        assert rows['normal'].status == 'done'  # done 状态不受影响
        assert rows['normal'].error == ''

        # 已经没有 fetching 残留了：再跑一次必须是纯 no-op
        app._reset_stuck_prefetch()
        clean_db.expire_all()
        rows = {sc.tag: sc for sc in clean_db.query(SearchCache).all()}
        assert rows['normal'].status == 'done'
        assert rows['normal'].error == ''


class TestPrefetchThreadLiveness:
    def test_interval_zero_pauses_thread_but_allows_rerun(self, monkeypatch):
        """interval=0 应暂停循环（睡 60s 继续检查）而非永久退出；重新置>0 后仍能执行预取。"""
        captured = {}

        class _FakeThread:
            def __init__(self, **kwargs):
                captured['target'] = kwargs.get('target')
                captured['daemon'] = kwargs.get('daemon')

            def start(self):
                pass

            def is_alive(self):
                # 与 threading.Thread 接口一致：`_start_prefetch_thread` 会把实例存进
                # background._prefetch_thread，后续健康检查（S12）要问它 is_alive()
                return False

        monkeypatch.setattr(app.threading, 'Thread', _FakeThread)

        sleeps = []
        loops = []

        def _fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                app._prefetch_state['interval'] = 5  # 暂停期间重新启用
            if len(sleeps) >= 3:
                raise RuntimeError('stop-loop')

        monkeypatch.setattr(app.time, 'sleep', _fake_sleep)
        monkeypatch.setattr(app, '_prefetch_loop', lambda: loops.append(1))

        old_interval = app._prefetch_state['interval']
        try:
            app._prefetch_state['interval'] = 60
            app._start_prefetch_thread()
            assert captured.get('target') is not None
            _run = captured['target']

            app._prefetch_state['interval'] = 0
            with pytest.raises(RuntimeError, match='stop-loop'):
                _run()
        finally:
            app._prefetch_state['interval'] = old_interval

        # interval=0 时走了 sleep(60) 暂停路径而非 break，随后重启用 interval=5 成功执行了 _prefetch_loop
        assert 60 in sleeps
        assert loops == [1]


class TestPrefetchRefreshBookmarks:
    """最终收藏数刷新：满 1 天刷新一次，<10 删除，保护已下载/用户点过下载的。"""

    def _old_illust(self, clean_db, pid, days=2, **kw):
        from pixivviewer import models
        old = datetime.now(timezone.utc) - timedelta(days=days)
        illust = Illust(pixiv_id=pid, title=f'p{pid}', prefetch_source=1,
                        bookmark_count=0, created_at=old, **kw)
        clean_db.add(illust)
        return illust

    def _mock_detail(self, monkeypatch, bookmark_count):
        class _FakeSession:
            def close(self):
                pass

        calls = []
        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail',
                            lambda s, pid, limiter=None, return_dead=False:
                            (calls.append(pid) or {'bookmark_count': bookmark_count}))
        return calls

    def _mock_dead_detail(self, monkeypatch):
        class _FakeSession:
            def close(self):
                pass

        calls = []
        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail',
                            lambda s, pid, limiter=None, return_dead=False:
                            (calls.append(pid) or fetcher.DEAD_DETAIL))
        return calls

    def test_refresh_updates_bookmark_once(self, clean_db, monkeypatch):
        """满 1 天才入选：拉一次详情写回最终收藏数，且只刷这一次。

        "该刷的"与"今天刚入库、还没满一天不该刷的"是同一入口的两种输入，故合为一条：
        后者必须既不发请求、也不动任何标记。
        """
        self._old_illust(clean_db, 5001)
        clean_db.add(SearchCache(tag='t', illust_ids='[5001]', status='done'))
        self._old_illust(clean_db, 5004, days=0)  # 今天入库 → 不满足满 1 天
        safe_commit(clean_db)
        calls = self._mock_detail(monkeypatch, 250)

        app._prefetch_refresh_bookmarks()

        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5001).first()
        assert illust.bookmark_count == 250
        assert illust.prefetch_refresh_at is not None
        assert calls == [5001]
        fresh = clean_db.query(Illust).filter(Illust.pixiv_id == 5004).first()
        assert fresh.prefetch_refresh_at is None, '今天入库的作品不参与本轮刷新'
        assert fresh.refresh_failed_at is None, '没到一天的作品不该被写退避标记'

        # 只刷新一次：第二次调用不再请求详情
        app._prefetch_refresh_bookmarks()
        assert calls == [5001]

    def test_refresh_deletes_low_bookmark(self, clean_db, monkeypatch):
        """最终收藏数 < 10：未受保护的当场删除并摘除缓存引用；受保护的原地更新、保留。

        同一分支"受保护与否"的两种输入，断言同构，故合为一条。
        """
        self._old_illust(clean_db, 5002)
        self._old_illust(clean_db, 5003, download_status='done')
        clean_db.add(SearchCache(tag='t', illust_ids='[5002]', status='done'))
        clean_db.add(SearchCache(tag='t_keep', illust_ids='[5003]', status='done'))
        safe_commit(clean_db)
        self._mock_detail(monkeypatch, 3)

        app._prefetch_refresh_bookmarks()

        assert clean_db.query(Illust).filter(Illust.pixiv_id == 5002).first() is None
        sc = clean_db.query(SearchCache).filter(SearchCache.tag == 't').first()
        assert json.loads(sc.illust_ids) == []
        kept = clean_db.query(Illust).filter(Illust.pixiv_id == 5003).first()
        assert kept is not None, '已下载保护，不删'
        assert kept.bookmark_count == 3
        assert kept.prefetch_refresh_at is not None

    def test_refresh_pass_keeps_queued_download(self, clean_db, monkeypatch):
        """排队中的作品即使详情已永久失效（DEAD_DETAIL）也不能被刷新清理删掉。

        审计 S3：这个窗口里行状态是 None、也没有 DownloadLog，`_is_user_owned`
        判不出来，旧实现会直接删行 → 排队中的下载静默消失。
        """
        from pixivviewer import runtime
        self._old_illust(clean_db, 5011)
        clean_db.add(SearchCache(tag='t', illust_ids='[5011]', status='done'))
        safe_commit(clean_db)
        self._mock_dead_detail(monkeypatch)
        with runtime._download_queue_lock:
            runtime._queued_downloads.add(5011)
        try:
            app._prefetch_refresh_bookmarks()
        finally:
            with runtime._download_queue_lock:
                runtime._queued_downloads.discard(5011)

        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5011).first()
        assert illust is not None, '排队中的下载不能被最终收藏数刷新删掉'
        assert illust.prefetch_refresh_at is not None  # kept_dead：保留并标记完成

    def test_refresh_generic_exception_does_not_bubble(self, clean_db, monkeypatch):
        """未分类异常（如详情解析 KeyError）不许冒泡：否则容量清理被跳过、上限失效。"""
        self._old_illust(clean_db, 5012)
        clean_db.add(SearchCache(tag='t', illust_ids='[5012]', status='done'))
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())

        def _boom(session, pid, limiter=None, return_dead=False):
            raise KeyError('unexpected payload')

        monkeypatch.setattr(app.fetcher, '_get_illust_detail', _boom)

        app._prefetch_refresh_bookmarks()          # 关键：不抛

        assert app._prefetch_state['refresh_stats']['aborted'] == 'unknown'
        assert clean_db.query(Illust).filter(Illust.pixiv_id == 5012).first() is not None

    def test_refresh_backoff_marker_lifecycle(self, clean_db, monkeypatch):
        """暂时性失败的退避状态机：写标记 → 退避期内不再入选 → 过期后重试并清标记。

        回归背景：旧实现失败静默 continue、无任何标记，永久失败的死作品每轮占满
        100 个名额（head-of-line blocking），后续作品被饿死。三段是同一条状态机的
        前后相继（同一套"老作品 + 假详情"），故合为一条。
        """
        self._old_illust(clean_db, 5005)
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())

        # ① 暂时性失败（详情返回 None）→ 写退避标记，但不标记刷新完成
        failed = []
        monkeypatch.setattr(app.fetcher, '_get_illust_detail',
                            lambda s, pid, limiter=None, return_dead=False:
                            (failed.append(pid) or None))

        app._prefetch_refresh_bookmarks()

        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5005).first()
        assert illust is not None
        assert illust.prefetch_refresh_at is None
        assert illust.refresh_failed_at is not None  # 退避标记已写

        # ② 刚写过标记的那条：第二轮不再尝试（标记生效）
        app._prefetch_refresh_bookmarks()

        assert failed == [5005], '刚写过退避标记的作品，退避期内不该再发一次详情请求'

        # ③ 退避期内（1 小时前失败）的其它作品：同样不入选，不浪费名额
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        self._old_illust(clean_db, 5006)
        self._old_illust(clean_db, 5007)
        for pid in (5006, 5007):
            row = clean_db.query(Illust).filter(Illust.pixiv_id == pid).first()
            row.refresh_failed_at = recent
        safe_commit(clean_db)

        app._prefetch_refresh_bookmarks()

        assert failed == [5005], '仍在退避期内的作品一个都不该入选'

        # ④ 退避过期（2 天前失败）→ 重新入选并重试，成功后清掉失败标记
        old = datetime.now(timezone.utc) - timedelta(days=2)
        row = clean_db.query(Illust).filter(Illust.pixiv_id == 5007).first()
        row.refresh_failed_at = old
        safe_commit(clean_db)
        calls = self._mock_detail(monkeypatch, 200)

        app._prefetch_refresh_bookmarks()

        assert calls == [5007]
        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5007).first()
        assert illust.prefetch_refresh_at is not None
        assert illust.refresh_failed_at is None  # 成功清标记

    def test_refresh_deletes_permanently_dead(self, clean_db, monkeypatch):
        """永久失败（DEAD_DETAIL，404/删除类）：未下载且无用户下载日志 → 删行。

        删除要同时满足三件事：行没了、**全部**标签列表里的引用摘掉、留一条
        `prefetch_deleted` 日志（该 action 不构成保护，否则同一作品再次入库后
        会获得永久豁免）。三件事都发生在同一次删除里，故合为一条。
        """
        from pixivviewer.models import DownloadLog
        self._old_illust(clean_db, 5008)
        clean_db.add(SearchCache(tag='t', illust_ids='[5008]', status='done'))
        clean_db.add(SearchCache(tag='t2', illust_ids='[5008, 900]', status='done'))
        self._old_illust(clean_db, 5013)
        safe_commit(clean_db)
        self._mock_dead_detail(monkeypatch)

        app._prefetch_refresh_bookmarks()

        assert clean_db.query(Illust).filter(Illust.pixiv_id == 5008).first() is None
        sc = clean_db.query(SearchCache).filter(SearchCache.tag == 't').first()
        assert json.loads(sc.illust_ids) == []
        sc2 = clean_db.query(SearchCache).filter(SearchCache.tag == 't2').first()
        assert json.loads(sc2.illust_ids) == [900]

        assert clean_db.query(Illust).filter(Illust.pixiv_id == 5013).first() is None
        log = clean_db.query(DownloadLog).filter(DownloadLog.pixiv_id == 5013).first()
        assert log is not None
        assert log.action == 'prefetch_deleted'
        assert background._is_user_owned(clean_db, 5013) is False

    def test_refresh_keeps_dead_protected(self, clean_db, monkeypatch):
        """永久失败但受保护（已下载 / 用户点过下载）→ 保留行、标记刷新完成退出队列。

        两条保护渠道共用同一条守卫，只有"保护从哪来"不同，故合为一条。
        """
        from pixivviewer.models import DownloadLog
        self._old_illust(clean_db, 5009, download_status='done')
        self._old_illust(clean_db, 5012)
        clean_db.add(DownloadLog(pixiv_id=5012, action='failed', message='下载失败'))
        safe_commit(clean_db)
        self._mock_dead_detail(monkeypatch)

        app._prefetch_refresh_bookmarks()

        downloaded = clean_db.query(Illust).filter(Illust.pixiv_id == 5009).first()
        assert downloaded is not None
        assert downloaded.prefetch_refresh_at is not None
        assert downloaded.refresh_failed_at is None
        logged = clean_db.query(Illust).filter(Illust.pixiv_id == 5012).first()
        assert logged is not None
        assert logged.prefetch_refresh_at is not None

    def test_refresh_force_done_after_long_failure(self, clean_db, monkeypatch):
        """失败超过 FORCE_DONE（14 天）→ 无网络请求，直接强制标记完成。

        兜底意义：未刷新的作品豁免容量淘汰，长期失败不兜底会无限累积、
        单独顶破容量上限。
        """
        long_ago = datetime.now(timezone.utc) - timedelta(days=15)
        self._old_illust(clean_db, 5011)
        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5011).first()
        illust.refresh_failed_at = long_ago
        safe_commit(clean_db)
        calls = self._mock_detail(monkeypatch, 100)

        app._prefetch_refresh_bookmarks()

        assert calls == []  # 未发起任何详情请求
        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 5011).first()
        assert illust.prefetch_refresh_at is not None
        assert illust.refresh_failed_at is None

    def test_refresh_auth_error_aborts_without_backoff_marker(self, clean_db, monkeypatch):
        """认证失效（PixivAuthError）：中止本轮、不写退避标记、异常不逃逸，
        且整轮走完后容量清理照常执行。

        回归背景：① 异常冒泡到 `_prefetch_loop` 会让容量清理被跳过 → 上限彻底失效；
        ② 若给作品写退避标记，Cookie 修好后这批作品还要白等 24h。刷新级与循环级是
        同一件事的两面（"中止但不冒泡"），故合为一条：走 `_prefetch_loop` 跑完整一轮。
        """
        clean_db.add(SearchCache(tag='t'))
        self._old_illust(clean_db, 5020)
        self._old_illust(clean_db, 5021)
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        calls = []
        cleaned = []

        def _raise_auth(s, pid, limiter=None, return_dead=False):
            calls.append(pid)
            raise fetcher.PixivAuthError('HTTP 401')

        monkeypatch.setattr(app, 'search_by_tag', lambda tag, **kwargs: ([], False))
        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail', _raise_auth)
        monkeypatch.setattr(app, '_prefetch_capacity_cleanup',
                            lambda: cleaned.append(1))

        app._prefetch_loop()  # 不应抛异常

        assert calls == [5020]  # 第一条即认证失败 → 中止，不再请求第二条
        for pid in (5020, 5021):
            illust = clean_db.query(Illust).filter(Illust.pixiv_id == pid).first()
            assert illust is not None
            assert illust.prefetch_refresh_at is None
            assert illust.refresh_failed_at is None  # 全局问题不记在作品头上
        assert cleaned == [1], '认证失效只中止刷新，容量清理必须照常执行（否则上限失效）'

    def test_refresh_global_failure_streak(self, clean_db, monkeypatch):
        """限流/连接错误的连续计数：连满 N 条即熔断，普通失败会重置计数。

        限流是账户级状态：按单作品记退避会把整队列刷上 24h，Pixiv 恢复后还要多等一天；
        但偶发的一条失败也不能误熔断。两个方向共用同一套"老作品 + 按序列返回的假详情"，
        只有喂进去的序列不同，故合为一条。
        """
        class _FakeSession:
            def close(self):
                pass

        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())

        # ① 连续 3 条全局性失败 → 熔断，且一条退避标记都不写
        for pid in range(5030, 5036):
            self._old_illust(clean_db, pid)
        safe_commit(clean_db)
        calls = []
        monkeypatch.setattr(app.fetcher, '_get_illust_detail',
                            lambda s, pid, limiter=None, return_dead=False:
                            (calls.append(pid) or fetcher.RETRYABLE_GLOBAL_DETAIL))

        app._prefetch_refresh_bookmarks()

        assert len(calls) == config.PREFETCH_REFRESH_ABORT_STREAK  # 凑满即熔断
        for pid in range(5030, 5036):
            illust = clean_db.query(Illust).filter(Illust.pixiv_id == pid).first()
            assert illust.refresh_failed_at is None
            assert illust.prefetch_refresh_at is None

        # ② 中间夹一条普通失败 → 连续计数被重置，不会误熔断
        _wipe_illusts_and_tags(clean_db)
        for pid in range(5040, 5046):
            self._old_illust(clean_db, pid)
        safe_commit(clean_db)
        seq = [fetcher.RETRYABLE_GLOBAL_DETAIL, None,
               fetcher.RETRYABLE_GLOBAL_DETAIL, fetcher.RETRYABLE_GLOBAL_DETAIL,
               fetcher.RETRYABLE_GLOBAL_DETAIL, {'bookmark_count': 50}]
        calls = []

        def _fake(s, pid, limiter=None, return_dead=False):
            calls.append(pid)
            return seq[len(calls) - 1]

        monkeypatch.setattr(app.fetcher, '_get_illust_detail', _fake)

        app._prefetch_refresh_bookmarks()

        # 第 2 条普通失败重置计数 → 第 5 条才凑满 3 连击，第 6 条不再尝试
        assert calls == [5040, 5041, 5042, 5043, 5044]
        marked = clean_db.query(Illust).filter(Illust.pixiv_id == 5041).first()
        assert marked.refresh_failed_at is not None  # 普通失败照常写退避

    def test_refresh_stats_recorded(self, clean_db, monkeypatch):
        """每轮刷新写入结构化统计（供 /api/prefetch/status 与设置页展示）。

        一并覆盖中止原因：认证失效要能在设置页看见"这轮为什么没刷完"。两段都是
        "跑完一轮后读统计"，断言同构，故合为一条。
        """
        self._old_illust(clean_db, 5060)                          # 成功
        self._old_illust(clean_db, 5061)                          # 低收藏 → 删除
        self._old_illust(clean_db, 5062)                          # 暂时性失败
        self._old_illust(clean_db, 5063)                          # 永久失败 → 删除
        self._old_illust(clean_db, 5064, download_status='done')  # 永久失败但已下载 → 保留
        safe_commit(clean_db)

        class _FakeSession:
            def close(self):
                pass

        seq = [{'bookmark_count': 100}, {'bookmark_count': 3}, None,
               fetcher.DEAD_DETAIL, fetcher.DEAD_DETAIL]
        calls = []

        def _fake(s, pid, limiter=None, return_dead=False):
            calls.append(pid)
            return seq[len(calls) - 1]

        monkeypatch.setattr(app, 'build_pixiv_session', lambda: _FakeSession())
        monkeypatch.setattr(app.fetcher, '_get_illust_detail', _fake)

        app._prefetch_refresh_bookmarks()

        stats = app._prefetch_state['refresh_stats']
        assert stats['processed'] == 5
        assert stats['ok'] == 2            # 5060 + 5061（5061 随后按低收藏删除）
        assert stats['deleted_low'] == 1
        assert stats['deleted_dead'] == 1
        assert stats['kept_dead'] == 1
        assert stats['failed_transient'] == 1
        assert stats['failed_global'] == 0
        assert stats['aborted'] == ''
        assert stats['at'] is not None

        # 认证失效：中止原因也要如实写进统计（否则设置页只说"这轮没刷完"）
        _wipe_illusts_and_tags(clean_db)
        self._old_illust(clean_db, 5070)
        safe_commit(clean_db)

        def _raise_auth(s, pid, limiter=None, return_dead=False):
            raise fetcher.PixivAuthError('HTTP 401')

        monkeypatch.setattr(app.fetcher, '_get_illust_detail', _raise_auth)

        app._prefetch_refresh_bookmarks()

        stats = app._prefetch_state['refresh_stats']
        assert stats['aborted'] == 'auth'
        assert stats['processed'] == 1


class TestResetPrefetchRefresh:
    """手动把作品放回刷新队列：必须指定范围，只影响预取来源作品。"""

    def test_reset_requires_scope(self, clean_db):
        """两种"范围无效"都必须原地返回 0：完全没给范围 / 给的标签不存在。

        范围无效等于什么都不做（绝不能退化成"清全库"），两条断言同构，故合为一条。
        """
        clean_db.add(Illust(pixiv_id=9200, title='a', prefetch_source=1,
                            refresh_failed_at=datetime.now(timezone.utc)))
        safe_commit(clean_db)

        assert app.reset_prefetch_refresh() == 0
        clean_db.expire_all()
        illust = clean_db.query(Illust).filter(Illust.pixiv_id == 9200).first()
        assert illust.refresh_failed_at is not None

        assert app.reset_prefetch_refresh(tag='nope') == 0

    def test_reset_tag_ignores_non_prefetch_and_other_tags(self, clean_db):
        clean_db.add_all([
            SearchCache(tag='t', illust_ids='[9201, 9202]'),
            Illust(pixiv_id=9201, title='a', prefetch_source=1,
                   refresh_failed_at=datetime.now(timezone.utc)),
            Illust(pixiv_id=9202, title='b',
                   refresh_failed_at=datetime.now(timezone.utc)),  # 非预取来源
            Illust(pixiv_id=9203, title='c', prefetch_source=1,
                   refresh_failed_at=datetime.now(timezone.utc)),  # 不在该标签列表
        ])
        safe_commit(clean_db)

        assert app.reset_prefetch_refresh(tag='t') == 1
        clean_db.expire_all()
        assert clean_db.query(Illust).filter(
            Illust.pixiv_id == 9201).first().refresh_failed_at is None
        assert clean_db.query(Illust).filter(
            Illust.pixiv_id == 9202).first().refresh_failed_at is not None
        assert clean_db.query(Illust).filter(
            Illust.pixiv_id == 9203).first().refresh_failed_at is not None

    def test_reset_tag_with_large_id_list_uses_json_each(self, clean_db):
        """整条 id 数组走 json_each 下推（单个绑定参数），不拼上万参数的 IN。"""
        ids = list(range(9300, 9400))
        clean_db.add(SearchCache(tag='big', illust_ids=json.dumps(ids)))
        clean_db.add_all([
            Illust(pixiv_id=i, title='x', prefetch_source=1,
                   refresh_failed_at=datetime.now(timezone.utc)) for i in ids
        ])
        safe_commit(clean_db)

        assert app.reset_prefetch_refresh(tag='big') == 100


class TestDownloadLockRegistry:
    """同一作品的下载锁：收尾时只能注销自己那把。

    回归：`_download_illust` 的 finally 曾无条件 pop。gunicorn --threads 下，
    任务 A 在 release 之后、pop 之前若被抢占，任务 B 会拿到 A 那把已释放的锁并
    开始下载，A 随后把它 pop 掉 —— 任务 C 再进来就拿到一把全新锁，同一作品被
    并发下载两次。
    """

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        from pixivviewer import background
        background.download_locks.clear()
        yield
        background.download_locks.clear()

    def test_release_only_unregisters_own_lock(self):
        """三种输入一表验完：自己那把要注销、被别人顶替的那把不能误删、无条目是 no-op。

        三者是同一个函数的行为矩阵（原先三条用例的前置准备与断言同构），故合为一条。
        """
        lock = threading.Lock()
        background.download_locks[555] = lock
        background._release_download_lock(555, lock)
        assert 555 not in background.download_locks, '自己那把要注销'

        old, new = threading.Lock(), threading.Lock()
        background.download_locks[555] = old
        background.download_locks[555] = new      # 新任务已顶替
        background._release_download_lock(555, old)
        assert background.download_locks[555] is new, '新任务的锁不能被旧任务误删'

        background._release_download_lock(999, threading.Lock())   # 不应抛异常
        assert 999 not in background.download_locks, '不存在的条目是纯 no-op'


class TestPrefetchRoundResilience:
    """审计 S4：单轮里任何一段异常都不许让本轮的容量清理被跳过。

    背景：容量清理是 `prefetch_max_illusts` 上限的**唯一**执行者（"暂停入库"
    方案已否决），一旦被异常跳过，入库就没人压得住，库会无限增长。
    """

    def _one_tag(self, clean_db):
        clean_db.add(SearchCache(tag='t'))
        safe_commit(clean_db)

    def test_prefetch_loop_survives_any_stage_error(self, clean_db, monkeypatch):
        """单轮里三处异常源（刷新阶段 / 单个标签 / 容量清理自己）都不许逃逸出循环，
        且只要走到清理那一步就必须真的执行它。

        三段的共同不变式就是本类的类注释：容量清理是上限的唯一执行者，任何一段异常
        都不能让它被跳过或让守护线程死掉；三段只有"哪一段炸"不同，故合为一条。
        """
        self._one_tag(clean_db)
        monkeypatch.setattr(app, 'search_by_tag', lambda tag, **kwargs: ([], False))

        # 循环里用的是裸名，解析发生在 background 的全局命名空间 → 补丁必须打在
        # background（打 app 命名空间对裸名调用无效）
        def _boom_refresh():
            raise RuntimeError('refresh exploded')

        monkeypatch.setattr(background, '_prefetch_refresh_bookmarks', _boom_refresh)
        cleaned = []
        monkeypatch.setattr(app, '_prefetch_capacity_cleanup', lambda: cleaned.append(1))

        app._prefetch_loop()

        assert cleaned == [1], '刷新阶段抛异常时容量清理仍必须执行'
        assert app._prefetch_state['running'] is False, '异常不得逃逸出循环'

        # ② 单个标签抛异常
        monkeypatch.setattr(background, '_prefetch_refresh_bookmarks', lambda: None)

        def _boom_tag(tag):
            raise RuntimeError('tag exploded')

        monkeypatch.setattr(background, '_prefetch_one_tag', _boom_tag)
        cleaned.clear()

        app._prefetch_loop()

        assert cleaned == [1], '单个标签抛异常时容量清理仍必须执行'

        # ③ 容量清理自己抛异常：同样不许逃逸（守护线程靠它继续存活）
        monkeypatch.setattr(background, '_prefetch_one_tag', lambda tag: None)

        def _boom_cleanup():
            raise RuntimeError('cleanup failed')

        monkeypatch.setattr(app, '_prefetch_capacity_cleanup', _boom_cleanup)

        app._prefetch_loop()

        assert app._prefetch_state['running'] is False, '清理自身抛异常也不得逃逸出循环'

    def test_prefetch_one_tag_status_write_failure_does_not_raise(self, clean_db, monkeypatch,
                                                                 caplog):
        """失败状态回写再失败也不许冒泡（否则整轮中止、且 fetching 残留无人处理）。"""
        from sqlalchemy.exc import OperationalError
        clean_db.add(SearchCache(tag='t', status='done'))
        safe_commit(clean_db)

        def _boom_search(tag, **kwargs):
            raise RuntimeError('search exploded')

        monkeypatch.setattr(app, 'search_by_tag', _boom_search)
        real_commit = background.safe_commit
        calls = {'n': 0}

        def _flaky_commit(db, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                return real_commit(db, *args, **kwargs)   # 抢占 fetching 成功
            db.rollback()                                 # 与真实 safe_commit 同款语义
            raise OperationalError('UPDATE search_cache', {},
                                   Exception('database is locked'))

        monkeypatch.setattr(background, 'safe_commit', _flaky_commit)

        with caplog.at_level(logging.ERROR, logger='pixivviewer.background'):
            background._prefetch_one_tag('t')             # 关键：不抛

        assert calls['n'] == 2
        assert '失败状态回写失败' in caplog.text
        row = clean_db.query(SearchCache).filter(SearchCache.tag == 't').first()
        assert row.status == 'fetching', '回写失败的残留状态如实保留（由日志暴露）'


# ── SearchCache.illust_ids 并发一致性（审计 S10）──


class _SpyLock:
    """记下 `with` 进入次数与"当前持有者是哪个线程"的锁（代理真锁，不改互斥语义）。

    记 owner 而不是用 `Lock.locked()`：后者是所有线程共享的视图，别的线程持锁时
    也会是 True，断言分不清是谁在锁里。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.entries = 0
        self.owner: int | None = None

    def __enter__(self):
        self._lock.acquire()
        self.entries += 1
        self.owner = threading.get_ident()
        return self

    def __exit__(self, *exc):
        self.owner = None
        self._lock.release()
        return False


def _search_cache_dirty(db, marker: int) -> bool:
    """本次会话里是否有一行 SearchCache 被改成了含 marker 的 illust_ids。"""
    return any(isinstance(obj, SearchCache) and str(marker) in (obj.illust_ids or '')
               for obj in db.dirty)


def test_search_cache_merge_and_remove_serialized(clean_db, monkeypatch):
    """并发删除引用不得被预取合并的过期快照写回（幽灵引用）。

    编排：把合并的 commit 卡在"已经读到旧 illust_ids、还没提交"的那一刻，另一
    线程在这段窗口里删掉引用并提交。
    - 有 `_search_cache_guard`：删除侧要等合并的读改写（含提交）走完，最终状态
      里不含已删 pid；
    - 没有锁：合并会把自己那份过期快照写回去，pid 复活（这就是 S10 的丢更新）。
    """
    tag, victim, fresh = 'conc', 9201, 9202
    clean_db.add(SearchCache(tag=tag, illust_ids=json.dumps([victim]), status='done'))
    clean_db.add(Illust(pixiv_id=victim, title='victim'))
    safe_commit(clean_db)

    merge_at_commit = threading.Event()
    removal_done = threading.Event()
    real_commit = background.safe_commit

    def _slow_merge_commit(db, *args, **kwargs):
        if _search_cache_dirty(db, fresh) and not merge_at_commit.is_set():
            merge_at_commit.set()
            removal_done.wait(0.5)     # 让出窗口：删除侧若没锁就会插进来
        return real_commit(db, *args, **kwargs)

    monkeypatch.setattr(background, 'safe_commit', _slow_merge_commit)

    pages = []

    def _fake_search(t, **kwargs):
        pages.append(t)
        if len(pages) == 1:
            return [{'pixiv_id': fresh}], False
        return [], False

    monkeypatch.setattr(app, 'search_by_tag', _fake_search)

    errors: list[BaseException] = []

    def _merge():
        try:
            background._prefetch_one_tag(tag)
        except BaseException as e:      # noqa: BLE001 —— 汇总后统一断言
            errors.append(e)

    merger = threading.Thread(target=_merge, daemon=True)
    try:
        merger.start()
        assert merge_at_commit.wait(5), '合并没有走到"读到旧值、尚未提交"那一刻'

        with get_session() as db:
            background._remove_pids_from_search_caches(db, [victim])
            safe_commit(db)
        removal_done.set()
    finally:
        # 断言失败也必须把线程收回来：它还持着 _search_cache_guard，泄漏出去会污染
        # 后续用例（这正是本用例第一次写成时踩到的坑）
        merger.join(10)

    assert not errors, f'合并线程出错：{errors!r}'
    assert not merger.is_alive()
    with get_session() as db:
        ids = json.loads(db.query(SearchCache).filter(SearchCache.tag == tag).first().illust_ids)
    assert victim not in ids, f'已删引用被合并的过期快照写回（幽灵引用）：{ids}'
    assert fresh in ids, '本次预取抓到的作品必须在缓存里'


def test_search_cache_lock_scope_excludes_network(clean_db, monkeypatch):
    """锁只罩 illust_ids 的读改写与提交，不罩搜索等网络阶段。"""
    clean_db.add(Illust(pixiv_id=9301, title='x'))
    safe_commit(clean_db)

    guard = _SpyLock()
    monkeypatch.setattr(background, '_search_cache_guard', guard)

    held_during_search: list[bool] = []

    def _fake_search(tag, **kwargs):
        held_during_search.append(guard.owner == threading.get_ident())
        if len(held_during_search) == 1:
            return [{'pixiv_id': 9301}], False
        return [], False

    monkeypatch.setattr(app, 'search_by_tag', _fake_search)

    held_at_merge_commit: list[bool] = []
    real_commit = background.safe_commit

    def _spy_commit(db, *args, **kwargs):
        if _search_cache_dirty(db, 9301):
            held_at_merge_commit.append(guard.owner == threading.get_ident())
        return real_commit(db, *args, **kwargs)

    monkeypatch.setattr(background, 'safe_commit', _spy_commit)

    background._prefetch_one_tag('lock_scope')

    assert held_during_search, '搜索没被调用？'
    assert not any(held_during_search), '网络阶段持锁会把删除请求一起拖住'
    assert held_at_merge_commit == [True], '合并的读改写（含提交）必须在锁内'
    assert guard.entries == 1, '只有合并那一段该进锁'


def test_remove_pids_from_search_caches_holds_guard(clean_db, monkeypatch):
    """删除引用侧也要持同一把锁（否则合并侧的互斥是空的）。"""
    guard = _SpyLock()
    monkeypatch.setattr(background, '_search_cache_guard', guard)
    clean_db.add(SearchCache(tag='g', illust_ids='[1, 2]', status='done'))
    safe_commit(clean_db)

    with get_session() as db:
        background._remove_pids_from_search_caches(db, [1])
        safe_commit(db)

    assert guard.entries == 1, '删除引用必须整体在 _search_cache_guard 内'
    row = clean_db.query(SearchCache).filter(SearchCache.tag == 'g').first()
    assert json.loads(row.illust_ids) == [2]


def test_remove_pids_empty_list_does_not_touch_guard(clean_db, monkeypatch):
    """空 pid 列表直接返回：不必要地拿锁会平白和预取合并串行化。"""
    guard = _SpyLock()
    monkeypatch.setattr(background, '_search_cache_guard', guard)

    with get_session() as db:
        background._remove_pids_from_search_caches(db, [])

    assert guard.entries == 0


# ── 后台线程心跳（审计 S12）──

def test_prefetch_loop_records_failures(clean_db, monkeypatch):
    """单轮异常不退出线程，但要留痕：last_error 记下 → 下一轮干净收尾时清空。

    覆盖两处异常源（读标签列表都失败 / 单个标签失败）—— 它们共同的不变式是
    "不冒泡 + 留痕 + `running` 复位"，断言同构，故合为一条。
    """
    state = background._prefetch_state
    monkeypatch.setitem(state, 'last_error', None)

    def _boom_session():
        raise RuntimeError('database is locked')

    with monkeypatch.context() as m:
        # 标签列表都读不出来（DB 不可用）也要留痕，并且不能把线程打死
        m.setattr(app, 'get_session', _boom_session)
        background._prefetch_loop()          # 关键：不抛

    assert 'database is locked' in state['last_error']
    assert state['running'] is False, 'finally 必须复位 running'

    clean_db.add(SearchCache(tag='boom', status='done'))
    safe_commit(clean_db)
    monkeypatch.setattr(background, '_prefetch_refresh_bookmarks', lambda: None)
    monkeypatch.setattr(app, '_prefetch_capacity_cleanup', lambda: None)

    def _boom(tag):
        raise RuntimeError('网络挂了')

    monkeypatch.setattr(background, '_prefetch_one_tag', _boom)
    background._prefetch_loop()
    assert '网络挂了' in state['last_error'], '出错必须留痕（否则只能看 last_check 变旧）'
    assert state['last_check'], '出错的那轮也要更新时间戳（线程确实在跑）'

    monkeypatch.setattr(background, '_prefetch_one_tag', lambda tag: None)
    background._prefetch_loop()
    assert state['last_error'] is None, '整轮干净收尾才清空：非空即代表最近一轮就有问题'
