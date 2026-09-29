# Pixiv 搜索渐进提速实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 `subagent-driven-development`（推荐）或 `executing-plans`，按任务执行并逐段审查。步骤用 checkbox 追踪。

**Goal:** 在不提高 Pixiv 详情请求速率的前提下，提前展示已通过筛选的搜索结果，并在全局限流时安全地返回部分结果。

**Architecture:** 详情请求熔断和在途上限归 `pixiv_client.py`；`fetcher.py` 负责逐详情结果回调、过滤、预算、分页和缓存；`routes_search.py` 在锁内维护可轮询的任务快照；`static/page-index.js` 仅显示 provisional 预览，完整页和游标仍只在任务完成时提交。限流造成的 `partial` 终态不推进游标、不写成功缓存。

**Tech Stack:** Python 3.13、Flask、requests、SQLAlchemy/SQLite、原生 ES2020 JavaScript、pytest；所有默认测试离线运行，经 `scripts/run_tests.ps1` 执行。

---

## 执行前置条件

1. 当前工作区的 Pixiv client 适配层改造（`pixiv_client.py`、`fetcher.py`、fixtures 和契约测试）尚有未提交文件。**不要在当前 dirty `main` 上实施本计划**；等适配层改造稳定并提交后，按 `using-git-worktrees` 技能从包含该提交的干净基线上创建 `feature/search-throughput` worktree。
2. 实施前先读该基线的 `AGENTS.md` 与 `docs/architecture.md`，再运行下面的基线命令。保留 `app.py` 中被测试补丁的 from-import seam；路由必须继续在函数体内延迟 `import app` 后引用这些符号。
3. 详情桶常量保持 `DETAIL_RATE_PER_MINUTE=45`、`FILL_RATE_PER_MINUTE=20`、`TOTAL_RATE_PER_MINUTE=60`。不使用代理/IP、账号或 Cookie 轮换，不在默认测试里读真实 Cookie，也不运行未经显式授权的真实 Pixiv 请求。
4. 目标模块职责稳定：`pixiv_client.py` 不导入 DB、不认识 `Illust`；`fetcher.py` 不拼 Ajax URL、不读取 Pixiv payload 原始字段；`runtime._search_tasks` 保持 `-w 1` 单进程语义。
5. 在新 worktree 中先保存基线：运行 `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_fetcher.py tests\test_app.py -q` 与 `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q`；记录完整基线是否通过。当前代码在 status 终态前不公开结果，因此基线首结果时间等于任务完成时间。实施后用同一固定延迟 fake Session 记录首结果与完整页时间，和该行为基线比较。

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
| `tests/test_pixiv_contract.py`（仅摘要 API 有证据时） | 新端点 URL、脱敏 payload 字段和规范解析契约。 |
| `docs/superpowers/specs/2026-09-29-search-throughput-design.md` | 所有实现验证后标记“已实现”，写入实际验证和未完成的摘要接口结论。 |

**不改 `runtime.py` 的模块边界**：搜索任务字典已由 `routes_search.py` 构造，增量字段放在该任务对象中即可；除非基线代码已把状态结构收口到 `runtime.py`，否则不移动状态。

## 任务依赖顺序

为遵循已批准设计中的实施顺序，Task 1 只实现并测试尚未接入请求路径的 gate 状态机；随后先完成渐进进度与 UI（Task 3 → Task 4 → Task 5），最后在已有 `partial` 处理链后接通 gate（Task 2）。**实际执行顺序：Task 1 → Task 3 → Task 4 → Task 5 → Task 2 → Task 6 → Task 7。**这样在新熔断开始影响搜索前，限流的部分结果/游标/UI 语义已经完成并有回归测试。

## Task 1：建立离线详情 gate 的红灯测试

**Files:**
- Create: `tests/test_pixiv_client_limits.py`
- Modify: `pixiv_client.py`（下一步实现）

- [ ] **Step 1: 写 gate 的失败测试**

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

