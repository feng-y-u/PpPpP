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
import logging
import os
import threading
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.exc import OperationalError

import background
import routes_download
from models import DownloadLog, Illust, get_session, safe_commit
from runtime import (_download_progress, _queued_downloads,
                     _download_queue_lock, is_queued_download,
                     download_cancellations)


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200, headers: dict | None = None):
        self._content = content
        self.status_code = status_code
        self.headers = headers or {}

    @property
    def is_redirect(self) -> bool:
        """与 requests.Response 同义：3xx 且带 Location。"""
        return 300 <= self.status_code < 400 and 'Location' in self.headers

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=8192):
        yield self._content


class _FakeSession:
    """下载引擎的替身 session：固定返回字节，或在指定 URL 上抛错/重定向。"""

    def __init__(self, fail_on: str | None = None, content: bytes = b'x' * 16,
                 redirect_on: str | None = None,
                 redirect_to: str = 'https://evil-cdn.example/x.jpg'):
        self.fail_on = fail_on
        self.content = content
        self.redirect_on = redirect_on
        self.redirect_to = redirect_to
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.closed = False
        # 真 session 的会话级 Cookie 头（build_pixiv_session 会挂上 PHPSESSID）：
        # 无凭据会话的替身里不带这个键，便于断言"没把凭据发出去"
        self.headers: dict = {'Cookie': 'PHPSESSID=secret'}

    def get(self, url, timeout=None, stream=False, allow_redirects=True):
        self.calls.append(url)
        self.kwargs.append({'allow_redirects': allow_redirects, 'timeout': timeout})
        if self.fail_on and url == self.fail_on:
            raise OSError('模拟下载中断')
        if self.redirect_on and url == self.redirect_on:
            return _FakeResponse(b'', status_code=302, headers={'Location': self.redirect_to})
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


def _enqueue(pid: int) -> None:
    """按生产纪律入队（写点必须在队列锁内）。"""
    with _download_queue_lock:
        _queued_downloads.add(pid)


def _assert_no_dangling_state(pid: int):
    """下载结束后不得残留锁、取消标记或进度条目。"""
    assert pid not in background.download_locks
    assert pid not in download_cancellations
    assert pid not in _download_progress
    assert not is_queued_download(pid)


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


# ── 提交失败（S2）：不得让作品卡在 downloading ──

def _fail_commit_on(monkeypatch, call_no: int, *, before_raise=None) -> dict:
    """让 background.safe_commit 在第 call_no 次调用时失败。

    替身保持真实语义：**先 rollback 再抛** —— safe_commit 的契约就是失败即回滚，
    不还原这一点会让被救路径看到"事务里已写好的 done"，测出假象。
    """
    real = background.safe_commit
    calls = {'n': 0, 'raised': 0}

    def _flaky(db, *args, **kwargs):
        calls['n'] += 1
        if calls['n'] == call_no:
            calls['raised'] += 1
            db.rollback()
            if before_raise is not None:
                before_raise()
            raise OperationalError('UPDATE illusts', {},
                                   Exception('database is locked'))
        return real(db, *args, **kwargs)

    monkeypatch.setattr(background, 'safe_commit', _flaky)
    return calls


def test_download_terminal_commit_failure_resets_to_failed(dl_env, monkeypatch, caplog):
    """完成态写入失败 → 复位为 failed，绝不留在 downloading（审计 S2 核心）。"""
    pid = 80201
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1))
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: _FakeSession())
    calls = _fail_commit_on(monkeypatch, 2)      # 1=开始下载，2=下载结束

    with caplog.at_level(logging.ERROR, logger='background'):
        background._download_illust(pid)

    assert calls['raised'] == 1
    status, paths, actions = _read(pid)
    assert status == 'failed', '提交失败必须复位为 failed，不能卡在 downloading'
    with get_session() as db:
        msgs = [log.message for log in
                db.query(DownloadLog).filter(DownloadLog.pixiv_id == pid).all()]
    assert any('状态写入失败' in m for m in msgs), '复位要留痕'
    assert 'done' not in actions
    assert '下载状态提交失败' in caplog.text, '必须留下 logger.error 供运维排查'
    _assert_no_dangling_state(pid)


