# 搜索渐进提速：执行勘误与跨任务补充

> 来源：Task 1 / Task 3 的两阶段审查（spec 合规 + 代码质量）。这里记录的**不是**新需求，
> 而是按计划字面实施会踩到的陷阱与必须由某一任务明确认领的决策。
> Task 7 回写计划时应把这些条目合并进对应任务，并在本文件标注"已并入"。

---

## Task 6：缓存边界与用户摘要接口（已实施）

### 实施结果

- 新增 `fetcher._TAG_SEARCH_CACHE_TTL = 120.0`，**仅** `search_by_tag` 的
  `_cache_get/_cache_put` 使用。发现页/关注页仍是 `_SEARCH_CACHE_TTL = 30.0`；
  作者搜索仍是 `_USER_SEARCH_CACHE_TTL = 600.0`，缓存键里的 `_blocked_fingerprint(blocked)`
  分量与键格式一字未动。
- `search_by_tag` 与 `browse_discovery` 在 `budget_exhausted()` 为真时不再写成功缓存
  （与 `search_by_user` 早已有的守卫对齐）。当前只有作者搜索会启用详情预算
  （`routes_search` 的 `_user_fn`），这两条是防"哪天给标签/发现路径也开预算"时静默退化的护栏。
- 屏蔽标签增删仍走 `routes_settings` → `clear_search_cache()` 整体清空；新增用例从路由入口
  （`POST` / `DELETE /api/blocked-tags`）钉住"立即生效"，因为标签缓存键里**没有**屏蔽指纹。
- 限流截断（`SearchRateLimitedError`）不写成功缓存已有用例；本次补上"上游抛 `PixivAuthError`
  时一条缓存都不写"的断言。

### 观察（本次刻意未改，留给后续判断）

- **`_cache_get(key, ttl=)` 的 `ttl` 是惰性形参**：条目实际的 TTL 是写入时存进
  `(ts, entry_ttl, value)` 的那个值，读取时只认它。传 `ttl` 仅让调用点的意图可见
  （作者搜索 600 秒那条一直如此）。标签路径的 120 秒因此同样由
  `_cache_put(..., ttl=_TAG_SEARCH_CACHE_TTL)` 落地，没有改 `_cache_get` 的语义。
- **空页照样进缓存**：标签/发现路径"上游成功但返回空页"会写缓存（`if not illusts_data` 分支），
  本次只把标签那条从 30 秒延长到 120 秒。空页不是异常，Pixiv 在 Cookie 失效时也会静默返回
  空结果，所以这是既有语义；作者搜索路径的空结果按设计不写缓存。

### 摘要端点 go/no-go：**no-go**（未发现满足字段的离线契约证据）

只读检查范围与逐条结论（未运行任何真实 Cookie / 真实 Pixiv 请求）：

1. `pixiv-api-http-main/core/api/app.js` 的路由表只有 illust / manga / novel / search / follow
   五组，**没有任何用户维度的作品列表路由**；`core/api/module/user/pid.js` 是 **0 字节空文件**
   （`core/api/module/illust/index.js` 只 `export * from './pid.js'` 与 `../manga/manga-pid.js`）；
   在整个参考实现里 grep `profile` 与 `user/` **零命中**。也就是说参考实现连
   `profile/all` 都没有，更没有"用户作品分页端点"这一形态可供对照。
2. `tests/fixtures/pixiv/user_profile_all.json` 的 `body.illusts` 形如
   `{"100000305": {}, "100000303": {}, …}` —— **值全是空对象**，只有键（作品 id）可用；
   `tests/fixtures/pixiv/README.md` 亦记为"`body.illusts` 的键即作品 id"。
   它无法证明 `title / page_count / thumb / upload_date / tags / bookmark_count` 中的任何一项，
   更没有分页终止信息。
3. 带摘要字段的既有样本（`search_illustrations.json`、`discovery_artworks.json`、
   `follow_latest.json`）分别属于关键词搜索、发现页、关注流 —— 都不是按用户的作品分页，
   也证明不了按用户维度的分页终止信息（`follow_latest.json` 的 `isLastPage` 属关注流）。
4. 现有样本全部是**手工编写**的（README 的刷新流程 `scripts/pixiv_capture.py` 需要真实 Cookie），
   本身即为弱证据，达不到"脱敏样本证明全部必需字段可从规范解析得到"这条杠。

判定：**不新增 `endpoint_*` / `fetch_*`，不改 `pixiv_client.py`、
`tests/test_pixiv_contract.py`、`tests/fixtures/pixiv/`**。`search_by_user` 继续以
`profile/all`（id 集合/游标来源）＋逐条详情为路径，详情预算、过滤与缓存语义不变。
这是「未发现满足字段的离线契约证据」的明确结论，不是待办。

## Task 4 必读（消除活陷阱）

1. **publisher 只经 `progress` 形参注入 `search_by_tag` / `search_by_user` / `browse_discovery`。**
   `fetcher.paginated_search` **不再接受** `progress`：它只看页边界、看不到条目，
   形参只会变成"接线通过、测试全绿、端到端零事件"的静默失效陷阱。
   Task 4 的 `_tag_fn` / `_user_fn` / `_browse_fn` 闭包各自捕获 publisher 后传给对应
   `search_by_*`。计划 Task 4 Step 2 里"沿 `app.paginated_search` 和对应 `app.search_by_*`
   闭包传入"的表述以本条为准。
2. **`error` 终态不得在响应里带 preview。** 已发布的预览可能对应随后回滚的行
   （`_publish` 发生在 `safe_commit` 之前）。状态端点对 `error` 必须返回空 results。
