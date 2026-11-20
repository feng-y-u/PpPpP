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


class TestXFetchProgress:
    """抓取进度的状态机：页面靠它渲染"抓取中 1/3 页 · 12 条"。

    三条硬边界：fetching 期间每翻一页更新一次；失败必须收口成 error（页面
    上的"正在抓取"不能永远转下去）；成功收口成 done。启动重置还要把进程
    挂掉时停在 fetching 的进度翻成 error。
    """

    def _tweet(self, **kw):
        from pixivviewer.x_client import XMedia, XTweet
        base = dict(tweet_id='1800000000000000951', author_name='画师',
                    author_screen_name='Artist_X', text='t',
                    media=[XMedia(url='https://pbs.twimg.com/media/a.jpg', width=1, height=1)])
        base.update(kw)
        return XTweet(**base)

    def test_success_marks_done_with_stats(self, clean_db, monkeypatch):
        _add_tag(clean_db, tag='#pg')
        batch = [self._tweet(tweet_id=f'18000000000000009{i}') for i in range(3)]

        def _fake_search(tag, cursor=None):
            return batch, None

        monkeypatch.setattr(background.x_client, 'search_timeline', _fake_search)
        out = background._x_fetch_tag('#pg')

        assert out['error'] is None
        p = background.runtime.x_fetch_progress('#pg')
        assert p['status'] == 'done'
        assert p['found'] == 3
        assert p['pages_done'] == 1
        assert p['finished_at']
        background.runtime._x_fetch_progress.pop('#pg', None)

    def test_failure_marks_error_not_stuck_fetching(self, clean_db, monkeypatch):
        _add_tag(clean_db, tag='#pf')

        def _boom(tag, cursor=None):
            raise background.x_client.XRateLimited('限流')

        monkeypatch.setattr(background.x_client, 'search_timeline', _boom)
        background._x_fetch_tag('#pf')

        p = background.runtime.x_fetch_progress('#pf')
        assert p['status'] == 'error'
        assert '限流' in (p['error'] or '')
        assert p['finished_at']
        background.runtime._x_fetch_progress.pop('#pf', None)

    def test_startup_reset_closes_stuck_fetching_progress(self, clean_db, monkeypatch):
        background.runtime.x_fetch_progress_update(
            '#pz', status='fetching', pages_done=1, pages_total=3, found=5)
        _add_tag(clean_db, tag='#pz', status='fetching')

        background._reset_stuck_x()

        p = background.runtime.x_fetch_progress('#pz')
        assert p['status'] == 'error', '进程重启后进度不能停在 fetching'
        assert '中断' in (p['error'] or '')
        background.runtime._x_fetch_progress.pop('#pz', None)


class TestXStoreTweets:
    """入库的两条不变式：tweet_id 只收纯数字（会进路径/URL）；作者为空时补写。

    补写这条来自 2026-11-20 的线上现象：X 把 user 结构从 legacy 迁到 core 后，
    旧解析器把作者存成空串，历史行永远显示"未知作者"，H1「同作者」判定
    也因为映射键为空永不触发。重新抓到同一条推文时必须把作者补回去。
    """

    def _tweet(self, **kw):
        from pixivviewer.x_client import XMedia, XTweet
        base = dict(tweet_id='1800000000000000901', author_name='画师',
                    author_screen_name='Artist_X', text='t',
                    media=[XMedia(url='https://pbs.twimg.com/media/a.jpg', width=1, height=1)])
        base.update(kw)
        return XTweet(**base)

    def test_backfills_empty_author_on_existing_row(self, clean_db):
        stale = XWork(tweet_id='1800000000000000901', tag='#t',
                      author_name='', author_screen_name='', media_count=1)
        clean_db.add(stale)
        clean_db.commit()

        background._x_store_tweets(clean_db, '#t', [self._tweet()])
        clean_db.commit()

        clean_db.refresh(stale)
        assert stale.author_screen_name == 'artist_x', '作者必须补写（handle 统一小写）'
        assert stale.author_name == '画师'
        assert clean_db.query(XWork).count() == 1, '不能因为补写多插一行'

    def test_non_digit_tweet_id_is_skipped(self, clean_db):
        """tweet_id 会拼进 downloads/x_<id>/ 与 URL：非纯数字一律不入库。"""
        background._x_store_tweets(clean_db, '#t', [self._tweet(tweet_id='../../etc')])
        assert clean_db.query(XWork).count() == 0


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


class TestXDownloadSession:
    def test_download_does_not_require_pixiv_cookie(self, clean_db, monkeypatch, tmp_path):
        """X 下载与 Pixiv 凭据解耦：没有 cookies.txt 也必须能下载成功。

        2026-11-20 本地联调实测：worker 曾复用 pixiv_client 的无凭据会话，
        其内部要求 cookies.txt 存在 → 没配 Pixiv 的部署上每张图都失败。
        """
        import os

        from pixivviewer import pixiv_client

        monkeypatch.setattr(pixiv_client, 'COOKIE_PATH',
                            str(tmp_path / 'no-such-pixiv-cookie.txt'))
        monkeypatch.setattr(background.config, 'DOWNLOAD_DIR', str(tmp_path))
        work = _add_work(clean_db, saved_status=None)

        class _Resp:
            status_code = 200
            headers = {'Content-Type': 'image/jpeg'}
            is_redirect = False

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=8192):
                return iter([b'\xff\xd8\xff\xe0fake-jpeg-bytes'])

        class _Session:
            headers = {}

            def get(self, url, timeout=None, stream=True, allow_redirects=False):
                return _Resp()

            def close(self):
                pass

        monkeypatch.setattr(background.x_client, 'build_image_session', lambda: _Session())

        background._download_x_work(work.id)

        clean_db.refresh(work)
        assert work.saved_status == 'done'
        assert work.saved_paths_list, '下载成功必须记录文件路径'
        for path in work.saved_paths_list:
            assert os.path.isfile(path)
            os.remove(path)


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