def test_download_failure_commit_failure_resets_to_failed(dl_env, monkeypatch):
    """失败态写入也提交失败 → 仍要复位，不能因为一次失败而永久卡死。"""
    pid = 80202
    urls = _urls(pid, 2)
    with get_session() as db:
        _make_illust(db, pid, urls)
    monkeypatch.setattr(background, 'build_pixiv_session',
                        lambda: _FakeSession(fail_on=urls[1]))
    _fail_commit_on(monkeypatch, 2)

    background._download_illust(pid)

    status, paths, actions = _read(pid)
    assert status == 'failed'
    assert paths is None
    assert not os.path.isdir(os.path.join(str(dl_env), str(pid)))
    _assert_no_dangling_state(pid)


def test_download_start_commit_failure_aborts_without_touching_state(dl_env, monkeypatch):
    """起始状态没落库 → 不下载（否则结尾 CAS 必然失败，白下载一轮再自删）。"""
    pid = 80203
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1))
    fake = _FakeSession()
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: fake)
    _fail_commit_on(monkeypatch, 1)

    background._download_illust(pid)

    assert fake.calls == [], '起始提交失败后不应发起任何下载请求'
    status, paths, actions = _read(pid)
    assert status is None and paths is None
    assert actions == [], '回滚后不应残留 start 日志'
    assert not os.path.isdir(os.path.join(str(dl_env), str(pid)))
    _assert_no_dangling_state(pid)


def test_rescue_does_not_clobber_state_taken_over_by_others(dl_env, monkeypatch):
    """终态提交失败但行已被别的路径（如 reset）置空 → 复位不得覆盖，只记日志。"""
    pid = 80204
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1))
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: _FakeSession())

    def _reset_wins():
        with get_session() as other:
            other.execute(sa_update(Illust).where(Illust.pixiv_id == pid)
                          .values(download_status=None))
            safe_commit(other)

    _fail_commit_on(monkeypatch, 2, before_raise=_reset_wins)

    background._download_illust(pid)

    status, paths, actions = _read(pid)
    assert status is None, 'reset 的结果不能被复位逻辑覆盖成 failed'
    with get_session() as db:
        msgs = [log.message for log in
                db.query(DownloadLog).filter(DownloadLog.pixiv_id == pid).all()]
    assert not any('状态写入失败' in m for m in msgs), '没救到东西就不该写复位日志'
    _assert_no_dangling_state(pid)


# ── 幽灵 downloading 自愈（S2 第二半，routes_download）──

class _SyncExecutor:
    """把 submit 变成同步执行，让下载在请求内跑完，便于断言最终状态。"""

    def __init__(self):
        self.submitted: list[int] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append(args[0] if args else None)
        fn(*args, **kwargs)


def _post_download(client, pid: int):
    token = client.get('/csrf-token').get_json()['token']
    return client.post(f'/download/{pid}', headers={'X-CSRF-Token': token})


def test_trigger_download_recovers_ghost_downloading(dl_env, clean_db, monkeypatch, client):
    """状态停在 downloading 但 worker 已不在 → 自动复位并真正重新下载。"""
    pid = 80205
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1), status='downloading')
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: _FakeSession())
    executor = _SyncExecutor()
    monkeypatch.setattr(routes_download, 'download_executor', executor)
    assert pid not in _download_progress and pid not in _queued_downloads

    resp = _post_download(client, pid)

    assert resp.status_code == 200 and resp.get_json()['status'] == 'accepted'
    assert executor.submitted == [pid], '幽灵状态应被复位后继续正常下载'
    status, paths, actions = _read(pid)
    assert status == 'done'
    assert paths and all(os.path.isfile(p) for p in paths)
    with get_session() as db:
        msgs = [log.message for log in
                db.query(DownloadLog).filter(DownloadLog.pixiv_id == pid).all()]
    assert any('残留 downloading' in m for m in msgs)
    _assert_no_dangling_state(pid)


