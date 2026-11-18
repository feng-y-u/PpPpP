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
    # 表不存在就跳过：收藏夹移除后 `create_all()` 不再建 `collection_items`，于是
    # 全新库（user_version=0）与已跑过 v5 的库都没有这张表，原来的 ALTER 会直接抛
    # "no such table" 让 init_db() 起不来。对**仍然存在该表的旧库**本函数行为与发布
    # 时逐字一致 —— 存在性检查只影响"无表可迁移"这一种调用方。
    if conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_items'"
    ).first() is None:
        return

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


def add_illust_refresh_failed_at(conn: Connection) -> None:
    """补 illusts.refresh_failed_at（v4）：预取刷新失败退避时间戳。

    失败写、成功清；backoff 期内该作品不再入选刷新候选，防止永久失败
    的死作品占住队列名额（head-of-line blocking）。幂等：已存在则跳过。
    """
    if "refresh_failed_at" not in _column_names(conn, "illusts"):
        conn.exec_driver_sql(
            'ALTER TABLE illusts ADD COLUMN "refresh_failed_at" DATETIME'
        )


def drop_collection_tables(conn: Connection) -> None:
    """v5：彻底移除本地收藏夹功能，删除其两张表。

    子表先删：`collection_items.collection_id` 引用 `collections.id`，开启外键的库先删父表会失败。
    `IF EXISTS` 让重复执行成为 no-op —— runner 靠 `PRAGMA user_version` 保证只跑一次，
    但手工/异常重入时不应报错。
    """
    conn.exec_driver_sql("DROP TABLE IF EXISTS collection_items")
    conn.exec_driver_sql("DROP TABLE IF EXISTS collections")


def add_search_cache_alias_tags(conn: Connection) -> None:
    """v6：给预取标签加同义词列表（组 = 一行的核心列）。

    `alias_tags` 存组名之外的额外搜索词（JSON 数组）；实际搜索词 =
    `[tag] + alias_tags`。老库补列后默认 `'[]'`，行为与单标签完全一致，
    不需要任何数据迁移。幂等：已存在则跳过（`init_db()` 也会兜底调一次）。
    """
    if conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_cache'"
    ).first() is None:
        # 表还没建（迁移测试/异常库）：没有列可补，直接 no-op 而不是 ALTER 报错
        return
    if "alias_tags" not in _column_names(conn, "search_cache"):
        conn.exec_driver_sql(
            "ALTER TABLE search_cache "
            "ADD COLUMN alias_tags TEXT NOT NULL DEFAULT '[]'"
        )


def migrate_x_tables(conn: Connection) -> None:
    """v7：X（Twitter）标签抓图相关的表。

    与 illusts 完全隔离 —— 不改任何既有列，删掉这些表就等于关掉整个功能。
    `tweet_id` 用 TEXT：X 的 id 是 64 位整数，JS 的 Number 只有 53 位精度。
    全部 `IF NOT EXISTS`：`init_db()` 里 create_all 先建过一遍，这里再跑一次是
    no-op；迁移测试直接对空库执行也能建出来。
    """
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS x_tags ("
        " tag VARCHAR PRIMARY KEY,"
        " enabled INTEGER NOT NULL DEFAULT 1,"
        " status VARCHAR NOT NULL DEFAULT 'idle',"
        " error VARCHAR NOT NULL DEFAULT '',"
        " last_fetch_at DATETIME,"
        " total INTEGER NOT NULL DEFAULT 0)"
    )
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS x_works ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " tweet_id VARCHAR NOT NULL UNIQUE,"
        " tag VARCHAR NOT NULL DEFAULT '',"
        " author_name VARCHAR NOT NULL DEFAULT '',"
        " author_screen_name VARCHAR NOT NULL DEFAULT '',"
        " text TEXT NOT NULL DEFAULT '',"
        " tweeted_at DATETIME,"
        " media_urls TEXT NOT NULL DEFAULT '[]',"
        " media_count INTEGER NOT NULL DEFAULT 0,"
        " saved_status VARCHAR,"
        " saved_paths TEXT,"
        " saved_at DATETIME,"
        " link_pixiv_id INTEGER,"
        " dup_status VARCHAR,"
        " dup_reason VARCHAR,"
        " dup_pixiv_id INTEGER,"
        " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_x_works_tag_created ON x_works(tag, created_at)"
    )
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_x_works_tweet_id ON x_works(tweet_id)"
    )
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS artist_links ("
        " pixiv_user_id INTEGER PRIMARY KEY,"
        " x_screen_name VARCHAR NOT NULL DEFAULT '',"
        " source VARCHAR NOT NULL DEFAULT 'manual',"
        " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_artist_links_x_screen_name ON artist_links(x_screen_name)"
    )
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS work_links ("
        " pixiv_id INTEGER NOT NULL,"
        " tweet_id VARCHAR NOT NULL,"
        " source VARCHAR NOT NULL DEFAULT 'manual',"
        " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        " PRIMARY KEY (pixiv_id, tweet_id))"
    )
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS image_hashes ("
        " path VARCHAR PRIMARY KEY,"
        " pixiv_id INTEGER NOT NULL DEFAULT 0,"
        " dhash VARCHAR NOT NULL DEFAULT '',"
        " mtime FLOAT NOT NULL DEFAULT 0,"
        " size INTEGER NOT NULL DEFAULT 0)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_image_hashes_pixiv_id ON image_hashes(pixiv_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_image_hashes_dhash ON image_hashes(dhash)"
    )


MIGRATIONS = (
    (1, migrate_collection_positions),
    (2, migrate_illust_schema),
    (3, repair_illust_schema),
    (4, add_illust_refresh_failed_at),
    (5, drop_collection_tables),
    (6, add_search_cache_alias_tags),
    (7, migrate_x_tables),
)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]
