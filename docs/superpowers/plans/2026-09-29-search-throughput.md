# Pixiv 搜索渐进提速实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 `subagent-driven-development`（推荐）或 `executing-plans`，按任务执行并逐段审查。步骤用 checkbox 追踪。
>
> **Task 7 回写（2026-09-29）：本计划已全部执行完毕。** 实际执行顺序 Task 1 → 3 → 4 → 5 → 2 → 6 → 7；任务末尾的「实施记录」给出真实 commit 与实测数字。正文里的**勘误**条目来自 `2026-09-29-search-throughput-execution-notes.md`，该文件对应条目已标注「已并入」。Task 5 的手工浏览器验收（Step 3）**从未执行**（本环境无浏览器），其可执行步骤已按验收要求写进 Task 5。

**Goal:** 在不提高 Pixiv 详情请求速率的前提下，提前展示已通过筛选的搜索结果，并在全局限流时安全地返回部分结果。

**Architecture:** 详情请求熔断和在途上限归 `pixiv_client.py`；`fetcher.py` 负责逐详情结果回调、过滤、预算、分页和缓存；`routes_search.py` 在锁内维护可轮询的任务快照；`static/page-index.js` 仅显示 provisional 预览，完整页和游标仍只在任务完成时提交。限流造成的 `partial` 终态不推进游标、不写成功缓存。

**Tech Stack:** Python 3.13、Flask、requests、SQLAlchemy/SQLite、原生 ES2020 JavaScript、pytest；所有默认测试离线运行，经 `scripts/run_tests.ps1` 执行。

---

## 执行前置条件

1. 当前工作区的 Pixiv client 适配层改造（`pixiv_client.py`、`fetcher.py`、fixtures 和契约测试）尚有未提交文件。**不要在当前 dirty `main` 上实施本计划**；等适配层改造稳定并提交后，按 `using-git-worktrees` 技能从包含该提交的干净基线上创建 `feature/search-throughput` worktree。→ 已完成：worktree `E:\pixiv\.worktrees\search-throughput`，基线 `d961824`。
2. 实施前先读该基线的 `AGENTS.md` 与 `docs/architecture.md`，再运行下面的基线命令。保留 `app.py` 中被测试补丁的 from-import seam；路由必须继续在函数体内延迟 `import app` 后引用这些符号。→ 已完成。
3. 详情桶常量保持 `DETAIL_RATE_PER_MINUTE=45`、`FILL_RATE_PER_MINUTE=20`、`TOTAL_RATE_PER_MINUTE=60`。不使用代理/IP、账号或 Cookie 轮换，不在默认测试里读真实 Cookie，也不运行未经显式授权的真实 Pixiv 请求。→ 全程遵守；Task 7 用 `tests/test_pixiv_client_limits.py::test_detail_rate_constants_are_not_raised` 与 mock 脚手架复核（45/20/60 未变）。
4. 目标模块职责稳定：`pixiv_client.py` 不导入 DB、不认识 `Illust`；`fetcher.py` 不拼 Ajax URL、不读取 Pixiv payload 原始字段；`runtime._search_tasks` 保持 `-w 1` 单进程语义。→ 遵守。
5. 在新 worktree 中先保存基线：运行 `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_fetcher.py tests\test_app.py -q` 与 `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q`；记录完整基线是否通过。当前代码在 status 终态前不公开结果，因此基线首结果时间等于任务完成时间。实施后用同一固定延迟 fake Session 记录首结果与完整页时间，和该行为基线比较。

   → 已完成。**实测基线（`d961824`，Task 1 之前）**：`601 collected = 595 passed / 2 skipped / 4 failed(env)`。4 个失败全部是 `tests/test_test_setup.py::test_temp_root_*`（沙箱下子 PowerShell 的语言模式不同 → `subprocess.CalledProcessError`），`tests/test_test_setup.py` 在本分支**一字未改**，所以同一环境下基线与本分支失败集合完全相同。基线首结果时间 == 完成时间（status 终态前不公开任何结果）这一点在 Task 7 的 mock 对比里被实测复现（旧代码 30.74s == 30.74s）。

## 文件地图

| 文件 | 责任 |
|---|---|
| `pixiv_client.py` | 新增共享详情请求 gate（最大在途 2、403/429 熔断、半开探测），接入详情 HTTP attempt；保留现有响应解析、Cookie、连接池和速率常量。 |
| `fetcher.py` | 增加详情批次限流结果与逐结果回调；在 `_process_items` 过滤通过后发布 preview；`SearchRateLimitedError` 穿过分页；完整结果才入缓存。 |
| `routes_search.py` | 在 `_submit_search_task` 里维护 progress/revision/preview；状态端点复制一致快照；新增 `partial` 终态并保留原 cursor。 |
| `static/page-index.js` | 扩展 `pollSearch` 的 progress 回调，独立维护临时预览；只有 `done` 更新 `loadedPages`、cursor 和 `pvCache`。 |
| `tests/test_pixiv_client_limits.py`（新建） | 使用 fake clock、fake Session/线程验证 gate、冷却和并发，不使用 Pixiv Cookie。 |
| `tests/test_fetcher.py`、`tests/test_settings_api.py` | 回归详情收集、逐结果筛选发布、限流结果传播、缓存不写残缺页、预算/游标不回退，以及屏蔽标签变更后的缓存失效。 |
| `tests/test_app.py` | 回归运行中 status 快照、revision、`partial` 响应、任务清理及 `app` monkeypatch seam。 |
| `tests/test_pixiv_contract.py`（仅摘要 API 有证据时） | 新端点 URL、脱敏 payload 字段和规范解析契约。→ **未改动**（Task 6 判定 no-go）。 |
| `docs/superpowers/specs/2026-09-29-search-throughput-design.md` | 所有实现验证后标记“已实现”，写入实际验证和未完成的摘要接口结论。 |

**不改 `runtime.py` 的模块边界**：搜索任务字典已由 `routes_search.py` 构造，增量字段放在该任务对象中即可；除非基线代码已把状态结构收口到 `runtime.py`，否则不移动状态。→ 遵守（`runtime._search_tasks` 未动）。

## 任务依赖顺序

为遵循已批准设计中的实施顺序，Task 1 只实现并测试尚未接入请求路径的 gate 状态机；随后先完成渐进进度与 UI（Task 3 → Task 4 → Task 5），最后在已有 `partial` 处理链后接通 gate（Task 2）。**实际执行顺序：Task 1 → Task 3 → Task 4 → Task 5 → Task 2 → Task 6 → Task 7。**这样在新熔断开始影响搜索前，限流的部分结果/游标/UI 语义已经完成并有回归测试。

## Task 1：建立离线详情 gate 的红灯测试

**Files:**
- Create: `tests/test_pixiv_client_limits.py`
- Modify: `pixiv_client.py`（下一步实现）

- [x] **Step 1: 写 gate 的失败测试**

测试文件开头导入 `pytest` 和 `pixiv_client`，定义 fake clock，测试公开行为而非真实等待：