@pytest.mark.parametrize('signal', ['progress', 'queued'])
def test_ghost_detection_keeps_live_download(dl_env, clean_db, monkeypatch, client, signal):
    """实际在跑的下载（进度中/排队中）不能被误判成幽灵而复位。"""
    pid = 80206
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1), status='downloading')
    if signal == 'progress':
        _download_progress[pid] = {'current': 1, 'total': 2}
    else:
        _enqueue(pid)
    executor = _SyncExecutor()
    monkeypatch.setattr(routes_download, 'download_executor', executor)

    resp = _post_download(client, pid)

    assert resp.status_code == 200 and resp.get_json()['status'] == 'downloading'
    assert executor.submitted == []
    status, _, actions = _read(pid)
    assert status == 'downloading'
    with get_session() as db:
        msgs = [log.message for log in
                db.query(DownloadLog).filter(DownloadLog.pixiv_id == pid).all()]
    assert not any('残留 downloading' in m for m in msgs)
    assert not os.path.isdir(os.path.join(str(dl_env), str(pid)))


# ── 队列窗口（S3）──

def test_download_missing_row_logs_failed(dl_env, monkeypatch):
    """排队期间作品行被清理 → 不下载，但必须留下失败日志（旧实现无痕消失）。"""
    pid = 80301                       # 队列里有它，但库里没有这一行
    fake = _FakeSession()
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: fake)
    _enqueue(pid)

    background._download_illust(pid)

    assert fake.calls == [], '行不存在时不应发起任何下载请求'
    with get_session() as db:
        logs = db.query(DownloadLog).filter(DownloadLog.pixiv_id == pid).all()
    assert [l.action for l in logs] == ['failed']
    assert '作品行已被清理' in logs[0].message
    assert not os.path.isdir(os.path.join(str(dl_env), str(pid)))
    _assert_no_dangling_state(pid)


class _LockAuditedSet(set):
    """访问 `_queued_downloads` 时记录是否持有队列锁（供纪律测试断言）。

    只记录不抛：一次跑完能拿到全部违规点，而不是"谁先撞上算谁"。
    """

    def __init__(self, *args):
        super().__init__(*args)
        self.violations: list[str] = []

    def _audit(self, op: str) -> None:
        if not _download_queue_lock.locked():
            self.violations.append(op)

    def __iter__(self):
        self._audit('iter')
        return super().__iter__()

    def add(self, item):
        self._audit('add')
        return super().add(item)

    def discard(self, item):
        self._audit('discard')
        return super().discard(item)

    def __contains__(self, item):
        self._audit('contains')
        return super().__contains__(item)


def test_queued_download_access_always_under_lock(dl_env, clean_db, monkeypatch, client, app):
    """纪律守卫：所有 `_queued_downloads` 读写都必须持锁，且只经 snapshot/判定 helper。

    用带审计的 set 替换三个模块里的绑定（每个模块各自 from-import 了一份引用），
    再把主要生产路径跑一遍，最后一次性列出所有未持锁的访问。
    """
    pid = 80302
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1))
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: _FakeSession())

    guarded = _LockAuditedSet()
    import runtime
    for module in (runtime, background, routes_download):
        monkeypatch.setattr(module, '_queued_downloads', guarded)

    _finish = _SyncExecutor()             # 同步执行，让 worker 的 discard 也走到
    monkeypatch.setattr(routes_download, 'download_executor', _finish)

    # 1) 触发下载（add）→ 2) worker 全程（3 处 discard）→ 3) 取消（读 + discard）
    resp = _post_download(client, pid)
    assert resp.status_code == 200 and resp.get_json()['status'] == 'accepted'
    background._download_illust(pid)      # 覆盖 cancel-early-return / 正常路径的 discard
    with app.app_context():
        routes_download._cancel_download_internal(pid, reset=True)
    assert client.get('/api/downloads').status_code == 200      # 遍历读点

    assert guarded.violations == [], f'存在未持锁访问：{guarded.violations}'


