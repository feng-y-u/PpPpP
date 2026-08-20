from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
import shutil

from sqlalchemy.engine import Connection, Engine


Migration = tuple[int, Callable[[Connection], None]]


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
        backup_database(database, backup_dir)

    for version, migration in pending:
        with engine.begin() as conn:
            migration(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {version:d}")
        current_version = version