3. **result 事件必须按 `pixiv_id` 去重，且只保留前 `ITEMS_PER_PAGE` 个。**
   `_publish` 的去重集是 per-`_process_items` 的；一次搜索可能翻多页，
   发出的 result 会超过一页条数。
4. **预览 dict 来自未入库的模型**（`_publish_detail` 用 `illust_factory(item, detail).to_dict()`，
   此时 `id` / `created_at` 为 `None`），而 canonical 列表带真实 DB 值 ——
   合并**必须按 `pixiv_id`**，不能按行 `id`。

## Task 5 必读（终态与渲染语义）

1. **`done` 时必须用 canonical 页"替换"预览，不是合并。** 多页扫描可能接受多于
   `ITEMS_PER_PAGE` 件；canonical 取扫描顺序前 N 件，而预览是**完成顺序**，
   两边合法地可以不一致。
2. **`error` / 404 / 取消必须清空预览**，否则失败的搜索会在页面上留下幽灵卡片
   （现在 `pollSearch(..., onFail=undefined)`，error 分支只 toast）。
3. **不要用 `examined` 渲染完成比例。** `examined` 在任何详情完成之前就整批发出，
   "N/N examined" 会瞬间到 100% 而 accepted 仍为 0 —— 它只能当活动计数器，
   与"已找到 K 件"并列显示。
4. **`revision` 每次事件都会自增**（不只 `result`），这是 routes_search 的刻意选择：
   计数器变化也要对前端可见。后果是 `examined` 突发时"revision 未增长就不重渲染"
   的守卫会触发一次实际无内容变化的重渲染。**渲染判据应以 progress / preview 集合
   为准，别只看 revision。** 好消息是 `revision` 前进 ⟺ 快照有变化（重复的 result
   在被去重处提前返回，不会自增）。
5. **`progress.accepted` 不封顶**，可以大于 `len(results)`（preview 被
   `ITEMS_PER_PAGE` 截断）。"已找到 N 件"用 `accepted`，但渲染网格必须用 `results`，
   不能假定两者一致。
6. **Task 5 落地前不要把 Task 4 单独部署。** 未改动的 `static/page-index.js` 会把
   `partial` 响应当 `onDone(data)` 处理 —— 预览会被当成 canonical 页提交、`warning`
   被丢弃、`has_more=false` 写进分页状态。Task 5 必须显式分流 `partial`
   （`onProgress` + 单独的 `onPartial`，不能落进 `finishSearch`），
   并在 502/401/error 路径清空预览。
7. 终态响应的 `progress` 计数器必须与 `results` 一致：`error` / `cancelled` 已清空
   `results`，就**不能**再回一个非零 `accepted`（那些行可能已回滚）。Task 5 的
   "已找到 N 件"会在空网格上显示幽灵数字。

## Task 2 必读（预取调用方的限流语义）

`background._prefetch_one_tag` 调 `search_by_tag`，其宽 `except Exception` 会把
`SearchRateLimitedError` 变成"该标签本轮失败"。**决策：这就是预期语义**——
标签记 `error`、下轮重试；已入库作品不会丢（下轮从库里命中 existing 记录，不重拉详情）。

Task 2 必须补一条测试证明：熔断打开时预取轮次失败**不会**让该标签被永久卡死，
且 `_prefetch_loop` 继续跑（含容量清理）。

另外再钉一条路径：**`PixivRateLimitedError` 绝不能越过 `_fetch_details_parallel` 逃到路由层**。
该函数内部已把它捕获并翻成 `batch.rate_limited`；一旦哪天它逃到 `routes_search`，
宽 `except Exception` 会把它变成 HTTP 502 `error` —— 把"可重试的限流"静默降级成"失败"，
`partial` 语义直接失效。Task 2 接入闸门后需有测试覆盖这条链路。

## 已确定的实现判据（供 Task 6 / Task 7 复用）

- 限流、详情预算耗尽、认证/上游异常都**不写成功缓存**；只有完整结果才写。
- `_DetailFetchBatch.attempted` 只计**真正发出的**请求（被闸门拒绝的不计，也不记 `detail_failed`）。
- `_ProcessedItems.rate_limited` 只在 `search_by_*` 边界有意义：切片/拷贝会退化成普通 list。

## 文档欠账（Task 7 必须处理）

- `docs/technical-documentation.md` 仍写着 `_fetch_details_parallel → (results, attempted)`
  与 `_process_items → list[dict]`，已与实现不符。
- 回写时用真实数字，不要沿用中途的手写统计。

## 实施进度快照（供 Task 7 参考，最终以 git 为准）

| 任务 | 状态 | commit | 关键数字 |
|---|---|---|---|
| Task 1 详情 gate | 完成（2 轮审查 + 2 轮修复） | `ccf8a9a` | 新文件 37 例 |
| Task 3 逐条发布 + 限流批次 | 完成（spec + 质量两轮审查） | `be0dfee` | `test_fetcher.py` 101 例；全量 650 passed / 2 skipped；`fetcher.py` ~1249 行 |
| Task 6 缓存边界 + 摘要 no-go | 完成 | 本次提交（`perf: 缩短重复标签搜索等待并守住缓存边界`） | 新增 7 例（`test_fetcher.py` +5 / `test_settings_api.py` +2）；全量 686 passed / 2 skipped / 4 环境性失败（`test_temp_root_*`，基线上同样失败） |

基线（Task 1 之前）：全量 599 passed / 2 skipped（= 601 例）。
