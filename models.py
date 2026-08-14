from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, event, Boolean, Float, Integer, String, Text, DateTime, Index, ForeignKey, UniqueConstraint, text
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
    """带重试的 commit，处理 database is locked 错误。

    注意：flush/COMMIT 失败时 SQLAlchemy 已自动回滚事务并 expire 全部
    对象，本次未提交的修改随之丢失——内部重试只会得到一次空提交（静默
    掩盖数据丢失）。正确语义：失败后先 rollback() 恢复 session 可用状态
    （否则后续任何数据库操作都会抛 PendingRollbackError），再抛出原始
    错误；调用方需要时捕获异常、重建变更后重新调用本函数。
    （PRAGMA busy_timeout=10000 已提供 10 秒等锁窗口，locked 罕见。）
    """
    try:
        db_session.commit()
    except OperationalError as e:
        db_session.rollback()
        raise
    except Exception as e:
        db_session.rollback()
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
    bookmark_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    upload_date: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    thumb_url: Mapped[str] = mapped_column(String, default='')
    original_urls: Mapped[str] = mapped_column(Text, default='[]')
    local_paths: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    download_status: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    prefetch_source: Mapped[int] = mapped_column(Integer, default=0)
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

    def to_dict(self, favorite: bool = False) -> dict:
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
            # 注意：不输出 local_paths/local_dir（磁盘绝对路径）。前端判断
            # 已下载用 download_status，取图用 local_urls /api/image/...
            'download_status': self.download_status,
            'downloaded_at': self.downloaded_at.isoformat() if self.downloaded_at else None,
            'file_size': self.file_size,
            'is_favorite': favorite,
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


class SearchCache(Base):
    __tablename__ = 'search_cache'

    tag: Mapped[str] = mapped_column(String, primary_key=True)
    illust_ids: Mapped[str] = mapped_column(Text, default='[]')
    cached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default='idle')
    error: Mapped[str] = mapped_column(String, default='')
    total: Mapped[int] = mapped_column(Integer, default=0)


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
    position: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'collection_id': self.collection_id,
            'pixiv_id': self.pixiv_id,
            'position': self.position,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


def _rebuild_illusts_table(drop_cols: set[str]) -> None:
    """重建 illusts 表以删除列（SQLite < 3.35），保留完整 schema（PK/NOT NULL/DEFAULT）。"""
    with engine.connect() as conn:
        info_rows = conn.exec_driver_sql('PRAGMA table_info(illusts)').fetchall()
        # info_rows: (cid, name, type, notnull, dflt_value, pk)
        keep = [r for r in info_rows if r[1] not in drop_cols]
        col_defs = []
        for _cid, name, ctype, notnull, dflt, pk in keep:
            parts = [f'"{name}"', ctype]
            if notnull:
                parts.append('NOT NULL')
            if dflt is not None:
                parts.append(f'DEFAULT {dflt}')
            if pk:
                parts.append('PRIMARY KEY AUTOINCREMENT')
            col_defs.append(' '.join(parts))
        col_list = ', '.join(f'"{r[1]}"' for r in keep)
        conn.execute(text(f'CREATE TABLE _illusts_new ({", ".join(col_defs)})'))
        conn.execute(text(f'INSERT INTO _illusts_new ({col_list}) SELECT {col_list} FROM illusts'))
        conn.execute(text('DROP TABLE illusts'))
        conn.execute(text('ALTER TABLE _illusts_new RENAME TO illusts'))
        conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_illusts_pixiv_id ON illusts(pixiv_id)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_illusts_dl_status_created ON illusts(download_status, created_at)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_illusts_user_id ON illusts(user_id)'))
        conn.commit()


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
    if 'downloaded_at' not in columns:
        with engine.connect() as conn:
            conn.execute(text('ALTER TABLE illusts ADD COLUMN downloaded_at DATETIME'))
            conn.commit()
    if 'bookmark_updated_at' not in columns:
        with engine.connect() as conn:
            conn.execute(text('ALTER TABLE illusts ADD COLUMN bookmark_updated_at DATETIME'))
            conn.commit()

    # ── collection_items.position 列与 user_version 迁移 ──
    ci_cols = [c['name'] for c in inspector.get_columns('collection_items')]
    if 'position' not in ci_cols:
        with engine.connect() as conn:
            conn.execute(text('ALTER TABLE collection_items ADD COLUMN position REAL NOT NULL DEFAULT 0.0'))
            conn.commit()

    with engine.connect() as conn:
        user_version = conn.exec_driver_sql('PRAGMA user_version').scalar() or 0
    if user_version < 1:
        with engine.begin() as conn:
            rows = conn.execute(text(
                'SELECT id, collection_id FROM collection_items ORDER BY collection_id ASC, created_at ASC, id ASC'
            )).fetchall()
            last_cid = None
            counter = 0
            for row in rows:
                cid = row[1]
                if cid != last_cid:
                    last_cid = cid
                    counter = 0
                counter += 1
                conn.execute(text(
                    'UPDATE collection_items SET position = :p WHERE id = :id'
                ), {'p': counter * 1000.0, 'id': row[0]})
            conn.execute(text('PRAGMA user_version = 1'))

    # ── 清理已废弃的 is_favorite / favorited_at 列 ──
    illusts_cols = [c['name'] for c in inspector.get_columns('illusts')]
    has_is_fav = any(c == 'is_favorite' for c in illusts_cols)
    has_fav_at = any(c == 'favorited_at' for c in illusts_cols)
    if has_is_fav or has_fav_at:
        import sqlite3 as _sqlite3
        ver = tuple(int(x) for x in _sqlite3.sqlite_version.split('.'))
        if ver >= (3, 35, 0):
            with engine.connect() as conn:
                if has_is_fav:
                    conn.execute(text('ALTER TABLE illusts DROP COLUMN is_favorite'))
                if has_fav_at:
                    conn.execute(text('ALTER TABLE illusts DROP COLUMN favorited_at'))
                conn.commit()
        else:
            _rebuild_illusts_table({'is_favorite', 'favorited_at'})

    # ── SearchCache 预取：新增 prefetch_source 列，移除已废弃的 description 列 ──
    if 'prefetch_source' not in columns:
        with engine.connect() as conn:
            conn.execute(text('ALTER TABLE illusts ADD COLUMN prefetch_source INTEGER DEFAULT 0'))
            conn.commit()

    has_desc = any(c == 'description' for c in columns)
    if has_desc:
        import sqlite3 as _sqlite3
        ver = tuple(int(x) for x in _sqlite3.sqlite_version.split('.'))
        if ver >= (3, 35, 0):
            with engine.connect() as conn:
                conn.execute(text('ALTER TABLE illusts DROP COLUMN description'))
                conn.commit()
        else:
            _rebuild_illusts_table({'description'})


def get_session() -> Session:
    return Session(engine)


def get_favorite_pids(session: Session) -> set[int]:
    """'我的收藏'收藏夹中的全部 pixiv_id（is_favorite 语义的唯一来源）。"""
    dc = session.query(Collection).filter(Collection.name == '我的收藏').first()
    if not dc:
        return set()
    return {p[0] for p in session.query(CollectionItem.pixiv_id)
            .filter(CollectionItem.collection_id == dc.id).all()}