def test_api_downloads_survives_concurrent_queue_mutation(dl_env, clean_db, client):
    """并发改队列时 /api/downloads 始终可用，且 queued 列表自洽（快照语义）。"""
    pids = list(range(80400, 80420))
    with get_session() as db:
        for pid in pids:
            _make_illust(db, pid, _urls(pid, 1))

    stop = threading.Event()
    errors: list[BaseException] = []

    def _mutate():
        n = 0
        while not stop.is_set():
            n += 1
            pid = pids[n % len(pids)]
            with _download_queue_lock:
                _queued_downloads.add(pid)
            with _download_queue_lock:
                _queued_downloads.discard(pid)
            # 让出 GIL：纯 Python 紧凑循环会把主线程饿死（实测 150 次请求从 1s
            # 拖到 22s），那样测的是 GIL 而不是并发正确性。
            time.sleep(0.0005)

    mutator = threading.Thread(target=_mutate, daemon=True)
    mutator.start()
    try:
        for _ in range(40):
            resp = client.get('/api/downloads')
            assert resp.status_code == 200
            for item in resp.get_json()['queued']:
                assert item['pixiv_id'] in pids
    except BaseException as e:      # noqa: BLE001 —— 记录后统一断言，便于打印原异常
        errors.append(e)
    finally:
        stop.set()
        mutator.join(10)

    assert not errors, f'并发下 /api/downloads 出错：{errors!r}'
    assert not mutator.is_alive()


# ── 重复下载保护（S9）──

def test_trigger_download_queued_returns_queued_not_duplicate(dl_env, clean_db, monkeypatch, client):
    """已在队列中的作品再次触发：返回 queued，且不提交第二个任务、不动状态行。"""
    pid = 80501
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1))
    executor = _SyncExecutor()
    monkeypatch.setattr(routes_download, 'download_executor', executor)
    _enqueue(pid)                     # 排队窗口：已入队，worker 还没轮到

    resp = _post_download(client, pid)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'queued'
    assert '已在下载队列中' in body['message']
    assert executor.submitted == [], '重复触发不得再提交任务'
    assert _read(pid)[0] is None, '状态行不该被这次请求改动'
    assert is_queued_download(pid)


def test_batch_download_skips_queued(dl_env, clean_db, monkeypatch, client):
    """批量下载：已在队列中的 pid 计入 skipped，只对其余 pid 提交任务。"""
    queued_pid, fresh_pid = 80502, 80503
    with get_session() as db:
        _make_illust(db, queued_pid, _urls(queued_pid, 1))
        _make_illust(db, fresh_pid, _urls(fresh_pid, 1))
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: _FakeSession())
    executor = _SyncExecutor()
    monkeypatch.setattr(routes_download, 'download_executor', executor)
    _enqueue(queued_pid)

    token = client.get('/csrf-token').get_json()['token']
    resp = client.post('/api/download/batch', json={'ids': [queued_pid, fresh_pid]},
                       headers={'X-CSRF-Token': token})

    assert resp.status_code == 200
    body = resp.get_json()
    assert (body['accepted'], body['skipped']) == (1, 1)
    assert executor.submitted == [fresh_pid], '队列中的 pid 不得重复提交'
    assert _read(fresh_pid)[0] == 'done', '未排队的那个应当被正常下载'
    assert is_queued_download(queued_pid), '被跳过的那个留在队列里等 worker'


def test_download_illust_noop_when_done(dl_env, clean_db, monkeypatch, caplog):
    """done 的作品再被排进队列：不发请求、不写 start、不删上一次成功的文件。"""
    caplog.set_level(logging.INFO)
    pid = 80504
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 2))
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: _FakeSession())
    _enqueue(pid)
    background._download_illust(pid)          # 第一次：正常下完
    status, paths, _ = _read(pid)
    assert status == 'done' and paths and all(os.path.isfile(p) for p in paths)

    # 第二次：让下载必然失败 —— 若守卫失效，失败路径会把这些文件删掉
    failing = _FakeSession(fail_on=_urls(pid, 2)[0])
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: failing)
    _enqueue(pid)
    background._download_illust(pid)

    assert failing.calls == [], 'done 之后不得再发起任何下载请求'
    status_after, paths_after, actions = _read(pid)
    assert status_after == 'done'
    assert paths_after == paths
    assert all(os.path.isfile(p) for p in paths_after), '重复下载不得删掉已成功的文件'
    assert actions.count('start') == 1, '守卫必须在写 start 日志之前返回'
    assert any('已是 done 状态' in r.getMessage() for r in caplog.records)
    _assert_no_dangling_state(pid)


