# Pixiv 适配层实施计划

**状态：已实现并验证**（全量离线 599 passed / 2 skipped；基线 550/2，新增 49 条契约用例）。
分步完成情况见文末「执行记录」。

设计见 `docs/superpowers/specs/2026-09-29-pixiv-client-adapter-design.md`。

## 步骤

1. **新建 `pixiv_client.py`**（适配层）
   - 顶部 docstring 写清分层契约与"不要顺手重写"的机制（Cookie 认证、连接池、
     凭据分级、原图地址来自详情接口）。
   - 搬入：Cookie 状态与 `_load_cookie`、`build_pixiv_session`/`build_credentialless_session`、
     线程内连接池、`_TokenBucket` 与三个桶、`PixivAuthError`/`_is_auth_error`/`_warn_403`、
     `_detail_error_samples` 采样、`R18_TAGS`。
   - 新增：5 个 `endpoint_*` 拼装函数、`envelope_error`、`handle_list_request_error`、
     `parse_tags`/`extract_original_urls`/`item_pixiv_id`/`item_bookmark_count`/
     `parse_illust_summary`/`parse_illust_detail`。
   - 新增 6 个 `fetch_*`：详情（含重试分类与哨兵）、原图地址、搜索、发现、用户
     profile/all、关注最新。签名一律 `(session, …)`，session 由业务层创建。

2. **瘦身 `fetcher.py`**（业务层）
   - 删除上一步搬走的实现；从 `pixiv_client` import 并在文件内**再导出**同名符号
     （历史调用方 + 测试补丁 seam）。
   - 业务函数改为"建 session → 调 `pixiv_client.fetch_*` → 过滤/缓存/入库"。
   - `_illust_from_item` 改用 `parse_illust_summary`；`_process_items` 的 defer 分支
     改用 `item_bookmark_count` / `parse_tags`。

3. **`helpers.py`**：`_fetch_original_urls` 改走适配层；`R18_TAGS` 改从适配层取；
   去掉 `import fetcher`（叶子模块不再依赖业务层）。

4. **`routes_settings.py`**：Cookie 写盘后不再直接赋值 `fetcher._cookie_value/_cookie_mtime`，
   改调 `pixiv_client.set_cookie_cache(value, mtime)`（Cookie 落点仍单一来源 `config.COOKIE_PATH`）。

5. **样本与契约测试**
   - `tests/fixtures/pixiv/`：6 个脱敏样本。
   - `tests/test_pixiv_contract.py`：端点 URL、信封遍历、字段解析、键集合、错误分类。
   - `scripts/pixiv_capture.py`：脱敏抓取工具（可选，便于刷新样本）。

6. **迁移因 seam 移动而失效的既有测试引用**（把补丁打到新归属地）：
   `PixivAuthError` 之外的适配层状态与函数、`time.sleep`、`_total_limiter`、
   `_cookie_*`、`COOKIE_PATH`、`PROXY`、`SSL_VERIFY`、`_detail_error_samples`、
   日志 logger 名（`warn_403` 的 logger 现在是 `pixiv_client`）。
   `fetcher._fetch_details_parallel` / `_kick_background_fill` / `_get_user_profile_ids` /
   `build_pixiv_session` / `_get_illust_detail` 别名**保持可直接补丁**，不动。

7. **验证**：全量离线测试通过（基线 550 passed / 2 skipped）。
   `grep -n "/ajax/" fetcher.py` 应无输出。

8. **回写**：本文件与 spec 标记"已实现 + 验证结果"；更新 `docs/architecture.md`
   模块地图与测试契约表；`AGENTS.md` 架构表补一行 `pixiv_client.py`。

## 执行记录

- 步骤 1–8 全部完成。新增 `pixiv_client.py`（约 700 行）、`tests/test_pixiv_contract.py`（49 例）、
  `tests/fixtures/pixiv/`（9 个脱敏样本 + README）、`scripts/pixiv_capture.py`。
- `fetcher.py` 1433 → 1030 行（协议与认证代码整体搬走，业务流水线逐行保留）；`helpers.py`
  去掉 `import fetcher`（叶子模块不再反向依赖业务层）。
- **计划外的两处收紧**（都是为了消除"补丁静默失效"）：
  1. 限速桶不再从 `fetcher` 再导出（`_fill_limiter` / `_detail_limiter` / `_total_limiter`）——
     `background.py` 预取刷新改用 `pixiv_client._fill_limiter`，`fetcher._background_fill_details`
     同理。这样打在 `fetcher` 上的限速桶补丁会 **AttributeError 报错**而不是静默无效。
  2. `COOKIE_PATH` / `PROXY` / `SSL_VERIFY` / `_cookie_*` / `_detail_error_samples` 同样只在
     `pixiv_client`：既有测试里的这些补丁已同步迁移（test_fetcher / test_app / test_settings_api）。
- 既有测试**保留**的 seam（未破坏）：`fetcher.build_pixiv_session`、`fetcher._get_illust_detail`、
  `fetcher._get_user_profile_ids`、`fetcher._fetch_details_parallel`、`fetcher._kick_background_fill`、
  `fetcher._TokenBucket`、`fetcher._split_tags`、`fetcher.DETAIL_MAX_RETRIES`。
- 一次性迁移的测试补丁目标：`_total_limiter`（6 处）、`patch('fetcher.time.sleep')`（3 处）、
  `_detail_error_samples`（3 处）、cookie/连接池/SSL/PROXY 相关（test_fetcher 的
  `TestPooledSession` / `TestCredentiallessSession` / `test_session_does_not_retry_connect_errors`、
  test_app 的 `TestSessionFactory`、test_settings_api 的 `_isolate_cookies_txt` 夹具）、
  以及 1 处日志 `logger='fetcher'` → `'pixiv_client'`（`_warn_403` 的 logger 名随模块走）。
- 未改动：所有业务语义（过滤顺序、缓存键、游标算法、令牌桶速率、重试分类、取消/预算）、
  所有 HTTP 接口、DB schema、前端。
