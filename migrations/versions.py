import sqlite3

from sqlalchemy.engine import Connection


def _column_names(conn: Connection, table: str) -> set[str]:
    return {
        row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')
    }


def _ensure_illust_indexes(conn: Connection) -> None:
    columns = _column_names(conn, "illusts")
    if "pixiv_id" in columns:
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_illusts_pixiv_id "
            "ON illusts(pixiv_id)"
        )
    if {"download_status", "created_at"}.issubset(columns):
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_illusts_dl_status_created "
            "ON illusts(download_status, created_at)"
        )
    if "user_id" in columns:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_illusts_user_id ON illusts(user_id)"
        )


def rebuild_illusts_table(conn: Connection, drop_columns: set[str]) -> None:
    """Rebuild illusts for SQLite versions without DROP COLUMN support."""
    info_rows = conn.exec_driver_sql("PRAGMA table_info(illusts)").fetchall()
    keep = [row for row in info_rows if row[1] not in drop_columns]
    column_definitions = []
    for _cid, name, column_type, not_null, default, primary_key in keep:
        parts = [f'"{name}"', column_type]
        if not_null:
            parts.append("NOT NULL")
        if default is not None:
            parts.append(f"DEFAULT {default}")
        if primary_key:
            parts.append("PRIMARY KEY AUTOINCREMENT")
        column_definitions.append(" ".join(parts))

    column_list = ", ".join(f'"{row[1]}"' for row in keep)
    conn.exec_driver_sql(
        f'CREATE TABLE _illusts_new ({", ".join(column_definitions)})'
    )
    conn.exec_driver_sql(
        f"INSERT INTO _illusts_new ({column_list}) "
        f"SELECT {column_list} FROM illusts"
    )
    conn.exec_driver_sql("DROP TABLE illusts")
    conn.exec_driver_sql("ALTER TABLE _illusts_new RENAME TO illusts")
    _ensure_illust_indexes(conn)


def _drop_illust_columns(conn: Connection, columns: set[str]) -> None:
    present = columns & _column_names(conn, "illusts")
    if not present:
        return
    sqlite_version = tuple(int(part) for part in sqlite3.sqlite_version.split("."))
    if sqlite_version >= (3, 35, 0):
        for column in sorted(present):
            conn.exec_driver_sql(f'ALTER TABLE illusts DROP COLUMN "{column}"')
        _ensure_illust_indexes(conn)
    else:
        rebuild_illusts_table(conn, present)


def migrate_collection_positions(conn: Connection) -> None:
    columns = _column_names(conn, "collection_items")
    if "position" not in columns:
        conn.exec_driver_sql(
            "ALTER TABLE collection_items "
            "ADD COLUMN position REAL NOT NULL DEFAULT 0.0"
        )

    rows = conn.exec_driver_sql(
        "SELECT id, collection_id FROM collection_items "
        "ORDER BY collection_id ASC, created_at ASC, id ASC"
    ).fetchall()
    last_collection_id = None
    counter = 0
    for item_id, collection_id in rows:
        if collection_id != last_collection_id:
            last_collection_id = collection_id
            counter = 0
        counter += 1
        conn.exec_driver_sql(
            "UPDATE collection_items SET position = ? WHERE id = ?",
            (counter * 1000.0, item_id),
        )


def migrate_illust_schema(conn: Connection) -> None:
    columns = _column_names(conn, "illusts")
    additions = (
        ("file_size", "INTEGER DEFAULT 0"),
        ("downloaded_at", "DATETIME"),
        ("bookmark_updated_at", "DATETIME"),
        ("prefetch_source", "INTEGER DEFAULT 0"),
        ("prefetch_refresh_at", "DATETIME"),
    )
    for name, definition in additions:
        if name not in columns:
            conn.exec_driver_sql(
                f'ALTER TABLE illusts ADD COLUMN "{name}" {definition}'
            )
    _drop_illust_columns(
        conn, {"description", "is_favorite", "favorited_at"}
    )
    _ensure_illust_indexes(conn)


def repair_illust_schema(conn: Connection) -> None:
    """Reconcile columns for databases changed outside the migration runner."""
    migrate_illust_schema(conn)


MIGRATIONS = (
    (1, migrate_collection_positions),
    (2, migrate_illust_schema),
    (3, repair_illust_schema),
)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]
