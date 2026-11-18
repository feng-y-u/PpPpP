"""同作品判定（`pixivviewer/x_dedupe.py`）的单元测试。

三层证据强度不同，这里分别钉：
- E1（推文带 Pixiv 链接）→ 必须 confirmed；
- H1（同作者 + 时间窗 + 张数）→ 只能 suspected，且同一对人工确认后不再提示；
- E2（dHash 距离）→ 阈值内才命中；Pillow 缺失时降级为"不判定"而不是报错。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from pixivviewer import x_dedupe
from pixivviewer.models import ArtistLink, Illust, ImageHash, WorkLink, XWork
from pixivviewer.x_client import XMedia, XTweet


def _illust(clean_db, pixiv_id=555, user_id=42, page_count=1, upload_date=None):
    row = Illust(pixiv_id=pixiv_id, user_id=user_id, page_count=page_count,
                 bookmark_count=1, title='t')
    row.upload_date = upload_date
    clean_db.add(row)
    clean_db.commit()
    return row


def _work(tweet_id='1800000000000000001', screen='artist_a', tweeted_at=None, media_count=1):
    work = XWork(tweet_id=tweet_id, tag='#tag', author_screen_name=screen,
                 author_name='A', media_count=media_count, tweeted_at=tweeted_at)
    work.media_list = [{'url': 'https://pbs.twimg.com/media/x.jpg?name=orig', 'w': 1, 'h': 1}]
    return work


def _tweet(text='', urls=None, screen='artist_a'):
    return XTweet(tweet_id='1800000000000000001', author_screen_name=screen,
                  text=text, expanded_urls=urls or [],
                  media=[XMedia(url='https://pbs.twimg.com/media/x.jpg?name=orig')])


def test_e1_link_marks_confirmed(clean_db):
    work = _work()
    tweet = _tweet('pixiv.net/artworks/777', ['https://www.pixiv.net/artworks/777'])
    x_dedupe.apply_ingest_dedupe(clean_db, work, tweet)

    assert work.link_pixiv_id == 777
    assert work.dup_status == 'confirmed'
    assert work.dup_reason == 'link'
    assert work.dup_pixiv_id == 777


def test_e1_respects_manual_work_link(clean_db):
    clean_db.add(WorkLink(pixiv_id=777, tweet_id='1800000000000000001', source='manual'))
    clean_db.commit()
    work = _work()
    x_dedupe.apply_ingest_dedupe(clean_db, work, _tweet('pixiv.net/artworks/777'))

    assert work.dup_status == 'confirmed'
    assert work.dup_reason == 'manual'


def test_h1_same_author_same_count_within_window_is_only_suspected(clean_db):
    clean_db.add(ArtistLink(pixiv_user_id=42, x_screen_name='artist_a', source='manual'))
    clean_db.commit()
    # tweet 时间按 UTC 存，Illust.upload_date 是 JST 墙面时间（+9h）
    tweeted = datetime(2026, 11, 17, 1, 0, 0)
    _illust(clean_db, pixiv_id=901, user_id=42, page_count=2,
            upload_date=tweeted + timedelta(hours=9, minutes=30))

    work = _work(tweeted_at=tweeted, media_count=2)
    x_dedupe.apply_ingest_dedupe(clean_db, work, _tweet())

    assert work.dup_status == 'suspected'
    assert work.dup_reason == 'author'
    assert work.dup_pixiv_id == 901


def test_h1_outside_window_or_count_mismatch_is_not_flagged(clean_db):
    clean_db.add(ArtistLink(pixiv_user_id=42, x_screen_name='artist_a', source='manual'))
    clean_db.commit()
    tweeted = datetime(2026, 11, 17, 1, 0, 0)
    # 时间差 72 小时（窗口 48h）
    _illust(clean_db, pixiv_id=902, user_id=42, page_count=1,
            upload_date=tweeted + timedelta(hours=9 + 72))

    work = _work(tweeted_at=tweeted, media_count=1)
    x_dedupe.apply_ingest_dedupe(clean_db, work, _tweet())
    assert work.dup_status is None

    # 张数不同也不能算疑似
    tweeted2 = datetime(2026, 11, 17, 1, 0, 0)
    work2 = _work(tweet_id='1800000000000000002', tweeted_at=tweeted2, media_count=3)
    x_dedupe.apply_ingest_dedupe(clean_db, work2, _tweet())
    assert work2.dup_status is None


def test_confirm_pair_writes_work_link_and_ignore_marks(clean_db):
    work = _work()
    clean_db.add(work)
    clean_db.commit()

    x_dedupe.confirm_pair(clean_db, work, pixiv_id=1234)
    clean_db.commit()
    assert work.dup_status == 'confirmed'
    assert work.dup_reason == 'manual'
    assert clean_db.query(WorkLink).filter_by(pixiv_id=1234).count() == 1

    x_dedupe.ignore_pair(clean_db, work)
    clean_db.commit()
    assert work.dup_status == 'ignored'


def test_hash_match_respects_distance_threshold(clean_db, monkeypatch):
    clean_db.add(ImageHash(path='/tmp/a.jpg', pixiv_id=11, dhash='0000000000000000', mtime=0, size=1))
    clean_db.add(ImageHash(path='/tmp/b.jpg', pixiv_id=22, dhash='000000000000000f', mtime=0, size=1))
    clean_db.commit()

    # 距离 0 → 命中 11
    assert x_dedupe.find_hash_match(clean_db, '0000000000000000', max_distance=0) == (11, 0)
    # 距离 4 → 0 距离的 11 优先
    assert x_dedupe.find_hash_match(clean_db, '000000000000000f', max_distance=4) == (22, 0)
    # 距离阈值 0 时，只有完全相同的才算
    assert x_dedupe.find_hash_match(clean_db, 'ffffffffffffffff', max_distance=0) is None


def test_match_local_image_degrades_when_pillow_missing(clean_db, monkeypatch):
    monkeypatch.setattr(x_dedupe, 'PIL_AVAILABLE', False)

    def _boom(path):
        raise x_dedupe.HashUnavailable('no pillow')

    monkeypatch.setattr(x_dedupe, 'dhash', _boom)
    assert x_dedupe.match_local_image(clean_db, '/tmp/none.jpg', max_distance=6) is None


def test_index_local_files_reuses_unchanged_rows(clean_db, tmp_path, monkeypatch):
    img = tmp_path / 'p.jpg'
    img.write_bytes(b'not-a-real-jpeg')
    illust = Illust(pixiv_id=700, download_status='done', page_count=1, bookmark_count=1)
    illust.local_paths_list = [str(img)]
    clean_db.add(illust)
    clean_db.commit()

    calls = {'n': 0}

    def _fake_dhash(path):
        calls['n'] += 1
        return 'abcdefabcdefabcd'

    monkeypatch.setattr(x_dedupe, 'dhash', _fake_dhash)
    first = x_dedupe.index_local_files(clean_db, limit=10)
    clean_db.commit()
    assert first['indexed'] == 1
    assert calls['n'] == 1

    # 第二次：mtime/size 没变 → 不重复解码
    second = x_dedupe.index_local_files(clean_db, limit=10)
    assert second['indexed'] == 0
    assert calls['n'] == 1
