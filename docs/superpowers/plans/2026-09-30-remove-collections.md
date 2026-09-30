# Pixiv Viewer 移除收藏夹 / 收藏功能实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 `subagent-driven-development`（推荐）或 `executing-plans`，按任务执行并逐段审查。步骤用 checkbox 追踪。设计依据：`docs/superpowers/specs/2026-09-30-remove-collections-design.md`（下称 spec）。

**Goal:** 彻底移除本地「收藏夹 / 收藏」功能：删除其路由、模型、前后端入口与过滤，新增迁移 v5 删掉 `collections` / `collection_items` 两表，并把预取容量淘汰的保护判定收窄为「已下载 + 用户操作类 `DownloadLog`」，全过程保持离线测试全绿。

**Architecture:** 后端按「先摘掉消费者、再删模型」的顺序拆：`background._is_user_owned()` 先去掉收藏夹判定（此时 `CollectionItem` 仍在，可独立回归）；随后一次性删除 `routes_collections.py` + `app.py` 布线 + `routes_gallery` 的收藏端点/参数 + `models.Collection/CollectionItem/get_favorite_pids` + `helpers`/`fetcher` 的收藏回填，测试同步改，保证该提交后仍全绿；前端最后删入口与状态。迁移 v5 独立成第一个任务，先把 schema 决策落地。

**Tech Stack:** Python 3.13 / Flask / SQLAlchemy + SQLite（`PRAGMA user_version` 迁移）/ 原生 ES2020 JavaScript / pytest；所有默认测试离线运行，经 `scripts/run_tests.ps1` 执行。

---

## 执行前置条件

1. **分支与隔离**：从当前 `main`（`99ad78b`）建 `refactor/remove-collections`，在 worktree `E:\pixiv\.worktrees\remove-collections` 内实施——`E:\pixiv` 主工作树可能被另一个会话并行使用（其预取/自动关注工作与本计划的 `background.py` 改动相邻），不要在主工作树里改这批文件。
   - ⚠️ **worktree 的 venv 必须用 junction 指向主 venv**（`run_tests.ps1` 按脚本相对路径找 `venv\Scripts\python.exe`）。**删除 worktree 之前必须先单独摘掉这个 junction**（`cmd /c rmdir <worktree>\venv`），否则任何递归删除都可能穿透 junction 删掉真实 `E:\pixiv\venv`。
2. **基线**：先跑 `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q` 记录基线。当前实测基线为 **688 passed / 2 skipped / 4 failed(env)**；那 4 个是 `tests/test_test_setup.py::test_temp_root_*`（沙箱下派生 PowerShell 的语言模式不同），**基线同样失败，不要为它们改动测试环境**，最终验收以「失败集合与基线完全相同」为准。
3. **沙箱注意**：设 `$env:TEMP = Join-Path $env:LOCALAPPDATA 'Temp\dsh-manual'; $env:TMP = $env:TEMP` 后再跑 `run_tests.ps1`，否则临时根回退到只读路径会让大批用例在 setup 阶段报 `PermissionError`。
4. **改动文件时**：Python 文件若需 `git add`，用 `git add --renormalize <file>` 并确认 `git show --stat` 里改动行数正常（本仓库的 blob 与工作树行尾不一致，普通 `git add` 会把整文件算作改写）。
5. **约束**：`DETAIL_RATE_PER_MINUTE=45` / `FILL_RATE_PER_MINUTE=20` / `TOTAL_RATE_PER_MINUTE=60` 不变；认证方式、Cookie 路径与原子写、连接池复用规则不变；默认测试离线、不读仓库根真实 `cookies.txt`；迁移**只追加新版本**，不修改已发布的 v1–v4。

## 文件地图

