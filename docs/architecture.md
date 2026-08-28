# 后端模块架构（模块化重构后）

> 2026-08-26 起实施（分支 `refactor/backend-modularization`；重构前状态 commit `6eafaea`，重构 commit 序列 `e947796`…`632d080`）。
> 本文档给出拆分后的模块职责地图与两条最重要的开发约定：`-w 1` 单进程语义、**app 命名空间测试补丁契约**。

## 模块地图

| 模块 | 职责一句话 | 关键符号 / 说明 |
|---|---|---|
| `app.py` | 组装入口：创建 Flask app、注册全部 Blueprint、启动后台线程；仅剩 4 个页面/辅助路由 | `/`、`/cache`、`/csrf-token`、`/favicon.ico`；import 时序：`init_db()` → `_reset_stuck_*`（清理残留）→ `background.start_background_threads()` → `atexit.register(_shutdown_background_threads)` |
| `runtime.py` | 进程内存状态（`-w 1` 单进程语义，全部模块级变量） | `_scan_cache`/`_SCAN_CACHE_TTL`、`_db_pids_cache`、`_thumb_sem`/`_thumb_failed`/`_THUMB_FAIL_COOLDOWN`、`_auto_follow_state`/`_auto_follow_stop`、`_prefetch_state`、`_queued_downloads`/`_download_progress`/`download_cancellations`/`download_executor`、`_search_tasks`/`_search_tasks_lock`/`SEARCH_TASK_TTL`、`_rate_limit_store` |
| `helpers.py` | 纯工具函数与库内查询 | `_scan_local_downloads`、`_build_orphan_dicts`、`_page_sort_key`、`_get_download_dir`、`_extract_ext`、`_proxy_thumb`、`_original_to_resized`、`_fmt_num`、`_safe_int`、`_fetch_original_urls`、`_pid_in_clause`、`query_cached_tag`、`_delete_illust_files`、`_next_collection_position`、`_compute_move_position` |
| `middleware.py` | 认证/CSRF/限流/安全头；app 级钩子随 `middleware_bp` 注册即全局生效 | `_rate_limit`、`_get_csrf_token`、`_get_json_body`、`_csrf_required`、`_require_login`（`before_app_request`）、`_security_headers`（`after_app_request`）、`_safe_next`、`_is_authed` |
| `background.py` | 后台线程与下载引擎（auto_follow / 预取 / 下载） | `_auto_follow_worker`、`_prefetch_one_tag`/`_prefetch_loop`/`_prefetch_capacity_cleanup` 等、`_download_illust`、`start_background_threads()`（幂等）、`_shutdown_background_threads()` |
| `fetcher.py` | Pixiv API 封装 | Cookie/OAuth 认证、`search_by_tag`/`search_by_user`/`browse_discovery`/`paginated_search`/`fetch_following`、作品详情、`clear_search_cache` |
| `models.py` | SQLAlchemy ORM + DB 会话 | `init_db`、`get_session`、`safe_commit`；Illust/BlockedTag/DownloadLog/Collection/CollectionItem/SearchCache |
| `config.py` | 常量、环境变量覆盖、`instance/settings.json` import 时覆盖 | `DOWNLOAD_DIR`、`PREFETCH_*`、`ACCESS_PASSWORD`、`SETTINGS_PASSWORD`、`COOKIE_SECURE`、`ITEMS_PER_PAGE` 等；**import 时执行全部副作用**（读 `.env`/settings.json、生成密钥） |
| `routes_search.py` | 搜索任务 / 状态轮询 / 缓存浏览 / following | `/search`、`/api/search/status/<task_id>`、`/api/cache/*`、`/api/following`；`_submit_search_task`/`_cleanup_search_tasks` |
| `routes_gallery.py` | 图库 / 详情 / 图片服务 / 缩略图代理 / 收藏 API（含页面路由） | `/gallery`、`/detail/<pid>`、`/api/gallery*`、`/thumb/<b64>`、`/api/image/<pid>/<index>`、`/api/favorite/<pid>`、`/api/open-dir`、`/api/illust/<pid>/collections` |
| `routes_download.py` | 下载触发/状态/取消/批量/下载管理（含页面路由） | `/download/<pid>`、`/api/download/batch`、`/download/cancel|reset/<pid>`、`/download_status/<pid>`、`/api/download/status/batch`、`/download_file/<pid>`、`/downloads`、`/api/downloads`；`_cancel_download_internal` |
| `routes_prefetch.py` | 预取管理 API | `/api/prefetch/config|tags|status|refresh`；`_PREFETCH_SETTINGS_KEYS` |
| `routes_collections.py` | 收藏夹全部路由 | `/api/collections` 全部（含 items / batch / move）；注：`/api/illust/<pid>/collections` 在 routes_gallery，不重复 |
| `routes_settings.py` | 登录 / 设置 / 屏蔽标签 / 自动关注控制（含页面路由） | `/login`(GET/POST)、`/settings`、`/api/settings*`、`/api/blocked-tags`、`/api/auto-follow/*`；`_SETTINGS_PATH`/`_SETTINGS_DEFAULTS`/`_load_settings`/`_settings_locked` |