- [ ] **Step 2: 运行 gate 测试并确认红灯**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_pixiv_client_limits.py -q`
Expected: FAIL，因为 `_DetailRequestGate` 与 `PixivRateLimitedError` 尚不存在。

- [ ] **Step 3: 实现 gate 状态机**

在 `pixiv_client.py` 增加 `PixivRateLimitedError` 和 `_DetailRequestGate`，接口固定为：`request_slot(pixiv_id)` context manager、`observe_response(pixiv_id, status, retry_after=None)`、只读属性 `is_open`。实现规则：

1. context manager 内用 `BoundedSemaphore(2)` 限制真实详情 HTTP 在途请求数，`finally` 必须释放；令牌桶等待放在拿 semaphore 之前，不能持有槽位睡眠。
2. 429 立即开路；以 `Retry-After` 的 delta-seconds/HTTP-date 为准，值无效或缺失时用 60 秒。
3. 仅当连续 60 秒窗口内有 3 个不同 PID 的详情 403 才开路；单个作品重复 403 不触发全局熔断，任一非 403 HTTP 状态（含 2xx、401、404、5xx）清除连续 403 集合，但 401/404/5xx 的原错误分类不变。
4. 冷却后仅一个调用可半开探测；探测收到非 403/429 的 HTTP 响应后恢复并清除失败集合/退避级数，探测再次受限则冷却翻倍，最大 900 秒。
5. gate 状态更新和“读→判定→写”在同一锁内完成；不持有锁进行网络 I/O。

- [ ] **Step 4: 重跑 gate 测试并提交**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_pixiv_client_limits.py -q`
Expected: 全部新增 gate 用例 PASS，无真实网络请求。

```bash
git add pixiv_client.py tests/test_pixiv_client_limits.py
git commit -m "fix: 为 Pixiv 详情请求增加共享熔断闸"
```

## Task 2：把 gate 接入详情请求并保留既有限速语义

**Files:**
- Modify: `pixiv_client.py`
- Test: `tests/test_pixiv_client_limits.py`、`tests/test_fetcher.py`

- [ ] **Step 1: 写详情请求集成失败测试**

对 `fetch_illust_detail` 使用 fake Session 和可计数 limiter，断言每次真实 attempt（包含 403/429 退避重试）都先通过详情桶、总桶和 gate；401 仍抛 `PixivAuthError`，404 仍是永久删除语义，连接错误仍 fail-fast。另断言单次 403 不会熔断，429/连续不同 PID 403 打开 gate 后，下一次详情调用在发出 HTTP 前得到 `PixivRateLimitedError`。

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_pixiv_client_limits.py tests\test_fetcher.py -q`
Expected: 新的 gate 集成断言 FAIL；既有分类测试仍运行并暴露任何兼容问题，无 `-k` 漏测风险。

- [ ] **Step 2: 最小接入 `fetch_illust_detail`**

在 `fetch_illust_detail` 的每一次 HTTP attempt 前依次等待传入的 detail/fill limiter 与 `_total_limiter`，之后才进入 gate 的 `request_slot(pixiv_id)`。每次 retry 都重新取令牌。将状态码和 `Retry-After` 交给 `observe_response` 后再执行 `raise_for_status()`；连接错误、401、404、其他 5xx 的原分类不变。打开的 gate 不得再发逐条 3/9 秒限流 retry。

`return_dead=True` 的刷新路径仍将全局限流映射为 `RETRYABLE_GLOBAL_DETAIL`，不能写入单作品失败退避；搜索路径则抛 `PixivRateLimitedError`，不得把它降级成 `None`。

- [ ] **Step 3: 运行 client 与详情回归并提交**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_pixiv_client_limits.py tests\test_fetcher.py -q`
Expected: 详情重试分类、`return_dead` 哨兵、限速常量及新 gate 测试全 PASS；速率常量仍为 45/20/60。

