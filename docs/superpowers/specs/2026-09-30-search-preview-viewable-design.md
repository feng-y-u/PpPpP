# Pixiv Viewer 搜索预览可查看设计

## 状态

**已实现并验证**（2026-09-30）：

| 改动 | 位置 |
|---|---|
| `find_running_preview`（锁内查 running 任务快照、取最新、返回副本） | `runtime.py` |
| 详情页查库未命中时用快照兜底 + `preview` 传给模板 | `routes_gallery.detail_page` |
| 预览态提示条 + 下载按钮 disabled | `templates/detail.html` |
| 预览卡点击跳详情、文案改为「可预览 · 暂不可下载」 | `static/page-index.js` |

验证：`tests/test_app.py::TestFindRunningPreview`（4 条）+ `::TestDetailPagePreviewFallback`（7 条，含一条走**真实 publisher** 的端到端用例）全绿；全量收集 682+8 条 = **684 passed / 2 skipped / 4 failed(env)** 约 21s，4 个失败仍是 `tests/test_test_setup.py::test_temp_root_*` 的沙箱环境性失败（与基线同款）。

**回写时的勘误（保留记录）**：计划里写的断言 `'preview-notice' in html` 是错的 —— 该字符串在 `detail.html` 的 `<style>` 里**恒在**，于是"DB 优先"用例的 `not in` 断言必失败。改为断言标记本身（`<div class="preview-notice">`）。这就是"断言选了一个恒真的字符串"的典型坑，在此留档。

## 背景与问题

搜索是异步的，且 `fetcher._process_items` 在每条详情到手的瞬间就通过 `progress` 发布 `result` 事件（`_publish_detail` / `_append_result`），前端 2 秒轮询一次就把它渲染成卡片 —— 首张卡片出现只要 0.06s，而任务 `done` 可能要到 30s 之后（作者搜索 / 带收藏数下限的标签搜索：一页 24 条详情，`DETAIL_RATE_PER_MINUTE=45`、单条 ≈1.33s；内部验收记录 `docs/superpowers/plans/2026-09-29-search-throughput.md` 实测 0.058s vs 30.74s）。

问题是这些卡片**点不开**：

1. 预览事件早于落库。真正的写入是 `_process_items` 末尾的 `_insert_new_illusts`，而 `safe_commit()` 在 `search_by_tag` / `search_by_user` / `browse_discovery` 里 —— 即**整页详情全部拉完之后**。这中间那些行只存在于搜索任务的内存快照（`runtime._search_tasks[task_id]['results']`）。
2. 所有"看"的接口都按 `pixiv_id` 查库：`/detail/<pid>`、`/api/detail/<pid>`、`POST /download/<pid>`、`/api/image/<pid>/<n>`。行没提交 → 一律 404。
3. 于是 2026-09-29 搜索吞吐改造把预览卡**刻意**做成只读：虚线描边 + 「筛选中」徽标、点击只弹「该作品仍在筛选中」、不渲染下载按钮、批量下载按 `data-preview-pid` 剔除。

用户视角就是："已经筛出来的作品不能看，非要等全部筛完。"快的部分（判定一条就发布）已经做了，卡住的是**提交粒度 = 整页**这个事务边界。

## 需求（用户已确认）

**让已经筛选出的预览作品立刻能打开看图**，不必等整页筛选完成。

## 目标

1. 点击预览卡能进入 `/detail/<pid>` 并看到图（缩略图/中图/原图链路照旧），不再只弹一句提示。
2. 不改变"未落库的行不可信"这条既有语义：**下载**仍然只在行落库后可用，且预览态必须**显式告知**用户为什么不能下载。
3. 不碰事务边界：不改 `_process_items` / `search_by_*` 的提交语义，不引入增量提交。

## 非目标

- **不做增量落库**（逐条 / 小批 commit 让预览卡直接变真卡）。那要把写入搬到 `_fetch_details_parallel` 的 collector 线程（独立的 SQLAlchemy session 与 SQLite 写锁、主事务冲突），风险与回归面远超收益；已被否决（见「决策与取舍」）。
- 不改灯箱：搜索页卡片点击的既有行为是**跳详情页**，不是开灯箱；本次只在详情页侧打通。
- 不给 `/api/detail/<pid>` 加同样的兜底：它只服务图库/缓存页的灯箱，那两页不会出现预览卡，加了是无人调用的分支。
- 不改详情页的下载实现（`/download/<pid>` 对未落库 pid 仍是 404，只是前端不再提供入口）。
- 不改 `paginated_search` 的预算/翻页语义。

