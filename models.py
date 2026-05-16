import json
import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, Integer, String, Text, DateTime
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
    cursor.execute('PRAGMA busy_timeout=5000;')
    cursor.close()


class Base(DeclarativeBase):
    pass


class Illust(Base):
    __tablename__ = 'illusts'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pixiv_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, default='')
    user_id: Mapped[int] = mapped_column(Integer, default=0)
    user_name: Mapped[str] = mapped_column(String, default='')
    tags: Mapped[str] = mapped_column(Text, default='[]')
    page_count: Mapped[int] = mapped_column(Integer, default=1)
    bookmark_count: Mapped[int] = mapped_column(Integer, default=0)
    upload_date: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    thumb_url: Mapped[str] = mapped_column(String, default='')
    original_urls: Mapped[str] = mapped_column(Text, default='[]')
    local_paths: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    download_status: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
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
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class Setting(Base):
    __tablename__ = 'settings'

    key: Mapped[str] = mapped_column(String, primary_key=True)
    current_page: Mapped[int] = mapped_column(Integer, default=1)


class DeletedRecord(Base):
    __tablename__ = 'deleted_records'

    pixiv_id: Mapped[int] = mapped_column(Integer, primary_key=True)


def init_db():
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