## 加载顺序

依赖单向、无循环 import：

```
helpers / runtime（叶子）→ middleware → background → routes_* → app.py（组装）
```

- `helpers`/`runtime` 只依赖 config/models/fetcher 与标准库（叶子模块）。
- `middleware` 读取 runtime 状态（限流存储）。
- `background` 依赖 helpers/runtime/models/fetcher。
- 各 `routes_*` 依赖 middleware/helpers/runtime/background/models，**模块间不互相导入**。
- `app.py` 最后组装：创建 app → 注册 7 个 Blueprint（middleware_bp 最先，保证 app 级钩子最先生效）→ 启动后台线程。

## `-w 1` 单进程语义与线程启动

- 全部内存状态（`runtime.py` 模块级变量）依赖**单进程常驻**，生产 gunicorn 必须 `-w 1`；多 worker 不共享（限流、搜索任务、预取/自动关注状态、下载队列均受影响）。
- 线程启动时序（app.py import 时执行，等价重构前）：`init_db()` → `_reset_stuck_downloads()` / `_reset_stuck_prefetch()`（清理残留下载与预取状态）→ `background.start_background_threads()` → `atexit.register(_shutdown_background_threads)`。
- `start_background_threads()` **幂等**：内部以 `_auto_follow_thread is not None` 守卫，重复调用不会启动第二个 auto_follow worker。

## 测试契约：app 命名空间是补丁 seam（最重要约定）

为保持既有测试的 monkeypatch 目标不变，`app.py` 特意用 from-import **再次导出**被测试补丁的符号到 app 命名空间（tests 大量 `app.<符号>` / `monkeypatch.setattr('app.<符号>')` / `setattr(app, '<符号>', ...)`）。**新增代码不得破坏这些导出；删除任何 from-import 前必须先 `grep "app\.<名>" tests/` 核对。**

已知契约示例（非穷尽）：

| app 命名空间符号 | 测试引用 |
|---|---|
| `search_by_tag` / `search_by_user` / `browse_discovery` / `paginated_search` | test_app.py / test_search_cache.py `@patch('app.<名>')` |
| `SEARCH_TASK_TTL`、`_cleanup_search_tasks` | test_app.py monkeypatch + 直接调用 |
| `time`、`threading` | test_prefetch.py monkeypatch |
| `platform`、`os` | test_auth.py 补丁 `app.platform.system` / `app.os.startfile` |
| `_rate_limit_store` | test_auth.py 清空（与 middleware 共享同一 dict） |
| `_scan_cache`、`_db_pids_cache` | conftest.py 重置 `ts` |
| `_prefetch_state` | test_prefetch.py / test_prefetch_api.py 原地改写 |
| `_prefetch_one_tag`、`_prefetch_capacity_cleanup`、`_prefetch_loop`、`_start_prefetch_thread`、`_prefetch_refresh_bookmarks`、`_reset_stuck_prefetch` | test_prefetch.py |
| `build_pixiv_session`、`fetcher`（模块导入） | test_prefetch.py |
| `_SETTINGS_PATH`、`_load_settings` | test_prefetch_api.py 夹具 `setattr(app, '_SETTINGS_PATH', ...)` |
| `ACCESS_PASSWORD`、`SETTINGS_PASSWORD` | test_auth.py |
| `_safe_next` | test_auth.py 直接调用 |
| `query_cached_tag` | test_search_cache.py 直接调用 |

**新增对 `app.<符号>` 的读取规则**：路由/后台模块若需读取**可能被测试 monkeypatch 的 app 命名空间符号**，必须在函数体内 `import app` 延迟导入并限定 `app.<符号>`（先例：routes_search.py:34/99、routes_prefetch.py:42/151、routes_settings.py:44/127/144/162/188），并在该行注释 `# 延迟导入…tests monkeypatch('app.<符号>')`。禁止在模块顶部 `from app import <符号>`（会造成循环 import，且看不到测试补丁）。

## 本次重构 commit 起点

- 设计 spec：`docs/superpowers/specs/2026-08-26-backend-modularization-design.md`（`416d147`）
- 实施计划：`docs/superpowers/plans/2026-08-26-backend-modularization.md`（`a3dcb58`）
- 重构 commit 序列：`e947796`（runtime+helpers）→ `3d05564` → `e20bcac`（middleware）→ `ee500ca` → `ce597fc`（background）→ `0484e23` → `5b26719`（routes_search/gallery）→ `c74fb45` → `73a8f0e`（routes_download/prefetch/collections/settings）→ `e228e49`（settings 写盘命名空间修正）→ `632d080`（收尾：app.py import 面清理 + architecture 模块地图文档）→ 收尾第二轮（残留死 import 清除 + 测试契约标注 + 本文档行号修正）
- 重构前状态（起点）：`6eafaea`（前端重塑落地 main 之后）