"""详情请求熔断闸（`pixiv_client._DetailRequestGate`）的离线测试。

为什么单独一个文件：闸的几条规则（在途并发上限、连续不同作品 403、429 冷却与半开
探测）全是"时间 + 并发 + 状态"的组合，用真实 sleep 或真实网络既测不出来也测不稳。
所以这里注入假时钟推进冷却（不做真实的 60 秒等待），只看公开行为
（`request_slot` / `observe_response` / `is_open`）。

本文件不发任何网络请求、不读 `cookies.txt`：闸尚未接入 `fetch_illust_detail`
（接入由后续任务完成），这里没有需要临时 Cookie 文件的调用路径。

**为什么每个 `request_slot` 调用都要绕一层工作线程**：`request_slot` 内部的
`Semaphore.acquire()` **没有超时**，槽位一旦泄漏（例如 `finally` 里的 `release()`
被改坏），主线程上的 `with gate.request_slot(...)` 会**永久阻塞** —— 测试进程再也
走不到断言，CI 只能靠超时 kill 收场，于是"红灯"变成"卡住"。把调用放进 daemon 工作
线程再 `join(timeout=...)`，泄漏就表现为一条快速失败的断言（见 `_run_bounded`）。
"""

from __future__ import annotations

import math
import sys
import threading
from email.utils import formatdate

import pytest

import pixiv_client

# 槽位泄漏时工作线程会永久阻塞在 `acquire()` 上；这个超时就是把"挂住"变成"断言失败"
# 的关键。取 2 秒而不是并发用例里的 5 秒：这里的线程只做纯内存操作（占槽位 /
# observe_response，没有 I/O），2 秒已是极宽松的上限；留短一点是为了泄漏时整套用例
# 迅速报红，而不是每个用例都白等满 5 秒（泄漏会同时命中十几处调用点）。
_SLOT_JOIN_TIMEOUT = 2.0

# 竞态用例里 `Barrier` 的等待上限：必须显著小于 `_SLOT_JOIN_TIMEOUT`，这样"名额预留被
# 改坏"时线程在一秒内自行散场（屏障超时 → BrokenBarrierError 被带回主线程），报出的是
# 用例自己的断言，而不是外层的"工作线程未在超时内结束"。正常路径上它只是保险丝：
# 所有线程都在做纯内存操作，真正需要的等待是毫秒级。
_RACE_BARRIER_TIMEOUT = 1.0


class FakeClock:
    """可注入的假时钟：闸的冷却全靠时间推进，测试不能真睡 60 秒。"""

    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _run_bounded(work, what='调用 request_slot'):
    """在 daemon 工作线程里执行 `work`，有界 join，并把线程里的异常带回调用线程。

    为什么必须绕这一层：`request_slot` 的 `Semaphore.acquire()` 没有超时，槽位泄漏后
    调用方永远等不到槽位、也就永远走不到断言 —— 测试进程只能被 CI kill。放进 daemon
    线程后主线程能用 `join(timeout=...)` 收回控制权，把"泄漏"报成断言失败（`what`
    标明是哪次调用卡住了）；`daemon=True` 是第二道保险，保证即使断言也没跑到，这些
    线程不会拖住进程退出。

    线程里捕获的异常**在调用线程重新抛出**，所以调用方的 `pytest.raises(...)` 照常
    生效（否则线程里的异常会静默变成"通过"）。
    """
    failures: list[BaseException] = []

    def runner():
        try:
            work()
        except BaseException as exc:  # 线程里的异常必须带回主线程，否则用例会假通过
            failures.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout=_SLOT_JOIN_TIMEOUT)
    assert not thread.is_alive(), f'工作线程未在超时内结束（疑似槽位泄漏）：{what}'
    if failures:
        raise failures[0]