| 文件 | 责任 |
|---|---|
| `migrations/versions.py` / `migrations/__init__.py` | 新增 v5：`DROP TABLE IF EXISTS collection_items` → `collections`，并登记进 `MIGRATIONS`。 |
| `background.py` | `_is_user_owned()` 去掉 `CollectionItem` 判定；`_prefetch_capacity_cleanup` 去掉 `fav_ids` 快照依赖；更新相关中文注释（保留所有「收藏数」= `bookmark_count` 语义）。 |
| `routes_collections.py` | **整文件删除**。 |
| `app.py` | 删除 `collections_bp` 的 import 与 `register_blueprint`（蓝图 7 → 6）。 |
| `routes_gallery.py` | 删除 `/api/favorite/<pid>`（GET/POST）、`/api/illust/<pid>/collections`、`/api/gallery` 的 `favorites` 与 `collection_id` 参数（含 JOIN `collection_items`、`favorite_total`、按 position 排序分支）、`/detail` 的 `to_dict(favorite=…)`、相关 import 与文件头注释。 |
| `models.py` | 删除 `Collection`、`CollectionItem`、`get_favorite_pids()`；`Illust.to_dict()` 去掉 `favorite` 形参与 `is_favorite` 字段。 |
| `helpers.py` | 删除 `get_favorite_pids` 导入、`query_cached_tag` 里的 `is_favorite` 回填、`_next_collection_position`。 |
| `fetcher.py` | 删除 `_mark_favorite()` 及其三处调用、`fav_pids = get_favorite_pids(db)` 预取与相关 import/注释。 |
| `templates/settings.html`、`gallery.html`、`detail.html` | 删除收藏夹管理卡片与其删除弹窗、管理收藏夹链接、添加到收藏夹下拉、移出收藏夹按钮、卡片 ♥ 样式、`#favBtn`、「收藏到…」弹窗。 |
| `static/page-gallery.js`、`page-detail.js`、`page-settings.js`、`lightbox.js`、`style.css` | 删除收藏夹视图状态/筛选/卡片 ♥/批量加入与移出/两个收藏夹下拉/`__lbSyncFav`/收藏夹选择弹窗/`lbFav` 按钮与 `/api/favorite` 调用/收藏夹样式。**保留**所有「收藏数」（`bookmark_count`）展示与排序。 |
| `tests/test_migrations.py` | 新增 v5 用例；调整引用 v1 收藏夹 position 的用例。 |
| `tests/test_app.py`、`test_models.py`、`test_fetcher.py`、`test_prefetch.py`、`test_prefetch_api.py`、`conftest.py` | 删除/改写收藏相关用例与夹具；`_is_user_owned` 的用例改为只覆盖 DownloadLog 语义。 |
| `docs/technical-documentation.md`、`docs/architecture.md`、`AGENTS.md` | 回写：功能表、目录树、模块表、序列图、API 清单、数据表、FAQ、性能表与测试契约。 |

## 任务依赖顺序

Task 1（迁移）独立可先做；Task 2（保护判定）必须早于 Task 3（否则 `CollectionItem` 已不存在，无法单独回归）；Task 3 是一次**不可拆分**的提交（删模型必须与所有消费者的删除同批，否则中间态无法启动/测试）；Task 4（前端）可在 Task 3 之后做；Task 5 收尾。

## Task 1：迁移 v5 —— 删掉收藏夹两张表

**Files:** Modify `migrations/versions.py`、`migrations/__init__.py`；Test `tests/test_migrations.py`

- [x] **Step 1: 写失败测试**。参照 `tests/test_migrations.py` 既有用例的写法（临时库 + `run_migrations`）：建一个带 `collections` / `collection_items` 数据的旧库（`PRAGMA user_version = 4`），跑 `run_migrations`，断言：① 两表消失（`sqlite_master` 查询）；② `illusts` 行数与内容不变；③ `PRAGMA user_version = 5`；④ **重复执行不抛错**（`IF EXISTS`）；⑤ 备份文件已生成（既有 runner 行为，沿用手法即可）。
- [x] **Step 2: 实现**。`migrations/versions.py` 新增函数，**先删子表再删父表**（`collection_items` 有 `FK → collections.id`）：

  ```python
  def drop_collection_tables(conn: Connection) -> None:
      """v5：彻底移除本地收藏夹功能，删除其两张表。

      子表先删：`collection_items.collection_id` 引用 `collections.id`，开启外键的库先删父表会失败。
      `IF EXISTS` 让重复执行成为 no-op —— runner 靠 `PRAGMA user_version` 保证只跑一次，
      但手工/异常重入时不应报错。
      """
      conn.exec_driver_sql("DROP TABLE IF EXISTS collection_items")
      conn.exec_driver_sql("DROP TABLE IF EXISTS collections")
  ```

  在 `migrations/__init__.py` 的 `MIGRATIONS` 序列**末尾追加 `(5, drop_collection_tables)`**（`runner.run_migrations` 校验版本唯一正整数且升序）。
