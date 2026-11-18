# X 标签抓图 实施计划

- 日期：2026-11-18
- 对应设计：`docs/superpowers/specs/2026-11-18-x-image-fetch-design.md`
- 状态：已实现（2026-11-18）

## 阶段 0：可行性探针

1. `scripts/x_probe.py`：读 Cookie/query_id → 跑一次 SearchTimeline → 打印脱敏诊断。
2. 部署后在服务器执行 `python scripts/x_probe.py '#标签'`。
   - 退出码 0：继续。
   - 1：按提示补 Cookie / 更新 query_id。
   - 2：网络/代理问题。

## 阶段 1：数据层

1. `models.py` 新增 `XTag` / `XWork` / `ArtistLink` / `WorkLink` / `ImageHash`。
   - `tweet_id` 用 `String`，注释写明 64 位精度原因。
2. `migrations/versions.py` 追加 `migrate_x_tables`（v7），全部 `CREATE TABLE IF NOT EXISTS`。
3. `tests/conftest.py` 的 `clean_db` 把新表加入清理列表。

## 阶段 2：适配层

1. `pixivviewer/x_client.py`：搜索 URL、解析、错误分类、Cookie、令牌桶、`probe_credentials`。
2. `_to_original()` 处理 `?name=small` → `?name=orig`（按 query 覆盖，不拼两个 name）。
3. 测试：`tests/test_x_client.py` + `tests/fixtures/x/search_page.json`。

## 阶段 3：同作品判定

1. `pixivviewer/x_dedupe.py`：
   - `apply_ingest_dedupe`（E1 + H1）
   - `dhash` / `hamming` / `index_local_files` / `find_hash_match`（E2）
   - `confirm_pair` / `ignore_pair`
2. 测试：`tests/test_x_dedupe.py`。

## 阶段 4：路由与页面

1. `pixivviewer/routes/xfeed.py`：`/x`、`/api/x/*`、`/ximg/<b64>`。
2. `templates/x.html` + `static/page-x.js`；9 个模板导航加「X 图片」。
3. `app.py` 注册 `xfeed_bp`。
4. 测试：`tests/test_x_routes.py`、`tests/test_x_image_proxy.py`；CSRF 矩阵补 X 端点。

## 阶段 5：后台与设置

1. `runtime.py`：`_x_prefetch_state`（键集合固定）、`x_download_executor`。
2. `background.py`：`_x_fetch_tag` / `_x_prefetch_once` / `_x_prefetch_loop` /
   `_hash_index_loop` / `_download_x_work` / `_start_x_threads`；health 加 `x_prefetch_alive`。
3. `config.py`：X 常量 + `SETTINGS_KEYS` + `SETTINGS_BOUNDS`。
4. `routes/settings.py`：保存后同步 `_x_prefetch_state` 并 `trigger_x_fetch()`。
5. `templates/settings.html` + `page-settings.js`：X 卡片（开关/间隔/页数/上限/query_id/
   指纹开关/阈值/Cookie/测试连接/重建索引）。

## 阶段 6：文档与部署

1. `requirements-x.txt`（可选 Pillow）。
2. README 增加「X 图片」章节（Cookie、query_id、探针、限流、ToS 风险）。
3. AGENTS.md：模块表、后台线程 3→4、设置键、迁移 v7、页面清单。
4. 本 spec/plan 标记已实现并记录验证结果。

## 验收

- `scripts/run_tests.ps1 -q` → 538 passed / 2 skipped / 0 failed。
- `/x` 与 `/settings` 无控制台错误；未配置 Cookie 时状态条明确提示。
- 不联网的测试覆盖适配层/去重/路由/代理/迁移。
