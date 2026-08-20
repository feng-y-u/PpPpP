from collections.abc import Callable, Iterable

from sqlalchemy.engine import Connection, Engine


Migration = tuple[int, Callable[[Connection], None]]


def run_migrations(engine: Engine, migrations: Iterable[Migration]) -> None:
    """Apply pending SQLite migrations in version order."""
    ordered = tuple(migrations)
    versions = [version for version, _migration in ordered]
    if versions != sorted(set(versions)) or any(version < 1 for version in versions):
        raise ValueError("migration versions must be unique positive integers in order")

    with engine.connect() as conn:
        current_version = conn.exec_driver_sql("PRAGMA user_version").scalar() or 0

    for version, migration in ordered:
        if version <= current_version:
            continue
        with engine.begin() as conn:
            migration(conn)
            conn.exec_driver_sql(f"PRAGMA user_version = {version:d}")
        current_version = version
