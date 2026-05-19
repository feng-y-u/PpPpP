import json
import os
import time
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, Integer, String, Text, DateTime, Index, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

from config import DATABASE_PATH

os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

engine = create_engine(
    f'sqlite:///{DATABASE_PATH}',
    connect_args={'check_same_thread': False},
)


@event.listens_for(engine, 'connect')
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('PRAGMA busy_timeout=10000;')
    cursor.execute('PRAGMA synchronous=NORMAL;')
    cursor.close()


def safe_commit(db_session, max_retries=3):
    """带重试的 commit，处理 database is locked 错误."""
    for attempt in range(max_retries):
        try:
            db_session.commit()
            return
        except OperationalError as e:
            if 'database is locked' in str(e) and attempt < max_retries - 1:
                time.sleep(1 + attempt)
            else:
                raise


class Base(DeclarativeBase):
    pass


class Illust(Base):
    __tablename__ = 'illusts'
    __table_args__ = (
        Index('ix_illusts_dl_status_created', 'download_status', 'created_at'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pixiv_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, default='')
    user_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    user_name: Mapped[str] = mapped_column(String, default='')
    tags: Mapped[str] = mapped_column(Text, default='[]')
    page_count: Mapped[int] = mapped_column(Integer, default=1)
    bookmark_count: Mapped[int] = mapped_column(Integer, default=0)
    upload_date: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    thumb_url: Mapped[str] = mapped_column(String, default='')
    original_urls: Mapped[str] = mapped_column(Text, default='[]')
    local_paths: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    download_status: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def tags_list(self) -> list[str]:
        try:
            return json.loads(self.tags)
        except (json.JSONDecodeError, TypeError):
            return []

    @tags_list.setter
    def tags_list(self, value: list[str]):
        self.tags = json.dumps(value, ensure_ascii=False)

    @property
    def original_urls_list(self) -> list[str]:
        try:
            return json.loads(self.original_urls)
        except (json.JSONDecodeError, TypeError):
            return []

    @original_urls_list.setter
    def original_urls_list(self, value: list[str]):
        self.original_urls = json.dumps(value, ensure_ascii=False)

    @property
    def local_paths_list(self) -> list[str] | None:
        if self.local_paths is None:
            return None
        try:
            return json.loads(self.local_paths)
        except (json.JSONDecodeError, TypeError):
            return None

    @local_paths_list.setter
    def local_paths_list(self, value: list[str] | None):
        if value is None:
            self.local_paths = None
        else:
            self.local_paths = json.dumps(value, ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'pixiv_id': self.pixiv_id,
            'title': self.title,
            'user_id': self.user_id,
            'user_name': self.user_name,
            'tags': self.tags_list,
            'page_count': self.page_count,
            'bookmark_count': self.bookmark_count,
            'upload_date': self.upload_date.isoformat() if self.upload_date else None,
            'thumb_url': self.thumb_url,
            'original_urls': self.original_urls_list,
            'local_paths': self.local_paths_list,
            'download_status': self.download_status,
            'file_size': self.file_size,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class BlockedTag(Base):
    __tablename__ = 'blocked_tags'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tag: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class DownloadLog(Base):
    __tablename__ = 'download_logs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pixiv_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(String, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'pixiv_id': self.pixiv_id,
            'action': self.action,
            'message': self.message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


def init_db():
    Base.metadata.create_all(engine)

    # Schema migrations for existing DBs
    from sqlalchemy import inspect as sa_inspect
    inspector = sa_inspect(engine)
    columns = [c['name'] for c in inspector.get_columns('illusts')]
    if 'file_size' not in columns:
        with engine.connect() as conn:
            conn.execute(text('ALTER TABLE illusts ADD COLUMN file_size INTEGER DEFAULT 0'))
            conn.execute(text('CREATE INDEX IF NOT EXISTS ix_illusts_dl_status_created ON illusts(download_status, created_at)'))
            conn.execute(text('CREATE INDEX IF NOT EXISTS ix_illusts_user_id ON illusts(user_id)'))
            conn.commit()


def get_session() -> Session:
    return Session(engine)
