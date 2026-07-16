import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from models import get_session, safe_commit, Illust, BlockedTag, DownloadLog, Collection, CollectionItem


@pytest.fixture(scope='session')
def app():
    import config
    config.DATABASE_PATH = os.path.join(
        tempfile.gettempdir(), f'pixiv_test_{os.getpid()}.db'
    )
    config.AUTO_FOLLOW_INTERVAL = 0

    from app import app as flask_app
    flask_app.config.update({'TESTING': True})
    yield flask_app

    db_path = config.DATABASE_PATH
    if os.path.exists(db_path):
        os.unlink(db_path)


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
    for table in [BlockedTag, DownloadLog, CollectionItem, Collection, Illust]:
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
        description='テスト説明文',
    )
    illust.tags_list = ['test', 'sample', 'original']
    illust.original_urls_list = [
        'https://i.pximg.net/img-original/img/0001/01/15/00/00/00/12345678_p0.jpg',
        'https://i.pximg.net/img-original/img/0001/01/15/00/00/00/12345678_p1.jpg',
    ]
    clean_db.add(illust)
    safe_commit(clean_db)
    return illust
