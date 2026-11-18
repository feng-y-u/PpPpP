"""X 后台引擎的不变式：卡死自愈、入库阶段不裸奔、下载互斥。

这三条都来自 2026-11-18 代码审查的发现：
- `fetching` 标记先于网络 I/O 提交，进程死在中间会让标签被永久跳过（仓库给
  预取修过一模一样的 `_reset_stuck_prefetch`，X 侧当时漏了对等物）；
- 入库阶段在 try/except 外，DB 异常会让标签停在 `fetching` 且轮次被中断；
- 路由只挡 `done`，重复提交的任务靠每作品互斥锁拦住。
"""
from __future__ import annotations

import threading

from pixivviewer import background
from pixivviewer.models import XTag, XWork


def _add_tag(clean_db, tag='#t', status='idle'):
    row = XTag(tag=tag, enabled=1, status=status)
    clean_db.add(row)
    clean_db.commit()
    return row


def _add_work(clean_db, tweet_id='1800000000000000001', saved_status=None):
    work = XWork(tweet_id=tweet_id, tag='#t', author_name='A',
                 author_screen_name='a', media_count=1, saved_status=saved_status)
    work.media_list = [{'url': 'https://pbs.twimg.com/media/x.jpg?name=orig', 'w': 1, 'h': 1}]
    clean_db.add(work)
    clean_db.commit()
    return work


class TestResetStuckX:
    def test_startup_resets_fetching_tags_and_stuck_works(self, clean_db):
        """启动即重置：fetching 标签 / queued、downloading 作品都不能残留。"""
        _add_tag(clean_db, tag='#stuck', status='fetching')
        _add_tag(clean_db, tag='#ok', status='done')
        w1 = _add_work(clean_db, tweet_id='1800000000000000001', saved_status='queued')
        w2 = _add_work(clean_db, tweet_id='1800000000000000002', saved_status='downloading')
        w3 = _add_work(clean_db, tweet_id='1800000000000000003', saved_status='done')

        background._reset_stuck_x()

        stuck = clean_db.query(XTag).filter_by(tag='#stuck').one()
        assert stuck.status == 'error'
        assert '中断' in stuck.error
        ok = clean_db.query(XTag).filter_by(tag='#ok').one()
        assert ok.status == 'done', '非 fetching 标签不能被动'
        for work, expected in ((w1, 'failed'), (w2, 'failed')):
            clean_db.refresh(work)
            assert work.saved_status == expected
        clean_db.refresh(w3)
        assert w3.saved_status == 'done', '已完成下载不受影响'

    def test_reset_is_idempotent_on_clean_state(self, clean_db):
        """空库/无残留时调用不报错也不写任何行。"""
        background._reset_stuck_x()
        assert clean_db.query(XTag).count() == 0


class TestXFetchTagResilience:
    def test_store_phase_failure_marks_tag_error_not_fetching(self, clean_db, monkeypatch):
        """入库阶段的 DB 异常必须落到标签 status='error'，不能让标签停在 'fetching'。"""
        _add_tag(clean_db, tag='#t', status='idle')
        # 网络阶段成功（返回空批），异常点在入库
        monkeypatch.setattr(background.x_client, 'search_timeline',
                            lambda tag, cursor=None: ([], None))

        def _boom(db, tag, tweets):
            raise RuntimeError('db exploded')

        monkeypatch.setattr(background, '_x_store_tweets', _boom)

        out = background._x_fetch_tag('#t')

        assert out['error'] and '入库' in out['error']
        row = clean_db.query(XTag).filter_by(tag='#t').one()
        assert row.status == 'error', '标签不得残留在 fetching（会被永久跳过）'
        assert '入库' in row.error

    def test_in_flight_tag_is_skipped(self, clean_db):
        """正在抓取的标签直接跳过（抢占检查本身也要钉住）。"""
        _add_tag(clean_db, tag='#busy', status='fetching')
        out = background._x_fetch_tag('#busy')
        assert '正在抓取' in (out['error'] or '')
        row = clean_db.query(XTag).filter_by(tag='#busy').one()
        assert row.status == 'fetching', '跳过时不得改写状态'


class TestXDownloadLock:
    def test_second_submission_skips_while_lock_held(self, clean_db, monkeypatch):
        """同作品重复提交：锁被占用时直接返回，不得把 DB 状态推进成 downloading。"""
        work = _add_work(clean_db, saved_status=None)
        lock = threading.Lock()
        lock.acquire()
        # 直接模拟"另一个线程正在下载这条"：函数读的是模块级 dict，
        # 换成一个已被占用的锁即可确定性地钉住抢占分支
        monkeypatch.setattr(background, '_x_download_locks', {work.id: lock})
        try:
            background._download_x_work(work.id)
        finally:
            lock.release()

        clean_db.refresh(work)
        assert work.saved_status is None, '锁被占用时不得推进下载状态'
        assert work.saved_paths_list == []

    def test_lock_release_unregisters_only_own_lock(self, clean_db):
        """注销资源只删自己那份：别的任务换上的新锁不能被 pop 掉。"""
        old = threading.Lock()
        new = threading.Lock()
        background._x_download_locks.clear()
        # 表里已被并发新任务换成 new；释放方持有的还是自己的 old —— 不能动 new
        background._x_download_locks[1] = new
        background._release_x_download_lock(1, old)
        assert background._x_download_locks[1] is new, '不得删掉并发新任务的锁'
        background._release_x_download_lock(1, new)
        assert 1 not in background._x_download_locks
        background._x_download_locks.clear()
