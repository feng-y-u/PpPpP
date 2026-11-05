# 预取标签同义词合并实施计划

对应 spec：`docs/superpowers/specs/2026-11-05-prefetch-tag-aliases-design.md`

状态：已实现（2026-11-05）。验证见 spec 的「验证结果」。

## 步骤 1：schema、模型属性与迁移

- `models.py`：`SearchCache` 加 `alias_tags` 列（`Text, default='[]'`）+ `alias_tags_list` property（坏 JSON → `[]`）。
- `models.init_db()`：迁移后幂等调用 `add_search_cache_alias_tags`。
- `migrations/versions.py`：新增 `add_search_cache_alias_tags(conn)`（已存在则 no-op），追加 `(6, ...)`；不改已发布版本。
- 测试：`test_migrations.py` 补 v6（老库补列、幂等、新库有列）；`test_models.py` 补属性默认值/往返/坏 JSON。

验证：`run_tests.ps1 tests/test_migrations.py tests/test_models.py -q`。

## 步骤 2：预取合并

- `background.py` 新增 `_find_alias_group(db, alias)`：扫描所有行 `alias_tags_list`，返回拥有该同义词的组名或 `None`。
- 重写 `_prefetch_one_tag`：
  - sources = `[tag] + row.alias_tags_list`；
  - 逐 source 搜索，单源失败记录后继续，`PixivAuthError` 中止剩余；
  - 跨源 `dict.fromkeys` 去重后与旧 ids 合并；
  - `total` / `cached_at` / `status` / `error` 按 spec 聚合；
  - tag 已不存在但成为别组同义词 → 直接返回。
- 测试：`test_prefetch.py` 补双源并集去重、部分失败、全失败、认证中止、竞态跳过。

验证：`run_tests.ps1 tests/test_prefetch.py -q`。

## 步骤 3：API

- `routes_prefetch.py`：
  - `GET /api/prefetch/tags` 加 `aliases`；
  - `POST /api/prefetch/tags/<path:tag>/aliases`（校验、409、存量行合并去重并删除、fetching 拒绝）；
  - `DELETE /api/prefetch/tags/<path:tag>/aliases`（只摘词，不动作品）；
  - `POST /api/prefetch/tags` 拦截"已是别组同义词"；
  - 增删与预取共用 `background._search_cache_guard`。
- 测试：`test_prefetch_api.py` 补上述矩阵 + CSRF。

验证：`run_tests.ps1 tests/test_prefetch_api.py tests/test_cache_page.py -q`。

## 步骤 4：前端

- `templates/settings.html`：帮助文案说明 `+` 是添加同义词、点击标签名仍是删除。
- `static/page-settings.js`：chip 加 `+`（`data-alias`，`stopPropagation`）、`+N` 与 title、`prompt()` 输入并 POST、删除确认提到同义词。
- `static/page-cache.js`：`updateCacheMeta` 显示 `· 同义词：...`，`error` 非空即显示。
- 无构建步骤、ES2020 语法上限；无内联脚本。

验证：手动开 `/settings` 与 `/cache` 各点一遍（需重启后端）。

## 步骤 5：文档与全量验证

- `AGENTS.md`「预取缓存」段补一段同义词语义与 API 要点。
- 全量：`powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q`（基线 430 passed / 2 skipped）。
- 实测（可选）：在真实库里把两个存量标签合并，核对并集条数与重复 pid 数，把结果回写 spec 的「验证结果」。

## 顺序与风险

1 → 2 → 3 → 4 → 5。风险点：
- 合并存量行时并发预取正在写该行（用 `fetching` 409 + `_search_cache_guard` 双保险）；
- 预取循环读到的旧 tag 列表可能包含刚被合并掉的标签（步骤 2 的竞态兜底）；
- `status/error` 语义变化（部分成功仍 `done`）需同步缓存页文案（步骤 4）。