def test_redownload_after_delete_still_works(dl_env, clean_db, monkeypatch):
    """回归：删除图库文件把状态复位成 None 后，重下必须照常可用（守卫不许挡住）。"""
    import helpers
    pid = 80505
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1))
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: _FakeSession())
    _enqueue(pid)
    background._download_illust(pid)
    status, paths, _ = _read(pid)
    assert status == 'done' and paths and all(os.path.isfile(p) for p in paths)

    with get_session() as db:
        row = db.query(Illust).filter(Illust.pixiv_id == pid).one()
        assert helpers._delete_illust_files(row) == 1
        safe_commit(db)
    assert _read(pid)[0] is None, '删除文件后状态应复位为 None'
    assert not any(os.path.isfile(p) for p in paths)

    fresh = _FakeSession()
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: fresh)
    _enqueue(pid)
    background._download_illust(pid)

    assert fresh.calls, '删除后的重下必须真正发起请求'
    status_after, paths_after, actions = _read(pid)
    assert status_after == 'done'
    assert paths_after and all(os.path.isfile(p) for p in paths_after)
    assert actions.count('start') == 2
    _assert_no_dangling_state(pid)


class _CountingExecutor:
    """只记录 submit、不执行任务：用来稳定制造"已入队但还没开始"的窗口。"""

    def __init__(self):
        self.submitted: list[int] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append(args[0] if args else None)
        return None


def test_trigger_download_duplicate_submit_is_atomic(dl_env, clean_db, monkeypatch, app):
    """并发重复触发同一作品：恰好一个任务被提交（判定与入队同锁原子完成）。"""
    pids = [80510 + i for i in range(8)]
    with get_session() as db:
        for pid in pids:
            _make_illust(db, pid, _urls(pid, 1))
    executor = _CountingExecutor()
    monkeypatch.setattr(routes_download, 'download_executor', executor)

    results: list[tuple[int, str]] = []
    errors: list[BaseException] = []
    threads_per_round = 6
    barrier = threading.Barrier(threads_per_round)
    lock = threading.Lock()

    def _hit(pid: int):
        client = app.test_client()
        token = client.get('/csrf-token').get_json()['token']
        try:
            barrier.wait(10)
            resp = client.post(f'/download/{pid}', headers={'X-CSRF-Token': token})
            with lock:
                results.append((pid, resp.get_json().get('status')))
        except BaseException as e:      # noqa: BLE001 —— 汇总后统一断言
            with lock:
                errors.append(e)

    for pid in pids:
        threads = [threading.Thread(target=_hit, args=(pid,), daemon=True)
                   for _ in range(threads_per_round)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert not any(t.is_alive() for t in threads), '并发触发出现死等'

    assert not errors, f'并发触发出错：{errors!r}'
    # 每个 pid 恰好一个 accepted，其余全是 queued
    for pid in pids:
        statuses = [s for p, s in results if p == pid]
        assert sorted(statuses) == ['accepted'] + ['queued'] * (threads_per_round - 1), statuses
        assert executor.submitted.count(pid) == 1, f'#{pid} 被重复提交：{executor.submitted}'


def test_trigger_download_submit_failure_does_not_leave_phantom_queue(dl_env, clean_db, monkeypatch, client):
    """提交任务失败（线程池已关闭）：必须把 pid 撤出队列，不能留下幻影排队。"""
    pid = 80520
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1))

    class _DeadExecutor:
        def submit(self, fn, *args, **kwargs):
            raise RuntimeError('cannot schedule new futures after shutdown')

    monkeypatch.setattr(routes_download, 'download_executor', _DeadExecutor())

    with pytest.raises(RuntimeError):
        _post_download(client, pid)

    assert not is_queued_download(pid), '提交失败后不得把 pid 留在队列里'
    assert _read(pid)[0] is None


