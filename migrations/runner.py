from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
import logging
import shutil

from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

Migration = tuple[int, Callable[[Connection], None]]


def _checkpoint_wal(engine: Engine) -> None:
    """把 WAL 里已提交的事务并回主库文件（非 WAL 库为 no-op）。

    WAL 模式下"已提交"不等于"已写进主库文件"：事务可能整段还躺在 `-wal` 里，
    此时直接 `copy2` 主库得到的备份会**缺数据**（迁移前的快照承诺落空）。
    TRUNCATE 保证 checkpoint 完还会截断 `-wal`；其他连接持读锁时它会等待
    `busy_timeout` 后返回 busy 而不是抛错，那种情况由 `backup_database` 复制
    侧车文件兜底，故此处失败只降级、不阻断迁移。
    """
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")


def backup_database(db_path: str | Path, backup_dir: str | Path | None = None) -> Path:
    """Copy an existing SQLite database to a uniquely timestamped backup file."""
    source = Path(db_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    target_dir = Path(backup_dir) if backup_dir is not None else source.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = target_dir / f"{source.name}.{timestamp}.bak"
    counter = 0
    while target.exists():
        counter += 1
        target = target_dir / f"{source.name}.{timestamp}.{counter}.bak"
    shutil.copy2(source, target)
    # belt & braces：checkpoint 没能并回主库时（其他 reader 占用、非 TRUNCATE
    # 能完成的场景），把 -wal/-shm 以同名后缀一并复制，副本仍能被 SQLite 正常
    # 打开并读到全部已提交数据。缺 -shm 时 SQLite 会自行重建，故复制它是为了
    # 保留"整套文件可一起搬运"的语义。
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, Path(str(target) + suffix))
    return target


def run_migrations(
    engine: Engine,
    migrations: Iterable[Migration],
    *,
    backup_dir: str | Path | None = None,
) -> None:
    """Apply pending SQLite migrations in version order."""
    ordered = tuple(migrations)
    versions = [version for version, _migration in ordered]
    if versions != sorted(set(versions)) or any(version < 1 for version in versions):
        raise ValueError("migration versions must be unique positive integers in order")

    with engine.connect() as conn:
        current_version = conn.exec_driver_sql("PRAGMA user_version").scalar() or 0

    pending = [(version, migration) for version, migration in ordered if version > current_version]
    database = engine.url.database
    if pending and database and database != ":memory:" and Path(database).is_file():
        # 先 checkpoint 再复制主库：否则 WAL 里尚未并回的事务不会进备份。
        # checkpoint 失败（如其他连接持读锁）只降级为告警 —— 备份仍要照做，
        # 由 backup_database 复制 -wal/-shm 兜底。
        try:
            _checkpoint_wal(engine)
        except Exception as e:
            logger.warning(f"WAL checkpoint failed before backup, copying -wal/-shm instead: {e}")
        backup_database(database, backup_dir)

    for version, migration in pending:
        with engine.begin() as conn:
            migration(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {version:d}")
        current_version = version
