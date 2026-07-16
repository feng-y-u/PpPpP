from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, event, Boolean, Integer, String, Text, DateTime, Index, ForeignKey, UniqueConstraint, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

from config import DATABASE_PATH

os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

engine = create_engine(
    f'sqlite:///{DATABASE_PATH}',
    connect_args={'check_same_thread': False},
)


@event.listens_for(engine, 'connect')
def set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('PRAGMA busy_timeout=10000;')
    cursor.execute('PRAGMA synchronous=NORMAL;')
    cursor.close()


def safe_commit(db_session: Session, max_retries: int = 3) -> None:
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
    description: Mapped[str] = mapped_column(Text, default='')
    original_urls: Mapped[str] = mapped_column(Text, default='[]')
    local_paths: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    download_status: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    favorited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def tags_list(self) -> list[str]:
        try:
            return json.loads(self.tags)
        except (json.JSONDecodeError, TypeError):
            return []

    @tags_list.setter
    def tags_list(self, value: list[str]) -> None:
        self.tags = json.dumps(value, ensure_ascii=False)

    @property
    def original_urls_list(self) -> list[str]:
        try:
            return json.loads(self.original_urls)
        except (json.JSONDecodeError, TypeError):
            return []

    @original_urls_list.setter
    def original_urls_list(self, value: list[str]) -> None:
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
    def local_paths_list(self, value: list[str] | None) -> None:
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
            'description': self.description,
            'original_urls': self.original_urls_list,
            'local_paths': self.local_paths_list,
            'download_status': self.download_status,
            'file_size': self.file_size,
            'is_favorite': self.is_favorite,
            'favorited_at': self.favorited_at.isoformat() if self.favorited_at else None,
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


class Collection(Base):
    __tablename__ = 'collections'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class CollectionItem(Base):
    __tablename__ = 'collection_items'
    __table_args__ = (
        UniqueConstraint('collection_id', 'pixiv_id', name='uq_collection_item'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_id: Mapped[int] = mapped_column(Integer, ForeignKey('collections.id'), nullable=False)
    pixiv_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'collection_id': self.collection_id,
            'pixiv_id': self.pixiv_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


def init_db() -> None:
    Base.metadata.create_all(engine)

    # 针对现有数据库的 Schema 迁移
    from sqlalchemy import inspect as sa_inspect
    inspector = sa_inspect(engine)
    columns = [c['name'] for c in inspector.get_columns('illusts')]
    if 'file_size' not in columns:
        with engine.connect() as conn:
            conn.execute(text('ALTER TABLE illusts ADD COLUMN file_size INTEGER DEFAULT 0'))
            conn.execute(text('CREATE INDEX IF NOT EXISTS ix_illusts_dl_status_created ON illusts(download_status, created_at)'))
            conn.execute(text('CREATE INDEX IF NOT EXISTS ix_illusts_user_id ON illusts(user_id)'))
            conn.commit()
    if 'description' not in columns:
        with engine.connect() as conn:
            conn.execute(text('ALTER TABLE illusts ADD COLUMN description TEXT DEFAULT ""'))
            conn.commit()
    if 'is_favorite' not in columns:
        with engine.connect() as conn:
            conn.execute(text('ALTER TABLE illusts ADD COLUMN is_favorite BOOLEAN DEFAULT 0'))
            conn.execute(text('ALTER TABLE illusts ADD COLUMN favorited_at DATETIME'))
            conn.commit()

    # 收藏夹表迁移：创建默认"我的收藏"并迁移现有收藏
    # Base.metadata.create_all above created the tables, so they always exist now
    from sqlalchemy import select
    sel = select(Collection).where(Collection.name == '我的收藏')
    with Session(engine) as sess:
        default = sess.execute(sel).scalar_one_or_none()
        if not default:
            default = Collection(name='我的收藏', description='默认收藏夹')
            sess.add(default)
            sess.commit()
        # 迁移尚未在默认收藏夹中的 is_favorite=True 记录
        sel2 = select(CollectionItem.pixiv_id).where(CollectionItem.collection_id == default.id)
        existing_ids = {row[0] for row in sess.execute(sel2).fetchall()}
        to_migrate = sess.execute(
            select(Illust).where(Illust.is_favorite == True)
        ).scalars().all()
        new_items = []
        for i in to_migrate:
            if i.pixiv_id not in existing_ids:
                new_items.append(CollectionItem(collection_id=default.id, pixiv_id=i.pixiv_id))
        if new_items:
            sess.add_all(new_items)
            sess.commit()


def get_session() -> Session:
    return Session(engine)