```bash
git add pixiv_client.py tests/test_pixiv_client_limits.py tests/test_fetcher.py
git commit -m "fix: 让详情重试共享速率闸与退避状态"
```

## Task 3：让详情流水线逐条发布已过滤结果并保留限流结果

**Files:**
- Modify: `fetcher.py`
- Test: `tests/test_fetcher.py`

- [ ] **Step 1: 写详情批次和 progress 回调失败测试**

新增用例验证：`_fetch_details_parallel` 在消费线程按 future 完成顺序回调 `(pixiv_id, detail)`；`_process_items` 只对通过收藏数、R18 和屏蔽标签过滤的记录调用 `on_result`；一个 PID 的 callback 只调用一次；触发 `PixivRateLimitedError` 后已完成详情仍被处理，但批次标为 rate-limited；`PixivAuthError` 从 future 原样传播，不可被宽泛 `except Exception` 静默转成详情失败。用例命名为 `test_detail_progress_callback_runs_in_collector_thread`、`test_process_items_publishes_only_filter_matches`、`test_rate_limited_batch_keeps_completed_details`、`test_parallel_detail_propagates_auth_error`。

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_fetcher.py -q`
Expected: 新增 callback/限流/AuthError 用例 FAIL，因为当前 `_fetch_details_parallel` 只返回最终 `(details, attempted)`，`_process_items` 没有结果回调；不依赖 `-k` 过滤式。

- [ ] **Step 2: 加入明确的批次结果类型**

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

- [ ] **Step 3: 在 `_process_items` 发布规范结果**

给 `_process_items`、`search_by_tag`、`search_by_user`、`browse_discovery`、`paginated_search` 增加默认 `None` 的可选 `progress` callback；`_fetch_details_parallel` 的 `on_detail(pid, detail)` 在 collector 线程报告每个完成详情（含失败）。`progress(event)` 使用三种明确事件：`{"type":"examined","pixiv_id":pid}`、`{"type":"detail_failed","pixiv_id":pid}`、`{"type":"result","result":dict}`；每个输入 PID 的 examined 恰好一次，只有通过路径所需过滤后才发 result。

- 已存在且通过全部过滤的记录可立即发布。
- 新详情在 `_fetch_details_parallel` 的 collector/search 线程回调；在发布前用既有 `blocked`、`min_bookmarks`、`hide_r18` 判定。不要把 SQLAlchemy session 交给 worker。
- favorite 状态用调用线程一次性取得的 `get_favorite_pids(db)` 标记；沿用 `_mark_favorites` 的字段语义。
- `defer_details` 继续直接用条目摘要；`min_bookmarks==0` 的标签路径不增加详情请求。
- callback 的候选只是 preview；`fetcher._cancelled()` 为真时禁止继续发布旧任务进度，但已完成作品仍按现有事务入库语义收尾。最终列表继续走 `_insert_new_illusts` 和现有 `safe_commit(db)` 批量入库。

- [ ] **Step 4: 明确率限流状态并阻止残缺缓存**

在 `_process_items` 中定义 `_ProcessedItems`，保持原有 list 迭代/JSON 输出兼容：

```python
class _ProcessedItems(list):
    def __init__(self, items=(), *, rate_limited=False):
        super().__init__(items)
        self.rate_limited = rate_limited
```

构造时把 `_DetailFetchBatch.rate_limited` 写入该属性。调用方完成 `safe_commit(db)` 后，若 `rate_limited`，抛 `SearchRateLimitedError`；`paginated_search` 必须显式重新抛出它，不能落入“普通页失败后当空页完成”的宽泛 `except`。`search_by_tag`、`browse_discovery`、`search_by_user` 在抛出前不得写成功缓存。

新增缓存测试：完整搜索仍可缓存；限流 partial 和中途详情预算耗尽均不缓存；重复完整画师搜索仍命中 600 秒缓存及屏蔽指纹。

- [ ] **Step 5: 跑 fetcher 回归并提交**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_fetcher.py -q`
Expected: 详情并发、连接池、过滤、收藏、详情预算、游标与新增 progress/partial 用例全 PASS。