## 决策与取舍

| 决策 | 选择 | 理由 | 代价 |
|---|---|---|---|
| 让预览可看的手段 | **详情页读内存快照兜底**（不落库） | 唯一不需要碰事务边界的活；详情页本来就"按 pid 拿数据渲染"，多一个数据源即可 | 详情页出现第二种数据来源，必须写清边界 |
| 兜底只认哪种任务状态 | **只认 `running`** | `done` / `partial` 的预览行**已经提交**（`safe_commit` 在 `raise SearchRateLimitedError` 之前），查库必然命中，兜底分支等于死代码；`error` / `cancelled` 的行已随事务回滚、快照也已清空，兜底会渲染出不存在的结果 | 任务刚被取消的极短窗口（≤ 一次轮询间隔）里点开仍是 404 |
| 与 DB 记录的优先级 | **DB 优先**，查不到才用快照 | 已落库的行是权威数据（`id` / `created_at` / `download_status` 都是真的）；快照是它的临时替代 | 无 |
| 下载按钮 | 预览态**渲染为 disabled + 显式提示** | `/download/<pid>` 此时必然 404；给一个点了必然报错的按钮比不给更糟 | 预览态不能下载（用户已知并接受） |
| 快照返回形态 | 返回 **dict 副本**，不返回任务里的对象 | 与既有约定一致（`_make_search_publisher` 也是 `dict(result)` 快照）；详情页会往 dict 里塞 `local_urls` / 中图地址等展示字段，绝不能污染任务快照 | 每次点击一次浅拷贝（可忽略） |
| 多个任务含同一 pid 时 | 取 **`created_at` 最新的 running 任务** | 提交新搜索会取消旧任务，但旧任务在它的线程察觉取消之前仍是 `running`，此时旧快照已被取代 | 无 |

### 为什么兜底不放在 fetcher / 搜索层

兜底要回答的是"这个 pid 现在能不能渲染"（HTTP 展示问题），而不是"这条作品该不该入库"（业务问题）。放进 `fetcher` 会让搜索层认识 HTTP；放进 `routes_search` 则要在 `routes_gallery` 里反向 import 搜索模块。快照本身住在 `runtime._search_tasks`，所以在 `runtime` 里放一个只读查找函数、由 `routes_gallery` 调用，依赖方向与既有模块图一致。

## 行为变化（用户可见）

1. **预览卡可点开**：搜索页虚线描边的预览卡点击 → 进入 `/detail/<pid>`，能看图、看标签、看作者，不再只弹提示。
2. **预览卡操作区文案**：「筛选中…」→「可预览 · 暂不可下载」，点开详情后仍带「筛选中」徽标语义的提示条。
3. **详情页在预览态显示提示条**并**禁用下载按钮**（按钮文案带"筛选中"）。
4. 仍未落库的作品**详细页相关作品**照常按画师查库（可能为空）；`file_size` / 本地图仍为无（未下载）。
5. 预览态跳出的"预览"语义没有丢：卡片虚线描边 + 「筛选中」徽标保留。

## 风险与边界

- **404 仍会发生**：任务被取消/失败后再点（快照已清空）、或打开的是一个已在内存里过期（TTL 600s）的任务 pid。这类 404 与改动前一致，属于"不可信结果不渲染"的正确行为。
- **不产生额外的 Pixiv 请求**：详情路径（作者搜索 / 带收藏数下限的标签搜索）的预览快照自带 `original_urls`，详情页直接用它拼中图/原图地址，不发新请求。仅 defer 路径（默认标签搜索 `min_bookmarks=0`）快照无 `original_urls`，会走详情页既有的 `_fetch_original_urls` 惰性拉取 —— 而那条路径下预览与落库之间只隔一次 `safe_commit`，实际上很少命中兜底。
- **单进程语义**：快照是进程内存，`-w 1` 前提不变（多 worker 下兜底会随 worker 分片失效，但那时既有前端预览也一样失效）。
