import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── ⚠ 必须在 import models/app 之前覆盖数据库路径 ──
# models.py 在 import 时即 create_engine(DATABASE_PATH)，事后覆盖无效，
# 会导致测试直连并清空生产数据库（2026-07-25 审查 P0-1）。
import config
_TEST_DB_PATH = os.path.join(tempfile.gettempdir(), f'pixiv_test_{os.getpid()}.db')
config.DATABASE_PATH = _TEST_DB_PATH
config.AUTO_FOLLOW_INTERVAL = 0

import pytest

from models import get_session, safe_commit, Illust, BlockedTag, DownloadLog, Collection, CollectionItem, SearchCache


@pytest.fixture(scope='session')
def app():
    from app import app as flask_app
    flask_app.config.update({'TESTING': True, 'SESSION_COOKIE_SECURE': False})
    yield flask_app
    # Windows 上需先释放引擎持有的文件句柄，否则 unlink 报 WinError 32
    import models
    models.engine.dispose()
    for suffix in ('', '-wal', '-shm'):
        p = _TEST_DB_PATH + suffix
        if os.path.exists(p):
            os.unlink(p)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    with get_session() as session:
        yield session
        session.rollback()


@pytest.fixture
def clean_db(db):
    """Clean all tables before the test."""
    for table in [BlockedTag, DownloadLog, CollectionItem, Collection, Illust, SearchCache]:
        db.query(table).delete()
    db.commit()
    return db


@pytest.fixture
def sample_illust(clean_db):
    illust = Illust(
        pixiv_id=12345678,
        title='テスト作品',
        user_id=87654321,
        user_name='テスト画師',
        page_count=3,
        bookmark_count=1500,
        thumb_url='https://i.pximg.net/c/250x250/img/test.jpg',
        upload_date=datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    )
    illust.tags_list = ['test', 'sample', 'original']
    illust.original_urls_list = [
        'https://i.pximg.net/img-original/img/0001/01/15/00/00/00/12345678_p0.jpg',
        'https://i.pximg.net/img-original/img/0001/01/15/00/00/00/12345678_p1.jpg',
    ]
    clean_db.add(illust)
    safe_commit(clean_db)
    return illust
