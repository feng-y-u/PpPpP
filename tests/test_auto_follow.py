"""自动关注后台线程（`background._auto_follow_worker`）。

审计 S20 遗留：`last_check` / `last_count` 只在**成功拉到关注列表并处理完一轮**时更新，
所以"很久没更新"既可能是"本来没有新作品"、也可能是"每轮都在失败" —— 界面上分不开。
S12 给预取做过同样的事（`_prefetch_state['last_error']`，"整轮干净收尾才清空"，
所以非空就代表最近一轮有问题），这里把自动关注那一半补齐。

补这一半之前，`_auto_follow_worker` 在本仓库**一个用例都没有**（只在真实运行里被间接
覆盖）：它的重试/留痕/恢复路径没有任何守卫。本文件补上，并刻意保留两条"不报错"的
边界（拉不到作品不算错、也不清旧错误），免得将来有人顺手把它写成假告警。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

import background
import models
import runtime

#: `/api/auto-follow/status` 的响应键集合（含 `alive`，由路由补上）。
#: 新增字段必须写进 `runtime._auto_follow_state` 的初始字面量 —— 该 dict 的并发约定是
#: "键集合固定、只改值"，懒加键会让 `jsonify` 遍历时抛
#: `RuntimeError: dictionary changed size during iteration`。
STATUS_KEYS = {'last_check', 'last_count', 'interval', 'auto_download', 'alive', 'last_error'}


def _item(pid: int) -> dict:
    """`fetch_following` 返回的单条作品。

    字段形状**照真实来源**写：`fetch_following` → `_process_items` → `Illust.to_dict()`，
    所以 `upload_date` 是 `isoformat()` **字符串**（不是 datetime 对象），`tags` /
    `original_urls` 是 list。注意"新作品入库"那条路径在当前实现下会因此踩到另一个
    既有缺陷（见下），本文件刻意只走"该 pid 已存在"的收尾路径，避免把两个问题混在
    一起 —— 那条缺陷已单独报告，不在本步范围内。

    刻意**不带** `original_urls`：`auto_download` 打开时 worker 会拿它去排队下载，
    用例里绝不希望真的发起下载。
    """
    return {
        'pixiv_id': pid,
        'title': f'作品 {pid}',
        'user_id': 100,
        'user_name': '画师',
        'page_count': 1,
        'bookmark_count': 5,
        'thumb_url': f'https://i.pximg.net/{pid}.jpg',
        'upload_date': '2026-09-11T00:00:00+00:00',
        'tags': [],
        'original_urls': [],
    }


def _seed_existing(pid: int) -> None:
    """预置一条已入库的作品，让 worker 那一轮走"没有新作品"的干净收尾路径。"""
    with models.get_session() as db:
        db.add(models.Illust(
            pixiv_id=pid, title=f'作品 {pid}', user_id=100, user_name='画师',
            page_count=1, bookmark_count=5, thumb_url=f'https://i.pximg.net/{pid}.jpg',
            upload_date=datetime(2026, 9, 11, tzinfo=timezone.utc),
        ))
        models.safe_commit(db)


def _wait_for(predicate, timeout: float = 5.0, message: str = '') -> bool:
    """轮询等待（后台线程没有 join 点，只能等状态收敛）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.fixture
def worker(monkeypatch):
    """启动一个**受控**的自动关注线程。

    - 自己一份 stop event（`monkeypatch` 换掉模块全局）：收尾时只停本用例起的线程，
      不碰 app 级别那个（`app.py` import 时就启动了，conftest 把它设成 interval=0）。
    - interval 设成 0.05s 让用例秒级跑完；`auto_download` 关掉，绝不真下载。
    - 两个值都在收尾还原（它是进程级共享状态）。
    """
    stop = threading.Event()
    monkeypatch.setattr(background, '_auto_follow_stop', stop)
    original_interval = runtime._auto_follow_state['interval']
    original_auto_download = runtime._auto_follow_state['auto_download']
    runtime._auto_follow_state['interval'] = 0.05
    runtime._auto_follow_state['auto_download'] = False
    started: list[threading.Thread] = []

    def start() -> threading.Thread:
        thread = threading.Thread(target=background._auto_follow_worker, daemon=True)
        thread.start()
        started.append(thread)
        return thread

    yield start

    runtime._auto_follow_state['interval'] = original_interval
    runtime._auto_follow_state['auto_download'] = original_auto_download
    stop.set()
    for thread in started:
        thread.join(timeout=5)