def _run_with_slot(gate, pixiv_id, body=None):
    """在工作线程里执行 `with gate.request_slot(pixiv_id): body()`。

    把整个 context block 一起搬进线程：`__enter__`（占槽位）与 `body` 必须在同一个
    线程、同一次持槽里完成，否则测的就不是真实调用顺序了。理由见 `_run_bounded`。
    """

    def work():
        with gate.request_slot(pixiv_id):
            if body is not None:
                body()

    _run_bounded(work, f'占用槽位并执行 body（pid={pixiv_id}）')


def _assert_slot_admitted(gate, pixiv_id):
    """确认闸当前放行：占一个槽位再立刻释放（同样在工作线程里，见 `_run_bounded`）。"""
    _run_with_slot(gate, pixiv_id)


def _assert_slot_refused(gate, pixiv_id, message):
    """确认闸在**发出请求之前**拒绝放行；调用方仍用 `pytest.raises` 包裹本函数。

    线程里抛出的 `PixivRateLimitedError` 被 `_run_bounded` 原样带回调用线程，所以
    `pytest.raises(pixiv_client.PixivRateLimitedError)` 依旧捕获得到；闸若错误放行，
    `pytest.fail(message)` 抛出的 `Failed` 也会被带回并让用例失败。
    """

    def work():
        with gate.request_slot(pixiv_id):
            pytest.fail(message)

    _run_bounded(work, f'被拒绝的槽位申请（pid={pixiv_id}）')


def _observe(gate, pixiv_id, status, retry_after=None):
    """按真实调用顺序走一遍：先占槽位，再上报响应（两者都在闸的 context manager 内）。"""
    _run_with_slot(
        gate, pixiv_id, lambda: gate.observe_response(pixiv_id, status, retry_after)
    )


def _open_via_three_distinct_403(gate, pids=(101, 102, 103)):
    for pid in pids:
        _observe(gate, pid, 403)


def test_three_distinct_403_open_gate():
    """3 个不同作品的 403 打开熔断闸；之后的下一个请求在发出去之前就被拒。"""
    clock = FakeClock()
    gate = pixiv_client._DetailRequestGate(clock=clock)

    _open_via_three_distinct_403(gate)

    assert gate.is_open
    with pytest.raises(pixiv_client.PixivRateLimitedError):
        _assert_slot_refused(gate, 104, '熔断期间不应发出详情请求')


