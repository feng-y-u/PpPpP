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