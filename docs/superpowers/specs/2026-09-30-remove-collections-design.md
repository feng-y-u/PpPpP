# Pixiv Viewer 移除「收藏夹 / 收藏」功能设计

## 状态

**已实现并验证**（2026-09-30，分支 `refactor/remove-collections`）：收窄保护判定 `627dcb1` → 迁移 v5 `ba34348` → 后端移除 `8fef717` → 前端移除 `663e445` → 审查后收尾（本批）。实测全量收集 682 条 = **676 passed / 2 skipped / 4 failed(env)** 约 21s；4 个失败是 `tests/test_test_setup.py::test_temp_root_*` 的环境性失败（沙箱内子 `powershell.exe` 退出码非 0），基线同样失败，与本次改动无关。

> ⚠️ **对不变式的一处显式偏离（必须记录）**：约定「迁移只追加新版本，不得修改已发布版本」，而 v1 `migrate_collection_positions` 加了 4 行存在性守卫 —— 模型删除后 `create_all()` 不再创建 `collection_items`，全新库（`user_version=0`）与已跑过 v5 的库跑 v1 的 `ALTER TABLE collection_items` 会抛 `no such table`、直接让 `init_db()` 起不来。守卫在**表不存在时 no-op**、**表存在时与原版逐字节一致**（只影响"原版本来就会崩"的调用方），并有回归测试 `test_fresh_database_skips_legacy_collection_migration`；替代方案（删掉 v1 / 重排 `MIGRATIONS`）会改变已发布版本号语义，更糟。

## 背景与问题

「收藏夹」是本应用里**唯一**的用户归属标记：收藏语义完全由 `Collection` 驱动 —— 用户点「收藏」就是在「我的收藏」这个特殊收藏夹里增删一条 `CollectionItem`；判断某作品是否被收藏要靠 `models.get_favorite_pids()`（`Illust.is_favorite` 列已在迁移 v2 删除，没有第二处存储）。

这套机制被三处功能复用，所以移除它不是删一个页面的事：

1. **图库/详情页的收藏入口与过滤**（「添加到收藏夹」「移出收藏夹」「管理收藏夹」、图库的收藏筛选）；
2. **预取容量淘汰的保护名单**：`background._is_user_owned()` 把「在收藏夹里」和「用户操作类 `DownloadLog`」一起当作"用户拥有的作品"，使其不被 `_prefetch_capacity_cleanup` 淘汰；
3. **数据库结构**：`collections` / `collection_items` 两表 + 迁移 v1（`migrate_collection_positions` 补 `collection_items.position` 并回填）+ 迁移 v2 里对 `illusts.is_favorite` / `favorited_at` 的删除。

用户决定不再需要收藏夹，连同「收藏」这个概念一起移除。

## 需求（用户已确认）

1. **彻底移除收藏夹功能，包含「收藏」语义**。不是只删某个入口，而是让收藏夹/收藏在运行时代码里不再存在。
2. **已存在的收藏夹数据：新增迁移 v5 删除 `collections` / `collection_items` 两张表**。遵守仓库约定（只追加新版本、不修改已发布的 v1；`migrations/runner.py` 升级前自动备份）。**不可逆**：收藏夹内容会从库里消失；作品、下载记录、标签一概不删。
3. **预取容量淘汰的保护判定改为只按「已下载 + 用户操作类 `DownloadLog`」**。「收藏」概念消失后，这是唯一还成立的用户归属信号。

## 目标

1. 运行时代码里不再有任何收藏夹/收藏的读写路径、API 端点与 UI 入口。
2. 数据库不再保留这两张表（迁移 v5）。
3. 离线测试保持全绿（已知 4 个 `test_temp_root_*` 为环境性失败，见下），且不再有用例断言收藏行为。
4. `AGENTS.md` / `docs/architecture.md` / `docs/technical-documentation.md` 与代码一致。

## 非目标

- **不新增**替代性的「喜欢 / 标记 / 置顶」功能，也不恢复 `Illust.is_favorite`。
- 不改下载、预取、搜索、限流、认证、缩略图等其它功能的行为（唯一例外见「行为变化」第 3 条）。
- 不为旧 API 保留兼容层或 410 语义：前后端同版本发布，直接移除端点。
- 不改动历史 spec/plan 文档（`docs/superpowers/` 旧文是当时的存档）。

## 决策与取舍

| 决策 | 选择 | 理由 | 代价 |
|---|---|---|---|
| 已存在的收藏夹数据 | 迁移 **v5 删表** | 用户确认彻底移除；留着死表会让 `create_all` 与新代码语义分叉，文档里也要永久挂着一条例外 | 不可逆（有迁移前自动备份兜底） |
| 预取保护判定 | `_is_user_owned()` 改为**仅**「已下载 + 用户操作类 `DownloadLog`」 | 收藏消失后仅剩的、真正表达"用户拥有"的信号 | 「只收藏未下载」的作品不再受保护，见「行为变化」第 3 条 |
| API 端点 | **直接移除**（旧客户端 404） | 单人自部署、前后端同版本 | 无兼容窗口 |
| 迁移实现 | 新增版本函数，**不修改** v1 | 仓库硬约定：已发布版本不得修改 | 需要在 `MIGRATIONS` 里登记并加迁移测试 |