```python
import pytest
import pixiv_client


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_three_distinct_403_open_gate(monkeypatch):
    clock = FakeClock()
    gate = pixiv_client._DetailRequestGate(clock=clock)

    for pid in (101, 102, 103):
        with gate.request_slot(pid):
            gate.observe_response(pid, 403)

    assert gate.is_open
    with pytest.raises(pixiv_client.PixivRateLimitedError):
        with gate.request_slot(104):
            pytest.fail('熔断期间不应发出详情请求')
```

再补四组用例：gate 的 barrier/event 并发测试验证最多两个 `request_slot` 同时持有；单个 PID 的 403 重试不计作三个不同作品；429 使用 `Retry-After`；冷却到期只允许一个半开探测，成功后复位，探测再次受限则指数退避且不超过 900 秒。任何走真实 `fetch_illust_detail`/`_fetch_details_parallel` 的默认测试都把 `pixiv_client.COOKIE_PATH` monkeypatch 到临时文件，不读仓库真实 Cookie。

- [x] **Step 2: 运行 gate 测试并确认红灯**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_pixiv_client_limits.py -q`
Expected: FAIL，因为 `_DetailRequestGate` 与 `PixivRateLimitedError` 尚不存在。

- [x] **Step 3: 实现 gate 状态机**

在 `pixiv_client.py` 增加 `PixivRateLimitedError` 和 `_DetailRequestGate`，接口固定为：`request_slot(pixiv_id)` context manager、`observe_response(pixiv_id, status, retry_after=None)`、只读属性 `is_open`。实现规则：

1. context manager 内用 `BoundedSemaphore(2)` 限制真实详情 HTTP 在途请求数，`finally` 必须释放；令牌桶等待放在拿 semaphore 之前，不能持有槽位睡眠。
2. 429 立即开路；以 `Retry-After` 的 delta-seconds/HTTP-date 为准，值无效或缺失时用 60 秒。
3. 仅当连续 60 秒窗口内有 3 个不同 PID 的详情 403 才开路；单个作品重复 403 不触发全局熔断，任一非 403 HTTP 状态（含 2xx、401、404、5xx）清除连续 403 集合，但 401/404/5xx 的原错误分类不变。
4. 冷却后仅一个调用可半开探测；探测收到非 403/429 的 HTTP 响应后恢复并清除失败集合/退避级数，探测再次受限则冷却翻倍，最大 900 秒。
5. gate 状态更新和“读→判定→写”在同一锁内完成；不持有锁进行网络 I/O。

- [x] **Step 4: 重跑 gate 测试并提交**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_pixiv_client_limits.py -q`
Expected: 全部新增 gate 用例 PASS，无真实网络请求。

```bash
git add pixiv_client.py tests/test_pixiv_client_limits.py
git commit -m "fix: 为 Pixiv 详情请求增加共享熔断闸"
```

**实施记录（Task 7 回写）**

| 项 | 实测 |
|---|---|
| commit | `ccf8a9a` `fix: 为 Pixiv 详情请求增加共享熔断闸` |
| 变更 | `pixiv_client.py` +249、`tests/test_pixiv_client_limits.py` 新建 626 行 |
| 新增用例 | 22 个测试函数 → 该时点 37 个 collected 用例（参数化展开） |
| 与计划差异 | 计划写“最大 900 秒”是自派生指数退避的上限；实现另给**服务器 `Retry-After`** 一个 6 小时天花板（`_DETAIL_MAX_SERVER_COOLDOWN`，见 `_open_locked`）。二者语义不同，`test_server_retry_after_beyond_cap_is_not_clamped` / `test_server_retry_after_beyond_ceiling_is_clamped_not_ignored` 各钉一条。 |

## Task 2：把 gate 接入详情请求并保留既有限速语义

**Files:**
- Modify: `pixiv_client.py`
- Test: `tests/test_pixiv_client_limits.py`、`tests/test_fetcher.py`

- [x] **Step 1: 写详情请求集成失败测试**

对 `fetch_illust_detail` 使用 fake Session 和可计数 limiter，断言每次真实 attempt（包含 403/429 退避重试）都先通过详情桶、总桶和 gate；401 仍抛 `PixivAuthError`，404 仍是永久删除语义，连接错误仍 fail-fast。另断言单次 403 不会熔断，429/连续不同 PID 403 打开 gate 后，下一次详情调用在发出 HTTP 前得到 `PixivRateLimitedError`。

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_pixiv_client_limits.py tests\test_fetcher.py -q`
Expected: 新的 gate 集成断言 FAIL；既有分类测试仍运行并暴露任何兼容问题，无 `-k` 漏测风险。

- [x] **Step 2: 最小接入 `fetch_illust_detail`**

在 `fetch_illust_detail` 的每一次 HTTP attempt 前依次等待传入的 detail/fill limiter 与 `_total_limiter`，之后才进入 gate 的 `request_slot(pixiv_id)`。每次 retry 都重新取令牌。将状态码和 `Retry-After` 交给 `observe_response` 后再执行 `raise_for_status()`；连接错误、401、404、其他 5xx 的原分类不变。打开的 gate 不得再发逐条 3/9 秒限流 retry。

`return_dead=True` 的刷新路径仍将全局限流映射为 `RETRYABLE_GLOBAL_DETAIL`，不能写入单作品失败退避；搜索路径则抛 `PixivRateLimitedError`，不得把它降级成 `None`。

- [x] **Step 3: 运行 client 与详情回归并提交**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_pixiv_client_limits.py tests\test_fetcher.py -q`
Expected: 详情重试分类、`return_dead` 哨兵、限速常量及新 gate 测试全 PASS；速率常量仍为 45/20/60。

```bash
git add pixiv_client.py tests/test_pixiv_client_limits.py tests/test_fetcher.py
git commit -m "fix: 让详情重试共享速率闸与退避状态"
```

**勘误（已并入：执行笔记「Task 2 必读（预取调用方的限流语义）」）**

1. **预取的宽 `except Exception` 把 `SearchRateLimitedError` 变成“该标签本轮失败”是预期语义**：标签记 `error`、下轮重试；已入库作品不会丢（下轮从库里命中 existing 记录，不重拉详情）。已由 `tests/test_fetcher.py::test_tag_recovers_next_round_and_cleanup_still_runs` 钉住（含 `_prefetch_loop` 继续跑容量清理），`test_open_gate_aborts_refresh_without_per_work_backoff` 钉住刷新路径不被误记成单作品失败。
2. **`PixivRateLimitedError` 绝不能越过 `_fetch_details_parallel` 逃到路由层**：该函数内部把它捕获并翻成 `batch.rate_limited`；一旦逃到 `routes_search`，宽 `except Exception` 会把它变成 HTTP 502 `error`，把“可重试的限流”静默降级成“失败”，`partial` 语义直接失效。已由 `tests/test_fetcher.py::test_open_gate_surfaces_as_rate_limited_without_http`（窄化到 fetcher 边界）与 `tests/test_pixiv_client_limits.py::test_detail_gate_is_not_reexported_by_fetcher` 钉住。
3. **被闸拒绝的请求不计 `attempted`、不记 `detail_failed`**：它根本没发出，不是“这件作品详情失败”。`_DetailFetchBatch.attempted` 只计真正发出的请求（见 Task 3 的类型定义）。

