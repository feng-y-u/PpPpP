# 实施计划：预取刷新失败退避与永久失败清理

- 日期：2026-09-08
- 状态：已实现（2026-09-08，验证结果见 spec 文末）
- Spec：`docs/superpowers/specs/2026-09-08-prefetch-refresh-retry-backoff-design.md`

## 步骤

1. **迁移 v4 + 模型 + 常量**
   - `migrations/versions.py`：新增 `add_illust_refresh_failed_at(conn)`（幂等补列），追加 `(4, ...)` 到 `MIGRATIONS`；**不动** v1/v2/v3 函数。
   - `models.py`：`Illust` 加 `refresh_failed_at: Mapped[datetime | None]`（nullable，无 default 语义）。
   - `config.py`：`PREFETCH_REFRESH_BACKOFF = 86400`、`PREFETCH_REFRESH_FORCE_DONE = 1209600`（14 天），注释说明用途。

2. **fetcher：永久失败识别**
   - 模块级 `DEAD_DETAIL = object()` 哨兵 + `_PERMANENT_REMOVE_KEYWORDS` frozenset + `_is_permanently_removed_message(msg)` 助手。
   - `_get_illust_detail(..., return_dead: bool = False)`：
     - `RequestException` 分支：`status == 404` → 立即返回 `DEAD_DETAIL if return_dead else None`（不再重试）；
     - `error:true` 分支：命中删除关键词 → 同上返回；其余维持日志 + None。
   - 确认 `_fetch_details_parallel` 及各搜索路径不传 `return_dead`（保持 None 语义）。

3. **background：刷新状态机**
   - `_prefetch_refresh_bookmarks` 重写：
     - 候选：`created_at < 满1天` 且 `(refresh_failed_at IS NULL OR refresh_failed_at < now - BACKOFF)`，`ORDER BY created_at ASC LIMIT max_items`（需 `from sqlalchemy import or_`）；
     - 网络请求仍放 DB session 外（沿用现有结构）；
     - 失败（None）→ 写 `refresh_failed_at = now`，commit；
     - `DEAD_DETAIL` → 未下载未收藏：删行 + `_remove_pids_from_search_caches`；已下载/已收藏：写 `prefetch_refresh_at` 保留；
     - 成功 → 更新收藏数 + `prefetch_refresh_at = now` + 清空 `refresh_failed_at`（<10 删除逻辑不变）；
     - force-done 扫描：`refresh_failed_at < now - FORCE_DONE` 的未刷新作品 → 写 `prefetch_refresh_at`、清标记（独立于候选，候选为空也执行）。

4. **测试**
   - `tests/test_prefetch.py`：
     - `_mock_detail`/失败 mock 签名加 `return_dead=False`；
     - `test_refresh_failure_keeps_candidate_for_retry` 改为新语义：失败写标记 + 下轮不再入选；补一条「标记过期（2 天前）→ 重新入选」；
     - 新增：成功清标记；DEAD 删除（含 SearchCache 引用同步）；DEAD 已下载保留；DEAD 已收藏保留；force-done（15 天前失败 → 无网络请求直接标记完成）；退避窗口内（1 小时前失败）不入选。
   - `tests/test_fetcher.py` `TestDetailRetryPolicy`：404 立即返回（return_dead True/False 两分支、无重试、无 sleep）；`error:true` 删除类 message → DEAD_DETAIL；权限类 message（年龄确认）→ None 且重试语义不变。
   - `tests/test_migrations.py`：确认 v4 跑通（现有用例按 `LATEST_SCHEMA_VERSION` 动态断言，应无需改）。
   - 顺带核对 `tests/test_models.py` 无列集合硬断言（已 grep：无）。

5. **验证**
   - `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q`（全量约 15s）。
   - 定向跑 `test_prefetch.py` + `test_fetcher.py::TestDetailRetryPolicy` + `test_migrations.py`。

6. **回写与提交**
   - spec/plan 标记「已实现 + 验证结果」；
   - 提交：`feat: 预取刷新失败退避 + 永久失败清理（防死作品占坑顶破容量上限）`（迁移/模型/背景/fetcher 一体）+ `docs: spec/plan 标记已实现并记录验证结果`。

## 迭代修订（2026-09-08 · 第二批：需求 2/3/5）

1. **fetcher**：新增 `RETRYABLE_GLOBAL_DETAIL` 哨兵；`requests.ConnectionError` 分支与
   「403/429 重试耗尽」分支按 `return_dead` 返回它（新增 `last_status` 记录最后一次状态码）；
   默认调用方（搜索/后台补全）仍收 `None`。
2. **config**：`PREFETCH_REFRESH_ABORT_STREAK = 3`。
3. **background**：`_prefetch_refresh_bookmarks` 内层加 `try/except (PixivAuthError,
   FileNotFoundError)`（中止本轮、不写标记、不冒泡）；循环维护 `global_fail_streak`
   并在达到阈值时 `break`；`session` 关闭加 `None` 守卫。
4. **测试**：`test_prefetch.py` 加 4 例、`test_fetcher.py` 加 3 例（清单见 spec 第二批验证结果）。
5. **文档**：AGENTS.md 两处同步（预取缓存失败状态机条目 + 迁移 v4）。
6. **验证**：定向 46 passed；全量 `run_tests.ps1 -q` → 283 passed（4 例环境性失败与基线一致）。