### 迁移 v5 设计（已核实）

- 新函数加在 `migrations/versions.py`：先 `DROP TABLE IF EXISTS collection_items`，再 `DROP TABLE IF EXISTS collections`（**子表先删**：`collection_items.collection_id` 引用 `collections.id`，先删父表在开启外键的库上会失败）。`IF EXISTS` 让重复执行不报错，满足"可重复运行"的验收项。
- 在 `migrations/__init__.py` 的 `MIGRATIONS` 序列末尾追加 `(5, <新函数>)`。`runner.run_migrations` 会校验版本号是**唯一正整数且升序**，并在首个待执行迁移前自动备份数据库（先 `PRAGMA wal_checkpoint(TRUNCATE)` 再复制主库，必要时连 `-wal`/`-shm` 一起复制），因此备份语义无需在 v5 里重复实现。
- `models.init_db()` 里的 `run_migrations(engine, MIGRATIONS)` 调用**无需改动**；`create_all()` 在模型删除后不会再创建这两张表。
- 必须加迁移测试：在带收藏数据的旧库上执行 v5 → 两表消失、`illusts` 数据不受影响、`user_version=5`、重复执行不抛错，且备份文件已生成。

## 行为变化（用户可见 / 可观测）

1. **UI 消失**：设置页「收藏夹管理」卡片与其删除确认弹窗、图库页「管理收藏夹」链接与「添加到收藏夹…」下拉、图库浮动「移出收藏夹」按钮、详情页收藏入口、图库的收藏筛选条件。
2. **API 消失**：`/api/collections*` 全部端点（含 items / batch / move / DELETE）不再注册，访问返回 404。
3. **预取保护名单变化（唯一的非 UI 行为变化）**：过去「只收藏、从未下载」的作品受保护；移除后它们与其它未刷新作品同等对待，预取超出 `prefetch_max_illusts` 时按现有三层淘汰规则**可能被淘汰**（下一轮可能重新入库）。已下载作品与用户操作类 `DownloadLog` 的保护**不变**。
4. **数据库**：升级后 `collections` / `collection_items` 不存在；`illusts` 表不变（不新增列）。

## 不变式（实施时必须保持）

- **迁移只追加新版本**；`init_db()` 的既有兜底调用（`repair_illust_schema`、`add_illust_refresh_failed_at`）语义不变；`create_all()` 在模型删除后不会再建这两张表。
- **`app.py` 的测试补丁 seam 契约**：删除任何 from-import 再导出前，先 `grep "app\.<名>" tests/` 核对（`collections_bp` 与相关符号可能被测试引用）。
- 速率常量（`DETAIL_RATE_PER_MINUTE=45` / `FILL_RATE_PER_MINUTE=20` / `TOTAL_RATE_PER_MINUTE=60`）、认证方式、Cookie 路径与原子写、连接池复用规则**不变**。
- 默认测试**离线**、不读仓库根真实 `cookies.txt`。
- `background._is_user_owned()` 改动后，缓存清理自己写的 `action='prefetch_deleted'` 仍**不算**保护；失败/取消待重试的 `DownloadLog` 仍算保护。
- `-w 1` 单进程内存语义不变。

## 验收标准

1. 运行时代码（`*.py` / `*.html` / `*.js`）里 `Collection` / `collection` / `收藏夹` / `get_favorite_pids` 零命中；`tests/` 中不再有用例断言收藏行为（迁移测试除外，它断言表被删除）。
2. 全量 `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q` 绿，失败集合与基线完全相同（仅 4 个环境性 `test_temp_root_*`）。
3. 迁移 v5 在**带收藏数据**的旧库上可执行、有备份、可重复运行不报错。
4. 启动流程与搜索结果页、图库页、详情页、设置页、缓存页在无收藏夹代码下正常渲染与运行（无 JS 报错、无残留 DOM 绑定目标）。
5. `grep -rn "api/collections"` 在 `static/` 与 `templates/` 零命中。

## 触点清单（已侦察）

> ⚠️ **先分清两个词，实现者最容易在这里误删**：**「收藏数」= Pixiv 的 `bookmark_count`（要保留）**，涉及 `min_bookmarks` 过滤、`/api/gallery` 的收藏数排序与统计、`_prefetch_refresh_bookmarks`（最终收藏数刷新）、设置页「最低收藏数」、缓存页「人气（收藏数）」——**全部不动**。本节说的「收藏/收藏夹」一律指本地 Collection。

