import shutil
import sqlite3

import pytest
from sqlalchemy import create_engine, event

from migrations import runner
from migrations.runner import backup_database, run_migrations
from migrations.versions import LATEST_SCHEMA_VERSION, MIGRATIONS

# v5 会删掉两张收藏夹表，所以"v1 回填 position"这类断言必须在 v5 之前跑。
# 按版本号取而不是 MIGRATIONS[:4]：将来追加 v6 也不会把 v5 的删表带进来。
MIGRATIONS_BEFORE_V5 = tuple(m for m in MIGRATIONS if m[0] < 5)


def _user_version(engine):
    with engine.connect() as conn:
        return conn.exec_driver_sql("PRAGMA user_version").scalar()


def _table_names(engine):
    with engine.connect() as conn:
        return {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def _fk_engine(database):
    """建一个开启外键约束的文件库。

    SQLite 默认不校验外键（`PRAGMA foreign_keys` 默认 0），只有显式打开才让
    "先删子表"成为可验证的约束：反序 DROP 父表会抛 FOREIGN KEY constraint failed。
    """
    engine = create_engine(f"sqlite:///{database}")

    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    event.listen(engine, "connect", _enable_foreign_keys)
    return engine


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


def test_runner_advances_version_only_for_successful_migrations():
    """`user_version` 只在迁移**真正成功**后推进。

    两段共用同一套"内存库 + 合成迁移"的准备，断言的也是同一个量（user_version），
    只有迁移成功/失败这一个输入不同，故合为一条用例（各自断言逐条保留）。
    """
    # ① 成功的迁移按序执行一次（重复调用是 no-op），版本推进到 2
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

    # ② 失败的迁移：不推进版本（下次启动会重试）
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

    # 只跑到 v4：本用例要验证的是"旧库升级不丢数据 + v1 回填 position"，
    # 而 v5 会把 collection_items 整表删掉（那部分由下面的 v5 用例覆盖）。
    run_migrations(engine, MIGRATIONS_BEFORE_V5)

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

    assert _user_version(engine) == 4
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


def test_fresh_database_skips_legacy_collection_migration():
    """全新库（没有 collection_items）跑完整迁移串必须是 no-op，不能抛 "no such table"。

    回归：收藏夹模型删除后 `create_all()` 不再建这两张表，v1
    `migrate_collection_positions` 的 `ALTER TABLE collection_items` 会直接炸掉
    `init_db()`（全新安装 + 每个测试的临时库都会起不来）。v1 因此加了存在性检查，
    无表可迁移时直接返回；本用例把这条件钉死。
    """
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE illusts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pixiv_id INTEGER NOT NULL UNIQUE,
                title VARCHAR DEFAULT ''
            )
            """
        )

    run_migrations(engine, MIGRATIONS)

    assert _user_version(engine) == LATEST_SCHEMA_VERSION


def test_collection_drop_migration_removes_tables_and_keeps_illusts(tmp_path):
    """v5：带收藏夹数据的旧库升级后两张表消失、illusts 不动、可重复执行、有备份。"""
    database = tmp_path / "pixiv.db"
    engine = _fk_engine(database)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE illusts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pixiv_id INTEGER NOT NULL UNIQUE,
                title VARCHAR DEFAULT ''
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO illusts (pixiv_id, title) "
            "VALUES (123, 'kept'), (456, 'also-kept')"
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE collection_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER NOT NULL REFERENCES collections(id),
                pixiv_id INTEGER NOT NULL,
                position REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.exec_driver_sql("INSERT INTO collections (name) VALUES ('我的收藏')")
        conn.exec_driver_sql(
            "INSERT INTO collection_items (collection_id, pixiv_id, position) "
            "VALUES (1, 123, 1000.0)"
        )
        conn.exec_driver_sql("PRAGMA user_version = 4")

    run_migrations(engine, MIGRATIONS)

    # ① 两表消失（外键打开着，说明确实是先删子表再删父表）
    tables = _table_names(engine)
    assert "collection_items" not in tables
    assert "collections" not in tables
    # ② illusts 行数与内容不变
    with engine.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT pixiv_id, title FROM illusts ORDER BY pixiv_id"
        ).all() == [(123, "kept"), (456, "also-kept")]
    # ③ 版本推进到最新（v6）
    assert _user_version(engine) == LATEST_SCHEMA_VERSION

    # ④ 重复执行不抛错：runner 再跑一次是 no-op；直接重放 v5 函数也必须是 no-op
    run_migrations(engine, MIGRATIONS)
    drop_collection_tables = dict(MIGRATIONS)[5]
    with engine.begin() as conn:
        drop_collection_tables(conn)

    # ⑤ 迁移前已备份，且备份里两张表的原始数据都还在（删除确实发生在备份之后）
    backups = sorted((tmp_path / "backups").glob("pixiv.db.*.bak"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("SELECT name FROM collections").fetchall() == [
            ("我的收藏",)
        ]
        assert backup.execute(
            "SELECT pixiv_id, position FROM collection_items"
        ).fetchall() == [(123, 1000.0)]
    finally:
        backup.close()


# ── v6：预取标签同义词列 ──

def test_alias_tags_migration_adds_column_with_empty_default(tmp_path):
    """v6：老库的 search_cache 补出 alias_tags 且默认 '[]'，重放幂等。

    默认值必须是空数组而不是 NULL：老标签的语义是"没有同义词"（sources = [tag]），
    升级后不能凭空多搜一个不存在的词。已有别名也不能被重放冲掉。
    """
    database = tmp_path / "pixiv.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE search_cache ("
            "tag VARCHAR PRIMARY KEY, illust_ids TEXT DEFAULT '[]',"
            " cached_at DATETIME, status VARCHAR DEFAULT 'idle',"
            " error VARCHAR DEFAULT '', total INTEGER DEFAULT 0)"
        )
        conn.exec_driver_sql(
            "INSERT INTO search_cache (tag, illust_ids) VALUES ('旧标签', '[1, 2]')"
        )
        conn.exec_driver_sql("PRAGMA user_version = 5")

    run_migrations(engine, MIGRATIONS)

    with engine.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT tag, illust_ids, alias_tags FROM search_cache"
        ).one() == ('旧标签', '[1, 2]', '[]')
    assert _user_version(engine) == LATEST_SCHEMA_VERSION

    add_alias_tags = dict(MIGRATIONS)[6]
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE search_cache SET alias_tags = '[\"星引擎party\"]'")
        add_alias_tags(conn)   # 幂等：已存在列时不重建、不清空
        assert conn.exec_driver_sql(
            "SELECT alias_tags FROM search_cache").scalar() == '["星引擎party"]'


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


def test_backup_captures_all_committed_data_in_both_journal_modes(tmp_path):
    """备份内容必须完整：WAL 库（数据可能只在 -wal 里）与普通回滚日志库都要覆盖。

    同一不变式（`run_migrations` 落下的备份读得到全部已提交数据），只有 journal 模式
    这一个输入不同，故合为一条用例；两段各自的准备与断言逐段保留（各自的临时库，
    避免互相污染）。原 `test_backup_still_works_on_non_wal_db` 的断言也在这里。
    """
    # ① WAL 库：备份前必须 checkpoint，否则停在 -wal 里的已提交数据不会进备份
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    engine, database = _wal_engine(wal_dir)
    # 前置条件：此刻主库文件单独拿出来是读不到这行的（数据确实只在 WAL 里）
    lone_before = wal_dir / "lone-before.db"
    shutil.copy2(database, lone_before)
    with pytest.raises(sqlite3.OperationalError):
        _rows_in(lone_before)

    run_migrations(engine, ((1, lambda conn: None),))

    backups = sorted((wal_dir / "backups").glob("*.bak"))
    assert len(backups) == 1
    assert _rows_in(backups[0]) == [("only-in-wal",)]

    # 证明是 checkpoint（而非复制侧车）起的作用：主库文件本身已含该行
    lone_after = wal_dir / "lone-after.db"
    shutil.copy2(database, lone_after)
    assert _rows_in(lone_after) == [("only-in-wal",)]

    # ② 非 WAL 库：checkpoint 是 no-op，备份行为与既有实现一致
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    database = plain_dir / "pixiv.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() != "wal"
        conn.commit()
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.exec_driver_sql("INSERT INTO t (v) VALUES ('rollback-journal')")

    run_migrations(engine, ((1, lambda conn: None),))

    backups = sorted((plain_dir / "backups").glob("*.bak"))
    assert len(backups) == 1
    assert _rows_in(backups[0]) == [("rollback-journal",)]
    assert not list((plain_dir / "backups").glob("*-wal")), "非 WAL 库不该产生侧车副本"


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