- [x] **Step 3: 跑定向与全量**：`scripts\run_tests.ps1 tests\test_migrations.py tests\test_models.py -q` → 全量 → 失败集合与基线一致。
- [x] **Step 4: 提交**：`git add --renormalize migrations/versions.py migrations/__init__.py tests/test_migrations.py` + `git diff --check` → commit `feat: 新增迁移 v5 删除收藏夹两表`。
- [x] Step 5: 自审（幂等性、子表先删、是否误改已发布版本、`MIGRATIONS` 顺序校验）。

## Task 2：预取保护判定收窄为「已下载 + 用户操作类 DownloadLog」

**Files:** Modify `background.py`；Test `tests/test_prefetch.py`、`tests/test_prefetch_api.py`

- [x] **Step 1: 改/写测试**。把现有「在收藏夹里 → 受保护」的用例改为断言**新语义**：只有「已下载」或「用户操作类 `DownloadLog`（含失败/取消待重试）」受保护；`action='prefetch_deleted'` **不算**保护。至少覆盖：`_is_user_owned` 对"未下载、无日志"的作品返回 `False`；对"有失败下载日志"返回 `True`；对只有 `prefetch_deleted` 日志返回 `False`。
- [x] **Step 2: 实现**。`background._is_user_owned()`：删掉 `db.query(CollectionItem)…` 分支，只留 DownloadLog 判定，并把 docstring 改成新语义（说明「收藏夹」已移除，保护只来源于下载意图）。`_prefetch_capacity_cleanup`：删掉 `fav_ids = {c.pixiv_id for c in db.query(CollectionItem.pixiv_id).all()}` 及其在保护判定里的使用（保护判定统一走 `_is_user_owned`）。清除该文件里其余把「收藏夹」当保护来源的注释与日志文案。
- [x] **Step 3: 确认没有漏改的收藏夹引用**：`grep -n "CollectionItem\|收藏夹" background.py` → 只应剩下「收藏数」（`bookmark_count`，如 `_prefetch_refresh_bookmarks`、`refresh_stats` 的 `deleted_low`）相关文案。
- [x] **Step 4: 定向 + 全量 + 提交**：`scripts\run_tests.ps1 tests\test_prefetch.py tests\test_prefetch_api.py -q` → 全量 → commit `refactor: 预取保护判定改为仅按已下载与用户下载日志`。
- [x] Step 5: 自审（保护判定是否变宽导致该删的不删、`prefetch_deleted` 语义是否仍不算保护、注释是否与代码一致）。

## Task 3：后端移除收藏 API、模型与回填（不可拆分）

**Files:** Delete `routes_collections.py`；Modify `app.py`、`routes_gallery.py`、`models.py`、`helpers.py`、`fetcher.py`；Test `tests/test_app.py`、`tests/test_models.py`、`tests/test_fetcher.py`、`conftest.py`

- [x] **Step 1: 先核对 seam 与调用点**（删之前必须做）：`grep -rn "app\.collections_bp\|app\.get_favorite_pids\|collections_bp" tests/ app.py`；`grep -rn "get_favorite_pids\|CollectionItem\|Collection\b\|is_favorite\|_next_collection_position\|_mark_favorite" --include=*.py . | grep -v "^./tests/"`。把结果贴进实施记录——**这决定了还有哪些调用点必须同批改**。
- [x] **Step 2: 改写测试（红灯）**。删除断言收藏功能的用例（`test_app.py` 的收藏契约类、`test_models.py` 的 Collection/CollectionItem 用例与 `TestIllustToDict` 的 `is_favorite` 断言、`test_fetcher.py` 的 `_mark_favorite` 用例、`conftest.py` 里与默认收藏夹有关的夹具）；给 `/api/favorite/<pid>`、`/api/illust/<pid>/collections`、`/api/collections*` 各加一条**断言 404** 的用例（证明端点确实消失，而不是忘了注册导致 500）。这些用例此刻应为红灯。
- [x] **Step 3: 实现（同一提交内完成）**：
  1. `routes_gallery.py`：删 `/api/favorite/<pid>`（GET/POST）、`/api/illust/<pid>/collections`；`/api/gallery` 删 `favorites` 与 `collection_id` 两个参数及其分支（JOIN `collection_items`、按 position 排序、`favorite_total`）；`/detail` 与图库条目改为 `i.to_dict()`（不再传 `favorite=`）；清理 import（`Collection`/`CollectionItem`/`get_favorite_pids`/`_next_collection_position`）与文件头注释里的收藏路由。
  2. `models.py`：删 `Collection`、`CollectionItem`、`get_favorite_pids()`；`Illust.to_dict()` 去掉 `favorite` 形参与 `is_favorite` 键（**注意** `test_models.py::TestIllustToDict` 断言完整字段集，Step 2 已同步）。
  3. `helpers.py`：删 `get_favorite_pids` 导入、`query_cached_tag` 里 `d['is_favorite']` 的回填与 `is_favorite: False` 初值、`_next_collection_position`。
  4. `fetcher.py`：删 `_mark_favorite()`、三处 `_publish(_mark_favorite(...))` 调用、`fav_pids = get_favorite_pids(db)` 预取、`get_favorite_pids` 导入与相关注释。
  5. `app.py`：删 `from routes_collections import bp as collections_bp` 与 `app.register_blueprint(collections_bp)`。
  6. 删除 `routes_collections.py`（`git rm`）。
