"""数据库滚动快照（`backups.py` + `background.take_database_snapshot`）。

背景：2026-09-30 的事故里，唯一能救回数据的是一份**迁移前自动备份** —— 而迁移备份的
时间点由"上次迁移"决定，距上次迁移越久、可恢复的点就越旧（那次恰好是 12 分钟前，
纯属运气）。这一层按固定间隔再留一份，并且**只删自己命名的文件**。
"""
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine

import backups
import background
import config
import routes_settings
import runtime


def _make_db(path, rows=3):
    """建一个含 illusts 表的 SQLite 库（列集最小化，只用来验证"数据在不在"）。"""
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE illusts (pixiv_id INTEGER PRIMARY KEY, title TEXT)')
    con.executemany('INSERT INTO illusts (pixiv_id, title) VALUES (?, ?)',
                    [(i, f't{i}') for i in range(rows)])
    con.commit()
    con.close()
    return path


def _rows(path):
    con = sqlite3.connect(f'file:///{str(path).replace(chr(92), "/")}?mode=ro', uri=True)
    try:
        return con.execute('SELECT count(*) FROM illusts').fetchone()[0]
    finally:
        con.close()


def _touch(path, when):
    os.utime(path, (when, when))


# ── 纯函数：清点 / 到期 / 保留 ──

def test_snapshot_inventory_and_prune_only_touch_own_files(tmp_path):
    """清点与保留策略是一体两面，一起验：只认 `<库名>.snapshot-*.bak`。

    - **清点**：迁移备份（`<库名>.<ts>.bak`）与手工文件既不计数、也不出现在列表里。
    - **保留**：留最新 N 份，被删快照的 `-wal`/`-shm` 侧车一起走（留着就是垃圾），
      且**绝不碰**迁移备份与手工文件 —— 那是恢复数据时最后的依靠。
    """
    db = _make_db(tmp_path / 'pixiv.db')
    bdir = tmp_path / 'backups'
    bdir.mkdir()
    now = time.time()
    snaps = []
    for i in range(5):
        # 文件名按时间递增，mtime 也递增 —— 清理以 mtime 为准
        snap = bdir / f'pixiv.db.snapshot-2026090{i + 1}T000000.000000Z.bak'
        snap.write_bytes(b'snap')
        _touch(snap, now - (5 - i) * 100)
        snaps.append(snap)
    newest = snaps[-1]
    (bdir / f'{newest.name}-wal').write_bytes(b'wal-sidecar')
    (bdir / f'{newest.name}-shm').write_bytes(b'shm-sidecar')
    foreign = bdir / 'pixiv.db.20260930T081047.164762Z.bak'
    foreign.write_bytes(b'migration-backup')
    handwritten = bdir / 'keep-me.txt'
    handwritten.write_text('x', encoding='utf-8')

    assert sorted(p.name for p in backups.snapshot_paths(db)) == sorted(p.name for p in snaps), \
        '清点必须认出全部 5 份快照（且不含迁移备份与手工文件）'

    removed = backups.prune_snapshots(db, 2)

    # 比较集合而不是顺序：`prune` 内部按"从新到旧"遍历，删除顺序没有语义
    assert sorted(p.name for p in removed) == sorted(p.name for p in snaps[:3])
    assert (sorted(p.name for p in backups.snapshot_paths(db))
            == sorted(p.name for p in snaps[-2:])), '保留的是最新两份'
    assert not (bdir / f'{snaps[0].name}-wal').exists(), '被删快照的侧车也要一起走'
    assert foreign.exists() and handwritten.exists(), '迁移备份与手工文件一律不碰'

    # keep<1 视为 1：永远至少留最新那份（不能把快照删光）
    backups.prune_snapshots(db, 0)
    assert [p.name for p in backups.snapshot_paths(db)] == [newest.name]


def test_snapshot_due_respects_interval_and_disabled(tmp_path):
    db = _make_db(tmp_path / 'pixiv.db')
    bdir = tmp_path / 'backups'
    bdir.mkdir()

    assert backups.snapshot_due(db, 86400) is True, '一份都没有时必须该做'
    assert backups.snapshot_due(db, 0) is False, 'interval<=0 = 该功能被禁用'

    snap = bdir / 'pixiv.db.snapshot-20260930T000000.000000Z.bak'
    snap.write_bytes(b'snap')
    now = time.time()
    _touch(snap, now - 100)

    assert backups.snapshot_due(db, 86400, now=now) is False, '未到间隔不该重复做'
    assert backups.snapshot_due(db, 60, now=now) is True, '超过间隔就该做'