```bash
git add fetcher.py tests/test_fetcher.py
git commit -m "perf+fix: 搜索详情逐条发布并标记限流结果"
```

## Task 4：发布一致的搜索任务快照和 `partial` 终态

**Files:**
- Modify: `routes_search.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: 写 running snapshot 与 partial 失败测试**

新测试直接提交一个带 `progress_callback` 的任务：worker 先发布一条结果，再由 `threading.Event` 阻塞；测试在阻塞期间请求 status，断言 `status=running`、该结果可见、revision 大于 0、complete 为 false；放行后断言最终为 done 且最终结果是 canonical 页。

再测试 `SearchRateLimitedError` 使任务以 HTTP 200 的 `partial` 终态返回已有 preview、`complete=false` 和限流 warning；输入 cursor 不前移，`has_more` 不被伪造；`partial` 到 TTL 后可清理。

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_app.py -q`
Expected: 新增 running progress、partial、cleanup 用例 FAIL，既有 `tests/test_app.py` 搜索/认证回归同时运行。

- [ ] **Step 2: 在 `_submit_search_task` 维护带锁快照**

任务字典初始化 `revision=0`、`progress={examined, accepted, detail_failed}`、`complete=False`、`warning=None`、`results=[]`，并保存 `input_cursor=cursor_str or None`。publisher 按 `progress` 事件类型在 `_search_tasks_lock` 内更新 counters；result 事件按 PID 去重，最多追加 `ITEMS_PER_PAGE` 个 preview，并更新 accepted 和 revision。网络 I/O 不持有该锁，写入任务的每条 result 使用新 dict 快照。

让任务函数接收可选 publisher；`search()` 中 `_browse_fn`、`_tag_fn`、`_user_fn` 将 publisher 沿 `app.paginated_search` 和对应 `app.search_by_*` 闭包传入，保持 `app.*` 延迟导入补丁契约。任务成功时用最终 canonical 结果替换 preview、填 cursor/has_more、complete=true；捕获 `SearchRateLimitedError` 时保留当前 preview、将任务 cursor 设回 `input_cursor`、complete=false、status=partial。

- [ ] **Step 3: 锁内复制 status response 并纳入 partial 清理**

`search_status` 在 `_search_tasks_lock` 内将 status、results 的 list copy、cursor、has_more、fetch_stats、revision、progress、complete、warning 复制到局部快照，解锁后再 `jsonify`。保留既有 error/401/502 响应。`_cleanup_search_tasks` 终态集合增加 `partial`。

