import shutil
import sqlite3

import pytest
from sqlalchemy import create_engine

from migrations import runner
from migrations.runner import backup_database, run_migrations
from migrations.versions import LATEST_SCHEMA_VERSION, MIGRATIONS


def _user_version(engine):
    with engine.connect() as conn:
        return conn.exec_driver_sql("PRAGMA user_version").scalar()


def test_backup_database_copies_existing_database_with_unique_timestamp(tmp_path):
    database = tmp_path / "pixiv.db"
    database.write_bytes(b"sqlite backup")

    first = backup_database(database, tmp_path / "backups")
    second = backup_database(database, tmp_path / "backups")

    assert first != second
    assert first.parent == tmp_path / "backups"
    assert first.read_bytes() == database.read_bytes()
    assert second.read_bytes() == database.read_bytes()


def test_runner_backs_up_file_database_only_when_versions_are_pending(tmp_path):
    database = tmp_path / "pixiv.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE existing (id INTEGER PRIMARY KEY)")

    run_migrations(engine, ((1, lambda conn: None),))
    backup_dir = tmp_path / "backups"
    assert len(list(backup_dir.iterdir())) == 1

    run_migrations(engine, ((1, lambda conn: None),))
    assert len(list(backup_dir.iterdir())) == 1


def test_runner_applies_pending_versions_once_in_order():
    engine = create_engine("sqlite://")
    applied = []

    def migration_1(conn):
        applied.append(1)
        conn.exec_driver_sql("CREATE TABLE first (id INTEGER PRIMARY KEY)")

    def migration_2(conn):
        applied.append(2)
        conn.exec_driver_sql("CREATE TABLE second (id INTEGER PRIMARY KEY)")

    migrations = ((1, migration_1), (2, migration_2))
    run_migrations(engine, migrations)
    run_migrations(engine, migrations)

    assert applied == [1, 2]
    assert _user_version(engine) == 2


def test_runner_does_not_advance_version_when_migration_fails():
    engine = create_engine("sqlite://")

    def failing_migration(conn):
        conn.exec_driver_sql("CREATE TABLE unfinished (id INTEGER PRIMARY KEY)")
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        run_migrations(engine, ((1, failing_migration),))

    assert _user_version(engine) == 0


def test_legacy_database_upgrades_without_losing_data():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE illusts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pixiv_id INTEGER NOT NULL UNIQUE,
                title VARCHAR DEFAULT '',
                user_id INTEGER DEFAULT 0,
                download_status VARCHAR,
                created_at DATETIME,
                description TEXT DEFAULT '',
                is_favorite BOOLEAN DEFAULT 0,
                favorited_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO illusts (pixiv_id, title, user_id) VALUES (123, 'kept', 456)"
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE collection_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER NOT NULL,
                pixiv_id INTEGER NOT NULL,
                created_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            INSERT INTO collection_items (collection_id, pixiv_id, created_at)
            VALUES (1, 11, '2025-01-01'), (1, 12, '2025-01-02'),
                   (2, 21, '2025-01-01')
            """
        )

    run_migrations(engine, MIGRATIONS)

    with engine.connect() as conn:
        illust_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(illusts)")
        }
        item_columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(collection_items)")
        }
        illust = conn.exec_driver_sql(
            "SELECT pixiv_id, title, user_id FROM illusts"
        ).one()
        positions = conn.exec_driver_sql(
            "SELECT pixiv_id, position FROM collection_items ORDER BY pixiv_id"
        ).all()

    assert _user_version(engine) == LATEST_SCHEMA_VERSION
    assert {"file_size", "downloaded_at", "bookmark_updated_at"}.issubset(
        illust_columns
    )
    assert {"prefetch_source", "prefetch_refresh_at", "refresh_failed_at"}.issubset(
        illust_columns
    )
    assert {"description", "is_favorite", "favorited_at"}.isdisjoint(
        illust_columns
    )
    assert "position" in item_columns
    assert tuple(illust) == (123, "kept", 456)
    assert positions == [(11, 1000.0), (12, 2000.0), (21, 1000.0)]


def test_version_one_database_runs_remaining_schema_upgrade():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE illusts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pixiv_id INTEGER NOT NULL UNIQUE,
                user_id INTEGER DEFAULT 0,
                download_status VARCHAR,
                created_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE collection_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER NOT NULL,
                pixiv_id INTEGER NOT NULL,
                position REAL NOT NULL DEFAULT 0.0,
                created_at DATETIME
            )
            """
        )
        conn.exec_driver_sql("PRAGMA user_version = 1")

    run_migrations(engine, MIGRATIONS)

    with engine.connect() as conn:
        columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(illusts)")
        }
    assert "prefetch_refresh_at" in columns
    assert _user_version(engine) == LATEST_SCHEMA_VERSION