**实施记录（Task 7 回写）**

| 项 | 实测 |
|---|---|
| commit | `89137bc` `fix: 让详情重试共享速率闸与退避状态` |
| 变更 | `pixiv_client.py` +151/-32、`tests/test_fetcher.py` +245、`tests/test_pixiv_client_limits.py` +292、`AGENTS.md` 重试策略一节、`docs/architecture.md` 增加 `_detail_gate` 行 |
| 新增用例 | 15 个测试函数；`test_pixiv_client_limits.py` 到该时点 48 个 collected 用例 |
| 实测（Task 7 mock） | 全部 403 的 24 件作者搜索：新实现**只发 3 个**详情 HTTP（3 个不同 PID 的 403 触发开路，之后在闸前被拒），旧实现发 **72 个**（24×3 次重试） |
| 实测（Task 7 mock） | 429（`Retry-After: 30`）：新实现**只发 1 个**详情 HTTP 即开路；旧实现 72 个。⚠️ 这条只在 `PIXIV_BASE_URL=http://`（适配器只 mount `https://`）或将来改 `status_forcelist` 时成立 —— 默认 https 路径下 urllib3 在传输层吃掉 429，闸收不到（见文末「遗留」）。 |

## Task 3：让详情流水线逐条发布已过滤结果并保留限流结果

**Files:**
- Modify: `fetcher.py`
- Test: `tests/test_fetcher.py`

- [x] **Step 1: 写详情批次和 progress 回调失败测试**

新增用例验证：`_fetch_details_parallel` 在消费线程按 future 完成顺序回调 `(pixiv_id, detail)`；`_process_items` 只对通过收藏数、R18 和屏蔽标签过滤的记录调用 `on_result`；一个 PID 的 callback 只调用一次；触发 `PixivRateLimitedError` 后已完成详情仍被处理，但批次标为 rate-limited；`PixivAuthError` 从 future 原样传播，不可被宽泛 `except Exception` 静默转成详情失败。用例命名为 `test_detail_progress_callback_runs_in_collector_thread`、`test_process_items_publishes_only_filter_matches`、`test_rate_limited_batch_keeps_completed_details`、`test_parallel_detail_propagates_auth_error`。

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_fetcher.py -q`
Expected: 新增 callback/限流/AuthError 用例 FAIL，因为当前 `_fetch_details_parallel` 只返回最终 `(details, attempted)`，`_process_items` 没有结果回调；不依赖 `-k` 过滤式。

- [x] **Step 2: 加入明确的批次结果类型**

在 `fetcher.py` 定义并使用以下类型，禁止以 `None` 混淆“单件失败”和“全局限流”：

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class _DetailFetchBatch:
    details: dict[int, dict]
    attempted: int
    rate_limited: bool = False
```

将 `_fetch_details_parallel` 改为返回 `_DetailFetchBatch`，增加可选 `on_detail`。限流异常出现时取消尚未开始的 futures、收齐已经在途的 futures、保留成功详情并令 `rate_limited=True`；`attempted` 仅计实际发出的请求。若 future 抛 `PixivAuthError`，取消未启动任务、收尾已在途任务后重新抛出，不记成普通详情失败。更新两处生产调用与测试中所有直接解包/模拟返回，测试统一访问 `.details`、`.attempted`、`.rate_limited`，不增加隐式 tuple 兼容层。

- [x] **Step 3: 在 `_process_items` 发布规范结果**

给 `_process_items`、`search_by_tag`、`search_by_user`、`browse_discovery`、`paginated_search` 增加默认 `None` 的可选 `progress` callback；`_fetch_details_parallel` 的 `on_detail(pid, detail)` 在 collector 线程报告每个完成详情（含失败）。`progress(event)` 使用三种明确事件：`{"type":"examined","pixiv_id":pid}`、`{"type":"detail_failed","pixiv_id":pid}`、`{"type":"result","result":dict}`；每个输入 PID 的 examined 恰好一次，只有通过路径所需过滤后才发 result。

**勘误（已并入：执行笔记「Task 4 必读」第 1 条）**：`progress` **只**注入 `search_by_tag` / `search_by_user` / `browse_discovery` 三个逐条目搜索函数 —— 上面那句“给 `paginated_search` 也加 progress”是**错的**，实现里刻意**不给** `paginated_search` 该形参：它只看页边界、看不到条目，收下也只能原样丢掉（“接线通过、测试全绿、端到端零事件”的静默失效陷阱）。`paginated_search` 的 docstring 专门写明了这条约束，`tests/test_fetcher.py::test_progress_keyword_is_rejected` 钉住它，`tests/test_app.py::test_publisher_goes_to_search_fn_not_paginated_search` 从路由侧再钉一次。

- 已存在且通过全部过滤的记录可立即发布。
- 新详情在 `_fetch_details_parallel` 的 collector/search 线程回调；在发布前用既有 `blocked`、`min_bookmarks`、`hide_r18` 判定。不要把 SQLAlchemy session 交给 worker。
- favorite 状态用调用线程一次性取得的 `get_favorite_pids(db)` 标记；沿用 `_mark_favorites` 的字段语义。
- `defer_details` 继续直接用条目摘要；`min_bookmarks==0` 的标签路径不增加详情请求。
- callback 的候选只是 preview；`fetcher._cancelled()` 为真时禁止继续发布旧任务进度，但已完成作品仍按现有事务入库语义收尾。最终列表继续走 `_insert_new_illusts` 和现有 `safe_commit(db)` 批量入库。

**勘误（已并入：执行笔记「Task 4 必读」第 4 条）**：预览 dict 来自**尚未入库**的模型（`_publish_detail` 用 `illust_factory(item, detail).to_dict()`，此时行 `id` / `created_at` 为 `None`），而 canonical 列表带真实 DB 值 —— 前端合并**必须按 `pixiv_id`**，不能按行 `id`。

- [x] **Step 4: 明确率限流状态并阻止残缺缓存**

在 `_process_items` 中定义 `_ProcessedItems`，保持原有 list 迭代/JSON 输出兼容：

```python
class _ProcessedItems(list):
    def __init__(self, items=(), *, rate_limited=False):
        super().__init__(items)
        self.rate_limited = rate_limited
```

构造时把 `_DetailFetchBatch.rate_limited` 写入该属性。调用方完成 `safe_commit(db)` 后，若 `rate_limited`，抛 `SearchRateLimitedError`；`paginated_search` 必须显式重新抛出它，不能落入“普通页失败后当空页完成”的宽泛 `except`。`search_by_tag`、`browse_discovery`、`search_by_user` 在抛出前不得写成功缓存。

**勘误（已并入：执行笔记「已确定的实现判据」）**：`rate_limited` 只挂在这个 list 子类对象上，`results[:n]` / `+` / `copy()` 都返回普通 list 并丢掉标记，所以**只在 `search_by_*` 边界读它**才有意义（`_ProcessedItems` 的 docstring 已写明）。

