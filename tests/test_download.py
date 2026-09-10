"""下载引擎与下载取消/重置的回归测试。

重点覆盖 2026-09-10 审计发现的 P0-1：reset 与 worker"固化 done"的竞态。
旧实现里 `_download_illust` 在"最后一次取消检查 → safe_commit"之间无条件写入
`download_status='done'`，而 `_cancel_download_internal(reset=True)` 会先删掉
整个作品目录再置空状态 —— 两者交错时留下 DB=done 但磁盘无文件的"幻影成功"：
图库显示已下载、点开全 404，trigger 被 done 挡回、reset 被状态守卫挡回，用户
只能删稿重下。

修复后两侧都走条件更新（CAS）：worker 只在状态仍是 downloading 时固化 done；
reset 先抢状态、抢到才删文件，抢不到就原样返回 409。
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager

import pytest

import background
import routes_download
from models import DownloadLog, Illust, get_session, safe_commit
from runtime import (_download_progress, _queued_downloads,
                     download_cancellations)
from sqlalchemy import update as sa_update


class _FakeResponse:
    def __init__(self, content: bytes):
        self._content = content

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=8192):
        yield self._content


class _FakeSession:
    """下载引擎的替身 session：固定返回字节，或在指定 URL 上抛错。"""

    def __init__(self, fail_on: str | None = None, content: bytes = b'x' * 16):
        self.fail_on = fail_on
        self.content = content
        self.calls: list[str] = []
        self.closed = False

    def get(self, url, timeout=None, stream=False):
        self.calls.append(url)
        if self.fail_on and url == self.fail_on:
            raise OSError('模拟下载中断')
        return _FakeResponse(self.content)

    def close(self):
        self.closed = True


@pytest.fixture
def dl_env(monkeypatch, tmp_path, clean_db):
    """隔离的下载目录 + 去掉页间延迟 + 清理进程级下载状态。"""
    import helpers
    monkeypatch.setattr(helpers, 'DOWNLOAD_DIR', str(tmp_path))
    monkeypatch.setattr(background, 'PAGE_DOWNLOAD_INTERVAL', 0)
    for state in (background.download_locks, download_cancellations,
                  _queued_downloads, _download_progress):
        state.clear()
    yield tmp_path
    for state in (background.download_locks, download_cancellations,
                  _queued_downloads, _download_progress):
        state.clear()


def _make_illust(db, pixiv_id: int, urls: list[str], status: str | None = None) -> Illust:
    illust = Illust(pixiv_id=pixiv_id, title=f'作品{pixiv_id}', download_status=status)
    illust.original_urls_list = urls
    db.add(illust)
    safe_commit(db)
    return illust


def _urls(pixiv_id: int, pages: int) -> list[str]:
    return [f'https://i.pximg.net/img-original/img/x/{pixiv_id}_p{i}.jpg'
            for i in range(pages)]


def _read(pid: int) -> tuple[str | None, list[str] | None, list[str]]:
    """读回作品的当前状态、本地路径与日志动作序列。"""
    with get_session() as db:
        row = db.query(Illust).filter(Illust.pixiv_id == pid).one()
        actions = [log.action for log in
                   db.query(DownloadLog).filter(DownloadLog.pixiv_id == pid)
                   .order_by(DownloadLog.id).all()]
        return row.download_status, row.local_paths_list, actions


def _assert_no_dangling_state(pid: int):
    """下载结束后不得残留锁、取消标记或进度条目。"""
    assert pid not in background.download_locks
    assert pid not in download_cancellations
    assert pid not in _download_progress


# ── 正常路径 ──

def test_download_success_marks_done_with_real_files(dl_env, monkeypatch):
    pid = 80001
    urls = _urls(pid, 2)
    with get_session() as db:
        _make_illust(db, pid, urls)
    fake = _FakeSession()
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: fake)

    background._download_illust(pid)

    status, paths, actions = _read(pid)
    assert status == 'done'
    assert paths and len(paths) == 2
    assert all(os.path.isfile(p) for p in paths), 'done 必须伴随真实存在的文件'
    with get_session() as db:
        row = db.query(Illust).filter(Illust.pixiv_id == pid).one()
        assert row.file_size == 32          # 2 页 × 16 字节
        assert row.downloaded_at is not None
    assert actions.count('done') == 1 and actions[0] == 'start'
    assert fake.closed, 'session 必须在 finally 里关闭'
    _assert_no_dangling_state(pid)


# ── 失败路径 ──

def test_download_page_failure_marks_failed_and_removes_files(dl_env, monkeypatch):
    pid = 80002
    urls = _urls(pid, 2)
    with get_session() as db:
        _make_illust(db, pid, urls)
    fake = _FakeSession(fail_on=urls[1])
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: fake)

    background._download_illust(pid)

    status, paths, actions = _read(pid)
    assert status == 'failed'
    assert paths is None
    assert 'done' not in actions
    assert 'failed' in actions
    assert not os.path.isdir(os.path.join(str(dl_env), str(pid))), '失败后不留半成品目录'
    _assert_no_dangling_state(pid)


def test_download_without_original_urls_never_marks_done(dl_env, monkeypatch):
    """无原图地址时置空以便重试，不能固化为 done（既有语义，CAS 不得改变）。"""
    pid = 80003
    with get_session() as db:
        _make_illust(db, pid, [])

    background._download_illust(pid)

    status, paths, actions = _read(pid)
    assert status is None
    assert paths is None
    # 既有语义：引擎先记 start，再发现无原图地址而记 failed（保持不动）
    assert actions == ['start', 'failed']
    with get_session() as db:
        msg = (db.query(DownloadLog)
               .filter(DownloadLog.pixiv_id == pid, DownloadLog.action == 'failed')
               .one().message)
    assert '无原图地址' in msg
    _assert_no_dangling_state(pid)


# ── 竞态路径 1：reset 抢先（worker 在写入窗口内被暂停）──

def test_reset_during_final_write_leaves_no_phantom_done(dl_env, clean_db, monkeypatch, client):
    """核心回归：reset 在"取消检查之后、固化 done 之前"完成 → 不得出现 done。"""
    pid = 80004
    urls = _urls(pid, 1)
    with get_session() as db:
        _make_illust(db, pid, urls)
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: _FakeSession())

    reached = threading.Event()
    release = threading.Event()
    real_datetime = background.datetime

    class _PausingDatetime(real_datetime):
        """在 worker 组装 done 状态（调用 datetime.now）时暂停：该调用恰好位于
        "最后一次取消检查"与"状态条件更新"之间，即审计确认的竞态窗口。"""

        @classmethod
        def now(cls, tz=None):
            reached.set()
            assert release.wait(10), '测试未能释放 worker'
            return real_datetime.now(tz)

    monkeypatch.setattr(background, 'datetime', _PausingDatetime)

    worker = threading.Thread(target=background._download_illust, args=(pid,),
                              name='race-worker', daemon=True)
    worker.start()
    assert reached.wait(10), 'worker 未到达写入窗口'

    token = client.get('/csrf-token').get_json()['token']
    resp = client.post(f'/download/reset/{pid}', headers={'X-CSRF-Token': token})
    assert resp.status_code == 200 and resp.get_json()['status'] == 'reset'

    release.set()
    worker.join(10)
    assert not worker.is_alive()

    status, paths, actions = _read(pid)
    assert status is None, 'reset 之后不得出现 done 幻影'
    assert paths is None
    assert 'done' not in actions, '不得留下"下载完成"日志'
    assert 'failed' in actions        # 重置日志
    assert not os.path.isdir(os.path.join(str(dl_env), str(pid))), '磁盘不应残留文件'
    _assert_no_dangling_state(pid)


# ── 竞态路径 2：worker 抢先（reset 守卫读到的是过期状态）──

def test_reset_yields_when_worker_commits_before_claim(dl_env, clean_db, monkeypatch, app):
    """守卫读到 downloading 但 worker 已抢先提交 done → 409，且不得删文件。"""
    pid = 80005
    work_dir = dl_env / str(pid)
    work_dir.mkdir()
    kept = work_dir / f'{pid}_p0.jpg'
    kept.write_bytes(b'z' * 4)
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1), status='downloading')

    def _update_after_worker_commit(entity):
        """在"守卫读取 → 条件更新"之间插入 worker 的抢先提交。

        用 sqlalchemy.update 直接建语句（不依赖被测模块的导入），这样回退修复
        后钩子仍会被 monkeypatch 安装，测试以**行为**失败（旧代码返回 200 并删了
        已完成的文件），而不是以 AttributeError 失败。
        """
        with get_session() as other:
            other.execute(
                sa_update(Illust)
                .where(Illust.pixiv_id == pid)
                .values(download_status='done',
                        local_paths=json.dumps([str(kept)]),
                        file_size=4)
            )
            safe_commit(other)
        return sa_update(entity)

    monkeypatch.setattr(routes_download, 'update', _update_after_worker_commit,
                        raising=False)

    with app.app_context():
        _, code = routes_download._cancel_download_internal(pid, reset=True)

    assert code == 409, '抢先失败的重置必须显式返回冲突，而不是删掉已完成的文件'
    assert kept.is_file(), '已完成的下载文件不能被重置删除'
    status, paths, actions = _read(pid)
    assert status == 'done'
    assert paths == [str(kept)]
    assert 'failed' not in actions, '未执行的重置不应写"下载已手动重置"日志'
    _assert_no_dangling_state(pid)


def test_reset_after_completion_returns_400_and_keeps_files(dl_env, clean_db, monkeypatch, client):
    """常规路径：已下载完成的作品重置应被守卫挡回（400），文件保持不动。"""
    pid = 80006
    urls = _urls(pid, 1)
    with get_session() as db:
        _make_illust(db, pid, urls)
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: _FakeSession())

    background._download_illust(pid)
    status, paths, _ = _read(pid)
    assert status == 'done'

    token = client.get('/csrf-token').get_json()['token']
    resp = client.post(f'/download/reset/{pid}', headers={'X-CSRF-Token': token})
    assert resp.status_code == 400

    status, after_paths, actions = _read(pid)
    assert status == 'done'
    assert after_paths == paths and all(os.path.isfile(p) for p in after_paths)
    assert 'failed' not in actions


# ── 竞态路径 3：真并发压力（两种顺序都必须收敛到一致状态）──

def test_reset_racing_worker_never_leaves_phantom_done(dl_env, clean_db, monkeypatch, app):
    """worker 与 reset 同时起跑若干轮，断言不变式：
    状态 done ⇒ 文件全部存在；且不残留锁与取消标记。"""
    import helpers
    assert helpers.DOWNLOAD_DIR == str(dl_env)

    for round_no in range(12):
        pid = 80100 + round_no
        with get_session() as db:
            _make_illust(db, pid, _urls(pid, 2))
        monkeypatch.setattr(background, 'build_pixiv_session', lambda: _FakeSession())

        gate = threading.Barrier(2, timeout=10)
        codes: dict[str, int] = {}

        def _run_worker():
            gate.wait()
            background._download_illust(pid)

        def _run_reset():
            gate.wait()
            with app.app_context():
                _, code = routes_download._cancel_download_internal(pid, reset=True)
            codes['reset'] = code

        t_worker = threading.Thread(target=_run_worker, daemon=True)
        t_reset = threading.Thread(target=_run_reset, daemon=True)
        t_worker.start()
        t_reset.start()
        t_worker.join(10)
        t_reset.join(10)
        assert not t_worker.is_alive() and not t_reset.is_alive()

        status, paths, actions = _read(pid)
        assert codes['reset'] in (200, 400, 409)
        if status == 'done':
            assert paths, 'done 必须带 local_paths'
            assert all(os.path.isfile(p) for p in paths), \
                f'第 {round_no} 轮出现幻影 done：DB=done 但文件已被重置删除'
            assert 'done' in actions
        else:
            assert paths is None or paths == []
            assert 'done' not in actions
        _assert_no_dangling_state(pid)