## 迭代修订（2026-09-08 · 第三批：需求 4/6）

1. **runtime**：`_prefetch_state` 加固定键 `refresh_stats`（初始化即存在）。
2. **background**：`_prefetch_refresh_bookmarks` 拆为入口（`try/finally` 落统计）+ `_refresh_bookmarks_pass`；
   新增 `reset_prefetch_refresh(tag=None, pixiv_id=None)`（json_each 下推、必须指定范围）。
3. **routes_prefetch**：`/api/prefetch/status` 增加 `refresh`/`pending_refresh`/`failed_backoff`；
   新增 `POST /api/prefetch/refresh-reset`（CSRF、400/404 语义、经 app 命名空间调用）。
4. **app.py**：from-import `reset_prefetch_refresh` 到 app 命名空间（测试 seam）。
5. **前端**：`templates/settings.html` 加 `#prefetchRefreshStats` 行；`static/page-settings.js`
   加 `loadPrefetchStatus()` 与徽章内 ⟳ `resetPrefetchRefresh(tag)`（`stopPropagation`）。
6. **测试**：`test_prefetch.py` +6 例、`test_prefetch_api.py` 状态字段与 reset 五条路径。
7. **文档**：AGENTS.md（观测与手动干预段）、`docs/architecture.md`（background 符号、
   routes_prefetch 路由表、测试契约表）、本 spec/plan 回写。
8. **验证**：定向 74 passed；`node --check` 通过；全量 294 passed（4 例环境性失败同基线）。

## 迭代修订（2026-09-08 · 第四批：剩余缺口全量收口）

1. **fetcher**：未识别报错采样器（`_record_detail_error` / `get_detail_error_samples`，
   上限 20 种、加锁）；`error:true` 未判死分支调用它。
2. **config**：`PREFETCH_INTAKE_PAUSE_RATIO = 0.8` / `PREFETCH_INTAKE_RESUME_RATIO = 0.6`。
3. **runtime**：`_prefetch_state` 加固定键 `intake_paused`。
4. **background**：
   - 新增 `_is_user_owned(db, pid)`（收藏 + 用户操作类 DownloadLog，逐条查询）；
   - 刷新路径改用它（替换整轮 `fav_ids` 快照）；死作品删除写
     `DownloadLog(action='prefetch_deleted')`；
   - 容量清理预加载下载日志集合，跳过用户拥有过的作品；
   - `_prefetch_loop` 加入库节流阀（积压 ≥ max×0.8 暂停入库、< max×0.6 恢复，滞回）。
5. **models**：`init_db()` 在 `repair_illust_schema` 后额外幂等调用 `add_illust_refresh_failed_at`
   （不改已发布迁移函数）。
6. **routes_prefetch**：status 增加 `intake_paused` / `detail_errors`。
7. **前端**：设置页健康行显示"入库已暂停"与未识别报错样本。
8. **索引**：1 万行实测（候选 0.81ms / force-done 0.65ms / 计数 ~0.45ms / 容量清理 6.04ms）
   → 不建索引，触发条件记入 spec。
9. **验证**：定向 142 passed；`node --check` 通过；全量 304 passed（4 例环境性失败同基线）。

## 迭代修订（2026-09-08 · 第五批：撤回节流阀 → 加大清理力度）

1. **config**：删 `PREFETCH_INTAKE_PAUSE_RATIO` / `PREFETCH_INTAKE_RESUME_RATIO`；
   加 `PREFETCH_REFRESH_BATCH = 300`、`PREFETCH_EVICT_UNREFRESHED_AFTER = 3 * 86400`。
2. **runtime**：删 `_prefetch_state['intake_paused']`。
3. **background**：
   - `_prefetch_loop` 去掉节流分支，恢复每轮入库；
   - `_prefetch_capacity_cleanup` 改三层淘汰（tier1 已刷新 → tier2 失败/超龄 → tier3 兜底），
     统计在 bulk delete 前算（否则 `ObjectDeletedError`）；
   - 新增 `_naive_utc()`；`_prefetch_refresh_bookmarks` 默认批量改为 `PREFETCH_REFRESH_BATCH`。
4. **routes_prefetch / 前端**：去掉 `intake_paused` 字段与"入库已暂停"提示。
5. **测试**：删除节流阀 3 例 + 新增三层淘汰 4 例 + "入库永不停" 1 例；status 字段断言同步。
6. **验证**：定向 82 passed；`node --check` 通过；全量 305 passed；端到端冒烟通过。

## 迭代修订（2026-09-08 · 第六批：入库 UNIQUE 竞态修复）

1. **fetcher**：新增 `_insert_new_illusts(db, illusts)`（`INSERT ... ON CONFLICT DO NOTHING`
   批量写入 + 按 pid 回查赢家行，SQLite ≥ 3.24）；`_process_items` 两处插入点
   （defer `db.add_all` / 非 defer 逐条 `db.add+flush`）统一改用它，两处都按 pid 去重
   再进结果；顶部加 `from sqlalchemy.dialects.sqlite import insert as sqlite_insert`。
2. **测试**：`TestInsertNewIllusts`（+2）、`TestProcessItemsDuplicateInsert`（+2）。
3. **文档**：本 plan/spec 回写（第六批）+ AGENTS.md「数据库」补一条入库冲突容忍约定。
4. **验证**：定向 65 passed；全量 309 passed（4 例环境性失败同基线）。