- [x] **Step 4: 定向 + 全量**：先 `scripts\run_tests.ps1 tests\test_app.py tests\test_models.py tests\test_fetcher.py -q`，再全量；失败集合必须与基线一致。另跑一次启动自检：`venv\Scripts\python -c "import app"`（蓝图数少一个后仍能装配）。
- [x] **Step 5: 提交**：commit `refactor: 移除收藏夹 API、模型与收藏回填`；`git diff --check` 干净。
- [x] Step 6: 自审（是否还有 `is_favorite` 泄漏到响应里、`/api/gallery` 的参数删除是否改变了默认视图行为、`to_dict` 字段变化是否影响前端未删的部分——由 Task 4 收口）。

## Task 4：前端移除收藏入口与状态

**Files:** Modify `templates/settings.html`、`templates/gallery.html`、`templates/detail.html`、`static/page-gallery.js`、`static/page-detail.js`、`static/page-settings.js`、`static/lightbox.js`、`static/style.css`

- [x] **Step 1: 模板**：`settings.html` 删「收藏夹管理」卡片（含 `#newCollectionName`、`#collectionList`）与删除确认弹窗（`#deleteCollectionModal`）；`gallery.html` 删「管理收藏夹」链接、收藏夹下拉、「添加到收藏夹…」选项、「移出收藏夹」按钮、`.card-fav-btn` 样式块；`detail.html` 删 `#favBtn`（保留其容器与其它统计按钮）、「收藏到…」弹窗（`#collectionPickerModal`）。
- [x] **Step 2: JS**：`page-gallery.js` 删 `activeCollectionId` 与 `collection_id` 参数、收藏视图与 `galleryFavTotal`、卡片 ♥ 渲染与点击、移出/批量加入收藏夹、两个收藏夹下拉及其加载、`__lbSyncFav`、收藏视图的空态文案（「暂无收藏作品」）；`page-detail.js` 删收藏夹选择弹窗逻辑与 `#favBtn` 绑定，并清掉导航上下文里的 `collection_id`（图库已无收藏夹视图）；`page-settings.js` 删收藏夹列表渲染/新建/删除；`lightbox.js` 删 `lbFav` 按钮、`isFav` 状态与 `/api/favorite` 调用。
- [x] **Step 3: CSS**：`style.css` 删 `.collection-check-item`（含移动端片段）与 `.batch-collection-select`。
- [x] **Step 4: 静态验证**（本环境无浏览器，必须如实声明）：
  - `node --check static/page-gallery.js static/page-detail.js static/page-settings.js static/lightbox.js`（逐个或循环）；
  - grep 断言：`grep -rn "api/collections\|api/favorite\|collection_id\|is_favorite\|收藏夹\|collectionPicker\|deleteCollectionModal\|lbFav" static/ templates/` → **零命中**；
  - 用 Node `vm` + 假 DOM 探针（一次性脚本放仓库外）验证图库页与详情页仍能初始化、无对已删 DOM 的绑定（沿用本项目既有做法）；
  - **浏览器手工验收无法在本环境执行**：把「启动服务 → 打开 /gallery、/detail/<pid>、/settings、/ 四页 → 确认无 JS 报错且无残留收藏入口」写进实施记录，标注为未执行。
- [x] **Step 5: 提交**：commit `refactor: 前端移除收藏夹入口与收藏状态`。
- [x] Step 6: 自审（是否有孤立的 CSS 类/事件绑定、被删下拉是否还有 `addEventListener` 指向、`page-detail.js` 的翻页序列在去掉 `collection_id` 后是否仍自洽）。

## Task 5：全量验证、文档回写与收尾

**Files:** Modify `docs/superpowers/specs/2026-09-30-remove-collections-design.md`、本计划、`AGENTS.md`、`docs/architecture.md`、`docs/technical-documentation.md`