| 区域 | 触点 | 规模 |
|---|---|---|
| 路由与布线 | `routes_collections.py` 整文件（9 个 handler）；`app.py` 的 `from routes_collections import bp as collections_bp` + `register_blueprint`（蓝图 7 → 6） | 1 文件删除 + 2 行 |
| 模型 | `models.Collection`、`models.CollectionItem`（含 FK 与 UNIQUE）；`models.get_favorite_pids()`；`Illust.to_dict(favorite=…)` 及其 `is_favorite` 字段 | `models.py` 4 处 |
| 业务/后台 | `background._is_user_owned()` 的 `CollectionItem` 判定；`_prefetch_capacity_cleanup` 里的 `fav_ids` 全量快照；相关中文注释与 `_prefetch_refresh_bookmarks` 文案 | `background.py` ~8 处 |
| helpers | `get_favorite_pids` 导入、`query_cached_tag` 里的 `is_favorite` 回填、`_next_collection_position` | `helpers.py` 4 处 |
| 图库/详情 API | `/api/gallery` 的 `favorites=true` 与 `collection_id=N`（含 JOIN `collection_items`、`favorite_total`、按 position 排序）、`/detail` 的 `to_dict(favorite=…)`、`/api/illust/<pid>/collections`、`/api/favorite/<pid>` GET+POST | `routes_gallery.py` ~30 处 |
| 搜索管线 | `fetcher._mark_favorite()`、结果里的 `is_favorite`、`fav_pids = get_favorite_pids(db)` 预取 | `fetcher.py` 6 处 |
| 模板 | `settings.html` 收藏夹管理卡片 + 删除弹窗；`gallery.html` 的 `.card-fav-btn` 样式、管理收藏夹链接、添加到收藏夹下拉、移出收藏夹按钮、收藏夹下拉；`detail.html` 的 `#favBtn` 与「收藏到…」弹窗 | 3 个模板 |
| 前端 JS/CSS | `page-gallery.js`（`activeCollectionId`、`collection_id` 参数、收藏视图与 `galleryFavTotal`、卡片 ♥、移出/批量加入、两个收藏夹下拉、`__lbSyncFav`）、`page-detail.js`（收藏夹选择弹窗、`#favBtn`、导航上下文里的 `collection_id`）、`page-settings.js`（收藏夹列表/新建/删除）、`lightbox.js`（`lbFav` 按钮与 `/api/favorite` 切换）、`style.css` 的 `.collection-check-item` / `.batch-collection-select` | ~100 命中 |
| 测试 | `test_app.py`(80)、`test_models.py`(25)、`test_migrations.py`(10)、`test_fetcher.py`(5)、`test_prefetch.py`(5)、`test_prefetch_api.py`(3)、`conftest.py`(2) —— 共 19 个测试文件中的 7 个需要改 | 7 个测试文件 |
| 文档 | `technical-documentation.md` ~25 处（功能表、目录树、模块表、序列图、API 清单、数据表、FAQ、性能表）、`architecture.md` 5 处（模块表 4 行 + 测试契约 1 行）、`AGENTS.md` 5 处 | 3 份活文档 |

**端点清单（删除后返回 404）**：`GET/POST /api/collections`、`PUT/DELETE /api/collections/<id>`、`GET/POST /api/collections/<id>/items`、`DELETE /api/collections/<id>/items/<pid>`、`POST/DELETE /api/collections/<id>/items/batch`、`POST /api/collections/<id>/items/<pid>/move`、`GET /api/illust/<pid>/collections`、`GET/POST /api/favorite/<pid>`。

**存档不动**：`docs/superpowers/` 下 2026-07/08 的收藏夹排序、预取、前端重塑等 spec/plan 与 `docs/risk-audit-report.md` 是当时记录，不回改；`technical-documentation.md` 里引用的 `PpPpP的收藏夹方案.md` 作为历史设计文档保留。

## 实施边界与顺序

- **分支**：`refactor/remove-collections`（仓库约定：重构用 `refactor/<slug>`），从当前 `main` 起；为不打扰共享工作树里可能并行的会话，在独立 worktree 中实施。
- **顺序（依赖安全）**：先改测试与调用方 → 移除路由与蓝图布线 → 删除前端入口与过滤 → 删除模型与 `get_favorite_pids` → 新增迁移 v5 → 改 `_is_user_owned()` → 文档回写。
- **每个任务**：先写/改失败测试（红灯）→ 实现 → spec 合规审查 → 代码质量审查 → 修复 → 单次提交（Conventional Commits + 中文）。
- **已知环境噪声**：`tests/test_test_setup.py::test_temp_root_*` 在沙箱下派生 PowerShell 子进程、语言模式不同而失败；基线同样失败，**不要**为了这 4 条改动测试环境。

## 回写清单（实施后）

- 本 spec：状态改为「已实现并验证」+ commit 表 + 测试/验证结果。
- 计划文档：勾选步骤、记录实际顺序与偏差。
- `AGENTS.md`：文件表删 `routes_collections.py` 行、蓝图数量 7 → 6、数据库一节删「收藏语义完全由 Collection 驱动」条目、预取一节改 `_is_user_owned()` 的描述、测试文件清单删「收藏契约」字样、`routes_gallery.py` 行去掉「收藏 API」。
- `docs/architecture.md`：模块地图与「测试契约」表里与收藏相关的符号。
- `docs/technical-documentation.md`：相关 API/数据模型章节。
