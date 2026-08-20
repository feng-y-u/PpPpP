import pytest
from sqlalchemy import create_engine

from migrations.runner import run_migrations
from migrations.versions import LATEST_SCHEMA_VERSION, MIGRATIONS


def _user_version(engine):
    with engine.connect() as conn:
        return conn.exec_driver_sql("PRAGMA user_version").scalar()


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
    assert {"prefetch_source", "prefetch_refresh_at"}.issubset(illust_columns)
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