- [x] **Step 1: 验收逐条核对**（spec「验收标准」5 条）：
  1. `grep -rn "Collection\|collection\|收藏夹\|get_favorite_pids\|is_favorite" --include=*.py --include=*.html --include=*.js . | grep -v "^./tests/\|^./docs/\|^./migrations/versions.py\|^./pixiv-api-http-main/"` → 运行时代码零命中（`migrations/versions.py` 的 v5 函数与 v1 历史版本、测试与文档除外）；
  2. 全量 `run_tests.ps1 -q` → 失败集合与基线完全相同（仅 4 个 `test_temp_root_*`）；记录实际数字；
  3. 迁移 v5 已在 Task 1 覆盖（带数据旧库、可重复执行、有备份）；
  4. 启动 + 五个页面渲染（无浏览器，用 `python -c "import app"` + 页面路由的 Flask test client 冒烟：`/`、`/gallery`、`/detail/<pid>`、`/settings`、`/cache` 返回 200）；
  5. `grep -rn "api/collections\|api/favorite" static/ templates/` → 零命中。
- [x] **Step 2: 文档回写**：
  - 本 spec：状态改「已实现并验证」，写入 commit 表与实测数字；
  - 本计划：勾选步骤、记录实际顺序与偏差；
  - `AGENTS.md`：文件表删 `routes_collections.py` 行、蓝图 7 → 6、删数据库一节「收藏语义完全由 Collection 驱动」条目、改预取一节 `_is_user_owned()` 描述（收藏夹 → 仅下载日志）、测试清单与 `routes_gallery.py` 行去掉收藏字样；
  - `docs/architecture.md`：模块表 4 行（`models`/`routes_gallery`/删 `routes_collections`/`background`）与「测试契约」表里的收藏符号；
  - `docs/technical-documentation.md`：功能表、目录树、模块表、序列图、API 清单（§13 收藏夹整节、`/api/favorite`、`/api/illust/<pid>/collections`）、数据表两张、FAQ、性能表、`_is_user_owned` 说明。
- [x] **Step 3: 全量复跑 + 提交**：commit `docs: 记录收藏夹功能移除与验证结果`。
- [x] Step 4: 收尾：确认工作树干净；把 worktree 的 venv junction 摘掉后再删除 worktree；报告遗留（见下）。

## 实施记录与勘误（Task 1–4，2026-09-30）

分支 `refactor/remove-collections`（worktree `E:\pixiv\.worktrees\remove-collections`，基线 `fbbc12b`）。基线全量 **688 passed / 2 skipped / 4 failed(env)**。

| 任务 | commit | 交付 |
|---|---|---|
| Task 2 | `627dcb1` | `background._is_user_owned()` 只认用户操作类 DownloadLog；`_prefetch_capacity_cleanup` 去掉 `fav_ids` 快照；`_refresh_bookmarks_pass` 去掉死变量；注释/日志文案改为新语义 |
| Task 1 | `ba34348` | 迁移 v5 `drop_collection_tables`（子表先删、`IF EXISTS`）+ v1 存在性守卫 + 迁移测试 |
| Task 3 | `8fef717` | 删 `routes_collections.py` 与蓝图布线（7→6）；删 `/api/favorite`、`/api/illust/<pid>/collections`、`/api/gallery` 的 `favorites`/`collection_id`；删 `Collection`/`CollectionItem`/`get_favorite_pids`/`to_dict(favorite=)`/`_next_collection_position`/`_mark_favorite`；新增 13 条 404 断言 |
| Task 4 | `663e445` | 前端 8 文件移除收藏入口与状态（`.card-fav-btn`、`#favBtn`、收藏夹选择弹窗、`lbFav`、`activeCollectionId`、`collection_id` 导航上下文、`galleryFavTotal`、`__lbSyncFav`、孤立 CSS） |

**执行中的三处勘误（计划/侦察的漏洞，均已修）**