def test_batch_download_submit_failure_does_not_leave_phantom_queue(dl_env, clean_db, monkeypatch, client):
    """批量下载同一失败语义：崩在哪个 pid 上就撤哪个，前面的已提交不受影响。"""
    ok_pid, dead_pid = 80521, 80522
    with get_session() as db:
        _make_illust(db, ok_pid, _urls(ok_pid, 1))
        _make_illust(db, dead_pid, _urls(dead_pid, 1))

    class _HalfDeadExecutor:
        def __init__(self):
            self.submitted: list[int] = []

        def submit(self, fn, *args, **kwargs):
            pid = args[0] if args else None
            if pid == dead_pid:
                raise RuntimeError('cannot schedule new futures after shutdown')
            self.submitted.append(pid)

    executor = _HalfDeadExecutor()
    monkeypatch.setattr(routes_download, 'download_executor', executor)

    token = client.get('/csrf-token').get_json()['token']
    with pytest.raises(RuntimeError):
        client.post('/api/download/batch', json={'ids': [ok_pid, dead_pid]},
                    headers={'X-CSRF-Token': token})

    assert executor.submitted == [ok_pid]
    assert is_queued_download(ok_pid), '已提交的任务必须留在队列里'
    assert not is_queued_download(dead_pid), '提交失败的 pid 不得留在队列里'


# ── 地址校验与凭据分级（审计 S7a）──

@pytest.mark.parametrize('pid,url', [
    (81001, 'http://i.pximg.net/img-original/img/x/1_p0.jpg'),   # 非 https（明文）
    (81002, 'http://169.254.169.254/latest/meta-data/'),         # 云元数据端点
    (81003, 'https://127.0.0.1/x.jpg'),                          # loopback
    (81004, 'https://10.1.2.3/x.jpg'),                           # 私网
    (81005, 'https://[::1]/x.jpg'),                              # IPv6 loopback
    (81006, 'https://user:pw@i.pximg.net/x.jpg'),                # userinfo
    (81007, 'https://i.pximg.net:8080/x.jpg'),                   # 非 443 端口
    (81008, 'https://localhost/x.jpg'),                          # 本机主机名
    (81009, 'https://db.internal/x.jpg'),                        # 内网主机名
])
def test_download_rejects_unsafe_url_without_request(dl_env, monkeypatch, pid, url):
    """硬性不合法地址：一个请求都不发，直接判失败并留痕。"""
    with get_session() as db:
        _make_illust(db, pid, [url])
    fake = _FakeSession()
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: fake)

    background._download_illust(pid)

    status, paths, actions = _read(pid)
    assert status == 'failed'
    assert paths is None
    assert fake.calls == [], '非法地址绝不能发起请求'
    with get_session() as db:
        log = db.query(DownloadLog).filter(DownloadLog.pixiv_id == pid,
                                          DownloadLog.action == 'failed').first()
        assert log is not None and '非法图片地址' in (log.message or '')
    assert not os.path.isdir(os.path.join(str(dl_env), str(pid))), '失败后不留半成品目录'
    _assert_no_dangling_state(pid)


def test_download_does_not_follow_redirect(dl_env, monkeypatch):
    """图片地址发生重定向 → 判定失败，且请求必须显式禁止跟随。"""
    pid = 81020
    url = 'https://i.pximg.net/img-original/img/x/redirect_p0.jpg'
    with get_session() as db:
        _make_illust(db, pid, [url])
    fake = _FakeSession(redirect_on=url,
                        redirect_to='https://169.254.169.254/x.jpg')
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: fake)

    background._download_illust(pid)

    status, paths, _ = _read(pid)
    assert status == 'failed'
    assert paths is None
    assert fake.kwargs and fake.kwargs[0]['allow_redirects'] is False, '不能跟随重定向'
    assert not os.path.isdir(os.path.join(str(dl_env), str(pid)))
    _assert_no_dangling_state(pid)