新增缓存测试：完整搜索仍可缓存；限流 partial 和中途详情预算耗尽均不缓存；重复完整画师搜索仍命中 600 秒缓存及屏蔽指纹。

- [x] **Step 5: 跑 fetcher 回归并提交**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_fetcher.py -q`
Expected: 详情并发、连接池、过滤、收藏、详情预算、游标与新增 progress/partial 用例全 PASS。

```bash
git add fetcher.py tests/test_fetcher.py
git commit -m "perf+fix: 搜索详情逐条发布并标记限流结果"
```

**实施记录（Task 7 回写）**

| 项 | 实测 |
|---|---|
| commit | **`51cfb22`** `perf+fix: 搜索详情逐条发布并标记限流结果`（执行笔记里记的 `be0dfee` 在本仓库不存在，**以 `51cfb22` 为准**） |
| 变更 | `fetcher.py` +324/-75、`tests/test_fetcher.py` +573 |
| 新增用例 | 18 个测试函数 → `tests/test_fetcher.py` 到该时点 **105** 个 collected 用例（实测；执行笔记原写的 101 其实是同一时点 `tests/test_app.py` 的数目） |
| 行数 | `fetcher.py` 到本轮结束约 1249 行（Task 6 后 1282 行，见「遗留」） |

## Task 4：发布一致的搜索任务快照和 `partial` 终态

**Files:**
- Modify: `routes_search.py`
- Test: `tests/test_app.py`

- [x] **Step 1: 写 running snapshot 与 partial 失败测试**

新测试直接提交一个带 `progress_callback` 的任务：worker 先发布一条结果，再由 `threading.Event` 阻塞；测试在阻塞期间请求 status，断言 `status=running`、该结果可见、revision 大于 0、complete 为 false；放行后断言最终为 done 且最终结果是 canonical 页。

再测试 `SearchRateLimitedError` 使任务以 HTTP 200 的 `partial` 终态返回已有 preview、`complete=false` 和限流 warning；输入 cursor 不前移，`has_more` 不被伪造；`partial` 到 TTL 后可清理。

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_app.py -q`
Expected: 新增 running progress、partial、cleanup 用例 FAIL，既有 `tests/test_app.py` 搜索/认证回归同时运行。

- [x] **Step 2: 在 `_submit_search_task` 维护带锁快照**

任务字典初始化 `revision=0`、`progress={examined, accepted, detail_failed}`、`complete=False`、`warning=None`、`results=[]`，并保存 `input_cursor=cursor_str or None`。publisher 按 `progress` 事件类型在 `_search_tasks_lock` 内更新 counters；result 事件按 PID 去重，最多追加 `ITEMS_PER_PAGE` 个 preview，并更新 accepted 和 revision。网络 I/O 不持有该锁，写入任务的每条 result 使用新 dict 快照。

让任务函数接收可选 publisher；`search()` 中 `_browse_fn`、`_tag_fn`、`_user_fn` 将 publisher 沿 `app.paginated_search` 和对应 `app.search_by_*` 闭包传入，保持 `app.*` 延迟导入补丁契约。任务成功时用最终 canonical 结果替换 preview、填 cursor/has_more、complete=true；捕获 `SearchRateLimitedError` 时保留当前 preview、将任务 cursor 设回 `input_cursor`、complete=false、status=partial。

**勘误（已并入：执行笔记「Task 4 必读」第 1–3 条）**

1. **publisher 只经 `progress` 形参注入 `search_by_*`**：上句“沿 `app.paginated_search` 和对应 `app.search_by_*` 闭包传入”以 Task 3 的勘误为准 —— `_tag_fn` / `_user_fn` / `_browse_fn` 各自捕获 publisher 后传给对应 `search_by_*`，`paginated_search` **不接受** `progress`。
2. **`error` 终态不得在响应里带 preview**：已发布的预览可能对应随后回滚的行（`_publish` 发生在 `safe_commit` 之前），所以状态端点对 `error` 必须返回空 results。`tests/test_app.py::test_error_terminal_drops_previews` 钉住。
3. **result 事件必须按 `pixiv_id` 去重，且只保留前 `ITEMS_PER_PAGE` 个**：`_publish` 的去重集是 per-`_process_items` 的，一次搜索可能翻多页，发出的 result 会超过一页条数。`tests/test_app.py::test_result_events_deduped_and_preview_capped_to_one_page` 钉住。

- [x] **Step 3: 锁内复制 status response 并纳入 partial 清理**

`search_status` 在 `_search_tasks_lock` 内将 status、results 的 list copy、cursor、has_more、fetch_stats、revision、progress、complete、warning 复制到局部快照，解锁后再 `jsonify`。保留既有 error/401/502 响应。`_cleanup_search_tasks` 终态集合增加 `partial`。

**勘误（已并入：执行笔记「Task 5 必读」第 4、5、7 条 + 后续 docs 提交 `06b2502`、`f3735b8`）**

1. **`revision` 每次事件都自增**（不只 `result`）：计数器变化也要对前端可见。后果是 `examined` 突发时“revision 未增长就不重渲染”的守卫会触发一次实际无内容变化的重渲染；渲染判据应以 progress / preview 集合为准。好消息是 `revision` 前进 ⟺ 快照有变化（重复 result 在被去重处提前返回，不会自增）。
2. **`progress.accepted` 不封顶**，可以大于 `len(results)`（preview 被 `ITEMS_PER_PAGE` 截断）。“已找到 N 件”用 `accepted`，渲染网格必须用 `results`。
3. **终态响应的 `progress` 计数器必须与 `results` 一致**：`error` / `cancelled` 已清空 `results`，就不能再回非零 `accepted`（那些行可能已回滚）。`tests/test_app.py::test_cancelled_terminal_drops_previews` + `test_error_terminal_drops_previews` 钉住。