1. **任务顺序**：实际先做 Task 2 再做 Task 1。`tests/conftest.py::clean_db` 会 `DELETE FROM collection_items`，而 v5 在同一个 session 级测试库里把表删掉 ⇒ **v5 一旦进树，所有用 clean_db 的用例都会报 `no such table`**（实测 HEAD `ba34348` 时为 `4 failed / 413 passed / 280 errors`）。因此 Task 2 的 RED→GREEN 必须在 v5 之前完成，且 **v5 必须与"删掉两张表的所有消费者"（Task 3）同批或紧邻落地**——这是"每个提交都绿"的硬约束，计划里的 Task 1 独立前置是错的。
2. **v1 迁移需要存在性守卫（计划完全没预见到）**：模型删除后 `create_all()` 不再创建 `collection_items`，于是**全新库**（`user_version=0`）跑已发布的 v1 `ALTER TABLE collection_items ADD COLUMN position` 会抛 `no such table: collection_items`，直接让 `init_db()` 失败（每个测试的临时库都是全新库 ⇒ 实测 3 errors）。修法：v1 里加 4 行存在性判断——**表不存在时 no-op，表存在时与原版逐字节相同**，因此对任何已升级过的库行为不变；并补回归测试 `test_fresh_database_skips_legacy_collection_migration`。这触碰了"不得修改已发布版本"的边界，按"只对缺表的新库放行、对存量库零变化"处理。
3. **两处内联 `CollectionItem` 判定是侦察漏项**：`routes_search.py::cache_item_delete` 与 `routes_prefetch.py::prefetch_tags_delete` 各自内联了收藏夹保护（不是走 `_is_user_owned`）。前者不删会 `ImportError` 导致 `import app` 与整套用例失败。删后"已下载作品仍受保护"的语义保留。

**计数核对（后端审查者用 `pytest --collect-only` id 差集实测订正）**：base 收集 **694** 条 → head **682** 条（688→676 passed），即 **−33 删除 / +21 新增（净 −12）**。删的 33 = 9 条 CSRF 矩阵参数（全是收藏端点）+ 7 `TestCollectionItemMove` + 4 `TestCollectionItemPositionAssignment` + 4 `TestFavoriteMembershipContract` + 1 条 gallery 收藏夹排序 + 1 条 gallery R18 收藏夹视图 + 1 条 `to_dict` favorite 覆盖 + 3 条 `TestPositionMigration` + 1 条预取 API + 1 条容量清理 + 1 条刷新保留已收藏；增的 21 = 13 条 404 参数 + 1 条蓝图数 + 2 条迁移 + 3 条 `TestIsUserOwned` + 1 条容量保护 + 1 条预取标签删除。**无仍有主体的覆盖被丢弃**（v1 的 position 回填仍由 `test_migrations.py` 断言），CSRF 矩阵 29 → 20 且删掉的 9 条正好是收藏端点。`venv\Scripts\python -c "import app"` 通过，蓝图列表为 `download/gallery/middleware/prefetch/search/settings`（6 个）。

**Task 5 待办（实施中发现的遗留）**

1. `scripts/_inspect_db.py` 仍把 `collections` / `collection_items` 列在检查表里，现在运行会报错（表已不存在）。
2. `helpers._compute_move_position` 已成死代码（唯一消费者是 `routes_collections.py`）；删它需同步清掉 `architecture.md` / `technical-documentation.md` 里的引用。
3. `templates/settings.html` 的预取说明文案里「…删除未下载未收藏作品（未完成最终刷新的暂不淘汰）」中的「未收藏」已过时（保护判定已不含收藏），需改写成"未下载且无用户下载意图"；该句里的「最终收藏数」必须保留。
4. 浏览器手工验收**未执行**（本环境无浏览器）；前端只做了 `node --check`、零命中 grep 与一次性 Node `vm` + 假 DOM 探针（71 断言通过），可照做的验收步骤见 Task 4 Step 4。

## 遗留与已知风险

1. **不可逆的数据删除**：v5 会真的删掉 `collections` / `collection_items`。迁移前 runner 会自动备份（`backups/pixiv.db.<UTC 时间戳>.bak` + 必要时 `-wal`/`-shm`），部署时请确认该目录可写。
2. **预取保护变窄**：只收藏未下载的作品不再受保护，超出 `prefetch_max_illusts` 时可能被淘汰（下一轮可能重新入库）。这是用户明确接受的行为变化。
3. **浏览器验收无法在本环境执行**：前端改动只做语法/静态/假 DOM 探针验证，真实渲染与交互需人工过一遍（步骤已写入 Task 4 Step 4）。
4. **`technical-documentation.md` 引用的 `PpPpP的收藏夹方案.md`** 作为历史设计文档保留；若该文件不存在于仓库，回写时一并说明。
5. **另一个会话可能同时在动 `background.py`（预取/自动关注）**：本计划的 Task 2 与它相邻，合并前先 `git log --oneline -5 -- background.py` 确认没有冲突面。