def test_download_uses_cookie_session_for_allowlisted_host(dl_env, monkeypatch):
    """白名单内主机（Pixiv 官方图床）继续走带凭据会话。"""
    pid = 81030
    urls = _urls(pid, 1)
    with get_session() as db:
        _make_illust(db, pid, urls)
    cookie_fake = _FakeSession()
    anon_fake = _FakeSession()
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: cookie_fake)
    monkeypatch.setattr(background, 'build_credentialless_session', lambda: anon_fake)

    background._download_illust(pid)

    assert _read(pid)[0] == 'done'
    assert cookie_fake.calls == urls
    assert anon_fake.calls == [], '白名单内不该动用无凭据会话'


def test_download_uses_credentialless_session_outside_allowlist(dl_env, monkeypatch):
    """白名单外的公网 https 主机：下载不中断，但绝不携带凭据（S7a+S7b 共用判定）。"""
    pid = 81031
    url = 'https://img-cdn.example.net/original/p0.jpg'
    with get_session() as db:
        _make_illust(db, pid, [url])
    cookie_fake = _FakeSession()
    anon_fake = _FakeSession()
    anon_fake.headers = {}          # 无凭据会话的替身：没有会话级 Cookie 头
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: cookie_fake)
    monkeypatch.setattr(background, 'build_credentialless_session', lambda: anon_fake)

    background._download_illust(pid)

    status, paths, _ = _read(pid)
    assert status == 'done', '换图床域名不该让下载中断'
    assert paths and os.path.isfile(paths[0])
    assert anon_fake.calls == [url], '白名单外主机必须走无凭据会话'
    assert cookie_fake.calls == [], '带凭据会话不得访问白名单外主机'
    assert 'Cookie' not in anon_fake.headers
    assert anon_fake.closed and cookie_fake.closed, '两个 session 都要在 finally 里关闭'
    _assert_no_dangling_state(pid)


# ── 中途取消（审计 S18 补齐）──

def test_download_cancelled_mid_flight_cleans_up_without_done(dl_env, monkeypatch):
    """下载途中被取消：删掉半成品文件、状态复位、记 cancelled 且**不得**固化 done。

    取消失败的代价很具体：状态留在 downloading（trigger 被"下载中"挡回）、磁盘留着
    半套文件（重新下载时页号错位）、or 状态 done 而文件不全（点开 404）。
    """
    pid = 81101
    urls = _urls(pid, 2)
    with get_session() as db:
        _make_illust(db, pid, urls)
    fake = _FakeSession()
    original_get = fake.get

    def _cancel_after_first_page(url, **kwargs):
        """第 1 页下完后（第 2 页取图时）用户点了取消。"""
        if url == urls[1]:
            download_cancellations.add(pid)
        return original_get(url, **kwargs)

    fake.get = _cancel_after_first_page
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: fake)

    background._download_illust(pid)

    status, paths, actions = _read(pid)
    assert status is None, '取消后必须复位成未下载，否则重下会被状态挡回'
    assert paths is None
    assert 'done' not in actions
    assert 'cancelled' in actions
    assert not os.path.isdir(os.path.join(str(dl_env), str(pid))), '取消后不留半成品目录'
    _assert_no_dangling_state(pid)


def test_download_cancelled_before_start_does_nothing(dl_env, monkeypatch):
    """取消发生在 worker 启动之前（queued 场景）：一行日志都不该写、不碰网络。

    这是"排队中取消"这一最常见取消姿势的回归守卫。旧实现把 `session_obj = None`
    放在取消检查**之后**，这条提前 return 让 finally 首行抛 UnboundLocalError，
    于是 lock.release() / 取消标记与进度清理全部跳过 —— 该作品的下载锁永远不放、
    取消标记永远留着，**再也下载不了**（worker 在 lock.acquire 处静默跳过）。
    """
    pid = 81102
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1))
    fake = _FakeSession()
    monkeypatch.setattr(background, 'build_pixiv_session', lambda: fake)
    download_cancellations.add(pid)        # 任务已入队但还没轮到执行
    _enqueue(pid)

    background._download_illust(pid)

    status, paths, actions = _read(pid)
    assert status is None, '取消标记在开头就该拦住整个下载'
    assert paths is None
    assert actions == [], '没有开始过就不该有 start/done 日志'
    assert fake.calls == [], '取消后不得发起任何取图请求'
    _assert_no_dangling_state(pid)

    # 症状级守卫：取消之后这个作品必须还能正常下载（锁泄漏会让它永久下不动）
    background._download_illust(pid)
    status, paths, actions = _read(pid)
    assert status == 'done' and paths, '取消过的作品必须能重新下载'
    assert fake.calls == _urls(pid, 1)