# ── WAL 备份完整性（审计 S5）──

def _wal_engine(tmp_path, database_name='pixiv.db'):
    """建一个 WAL 模式的库，并写入一行**只存在于 -wal 中**的数据。"""
    database = tmp_path / database_name
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode=WAL").scalar() == "wal"
        conn.commit()
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.exec_driver_sql("INSERT INTO t (v) VALUES ('only-in-wal')")
    assert (tmp_path / f"{database_name}-wal").is_file(), "前置条件：-wal 应当存在"
    return engine, database


def _rows_in(db_file):
    con = sqlite3.connect(db_file)
    try:
        return con.execute("SELECT v FROM t").fetchall()
    finally:
        con.close()


def test_backup_captures_wal_uncheckpointed_data(tmp_path):
    """备份前必须 checkpoint：否则停在 -wal 里的已提交数据不会进备份。"""
    engine, database = _wal_engine(tmp_path)
    # 前置条件：此刻主库文件单独拿出来是读不到这行的（数据确实只在 WAL 里）
    lone_before = tmp_path / "lone-before.db"
    shutil.copy2(database, lone_before)
    with pytest.raises(sqlite3.OperationalError):
        _rows_in(lone_before)

    run_migrations(engine, ((1, lambda conn: None),))

    backups = sorted((tmp_path / "backups").glob("*.bak"))
    assert len(backups) == 1
    assert _rows_in(backups[0]) == [("only-in-wal",)]

    # 证明是 checkpoint（而非复制侧车）起的作用：主库文件本身已含该行
    lone_after = tmp_path / "lone-after.db"
    shutil.copy2(database, lone_after)
    assert _rows_in(lone_after) == [("only-in-wal",)]


def test_backup_copies_wal_when_checkpoint_fails(tmp_path, monkeypatch):
    """checkpoint 失败时的兜底：-wal/-shm 一并复制，副本仍读得到全部数据。"""
    engine, database = _wal_engine(tmp_path)

    def _boom(engine_):
        raise RuntimeError("checkpoint busy")

    # raising=False：回退修复后 runner 里没有这个符号，测试也应报"行为不符"而不是
    # AttributeError（否则证伪检查只证明测试引用了新符号）
    monkeypatch.setattr(runner, "_checkpoint_wal", _boom, raising=False)

    run_migrations(engine, ((1, lambda conn: None),))

    backups = sorted((tmp_path / "backups").glob("*.bak"))
    assert len(backups) == 1
    assert (tmp_path / "backups" / f"{backups[0].name}-wal").is_file(), "-wal 必须一并复制"
    assert _rows_in(backups[0]) == [("only-in-wal",)], "副本必须包含已提交数据"


def test_backup_still_works_on_non_wal_db(tmp_path):
    """非 WAL 库：checkpoint 是 no-op，备份行为与既有实现一致。"""
    database = tmp_path / "pixiv.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() != "wal"
        conn.commit()
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.exec_driver_sql("INSERT INTO t (v) VALUES ('rollback-journal')")

    run_migrations(engine, ((1, lambda conn: None),))

    backups = sorted((tmp_path / "backups").glob("*.bak"))
    assert len(backups) == 1
    assert _rows_in(backups[0]) == [("rollback-journal",)]
    assert not list((tmp_path / "backups").glob("*-wal")), "非 WAL 库不该产生侧车副本"