# ── 真正落盘：内容完整（含 WAL 里已提交的事务）──

def test_take_snapshot_copies_database_and_checkpoints_wal(tmp_path):
    """快照必须包含**已提交但还在 -wal 里**的数据。

    这是最坏的一种假安全：文件看着在、行数却少。`PRAGMA wal_checkpoint(TRUNCATE)`
    由 `take_snapshot(engine=...)` 负责，这里用真实 engine 验证到行数级别。
    """
    db = tmp_path / 'pixiv.db'
    engine = create_engine(f'sqlite:///{db}')
    with engine.begin() as conn:
        conn.exec_driver_sql('PRAGMA journal_mode=WAL')
        conn.exec_driver_sql('CREATE TABLE illusts (pixiv_id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('INSERT INTO illusts (pixiv_id) VALUES (1)')

    snapshot = backups.take_snapshot(db, engine=engine)

    assert snapshot.name.startswith('pixiv.db.snapshot-') and snapshot.name.endswith('.bak')
    assert _rows(snapshot) == 1, 'WAL 里已提交的那一行必须在快照里'
    engine.dispose()


def test_take_database_snapshot_updates_state_and_prunes(tmp_path, monkeypatch):
    """`background.take_database_snapshot`：该做才做、如实更新运行态、并按 `keep` 淘汰。

    三件事合在一起验，因为它们发生在**同一次调用**里（留一份新的 → 清点 → 淘汰旧的 →
    回填状态）：拆成两个用例就要各自重放一遍"已有 3 份旧快照"的场景，而状态与淘汰数
    本来就该一起看（`count` 是淘汰后的份数，不是本次新增数）。
    """
    db = _make_db(tmp_path / 'pixiv.db', rows=2)
    bdir = tmp_path / 'backups'
    bdir.mkdir()
    now = time.time()
    for i in range(3):                      # 已有 3 份旧快照，便于验证淘汰
        old = bdir / f'pixiv.db.snapshot-2026090{i + 1}T000000.000000Z.bak'
        old.write_bytes(b'old')
        # mtime 都超过 1 天：`interval=86400` 下"该做"的前提（也顺带验证到期判定走 mtime）
        _touch(old, now - (5 - i) * 100000)
    monkeypatch.setattr(config, 'DATABASE_PATH', str(db))
    monkeypatch.setattr(backups, 'backup_dir_for', lambda path, backup_dir=None: bdir)
    runtime._snapshot_state.update({'interval': 86400, 'keep': 2, 'last_at': None,
                                    'last_path': None, 'last_error': '旧错误', 'count': 0,
                                    'last_pruned': 0})

    name = background.take_database_snapshot()

    assert name and name.endswith('.bak')
    state = runtime._snapshot_state
    assert state['last_path'] == name
    assert state['last_error'] is None, '成功必须清掉上一次的错误（非空 = 最近一次就失败）'
    assert state['count'] == 2, f'keep=2，留最新两份，实际 {state}'
    assert state['last_pruned'] == 2, '淘汰掉两份旧的（3 + 1 - 2）'
    assert _rows(bdir / name) == 2, '快照内容必须是完整的库（含 WAL 里已提交的行）'
    assert state['last_at'] and datetime.fromisoformat(state['last_at'])

    # 还没到间隔：不重复做，但份数照实回填
    assert background.take_database_snapshot() is None
    assert runtime._snapshot_state['count'] == 2


# ── 观测接口 ──

def test_backups_endpoint_reports_state_and_files(client, tmp_path, monkeypatch):
    db = _make_db(tmp_path / 'pixiv.db')
    bdir = tmp_path / 'backups'
    bdir.mkdir()
    snap = bdir / 'pixiv.db.snapshot-20260930T000000.000000Z.bak'
    snap.write_bytes(b'x' * 128)
    (bdir / 'pixiv.db.20260930T081047.164762Z.bak').write_bytes(b'migration-backup')
    monkeypatch.setattr(config, 'DATABASE_PATH', str(db))
    monkeypatch.setattr(backups, 'backup_dir_for', lambda path, backup_dir=None: bdir)

    data = client.get('/api/backups').get_json()

    assert set(data) == {'state', 'db_path', 'backup_dir', 'files'}
    assert set(data['state']) == {'enabled', 'interval', 'keep', 'last_at', 'last_path',
                                  'last_error', 'last_pruned', 'count'}
    assert [f['name'] for f in data['files']] == [snap.name], '迁移备份不出现在文件列表里'
    assert data['files'][0]['size'] == 128
    assert data['backup_dir'] == str(bdir)