- [x] **Step 4: 运行路由回归并提交**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_app.py -q`
Expected: 搜索入参、`app` monkeypatch seam、取消、认证错误、TASK_LOST、TTL 清理与 running/partial 新用例全 PASS。

```bash
git add routes_search.py tests/test_app.py
git commit -m "feat: 搜索任务状态支持增量结果与限流部分页"
```

**实施记录（Task 7 回写）**

| 项 | 实测 |
|---|---|
| commit | `4fcac74` `feat: 搜索任务状态支持增量结果与限流部分页`（运行时快照测试 12 个新函数；`routes_search.py` +262/-55、`tests/test_app.py` +389） |
| 后续纯文档提交 | `06b2502` `docs: 补充 Task 5 的 revision 自增与 accepted 不封顶语义`、`f3735b8` `docs: 补充 Task 4/5 的终态计数器一致性与部署顺序约束` |
| 钉住的测试 | `test_running_snapshot_then_done_replaces_preview_with_canonical`、`test_result_events_deduped_and_preview_capped_to_one_page`、`test_rate_limited_finishes_partial_keeping_preview`、`test_partial_keeps_input_cursor`、`test_partial_does_not_echo_discarded_cursor`、`test_partial_task_cleaned_up_after_ttl`、`test_error_terminal_drops_previews`、`test_cancelled_terminal_drops_previews`、`test_publisher_goes_to_search_fn_not_paginated_search`、`test_published_preview_is_a_snapshot_owned_by_task`、`test_route_wiring_publishes_real_progress_end_to_end`、`test_terminal_branch_failure_is_pinned_to_error` |

## Task 5：前端展示临时 preview，保持分页提交语义

**Files:**
- Modify: `static/page-index.js`
- Test: 本地 mock API 浏览器验收；服务端 API 行为由 Task 4 自动测试覆盖。

- [x] **Step 1: 实现独立 preview 状态和 poll 回调**

增加 `searchPreview=[]`、`searchPreviewIds=new Set()` 与 `lastSearchRevision=0`，不要把运行中的结果写入 `loadedPages`。扩展 `pollSearch(taskId, onDone, onFail, gen, onProgress, onPartial)`：running 响应先调用 `onProgress(data)` 再按原间隔轮询；revision 未增长时不重复渲染；旧 generation 仍立即退出；done 调 `onDone`，partial 调 `onProgress` 后单独调 `onPartial`，不能落入 `finishSearch`。同步更新 `doSearch` 与 `loadNextPage` 两处 `pollSearch` 调用，传入 preview handler 和 partial handler。

在卡片更新处对 `loadedPages` 与 `searchPreviewIds` 按 `pixiv_id` 去重并 `renderInChunks`；更新 `paginationStatus` 显示“已找到 N 件，仍在筛选”。preview 不调用 `saveSearchState()`，不修改 `nextCursor`、`hasMore` 或 `currentPage`。

- [x] **Step 2: 完成/部分/取消时分别收口 UI**

`doSearch` 开始新 generation 时清空 `searchPreview`/`searchPreviewIds`；取消和旧 generation 不得继续写 preview。`done` 调用现有 `finishSearch`，先清除 preview 再渲染 canonical 最终页，随后才更新 `loadedPages`、cursor、分页按钮和 `pvCache`。`partial` 保留当前已确认 preview、停止 loading、显示 `warning`，不把 preview 提交为页或写完整缓存；原有 `nextCursor` 不变：首次搜索没有 cursor 时下一页保持禁用，翻页中断时恢复按钮以便用户冷却后重试同一个 cursor。

**勘误（已并入：执行笔记「Task 5 必读」第 1、2、3、6 条）**

1. **`done` 时必须用 canonical 页“替换”预览，不是合并**：多页扫描可能接受多于 `ITEMS_PER_PAGE` 件；canonical 取扫描顺序前 N 件，而预览是完成顺序，两边合法地可以不一致。
2. **`error` / 404 / 取消必须清空预览**：否则失败的搜索会在页面上留下幽灵卡片（旧实现 `pollSearch(..., onFail=undefined)`，error 分支只 toast）。
3. **不要用 `examined` 渲染完成比例**：`examined` 在任何详情完成之前就整批发出，“N/N examined”会瞬间到 100% 而 accepted 仍为 0 —— 它只能当活动计数器，与“已找到 K 件”并列显示。
4. **部署顺序（硬约束）**：Task 5 落地前不要把 Task 4 单独部署。未改动的 `static/page-index.js` 会把 `partial` 响应当 `onDone(data)` 处理 —— 预览会被当成 canonical 页提交、`warning` 被丢弃、`has_more=false` 写进分页状态。Task 5 必须显式分流 `partial`（`onProgress` + 单独的 `onPartial`，不能落进 `finishSearch`），并在 502/401/error 路径清空预览。

- [ ] **Step 3: 用本地 mock API 手工验证，无新增 JS 工具链**

启动开发服务，使用仅在 DevTools console 中临时替换的 `window.fetch` mock `/search` 与 `/api/search/status/*`，不改应用文件、不请求 Pixiv。status 依次返回 revision 1（PID 11）、revision 2（PID 11+12）、done（canonical PID 12+11）；结果字典至少包含 `pixiv_id/title/user_id/user_name/page_count/bookmark_count/thumb_url/tags`。确认运行时网格按增量更新且无重复，done 后以 canonical 顺序替换。再验证 `partial` 显示 warning：首次搜索保持下一页禁用；翻页中断时保留原 `nextCursor` 并允许重试同一页；刷新页面不会恢复 partial preview。确认生产代码 CSP 下无 inline script、`eval` 或新构建依赖。

> **⚠️ 本步从未执行**：实施环境没有浏览器。前端逻辑改用一次性的 Node `vm` + fake-DOM 探针验证（54/54 断言通过，探针不入库），**真实 CSS/交互仍未验证**。下面给出可直接照做的手工步骤（Task 7 补写，供人工执行）。

**手工浏览器验收步骤（Task 5 Step 3，待人工执行）**

1. 起服务（离线，不要点真实搜索）：`flask run --debug` 或 `gunicorn -w 1 --threads 8 -b 127.0.0.1:8000 app:app`，打开 `http://127.0.0.1:8000/`。
2. 打开 DevTools → Console，粘贴下面的 mock（只覆盖本页的 `window.fetch`，不写文件、不发真实请求）：

   ```js
   (() => {
     const orig = window.fetch;
     const mk = (pid, title, ms) => new Promise(r => setTimeout(() => r(new Response(JSON.stringify({
       status: 'running', revision: 1, complete: false, warning: null, cursor: null, has_more: true,
       progress: { examined: 1, accepted: 1, detail_failed: 0 },
       results: [{ pixiv_id: pid, title: title, user_id: 'u1', user_name: 'author',
                   page_count: 1, bookmark_count: 100, thumb_url: '', tags: ['x'] }],
     }), { status: 200, headers: { 'Content-Type': 'application/json' } })), ms));
     let step = 0;
     window.fetch = (url, opt) => {
       if (String(url).startsWith('/search')) {
         step = 1;
         return Promise.resolve(new Response(JSON.stringify({ task_id: 'mock' }),
           { status: 200, headers: { 'Content-Type': 'application/json' } }));
       }
       if (String(url).includes('/api/search/status/')) {
         step += 1;
         if (step === 2) return mk(11, 'rev1', 200);            // running rev1：PID 11
         if (step === 3) return mk(11, 'rev2', 200);            // running rev2：11+12（下面拼两条）
         return Promise.resolve(new Response(JSON.stringify({
           status: 'done', revision: 9, complete: true, warning: null, cursor: null, has_more: false,
           progress: { examined: 2, accepted: 2, detail_failed: 0 },
           results: [ { pixiv_id: 12, title: 'canonical-1', user_id: 'u1', user_name: 'author',
                        page_count: 1, bookmark_count: 1, thumb_url: '', tags: [] },
                      { pixiv_id: 11, title: 'canonical-2', user_id: 'u1', user_name: 'author',
                        page_count: 1, bookmark_count: 1, thumb_url: '', tags: [] } ],
         }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
       }
       return orig(url, opt);
     };
   })();
   ```

   （rev2 那条若要看到 11+12 两张卡片，把 `mk()` 的 `results` 数组手工改成两条即可。）
3. 在搜索框输入任意标签并回车。**要看到的行为**：running 阶段网格增量出现 provisional 卡片、状态行显示“已找到 N 件，仍在筛选”、翻页按钮不可用/游标不动；`done` 之后网格按 canonical 顺序（12、11）整体替换，分页按钮与 `pvCache` 才更新；全程没有重复卡片。
4. `partial` 用例：把上面 `done` 分支的 `status` 改为 `'partial'`、`has_more: true`、`warning: '详情请求被限流，已显示部分结果'`、`cursor` 给一个非空值。**要看到的行为**：preview 保留、loading 停止、显示 warning、不把 preview 提交成完整页；首次搜索没有 cursor 时下一页保持禁用，翻页中断时原 `nextCursor` 保留且可重试同一页；刷新页面不恢复 partial preview。
5. `error` 用例：把分支改成 `{ status: 'error', results: [], progress: { examined: 0, accepted: 0, detail_failed: 0 } }`。**要看到的行为**：预览被清空（没有幽灵卡片）、只有错误提示；401/502 同理。
6. CSP 复核：`grep -nE "eval\(|new Function|type=\"text/javascript\"" static/page-index.js templates/index.html` 无命中；页面无 inline script；无新的构建产物/依赖。

- [x] **Step 4: 检查差异并提交**

Run: `git diff --check`
Expected: 无空白错误；手工 smoke 的三种终态符合预期。

```bash
git add static/page-index.js
git commit -m "feat: 搜索中渐进展示已确认作品"
```

**实施记录（Task 7 回写）**

| 项 | 实测 |
|---|---|
| commit | `41435bd` `feat: 搜索中渐进展示已确认作品`（`static/page-index.js` +233/-9） |
| 自动测试 | 无（本仓库无 JS 工具链，计划明确不引入） |
| 替代验证 | 一次性 Node `vm` + fake-DOM 探针 **54/54 断言通过**（探针已删除、未入库） |
| 未验证 | 真实浏览器里的 CSS/交互、滚动位置、真实 `renderInChunks` 时序、`partial` 下按钮的实际可用性 |

## Task 6：缓存边界与用户摘要接口 go/no-go

**Files:**
- Modify: `fetcher.py`、`tests/test_fetcher.py`、必要时 `tests/test_settings_api.py`
- Conditional modify: `pixiv_client.py`、`tests/test_pixiv_contract.py`、`tests/fixtures/pixiv/`

- [x] **Step 1: 给标签 TTL 与屏蔽标签失效写失败测试**

用 fake clock 测试标签缓存的专用 TTL 为 120 秒，TTL 内同条件查询不再打上游；TTL 到期才重新请求。通过 `routes_settings` 的屏蔽标签增删路径验证 `clear_search_cache()` 立即生效。发现页/关注页不得被无意改成 120 秒 TTL。

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_fetcher.py tests\test_settings_api.py -q`
Expected: 新 TTL/失效断言 FAIL，现有用户搜索缓存测试保持通过；不依赖可能返回零用例的 `-k` 过滤式。

- [x] **Step 2: 实现独立 tag TTL 和完整缓存规则**

新增 `_TAG_SEARCH_CACHE_TTL = 120.0`，仅 `search_by_tag` 的 `_cache_get/_cache_put` 使用该 TTL；`_SEARCH_CACHE_TTL` 仍控制 discovery/following 等其它结果。屏蔽标签改动继续通过 `clear_search_cache()` 全清。`SearchRateLimitedError`、详情预算耗尽、认证/上游请求异常都不能写入成功缓存；不得改变作者 600 秒缓存键中的 `_blocked_fingerprint`。

**勘误（已并入：执行笔记「Task 6 实施结果」）**

1. **空页仍旧进缓存，而且用的仍是 30 秒**：标签路径“上游成功但返回空页”写的是 `_SEARCH_CACHE_TTL`（30s），**不**跟随 120 秒标签 TTL。Pixiv 在 Cookie 失效时也会静默返回空结果，空页不是异常。`tests/test_fetcher.py::test_empty_tag_page_keeps_short_ttl` 钉住。
2. **`search_by_tag` 与 `browse_discovery` 在 `budget_exhausted()` 为真时不写成功缓存**（与 `search_by_user` 早已有的守卫对齐）。当前只有作者搜索启用详情预算，这两条是防“哪天给标签/发现路径也开预算”时静默退化的护栏。
3. **`_cache_get(key, ttl=...)` 的 `ttl` 是惰性形参**：条目实际 TTL 是写入时存进 `(ts, entry_ttl, value)` 的那个值，读取只认它；传 `ttl` 只让调用点意图可见。标签路径的 120 秒同样由 `_cache_put(..., ttl=_TAG_SEARCH_CACHE_TTL)` 落地。（该形参的处置见文末「遗留」。）

- [x] **Step 3: 摘要端点先做本地证据检查**

只读检查 `pixiv-api-http-main/` 与现存 `tests/fixtures/pixiv/`。只有发现明确的用户作品分页端点，且脱敏样本证明 `pixiv_id/title/user/page_count/thumb/upload_date/tags/bookmark_count/分页终止信息` 均可从规范解析得到时，才先在 `tests/test_pixiv_contract.py` 写红灯契约测试，再加 `endpoint_*`/`fetch_*` 到 `pixiv_client.py` 并让 `search_by_user` 使用它；`profile/all` 仍保持 ID 集合/游标来源，详情预算、过滤、缓存语义不变。不得为探索而运行真实 Cookie/真实 Pixiv 请求。

若参考实现与本地脱敏样本不能证明完整字段，明确判定 no-go：不加新 endpoint，继续现有逐详情路径，并在实施收尾记录“未发现满足字段的离线契约证据”。

**判定：no-go（未发现满足字段的离线契约证据）**。逐条依据：

1. `pixiv-api-http-main/core/api/app.js` 的路由表只有 illust / manga / novel / search / follow 五组，**没有任何用户维度的作品列表路由**；`core/api/module/user/pid.js` 是 **0 字节空文件**；全仓 grep `profile` 与 `user/` **零命中** —— 参考实现连 `profile/all` 都没有，更没有“用户作品分页端点”这一形态。
2. `tests/fixtures/pixiv/user_profile_all.json` 的 `body.illusts` 值**全是空对象**，只有键（作品 id）可用，证明不了 `title/page_count/thumb/upload_date/tags/bookmark_count` 任何一项，更没有分页终止信息。
3. 带摘要字段的既有样本（`search_illustrations.json`、`discovery_artworks.json`、`follow_latest.json`）分别属于关键词搜索、发现页、关注流，都不是按用户的作品分页。
4. 现有样本全部是**手工编写**的，达不到“脱敏样本证明全部必需字段可从规范解析得到”这条杠。

不加 `endpoint_*` / `fetch_*`，`pixiv_client.py`、`tests/test_pixiv_contract.py`、`tests/fixtures/pixiv/` 一字未改。

- [x] **Step 4: 跑缓存与契约回归并提交**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_fetcher.py tests\test_settings_api.py tests\test_pixiv_contract.py -q`
Expected: 缓存 TTL、屏蔽标签立即失效、用户 600 秒指纹缓存与适配层契约用例全 PASS；摘要 endpoint 若 no-go，则既有契约测试仍全 PASS且不新增真实网络依赖。

```bash
git add fetcher.py tests/test_fetcher.py tests/test_settings_api.py tests/test_pixiv_contract.py tests/fixtures/pixiv pixiv_client.py
git commit -m "perf: 缩短重复标签搜索等待并守住缓存边界"
```

若摘要 endpoint 判定 no-go，提交时只 add 实际修改文件，不要添加未修改的 conditional 路径。

**实施记录（Task 7 回写）**

| 项 | 实测 |
|---|---|
| commit | `ba47c9b` `perf: 缩短重复标签搜索等待并守住缓存边界`（实际只动 `fetcher.py` +38/-4、`tests/test_fetcher.py`、`tests/test_settings_api.py` 与执行笔记） |
| 新增用例 | **9 个**（`tests/test_fetcher.py` +7：`test_tag_cache_ttl_is_120s`、`test_empty_tag_page_keeps_short_ttl`、`test_discovery_cache_keeps_30s_ttl`、`test_following_cache_keeps_30s_ttl`、`test_budget_exhausted_tag_page_not_cached`、`test_budget_exhausted_discovery_page_not_cached`、`test_upstream_auth_error_not_cached`；`tests/test_settings_api.py` +2：`test_adding_blocked_tag_clears_tag_cache`、`test_deleting_blocked_tag_clears_tag_cache`）。**执行笔记写的“7 例”、任务书写的“8 例”都不对**：`git diff ba47c9b^..ba47c9b -- tests/` 显示 9 个 `+def test_`、0 个 `-def test_`，`git show 89137bc:tests/test_fetcher.py` 里这 9 个名字一个都不存在，`pytest --collect-only` 也正好收 9 条（无参数化）。 |
| 计数更正 | 该时点全量 **688 passed / 2 skipped / 4 failed(env) = 694 collected**（笔记里的“686 passed”是同一提交内少算了 2 条用例时的中途数字）。 |

## Task 7：全量验证、测量并回写设计记录

**Files:**
- Modify: `docs/superpowers/specs/2026-09-29-search-throughput-design.md`
- Modify: `docs/superpowers/plans/2026-09-29-search-throughput.md`

- [x] **Step 1: 跑全量离线测试和空白检查**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q`
Expected: 全量默认套件 0 failures/0 errors；用例总数为基线 601 加本计划新增用例。默认测试不访问真实 Pixiv、不读取仓库真实 Cookie。

Run: `git diff --check`
Expected: 无空白错误。

**实测（2026-09-29，HEAD `ba47c9b`）**

```
$env:TEMP = Join-Path $env:LOCALAPPDATA 'Temp\dsh-manual'; $env:TMP = $env:TEMP
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q
→ 4 failed, 688 passed, 2 skipped in 20.91s        （694 collected）
git diff --check → 无输出，exit 0
```

| 计数 | 基线 `d961824`（同环境实测） | HEAD `ba47c9b` |
|---|---|---|
| collected | 601 | 694 |
| passed | 595 | 688 |
| skipped | 2（`test_auth.py` POSIX chmod、`test_settings_api.py` POSIX chmod） | 2（同两条） |
| failed | 4（`test_test_setup.py::test_temp_root_*`，沙箱环境噪声） | 4（同 4 条） |

- 本计划新增 **93** 个 collected 用例（76 个新测试函数 + 参数化展开）：Task 1 22 函数、Task 2 15、Task 3 18、Task 4 12、Task 6 9。
- 4 个失败是**环境噪声**，不是回归：`tests/test_test_setup.py` 在本分支**未被改动过**（`git diff --stat d961824..HEAD` 里没有它），失败原因是该测试 spawn 的子 PowerShell 在沙箱下语言模式不同（`subprocess.CalledProcessError: ... [IO.Path]::GetTempPath()` 返回非 0），基线同环境同样失败 4 条（已用 `d961824` 临时 worktree 实跑复核）。**已按要求记录，不在本任务修复。**

- [x] **Step 2: 记录 mock 性能对比**

用同一组固定延迟 fake Session 比较执行前基线与新实现，记录：首条确认结果可轮询时间、完整页时间、详情 attempt 数、成功详情吞吐（成功数/分钟）、cache hit 数、403 率、429 数、峰值在途详情数。验收：首结果在任务 done 前出现；峰值在途不超过 2；429/连续 403 后不再启动新详情请求；速率常量没有上调；完整冷画师搜索只有在摘要 endpoint 有契约证据并减少详情请求时才要求总耗时下降。

**脚手架（一次性，未入库，跑完已删）**：`%TEMP%\t7-harness\harness.py`。用真 `fetcher`/`pixiv_client` 链路（含 gate、令牌桶），只替换 `fetcher.get_pooled_session` / `fetcher.build_pixiv_session` 为同一个 fake Session：固定每题 **50ms** 延迟、`status` 可切 200 / 403 / 429（429 带 `Retry-After: 30`），直接按 URL 末段取 pid；作者 `profile/all` 走进程内缓存，DB 用临时 `PIXIV_INSTANCE_DIR` 冷启动。基线 = `d961824` 的 `fetcher.py`+`pixiv_client.py` 副本（`has_gate: false`，脚注里打印模块路径证明加载的是哪一份）。搜索调用链与路由一致：`fetcher.paginated_search(search_fn=search_by_user, ..., detail_budget=ITEMS_PER_PAGE*2)`，24 个作者作品 id。首条可轮询结果的判据 = `progress.accepted` 第一次 ≥ 1 的时刻。

**实测（24 件，每题 50ms）**

| 场景 | 版本 | 首条可轮询结果 | 完整页 done | 详情 HTTP 次数 | 峰值在途 | collected results |
|---|---|---|---|---|---|---|
| 真令牌桶（45/20/60） | **新** | **0.058 s** | **30.74 s** | 24 | 1 | 24 |
| 真令牌桶 | 旧 `d961824` | 30.74 s（协议上 = done） | 30.74 s | 24 | 1 | 24 |
| 快桶（间隔 5 ms，让 gate 成为唯一约束） | **新** | **0.058 s** | **0.62 s** | 24 | **2** | 24 |
| 快桶 | 旧 | 0.28 s（= done） | 0.28 s | 24 | **5** | 24 |
| 全 403 | **新** | —（0 件通过） | 9.34 s（`SearchRateLimitedError` → `partial`） | **3**（PID 700000/1/2 各一次） | 1 | 0 |
| 全 403 | 旧 | — | 64.77 s | **72**（24×3 次退避重试） | 1 | 0 |
| 全 429（`Retry-After: 30`） | **新** | — | 6.68 s | **1** | 1 | 0 |
| 全 429 | 旧 | — | 64.77 s | **72** | 1 | 0 |

读法：

- **首结果**：第一批详情在 0.0578 s 就完成了，新实现 0.0582 s 就把它发布出去 —— 发布本身不引入可观测延迟。旧实现在结构上没有任何增量通道，`/api/search/status` 到 done 才第一次给出 results，所以“首条可轮询结果”== 30.74 s。**首结果提前约 530 倍，完整页时间不变**（30.74 s vs 30.74 s；这正是设计的非目标：不承诺缩短冷画师搜索的完成时间）。
- **峰值在途**：真令牌桶下 45/分钟（1.333 s 间隔）配 50 ms 服务时间，请求根本不会重叠，峰值恒为 1 —— 令牌桶是更紧的约束。把桶换成 5 ms 间隔（仅脚手架内替换，不改常量）后，新实现峰值正好 **2**（`_DETAIL_MAX_IN_FLIGHT`），旧实现 **5**（`FETCH_DETAIL_WORKERS`）。
- **限流后不再发新请求**：全 403 时第 3 个不同 PID 的 403 在 60 s 窗口内触发开路，之后所有 attempt 在闸前被拒 → 总 HTTP 恰好 3，9.34 s 后以 `partial` 收尾（旧实现 72 次、64.77 s）。全 429 时第 1 个响应就开路 → 总 HTTP 恰好 1。
- **速率常量未上调**：脚手架每次运行都回读 `pixiv_client.DETAIL_RATE_PER_MINUTE/FILL_RATE_PER_MINUTE/TOTAL_RATE_PER_MINUTE = 45/20/60`、`config.FETCH_DETAIL_WORKERS = 5`、`_DETAIL_MAX_IN_FLIGHT = 2`。
- **成功详情吞吐 / cache hit / 403 率 / 429 数**：本脚手架只测首结果与限流路径的对比，未覆盖真实上游吞吐与缓存命中率（缓存需要多次搜索同一条件，且真实吞吐受真实网络延迟支配，mock 里没有意义）。⇒ 见「仍未验证」。

**验收判据逐条落点**

| 判据 | 结论 | 实测 / 钉住的测试 |
|---|---|---|
| 首结果出现在任务 done **之前** | ✅ | 实测：新 0.058 s vs done 30.74 s（快桶 0.058 s vs 0.62 s）；`tests/test_app.py::test_running_snapshot_then_done_replaces_preview_with_canonical`、`tests/test_fetcher.py::test_existing_record_published_before_detail_batch_completes`、`test_detail_progress_callback_runs_in_collector_thread` |
| 峰值在途详情 ≤ **2** | ✅ | 实测（快桶）新 2 / 旧 5；`tests/test_pixiv_client_limits.py::test_at_most_two_requests_in_flight`（barrier 并发）、`test_slot_is_released_when_body_raises` |
| 429 之后不再启动新详情请求 | ✅（闸内） | 实测（fake Session 把 429 交给应用层，即 `PIXIV_BASE_URL=http://` / 改 `status_forcelist` 那条路径）总 HTTP = 1；`tests/test_pixiv_client_limits.py::test_observed_429_opens_gate_and_suppresses_further_attempts`、`test_429_retry_after_delta_seconds_sets_cooldown`。⚠️ **默认 https 路径下 urllib3 在传输层吃掉 429，闸收不到**（见「遗留」） |
| 3 个不同 403 之后不再启动新详情请求 | ✅ | 实测总 HTTP = 3；`tests/test_pixiv_client_limits.py::test_three_distinct_403_open_gate_and_next_call_refused_before_http`、`test_three_distinct_403_open_gate`、`test_single_pid_repeated_403_does_not_open_gate`、`test_403_outside_the_sixty_second_window_does_not_count` |
| 速率常量没有被上调 | ✅ | 实测回读 45/20/60；`tests/test_pixiv_client_limits.py::test_detail_rate_constants_are_not_raised` |
| 限流时以 `partial` 收尾、不推进游标、不写成功缓存 | ✅ | `tests/test_app.py::test_rate_limited_finishes_partial_keeping_preview`、`test_partial_keeps_input_cursor`、`tests/test_fetcher.py::test_rate_limited_result_not_cached`、`test_budget_exhausted_result_not_cached` |
| 冷画师搜索总耗时不必下降 | — | 遵守：30.74 s → 30.74 s（摘要 endpoint no-go，无减少详情请求的手段） |

- [x] **Step 3: 回写状态并做最终提交**

把真实完成范围、测试命令/结果、摘要 endpoint go/no-go、性能指标写回 spec 的状态区；勾完本计划对应步骤。只在所有离线测试通过后执行：

```bash
git add docs/superpowers/specs/2026-09-29-search-throughput-design.md docs/superpowers/plans/2026-09-29-search-throughput.md
git commit -m "docs: 记录搜索渐进提速实现与验证结果"
```

→ 本次提交除计划与 spec 外，还包含 `docs/technical-documentation.md`（陈旧类型行修正）、`docs/superpowers/plans/2026-09-29-search-throughput-execution-notes.md`（标注「已并入」+ 计数更正），故实际用 `git add docs/`。

## 遗留（Task 7 记录，**本次不修**）

1. **真实 429 / 传输层重试**：适配层 `Retry(total=1, connect=0, status_forcelist=[429,500,502,503])` 把真实 429 在 urllib3 里拦下并重试，耗尽后 requests 抛**不带 `.response`** 的 `RetryError`，于是应用层看到“其他”（退避 1 s）、`observe_response(429)` 不触发、闸不为 429 开路；更糟的是传输层会在**持有 gate 槽位期间**自己睡 `Retry-After`（实测每 fetch 约 11 s），把“某个线程的停顿”变成“全局 cap-2 的停顿”。修它要碰文档里的 62 s 放大不变量（`status_forcelist` 去掉 429，或 `respect_retry_after_header=False`），需要单独决策；`tests/test_fetcher.py::test_session_does_not_retry_connect_errors` 已把传输层三项配置钉住。
2. **`_cache_get(key, ttl=...)` 的 `ttl` 形参是惰性的**（有效 TTL 是写入时存下的那个）—— 既有行为，不属本次改动。倾向**删掉这个形参**，而不是继续加“看起来会生效”的调用点。
3. **浏览器 smoke（Task 5 Step 3）从未执行**：本环境无浏览器；前端逻辑只由一次性 Node `vm` + fake-DOM 探针验证（54/54）。真实 CSS/交互未验证，步骤已写进 Task 5 供人工执行。
4. **`fetcher.py` 已 1282 行**：publisher/缓存管线是否该拆成独立模块，是计划层面提出、本次**明确推迟**的问题（拆分风险与收益另评）。

## 计划自审映射

| 批准规格要求 | 对应任务 |
|---|---|
| 运行中展示已过滤结果；最终游标仍只在完成后提交 | Task 3、4、5 |
| 复用缓存、评估用户作品摘要 API；缺证据时不猜端点 | Task 6（**判定 no-go**） |
| 全局 403/429 冷却；部分结果不冒充完整、不缓存 | Task 1、2、3、4、6 |
| 保持 45/20/60 速率、作者 24 条切片、详情预算与既有取消行为 | Task 2、3、7（实测回读 45/20/60） |
| 部分状态清理、revision、PID 去重、manual UI mock 验收 | Task 4、5（**手工 UI 验收未执行**，步骤已补写）、7 |
| 离线验证并记录首结果时间、完整页时间、请求数与限流数 | Task 1–7（数字见 Task 7 Step 2） |