- [ ] **Step 4: 运行路由回归并提交**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_app.py -q`
Expected: 搜索入参、`app` monkeypatch seam、取消、认证错误、TASK_LOST、TTL 清理与 running/partial 新用例全 PASS。

```bash
git add routes_search.py tests/test_app.py
git commit -m "feat: 搜索任务状态支持增量结果与限流部分页"
```

## Task 5：前端展示临时 preview，保持分页提交语义

**Files:**
- Modify: `static/page-index.js`
- Test: 本地 mock API 浏览器验收；服务端 API 行为由 Task 4 自动测试覆盖。

- [ ] **Step 1: 实现独立 preview 状态和 poll 回调**

增加 `searchPreview=[]`、`searchPreviewIds=new Set()` 与 `lastSearchRevision=0`，不要把运行中的结果写入 `loadedPages`。扩展 `pollSearch(taskId, onDone, onFail, gen, onProgress, onPartial)`：running 响应先调用 `onProgress(data)` 再按原间隔轮询；revision 未增长时不重复渲染；旧 generation 仍立即退出；done 调 `onDone`，partial 调 `onProgress` 后单独调 `onPartial`，不能落入 `finishSearch`。同步更新 `doSearch` 与 `loadNextPage` 两处 `pollSearch` 调用，传入 preview handler 和 partial handler。

在卡片更新处对 `loadedPages` 与 `searchPreviewIds` 按 `pixiv_id` 去重并 `renderInChunks`；更新 `paginationStatus` 显示“已找到 N 件，仍在筛选”。preview 不调用 `saveSearchState()`，不修改 `nextCursor`、`hasMore` 或 `currentPage`。

- [ ] **Step 2: 完成/部分/取消时分别收口 UI**

`doSearch` 开始新 generation 时清空 `searchPreview`/`searchPreviewIds`；取消和旧 generation 不得继续写 preview。`done` 调用现有 `finishSearch`，先清除 preview 再渲染 canonical 最终页，随后才更新 `loadedPages`、cursor、分页按钮和 `pvCache`。`partial` 保留当前已确认 preview、停止 loading、显示 `warning`，不把 preview 提交为页或写完整缓存；原有 `nextCursor` 不变：首次搜索没有 cursor 时下一页保持禁用，翻页中断时恢复按钮以便用户冷却后重试同一个 cursor。

- [ ] **Step 3: 用本地 mock API 手工验证，无新增 JS 工具链**

启动开发服务，使用仅在 DevTools console 中临时替换的 `window.fetch` mock `/search` 与 `/api/search/status/*`，不改应用文件、不请求 Pixiv。status 依次返回 revision 1（PID 11）、revision 2（PID 11+12）、done（canonical PID 12+11）；结果字典至少包含 `pixiv_id/title/user_id/user_name/page_count/bookmark_count/thumb_url/tags`。确认运行时网格按增量更新且无重复，done 后以 canonical 顺序替换。再验证 `partial` 显示 warning：首次搜索保持下一页禁用；翻页中断时保留原 `nextCursor` 并允许重试同一页；刷新页面不会恢复 partial preview。确认生产代码 CSP 下无 inline script、`eval` 或新构建依赖。

- [ ] **Step 4: 检查差异并提交**

Run: `git diff --check`
Expected: 无空白错误；手工 smoke 的三种终态符合预期。

```bash
git add static/page-index.js
git commit -m "feat: 搜索中渐进展示已确认作品"
```

## Task 6：缓存边界与用户摘要接口 go/no-go

**Files:**
- Modify: `fetcher.py`、`tests/test_fetcher.py`、必要时 `tests/test_settings_api.py`
- Conditional modify: `pixiv_client.py`、`tests/test_pixiv_contract.py`、`tests/fixtures/pixiv/`

- [ ] **Step 1: 给标签 TTL 与屏蔽标签失效写失败测试**

用 fake clock 测试标签缓存的专用 TTL 为 120 秒，TTL 内同条件查询不再打上游；TTL 到期才重新请求。通过 `routes_settings` 的屏蔽标签增删路径验证 `clear_search_cache()` 立即生效。发现页/关注页不得被无意改成 120 秒 TTL。

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_fetcher.py tests\test_settings_api.py -q`
Expected: 新 TTL/失效断言 FAIL，现有用户搜索缓存测试保持通过；不依赖可能返回零用例的 `-k` 过滤式。

- [ ] **Step 2: 实现独立 tag TTL 和完整缓存规则**

新增 `_TAG_SEARCH_CACHE_TTL = 120.0`，仅 `search_by_tag` 的 `_cache_get/_cache_put` 使用该 TTL；`_SEARCH_CACHE_TTL` 仍控制 discovery/following 等其它结果。屏蔽标签改动继续通过 `clear_search_cache()` 全清。`SearchRateLimitedError`、详情预算耗尽、认证/上游请求异常都不能写入成功缓存；不得改变作者 600 秒缓存键中的 `_blocked_fingerprint`。

- [ ] **Step 3: 摘要端点先做本地证据检查**

只读检查 `pixiv-api-http-main/` 与现存 `tests/fixtures/pixiv/`。只有发现明确的用户作品分页端点，且脱敏样本证明 `pixiv_id/title/user/page_count/thumb/upload_date/tags/bookmark_count/分页终止信息` 均可从规范解析得到时，才先在 `tests/test_pixiv_contract.py` 写红灯契约测试，再加 `endpoint_*`/`fetch_*` 到 `pixiv_client.py` 并让 `search_by_user` 使用它；`profile/all` 仍保持 ID 集合/游标来源，详情预算、过滤、缓存语义不变。不得为探索而运行真实 Cookie/真实 Pixiv 请求。

若参考实现与本地脱敏样本不能证明完整字段，明确判定 no-go：不加新 endpoint，继续现有逐详情路径，并在实施收尾记录“未发现满足字段的离线契约证据”。

- [ ] **Step 4: 跑缓存与契约回归并提交**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests\test_fetcher.py tests\test_settings_api.py tests\test_pixiv_contract.py -q`
Expected: 缓存 TTL、屏蔽标签立即失效、用户 600 秒指纹缓存与适配层契约用例全 PASS；摘要 endpoint 若 no-go，则既有契约测试仍全 PASS且不新增真实网络依赖。

```bash
git add fetcher.py tests/test_fetcher.py tests/test_settings_api.py tests/test_pixiv_contract.py tests/fixtures/pixiv pixiv_client.py
git commit -m "perf: 缩短重复标签搜索等待并守住缓存边界"
```

若摘要 endpoint 判定 no-go，提交时只 add 实际修改文件，不要添加未修改的 conditional 路径。

## Task 7：全量验证、测量并回写设计记录

**Files:**
- Modify: `docs/superpowers/specs/2026-09-29-search-throughput-design.md`
- Modify: `docs/superpowers/plans/2026-09-29-search-throughput.md`

- [ ] **Step 1: 跑全量离线测试和空白检查**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q`
Expected: 全量默认套件 0 failures/0 errors；用例总数为基线 601 加本计划新增用例。默认测试不访问真实 Pixiv、不读取仓库真实 Cookie。

Run: `git diff --check`
Expected: 无空白错误。

- [ ] **Step 2: 记录 mock 性能对比**

用同一组固定延迟 fake Session 比较执行前基线与新实现，记录：首条确认结果可轮询时间、完整页时间、详情 attempt 数、成功详情吞吐（成功数/分钟）、cache hit 数、403 率、429 数、峰值在途详情数。验收：首结果在任务 done 前出现；峰值在途不超过 2；429/连续 403 后不再启动新详情请求；速率常量没有上调；完整冷画师搜索只有在摘要 endpoint 有契约证据并减少详情请求时才要求总耗时下降。

- [ ] **Step 3: 回写状态并做最终提交**

把真实完成范围、测试命令/结果、摘要 endpoint go/no-go、性能指标写回 spec 的状态区；勾完本计划对应步骤。只在所有离线测试通过后执行：

```bash
git add docs/superpowers/specs/2026-09-29-search-throughput-design.md docs/superpowers/plans/2026-09-29-search-throughput.md
git commit -m "docs: 记录搜索渐进提速实现与验证结果"
```

## 计划自审映射

| 批准规格要求 | 对应任务 |
|---|---|
| 运行中展示已过滤结果；最终游标仍只在完成后提交 | Task 3、4、5 |
| 复用缓存、评估用户作品摘要 API；缺证据时不猜端点 | Task 6 |
| 全局 403/429 冷却；部分结果不冒充完整、不缓存 | Task 1、2、3、4、6 |
| 保持 45/20/60 速率、作者 24 条切片、详情预算与既有取消行为 | Task 2、3、7 |
| 部分状态清理、revision、PID 去重、manual UI mock 验收 | Task 4、5、7 |
| 离线验证并记录首结果时间、完整页时间、请求数与限流数 | Task 1–7 |