class TestDetailRequestGate:
    def test_single_pid_repeated_403_does_not_open_gate(self):
        """单个作品反复 403 不计作三个不同作品：R18/权限类 403 只影响那一个作品。"""
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)

        for _ in range(5):
            _observe(gate, 101, 403)
            clock.advance(5)

        assert not gate.is_open
        _assert_slot_admitted(gate, 102)

    def test_403_while_already_open_does_not_extend_cooldown(self):
        """开路期间在途请求陆续返回 403：不叠加计数、也不重新计时延长冷却。

        为什么这条分支是承重的：开路前发出的请求会在闸已打开之后才回来（详情单次
        1.3s 量级）。若这些 403 继续喂 `_recent_403`，3 条就能在冷却期内把
        `_opened_at` 重新置为现在，把已开的闸再延长一整个冷却 —— 只要风控期间还有
        在途请求，闸就永远续期。下面的第二次断言（`opened_at + 60` 必须放行探测）
        才是判别点：只断言"59 秒时仍拒绝"的话，被延长到 90 秒的实现同样会通过。
        """
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)
        _open_via_three_distinct_403(gate)      # opened_at = 100，冷却 60 秒
        assert gate.is_open

        clock.advance(30)
        # 这 3 条 403 属于开路前就已发出的在途请求（并列的 429 用例同样直接上报，
        # 不占新槽位）：它们不构成"新的一轮连续 403"。
        for pid in (301, 302, 303):
            gate.observe_response(pid, 403)

        clock.advance(29)                       # opened_at + 59
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 304, '冷却未到期不应发出详情请求')

        clock.advance(1)                        # opened_at + 60：原冷却到期
        _assert_slot_admitted(gate, 304)

    def test_403_outside_the_sixty_second_window_does_not_count(self):
        """窗口是滚动的 60 秒：滑出窗口的旧 403 不能和新 403 凑成三个。"""
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)

        _observe(gate, 101, 403)
        clock.advance(61)          # 第一条 403 已滑出窗口
        _observe(gate, 102, 403)
        _observe(gate, 103, 403)
        assert not gate.is_open

        _observe(gate, 104, 403)   # 窗口内第 3 个不同作品
        assert gate.is_open

    @pytest.mark.parametrize('status', [200, 304, 401, 404, 500, 503])
    def test_non_403_response_clears_consecutive_403_set(self, status):
        """任一非 403 的 HTTP 响应都证明上游没在限流我们，连续集合清零。

        401/404/5xx 的**原错误分类**由调用方判定，本闸只观察状态码、不改分类。
        """
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)

        _observe(gate, 101, 403)
        _observe(gate, 102, 403)
        _observe(gate, 103, status)
        _observe(gate, 104, 403)
        _observe(gate, 105, 403)

        assert not gate.is_open, '清零后只攒到 2 个不同作品的 403'

    def test_at_most_two_requests_in_flight(self):
        """在途上限 2：第三个请求要等槽位释放，不能在途叠加（并发 3 实测触发 403）。"""
        gate = pixiv_client._DetailRequestGate(clock=FakeClock())
        both_holding = threading.Barrier(3, timeout=5)
        release = threading.Event()
        failures: list[BaseException] = []

        def hold(pid):
            try:
                with gate.request_slot(pid):
                    both_holding.wait()   # 主线程也在等 → 汇合即两个槽位确已在手
                    release.wait(timeout=5)
            except BaseException as exc:  # 线程里的异常要带回主线程，否则静默变成"通过"
                failures.append(exc)

        # daemon=True：槽位若泄漏，这些线程会一直阻塞在 acquire 上；非 daemon 会把
        # 整个 pytest 进程挂死（CI 只能靠 kill），daemon 至少让断言先失败退出。
        holders = [threading.Thread(target=hold, args=(pid,), daemon=True) for pid in (1, 2)]
        for t in holders:
            t.start()
        both_holding.wait()

        third_entered = threading.Event()

        def third():
            try:
                with gate.request_slot(3):
                    third_entered.set()
            except BaseException as exc:
                failures.append(exc)

        t3 = threading.Thread(target=third, daemon=True)
        t3.start()
        assert not third_entered.wait(0.3), '槽位未释放前第三个请求不该进入'
        release.set()
        assert third_entered.wait(5), '槽位释放后第三个请求应能进入（排队而不是被拒绝）'

        # 有界 join + 显式断言线程已结束：槽位泄漏时这里失败而不是无限期挂住测试进程。
        for t in holders + [t3]:
            t.join(timeout=5)
            assert not t.is_alive(), '工作线程未在超时内结束（疑似槽位泄漏）'
        assert not failures

    def test_slot_is_released_when_body_raises(self):
        """`finally` 必须释放槽位：泄漏后所有详情请求都会永久卡在等槽位上。"""
        gate = pixiv_client._DetailRequestGate(clock=FakeClock())

        def failing_body():
            raise RuntimeError('模拟 HTTP 中途失败')

        for _ in range(2):              # 上限就是 2，两次异常退出后槽位应已归还
            with pytest.raises(RuntimeError):
                # body 抛出的 RuntimeError 由工作线程带回本线程，pytest.raises 照常生效
                _run_with_slot(gate, 1, failing_body)

        entered = threading.Event()

        def acquire_again():
            with gate.request_slot(2):
                entered.set()

        worker = threading.Thread(target=acquire_again, daemon=True)
        worker.start()
        assert entered.wait(2), 'body 抛异常后槽位必须已被 finally 释放'
        worker.join(timeout=5)
        assert not worker.is_alive(), '工作线程未在超时内结束（疑似槽位泄漏）'

    @pytest.mark.parametrize('retry_after', ['7', 7])
    def test_429_retry_after_delta_seconds_sets_cooldown(self, retry_after):
        """429 立即开路，冷却以 Retry-After 的 delta-seconds 为准（字符串或数字都要认）。"""
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)

        _observe(gate, 101, 429, retry_after=retry_after)
        assert gate.is_open

        clock.advance(6)
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 102, '冷却未到期不应发出详情请求')

        clock.advance(1)               # 满 7 秒
        _assert_slot_admitted(gate, 102)

    def test_429_while_already_open_refreshes_cooldown(self):
        """开路期间另一条在途请求也吃到 429：按最新一次 429 重新计时，冷却不缩短。"""
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)
        _observe(gate, 101, 429, retry_after='60')

        clock.advance(50)
        # 这条请求在开路前就已发出，所以只需上报响应，不再占新槽位
        gate.observe_response(102, 429, retry_after='10')

        clock.advance(59)
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 103, '冷却应以最新一次 429 重新计时，且不被更短的 Retry-After 缩短')

        clock.advance(1)
        _assert_slot_admitted(gate, 103)

    def test_429_retry_after_http_date_sets_cooldown(self):
        """Retry-After 也允许 HTTP-date：按"距该时刻还有几秒"折算冷却。"""
        clock = FakeClock(now=100.0)
        gate = pixiv_client._DetailRequestGate(clock=clock)

        _observe(gate, 101, 429, retry_after=formatdate(130.0, usegmt=True))
        assert gate.is_open

        clock.advance(29)
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 102, 'HTTP-date 折算的冷却未到期')

        clock.advance(1)
        _assert_slot_admitted(gate, 102)

    def test_429_retry_after_naive_http_date_is_read_as_utc(self):
        """个别代理会漏掉 `GMT`：无时区的 HTTP-date 按 UTC 折算，不能被当成机器本地时间。"""
        clock = FakeClock(now=100.0)
        gate = pixiv_client._DetailRequestGate(clock=clock)

        _observe(gate, 101, 429, retry_after='Thu, 01 Jan 1970 00:02:10')
        clock.advance(29)
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 102, '无时区的 HTTP-date 也应按 UTC 折算成 30 秒冷却')

        clock.advance(1)
        _assert_slot_admitted(gate, 102)

    # 最后一个取值是**已经过去**的 HTTP-date（假时钟 now=100）：`seconds > 0` 的守卫
    # 只在这条路径上可观测（`'0'` 那条早就被 `or` 兜成 60 秒了）。
    # 倒数第二、三个是 Unicode 数字：`str.isdigit()` 对它们也为真，但 RFC 7231 的
    # delta-seconds 只允许 ASCII `1*DIGIT`。`'²'` 会让 `float()` 抛 `ValueError`
    # （必须降级成兜底冷却，不能把异常抛给调用方）；`'١٢'` 更阴 —— `float()` 认它并
    # 得到 12 秒，静默把服务器写的值换成了另一个数。
    @pytest.mark.parametrize('retry_after', [None, '', '   ', 'not-a-date', '-5', '0', '7.5',
                                             '\u00b2', '\u0661\u0662',
                                             formatdate(70.0, usegmt=True)])
    def test_429_invalid_retry_after_falls_back_to_60_seconds(self, retry_after):
        """Retry-After 缺失/无效/非正数/已过期时回退 60 秒。

        0 与负数等于"不冷却"，已过去的 HTTP-date 同理，真被限流时会让闸形同虚设，
        所以一律按无效处理。
        """
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)

        _observe(gate, 101, 429, retry_after=retry_after)
        assert gate.is_open

        clock.advance(59)
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 102, '兜底冷却 60 秒，未到期不应发出详情请求')

        clock.advance(1)
        _assert_slot_admitted(gate, 102)

    def test_cooled_down_gate_admits_exactly_one_probe(self):
        """冷却到期只放一个半开探测：全放会立刻回到触发风控的并发。"""
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)
        _open_via_three_distinct_403(gate)
        assert gate.is_open

        clock.advance(60)

        def probe():
            with gate.request_slot(201):       # 唯一一个探测名额
                assert gate.is_open, '半开探测期间闸仍算开路，只是放行了这一个探测'
                with pytest.raises(pixiv_client.PixivRateLimitedError):
                    _assert_slot_refused(gate, 202, '半开期间不得有第二个探测')

                gate.observe_response(201, 200)   # 探测拿到非限流响应 → 风控解除

        # 整个探测（连同里面那次"应被拒绝"的申请）放在工作线程里，理由见 `_run_bounded`
        _run_bounded(probe, '半开探测（pid=201）')
        assert not gate.is_open

        _run_with_slot(gate, 203, lambda: gate.observe_response(203, 200))   # 复位后恢复放行

    def test_cooldown_expiry_race_admits_exactly_one_probe(self):
        """规则 5：并发线程在同一瞬间等到冷却到期，也**只能有一个**拿到探测名额。

        上一条用例是串行调用，证明不了"读→判定→写"整体在一把锁里：只要把"预留名额"
        那三步拆开（判定完就放锁再写 `_probe_in_flight`），两个线程就会各自读到 False
        并双双成为探测。这里用 `threading.Barrier` 让所有线程在冷却到期的那一刻**同时**
        冲进 `request_slot`，直接暴露那个竞态。

        两个关键设计，缺一个用例就会变成"看起来在测并发、其实靠时序兜住"：
        - 赢家**持着槽位**等所有输家都申诉完（`settled` 屏障）才退场。若赢家立刻退出，
          探测名额会被 `finally` 归还，晚到的线程就作为**新探测**被合法放行 —— 于是
          "只放行一个"变成一句运气话，判别力也随之消失。
        - 赢家**不**上报响应。一旦上报，冷却会按当前时刻重新计时，输家就变成被"冷却
          未到期"拒绝，绕开了本用例真正要压的那条分支（已有探测在途）。
        """
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)
        _open_via_three_distinct_403(gate)
        clock.advance(60)                        # 冷却正好到期：所有线程从同一起跑线抢

        racers = 8
        rounds = 4                               # 多轮：单轮撞上那个窗口全靠运气
        start = threading.Barrier(racers, timeout=_RACE_BARRIER_TIMEOUT)
        settled = threading.Barrier(racers, timeout=_RACE_BARRIER_TIMEOUT)
        admitted: list[int] = []
        refused: list[int] = []
        failures: list[BaseException] = []
        tally_lock = threading.Lock()

        def race(pixiv_id):
            try:
                for _ in range(rounds):
                    start.wait()                 # 同步起跑，制造真实竞态而非串行调用
                    try:
                        with gate.request_slot(pixiv_id):
                            with tally_lock:
                                admitted.append(pixiv_id)
                            settled.wait()       # 持槽等输家申诉完，再归还探测名额
                            continue
                    except pixiv_client.PixivRateLimitedError:
                        with tally_lock:
                            refused.append(pixiv_id)
                    settled.wait()
            except BaseException as exc:         # 线程里的异常要带回主线程，否则假通过
                with tally_lock:
                    failures.append(exc)

        def run_race():
            threads = [threading.Thread(target=race, args=(1000 + i,), daemon=True)
                       for i in range(racers)]
            for t in threads:
                t.start()
            # 有界 join：名额预留被改坏时这些线程不会（也不该）挂死，但一旦挂住，
            # 这里失败而不是让整个 pytest 进程被卡住。
            for t in threads:
                t.join(timeout=_SLOT_JOIN_TIMEOUT)
                assert not t.is_alive(), '竞态线程未在超时内结束（疑似卡在 acquire 上）'

        # CPython 的 GIL 会在"判定 → 写入"之间把线程保护得比语义上更好：默认 5ms 的切换
        # 间隔下，每个线程都能一路跑完那几步，窗口撞不上（实测对照组 20 轮 0 次）。把切换
        # 间隔压到微秒级把这个窗口真正暴露出来（同样 8×4 的负载，检出率从 0/20 升到 4/15）；
        # 只影响本用例，收尾还原。
        previous_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            # 用文件里的有界线程助手兜住整轮竞态：回归时失败，而不是把测试进程挂住
            _run_bounded(run_race, '并发抢探测名额')
        finally:
            sys.setswitchinterval(previous_interval)

        assert not failures, failures
        assert len(admitted) == rounds, f'每轮只应放行一个探测，实际放行 {admitted}'
        assert len(refused) == (racers - 1) * rounds, f'其余线程都应被拒绝：{refused}'
        assert gate.is_open, '本轮没有探测判决，闸应继续开路'

    def test_probe_success_resets_backoff_and_failure_set(self):
        """探测成功 = 风控解除：退避级数与连续 403 集合一起清零，下次开路从头计时。"""
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)

        _open_via_three_distinct_403(gate)
        clock.advance(60)
        _observe(gate, 201, 200)           # 半开探测成功
        assert not gate.is_open

        _observe(gate, 301, 403)
        assert not gate.is_open, '复位后失败集合应已清空，403 计数从头开始'
        _observe(gate, 302, 403)
        _observe(gate, 303, 403)
        assert gate.is_open

        clock.advance(59)
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 999, '冷却应按初始 60 秒重新计时（不是翻倍后的 120 秒）')

        clock.advance(1)
        _assert_slot_admitted(gate, 999)

    def test_probe_failure_doubles_cooldown_capped_at_900_seconds(self):
        """探测再次受限说明退避不足：冷却翻倍，封顶 900 秒（15 分钟）。"""
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)

        _observe(gate, 101, 429, retry_after='600')
        clock.advance(600)
        _observe(gate, 102, 403)           # 探测受限：600 → 封顶 900
        assert gate.is_open

        clock.advance(899)
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 103, '冷却应为 900 秒')

        clock.advance(1)
        _observe(gate, 103, 403)           # 再次受限：仍不超过 900
        clock.advance(899)
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 104, '冷却翻倍后不得超过 900 秒上限')

        clock.advance(1)
        _assert_slot_admitted(gate, 104)

    def test_probe_doubling_backoff_is_capped_at_900_seconds(self):
        """自派生退避（探测连续受限的翻倍）必须封顶 900 秒。

        上一个用例从 600 秒起步，`max(900, 600)` 分不出"翻了倍但被封顶"与"没翻倍"，
        所以这里从近默认值 60 起步逐级翻倍：60 → 120 → 240 → 480 → 900 → 900。
        """
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)

        _observe(gate, 101, 429, retry_after='60')   # 服务器冷却，恰好等于初始值
        cooldown = 60.0
        observed = []
        clock.advance(cooldown)                      # 到期，进入半开
        for i in range(6):
            _observe(gate, 200 + i, 403)             # 探测仍受限 → 冷却翻倍（封顶）
            cooldown = min(cooldown * 2, pixiv_client._DETAIL_MAX_COOLDOWN)
            observed.append(cooldown)
            assert gate.is_open
            clock.advance(cooldown - 1)              # 差 1 秒：必须仍然拒绝
            with pytest.raises(pixiv_client.PixivRateLimitedError):
                _assert_slot_refused(gate, 300 + i, f'冷却应为 {cooldown}s，且不得超过 900s 上限')
            clock.advance(1)                         # 恰好到期，下一轮成为新的探测

        assert observed == [120.0, 240.0, 480.0, 900.0, 900.0, 900.0]
        assert max(observed) == pixiv_client._DETAIL_MAX_COOLDOWN

    def test_server_retry_after_beyond_cap_is_not_clamped(self):
        """服务器的 Retry-After 优先于 900s 上限：它比上限长时照样完整生效。

        900s 上限只约束我们自己的指数翻倍；把服务器的明确指示压短等于比 Pixiv
        允许的更早恢复请求。
        """
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)

        _observe(gate, 101, 429, retry_after='3600')
        assert gate.is_open

        clock.advance(900)
        assert gate.is_open, 'Retry-After=3600 不得被 900s 上限钳制'
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 102, '900s 后仍在服务器要求的冷却期内')

        clock.advance(2700)                          # 满 3600 秒
        _run_with_slot(gate, 102, lambda: gate.observe_response(102, 200))
        assert not gate.is_open

    def test_429_retry_after_non_finite_falls_back_and_gate_self_heals(self):
        """非有限的 Retry-After（`'9' * 400` 溢出成 inf）必须被解析器拒掉，闸仍能自愈。

        这是 Fix B 的一半：`inf` 冷却会让 `now - _opened_at < _cooldown` 恒真，本进程
        生命周期内再也不放行任何探测（详情拉取对搜索/预取/后台补全全部死掉，只留一行
        WARNING，直到重启）。所以这里断言的不是"冷却很小"，而是**过了天花板就一定有
        探测被放行** —— 冷却若为 `inf`，这条断言会以 `PixivRateLimitedError` 失败。
        """
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)

        _observe(gate, 101, 429, retry_after='9' * 400)
        assert gate.is_open

        clock.advance(pixiv_client._DETAIL_MAX_SERVER_COOLDOWN)   # 任何有限冷却都必已到期
        _assert_slot_admitted(gate, 102)

    def test_server_retry_after_beyond_ceiling_is_clamped_not_ignored(self):
        """服务器值超过天花板时按天花板上限冷却：既不照单全收，也不缩回 900 秒。

        Fix B 的另一半：900 秒上限只管我们自己的指数翻倍（见上一条用例），服务器指示
        另由一把**有限**的尺子收口 —— 否则 `Retry-After: 86400`（乃至 inf）会把闸钉死。
        这里逐点验算，确认钳制到的是天花板本身而不是某个更小的值。
        """
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)
        ceiling = pixiv_client._DETAIL_MAX_SERVER_COOLDOWN
        assert math.isfinite(ceiling), '天花板必须是有限值，否则闸会永久卡死'

        _observe(gate, 101, 429, retry_after='86400')
        assert gate.is_open

        clock.advance(900)                       # 900s 自派生上限管不到服务器值
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 102, '服务器值不得被 900s 上限缩短')

        clock.advance(ceiling - 900 - 1)         # 差 1 秒到天花板
        with pytest.raises(pixiv_client.PixivRateLimitedError):
            _assert_slot_refused(gate, 102, '天花板内不得提前放行')

        clock.advance(1)                         # 恰好到期：钳制到的是天花板本身
        _assert_slot_admitted(gate, 102)

    def test_probe_exiting_without_http_verdict_releases_probe_reservation(self):
        """探测体没拿到 HTTP 响应就退出时必须归还探测名额。

        连接错误/超时不会调用 `observe_response`；名额若不归还，闸会永远卡在
        "已半开但无人能探测"的状态 —— 那等于永久停掉所有详情请求。
        """
        clock = FakeClock()
        gate = pixiv_client._DetailRequestGate(clock=clock)
        _open_via_three_distinct_403(gate)
        clock.advance(60)                            # 冷却到期，可以半开了

        _assert_slot_admitted(gate, 201)             # 探测体直接退出，没有 HTTP 判决

        assert gate.is_open, '没有判决就不算探测成功，闸应继续开路'
        # 名额已归还，可再次成为探测
        _run_with_slot(gate, 202, lambda: gate.observe_response(202, 200))
        assert not gate.is_open


def test_detail_rate_constants_are_not_raised():
    """45/20/60 是实测绕开 403 的红线（并发 3 即 403）：任何"提速"都不得抬高它们。"""
    assert pixiv_client.DETAIL_RATE_PER_MINUTE == 45
    assert pixiv_client.FILL_RATE_PER_MINUTE == 20
    assert pixiv_client.TOTAL_RATE_PER_MINUTE == 60