def test_cancel_route_marks_downloading_and_returns_cancelling(dl_env, clean_db, client):
    """`POST /download/cancel/<pid>`：下载中 → cancelling + 取消标记 + 退出队列。"""
    pid = 81103
    with get_session() as db:
        _make_illust(db, pid, _urls(pid, 1), status='downloading')
    _enqueue(pid)

    resp = _post(client, f'/download/cancel/{pid}')

    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'cancelling'
    assert pid in download_cancellations, 'worker 靠这个标记在下一个检查点退出'
    assert not is_queued_download(pid), '取消必须把它移出队列'


def test_cancel_route_rejects_idle_and_missing_illust(dl_env, clean_db, client):
    """未在下载中 → 400；作品不存在 → 404（前端据此区分"不用管"和"刷新列表"）。"""
    idle_pid = 81104
    with get_session() as db:
        _make_illust(db, idle_pid, _urls(idle_pid, 1), status='done')

    idle = _post(client, f'/download/cancel/{idle_pid}')
    missing = _post(client, '/download/cancel/81199')

    assert idle.status_code == 400
    assert '未在下载中' in idle.get_json()['error']
    assert idle_pid not in download_cancellations, '拒绝的取消不得留下标记'
    assert missing.status_code == 404


def test_download_status_routes(dl_env, clean_db, client):
    """单条状态、批量状态、下载管理页三个只读入口的正常与异常分支。"""
    done_pid, failed_pid = 81105, 81106
    with get_session() as db:
        _make_illust(db, done_pid, _urls(done_pid, 1), status='done')
        _make_illust(db, failed_pid, _urls(failed_pid, 1), status='failed')

    single = client.get(f'/download_status/{done_pid}')
    assert single.status_code == 200
    assert single.get_json()['status'] == 'done'
    assert client.get('/download_status/81198').status_code == 404

    batch = client.get(f'/api/download/status/batch?ids={done_pid},{failed_pid},81197')
    assert batch.status_code == 200
    statuses = batch.get_json()['statuses']
    assert statuses[str(done_pid)] == 'done'
    assert statuses[str(failed_pid)] == 'failed'
    assert statuses['81197'] == 'none', '库里没有的 pid 要给出 none 而不是缺失键'

    assert client.get('/api/download/status/batch').status_code == 400
    assert client.get('/api/download/status/batch?ids=abc').status_code == 400

    page = client.get('/downloads')
    assert page.status_code == 200
    assert 'text/html' in page.headers['Content-Type']


def test_api_downloads_reports_queue_and_progress(dl_env, clean_db, client):
    """`/api/downloads` 聚合：活动/排队/完成/日志四段都必须有数据（下载管理页靠它）。"""
    active_pid, done_pid = 81107, 81108
    with get_session() as db:
        _make_illust(db, active_pid, _urls(active_pid, 2), status='downloading')
        _make_illust(db, done_pid, _urls(done_pid, 1), status='done')
        db.add(DownloadLog(pixiv_id=done_pid, action='done', message='下载完成'))
        safe_commit(db)
    _enqueue(active_pid)
    _download_progress[active_pid] = {'current': 1, 'total': 2}

    resp = client.get('/api/downloads')

    assert resp.status_code == 200
    body = resp.get_json()
    assert [i['pixiv_id'] for i in body['active']] == [active_pid]
    assert body['active'][0]['progress'] == {'current': 1, 'total': 2}, \
        '进度取自进程内 _download_progress'
    assert [i['pixiv_id'] for i in body['queued']] == [active_pid]
    assert [i['pixiv_id'] for i in body['completed']] == [done_pid]
    assert any(log['pixiv_id'] == done_pid for log in body['logs'])


def _post(client, path, payload=None):
    token = client.get('/csrf-token').get_json()['token']
    return client.post(path, json=payload or {}, headers={'X-CSRF-Token': token})
