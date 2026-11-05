from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, event, Integer, String, Text, DateTime, Index
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


def iso_utc(value: datetime | None) -> str | None:
    """把**按 UTC 存储**的时间戳序列化成带偏移量的 ISO 8601（前端才能换算成本地时间）。

    为什么需要它：SQLite 的 `DATETIME` 列不保存偏移量，`datetime.now(timezone.utc)`
    写进去、读回来是不带 `tzinfo` 的 UTC 墙面时间；裸 `isoformat()` 于是输出
    `2026-08-29T14:54:40.295936` 这种**没有偏移标记**的字符串。按 ECMAScript 的
    Date Time String Format，带时间但无偏移会被解释成**本地时间** —— 浏览器
    `new Date()` 于是把 UTC 数字原样当本地时间渲染：上海用户看到的是 UTC 时刻，
    改系统/浏览器时区也不会挪动它（正因如此才会误以为"时区设置没生效"）。
    这里补上 `+00:00`，`new Date()` 才会做真正的时区换算。

    只给**确实按 UTC 存储**的列用（`created_at` / `downloaded_at` /
    `SearchCache.cached_at` / `DownloadLog.created_at`）。`illusts.upload_date`
    例外：它是 Pixiv 的 JST 墙面时间（`updateDate` 带 `+09:00`，落库时偏移被
    SQLite 丢掉），补 `+00:00` 会整体偏 9 小时，所以那个字段仍走裸 `isoformat()`。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


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
    # 预取作品"最终收藏数"刷新标记：入库满 1 天后刷新一次收藏数并写入该时间戳
    prefetch_refresh_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    # 预取刷新失败退避时间戳：失败写、成功清；退避期内不再入选候选，
    # 防止永久失败的死作品占住刷新队列名额（配合 _prefetch_refresh_bookmarks）
    refresh_failed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
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
            # upload_date 是 Pixiv 的 JST 墙面时间，不能补 +00:00（见 iso_utc 文档）
            'upload_date': self.upload_date.isoformat() if self.upload_date else None,
            'thumb_url': self.thumb_url,
            'original_urls': self.original_urls_list,
            # 注意：不输出 local_paths/local_dir（磁盘绝对路径）。前端判断
            # 已下载用 download_status，取图用 local_urls /api/image/...
            'download_status': self.download_status,
            'downloaded_at': iso_utc(self.downloaded_at),
            'file_size': self.file_size,
            'created_at': iso_utc(self.created_at),
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
            'created_at': iso_utc(self.created_at),
        }


class SearchCache(Base):
    __tablename__ = 'search_cache'

    tag: Mapped[str] = mapped_column(String, primary_key=True)
    illust_ids: Mapped[str] = mapped_column(Text, default='[]')
    # 组名之外的额外搜索词（同义词，JSON 数组）。实际搜索词 = [tag] + alias_tags：
    # 「吉星排队」与「星引擎party」这种同义标签合并成一行，结果并集去重，
    # 避免缓存页出现两个高度重叠的条目。老数据 '[]' → 行为与单标签完全一致。
    alias_tags: Mapped[str] = mapped_column(Text, default='[]')
    cached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default='idle')
    error: Mapped[str] = mapped_column(String, default='')
    total: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def alias_tags_list(self) -> list[str]:
        try:
            value = json.loads(self.alias_tags)
        except (json.JSONDecodeError, TypeError):
            return []
        return [t for t in value if isinstance(t, str)] if isinstance(value, list) else []

    @alias_tags_list.setter
    def alias_tags_list(self, value: list[str]) -> None:
        self.alias_tags = json.dumps(list(value), ensure_ascii=False)


def _rebuild_illusts_table(drop_cols: set[str]) -> None:
    """重建 illusts 表以删除列（SQLite < 3.35），保留完整 schema（PK/NOT NULL/DEFAULT）。"""
    from migrations.versions import rebuild_illusts_table

    with engine.begin() as conn:
        rebuild_illusts_table(conn, drop_cols)


def init_db() -> None:
    Base.metadata.create_all(engine)
    from migrations import MIGRATIONS, run_migrations
    from migrations.versions import (
        add_illust_refresh_failed_at, add_search_cache_alias_tags, repair_illust_schema,
    )

    run_migrations(engine, MIGRATIONS)
    # Keep the historical startup behavior that repairs externally drifted columns.
    # add_illust_refresh_failed_at 是 v4 列、add_search_cache_alias_tags 是 v6 列，
    # 而 repair_illust_schema 只认 v2 的列集（不得修改已发布版本），故这里额外幂等
    # 各补一次，覆盖"库被外部改动丢列"的场景。
    with engine.begin() as conn:
        repair_illust_schema(conn)
        add_illust_refresh_failed_at(conn)
        add_search_cache_alias_tags(conn)


def get_session() -> Session:
    return Session(engine)
