# ── 数据库滚动快照（与「迁移前自动备份」互补）──
#
# 为什么需要这一层：迁移备份的时间点由"上次迁移"决定，距上次迁移越久、意外删除后可
# 恢复的点就越旧。2026-09-30 的事故（测试夹具连到生产库后清空了 illusts）里，救回
# 数据的恰好是一次 12 分钟前的迁移备份 —— 纯属运气；如果那次误删早发生十分钟，最近
# 的备份就是三周前的。
#
# 纪律：
#   1. 只写 `<库名>.snapshot-<UTC 时间戳>.bak`，清理时**只删自己这种命名的文件**
#      （迁移备份 `pixiv.db.<时间戳>.bak` 与目录里的其它文件一律不碰）。
#   2. 快照前先 `PRAGMA wal_checkpoint(TRUNCATE)`（复用迁移那套），否则 WAL 里已提交
#      的事务不会进主库文件 —— "快照缺最新数据"是最坏的一种假安全。
#   3. 清点/清理以 **mtime** 为准（文件名时间戳是 UTC，而 mtime 是本地可比的量）。
from __future__ import annotations

import logging
import time
from pathlib import Path

from migrations.runner import _checkpoint_wal, backup_database

logger = logging.getLogger(__name__)

SNAPSHOT_LABEL = 'snapshot-'


def backup_dir_for(db_path: str | Path, backup_dir: str | Path | None = None) -> Path:
    """快照目录：默认与迁移备份同处 `<实例目录>/backups`。"""
    if backup_dir is not None:
        return Path(backup_dir)
    return Path(db_path).parent / 'backups'


def snapshot_paths(db_path: str | Path, backup_dir: str | Path | None = None) -> list[Path]:
    """本库自己的快照文件，按 mtime 从新到旧。

    只认 `<库名>.snapshot-*.bak`：迁移备份与用户手工放进来的文件都不算快照，
    既不会被清点也不会被删除。
    """
    source = Path(db_path)
    directory = backup_dir_for(source, backup_dir)
    if not directory.is_dir():
        return []
    pattern = f'{source.name}.{SNAPSHOT_LABEL}*.bak'
    files = [p for p in directory.glob(pattern) if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def latest_snapshot(db_path: str | Path, backup_dir: str | Path | None = None) -> Path | None:
    found = snapshot_paths(db_path, backup_dir)
    return found[0] if found else None


def snapshot_due(db_path: str | Path, interval: int, *,
                 backup_dir: str | Path | None = None, now: float | None = None) -> bool:
    """是否该再做一份快照。`interval <= 0` 恒为 False（该功能被禁用）。"""
    if interval <= 0:
        return False
    newest = latest_snapshot(db_path, backup_dir)
    if newest is None:
        return True
    age = (time.time() if now is None else now) - newest.stat().st_mtime
    return age >= interval


def prune_snapshots(db_path: str | Path, keep: int, *,
                     backup_dir: str | Path | None = None) -> list[Path]:
    """保留最新的 `keep` 份快照，删除更旧的（含它们的 `-wal`/`-shm` 侧车文件）。

    返回被删掉的快照路径。`keep < 1` 视为 1：永远至少留最新那份 —— "快照被自己的
    保留策略删光"是这套机制最不该出现的失效方式。
    """
    keep = max(1, int(keep))
    removed: list[Path] = []
    for stale in snapshot_paths(db_path, backup_dir)[keep:]:
        for path in (stale, Path(str(stale) + '-wal'), Path(str(stale) + '-shm')):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as e:
                logger.warning(f'[backup] 删除旧快照失败 {path}: {e}')
                continue
            if path == stale:
                removed.append(stale)
    return removed


def take_snapshot(db_path: str | Path, *, backup_dir: str | Path | None = None,
                  engine=None) -> Path:
    """做一份快照；返回新快照路径。

    `engine` 非空时先做 WAL checkpoint（保证已提交事务在快照里）；为空表示调用方
    确认"此刻没有活跃连接"，只做文件复制。**清理旧份不在这里做** —— 保留策略由
    调用方显式调 `prune_snapshots`（两件事的失败影响面不同，混在一起会让"快照失败"
    与"清理失败"互相掩盖）。
    """
    if engine is not None:
        try:
            _checkpoint_wal(engine)
        except Exception as e:
            # checkpoint 失败（如别的连接持读锁）只降级：`backup_database` 会把
            # -wal/-shm 一并复制，副本仍能读到全部已提交数据
            logger.warning(f'[backup] 快照前 WAL checkpoint 失败，改为连侧车文件一起复制: {e}')
    return backup_database(db_path, backup_dir, label=SNAPSHOT_LABEL)