def test_clean_round_updates_last_check_and_clears_error(clean_db, monkeypatch, worker):
    """正常路径：处理完一轮 → 更新 `last_check` / `last_count` 并清掉上一轮的 `last_error`。

    走"该 pid 已入库"这条真实且最常见的收尾路径（关注列表里绝大多数是已知作品），
    因此这一轮 `new_illusts` 为空、不触发写入。
    """
    _seed_existing(900001)
    monkeypatch.setattr(background, 'fetch_following', lambda page=1: ([_item(900001)], False))
    monkeypatch.setitem(runtime._auto_follow_state, 'last_check', None)
    monkeypatch.setitem(runtime._auto_follow_state, 'last_error', '上一轮留下的错误')

    worker()

    assert _wait_for(lambda: runtime._auto_follow_state['last_check']), '成功一轮必须更新 last_check'
    assert runtime._auto_follow_state['last_count'] == 0, '这一轮没有新作品'
    assert runtime._auto_follow_state['last_error'] is None, '干净收尾要清空，否则"非空=最近一轮有问题"不成立'
    with models.get_session() as db:
        assert db.query(models.Illust).filter(models.Illust.pixiv_id == 900001).count() == 1, \
            '那一轮应当完整跑完（含入库前的查重），而不是中途异常'


def test_failure_records_error_and_next_good_round_clears_it(clean_db, monkeypatch, worker):
    """失败路径：出错留痕、线程不退出、`last_check` 语义不变；恢复后清空。"""
    _seed_existing(900002)
    mode = {'fail': True}

    def flaky_fetch(page=1):
        if mode['fail']:
            raise RuntimeError('模拟网络挂了')
        return ([_item(900002)], False)

    monkeypatch.setattr(background, 'fetch_following', flaky_fetch)
    monkeypatch.setitem(runtime._auto_follow_state, 'last_check', None)
    monkeypatch.setitem(runtime._auto_follow_state, 'last_error', None)

    worker()

    assert _wait_for(lambda: runtime._auto_follow_state['last_error']), \
        '出错必须留痕，否则界面上只剩"last_check 很旧"这种含糊信号'
    assert '模拟网络挂了' in runtime._auto_follow_state['last_error']
    assert runtime._auto_follow_state['last_check'] is None, \
        'last_check 只在成功处理完一轮时更新（S20 的界面文案"上次成功检查"依赖这个语义）'

    # 下一轮恢复：证明线程没被异常打死，且错误被清掉
    mode['fail'] = False
    assert _wait_for(lambda: runtime._auto_follow_state['last_check']), '线程必须能在下一轮恢复'
    assert runtime._auto_follow_state['last_error'] is None, '恢复后不得继续挂着旧错误'


def test_empty_following_list_is_not_an_error(clean_db, monkeypatch, worker):
    """拉不到任何作品**不算**出错，也不清旧错误。

    这里刻意反着来（不加告警）：Cookie 失效时 Pixiv 也是**静默返回空结果**，所以
    "空列表"没法区分"没关注/没新作品"和"认证失效"。凭空写 `last_error` 会变成假告警，
    而清掉旧错误会抹掉真的证据 —— 两种都不做，含糊留给界面文案去说清。
    """
    calls: list[int] = []

    def empty_fetch(page=1):
        calls.append(page)
        return ([], False)

    monkeypatch.setattr(background, 'fetch_following', empty_fetch)
    monkeypatch.setitem(runtime._auto_follow_state, 'last_error', '更早那轮的错误')
    monkeypatch.setitem(runtime._auto_follow_state, 'last_check', None)

    worker()

    assert _wait_for(lambda: len(calls) >= 3), '至少要跑过几轮才说明问题'
    assert runtime._auto_follow_state['last_error'] == '更早那轮的错误', \
        '空列表既不能凭空报错，也不能把原有错误抹掉'
    assert runtime._auto_follow_state['last_check'] is None


def test_status_key_set_is_stable_while_worker_writes(client, clean_db, monkeypatch, worker):
    """竞态路径：线程在写 state 的同时反复读接口，键集合必须恒定且不抛。

    这是 `_auto_follow_state` 无锁读写的**前提条件**（见 AGENTS.md「并发」）：
    键集合固定、只改值，`jsonify` 遍历时才不会撞 size change。
    """
    _seed_existing(900003)
    mode = {'fail': True}

    def flaky_fetch(page=1):
        if mode['fail']:
            raise RuntimeError('boom')
        return ([_item(900003)], False)

    monkeypatch.setattr(background, 'fetch_following', flaky_fetch)
    monkeypatch.setitem(runtime._auto_follow_state, 'last_error', None)

    worker()

    seen_shapes = set()
    for i in range(80):
        resp = client.get('/api/auto-follow/status')
        assert resp.status_code == 200, f'第 {i} 次读接口失败：{resp.get_data(as_text=True)[:200]}'
        data = resp.get_json()
        seen_shapes.add(frozenset(data))
        mode['fail'] = not mode['fail']          # 让线程在两态之间来回切
        assert data['last_error'] is None or 'boom' in data['last_error']
        time.sleep(0.005)

    assert len(seen_shapes) == 1, f'响应键集合必须恒定（懒加键会让并发遍历崩）：{seen_shapes}'
    assert STATUS_KEYS <= set(next(iter(seen_shapes))), 'S20/S21 的字段一个都不能少'
