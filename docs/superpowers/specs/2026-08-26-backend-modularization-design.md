# 后端模块化设计：app.py 按域拆分（第一期重构）

- 日期：2026-08-26
- 状态：已批准（brainstorming 流程确认，方案 A）
- 范围：仅后端文件拆分与整理；零行为变更

---

## 背景与目标

`app.py` 2182 行，混合 50+ 路由、6 组进程内存状态、4 类后台线程、下载引擎、复杂查询与工具函数（"上帝模块"）。重构目标：按域拆分为职责单一的扁平模块（保持项目「无 `__init__.py`、模块直接导入」约定），为第二期「状态生命周期精化」铺路。

## 核心约束（不可违反）

| 约束 | 保证方式 |
|------|---------|
| `import app` / `from app import app` 全部保持 | `app.py` 保留根目录同名文件作为组装入口（tests 的 13 处 import 零改动） |
| 所有 URL 路径不变 | Blueprint 无前缀注册（`Blueprint('search', __name__)`，`register_blueprint` 不带 url_prefix） |
| 所有 API 响应/错误码/CSRF 语义不变 | 仅搬迁代码，不改逻辑 |
| `-w 1` 进程内状态语义不变 | runtime.py 用模块级变量平级搬运，不做类化 |
| import 时启动后台线程的时序不变 | `start_background_threads()` 函数迁入 background.py，app.py import 时调用（等价时序） |
| 无新依赖、无网络/DB 行为变化 | 纯代码组织 |

## 目标文件结构

| 文件 | 规模 | 职责 |
|------|------|------|
| `app.py` | ~100 行 | 组装：创建 Flask app、注册 Blueprint、启动后台线程；简单页面路由（`/`、`/cache`、`/favicon.ico`） |
| `runtime.py` | ~80 行 | 模块级内存状态平移（见下） |
| `background.py` | ~450 行 | 后台线程与下载引擎（见下） |
| `helpers.py` | ~350 行 | 纯工具函数与查询（见下） |
| `middleware.py` | ~150 行 | 认证/CSRF/限流/安全头中间件 |
| `routes_search.py` | ~250 行 | 搜索任务、缓存浏览、following |
| `routes_gallery.py` | ~350 行 | 图库、详情、图片服务、缩略图代理、收藏 API、打开目录（含 `/gallery`、`/detail/<pid>` 页面路由） |
| `routes_download.py` | ~200 行 | 下载触发/状态/取消/批量/下载管理（含 `/downloads` 页面路由） |
| `routes_prefetch.py` | ~150 行 | 预取管理 API |
| `routes_collections.py` | ~250 行 | 收藏夹全部路由 |
| `routes_settings.py` | ~150 行 | 登录/设置/屏蔽标签/自动关注控制（含 `/login`、`/settings` 页面路由） |

加载顺序（依赖单向，无循环 import）：
`helpers`/`runtime`（叶子）→ `middleware` → `background` → `routes_*` → `app.py`（组装）

## 迁移映射

### runtime.py（状态平移清单）

| 状态 | 说明 |
|------|------|
| `_scan_cache` / `_SCAN_CACHE_TTL` | downloads 目录扫描缓存 |
| `_db_pids_cache` / `_DB_PIDS_CACHE_TTL` | 图库孤儿判定全表 pid 集合缓存 |
| `_thumb_sem` / `_thumb_failed` / `_THUMB_FAIL_COOLDOWN` | 缩略图代理并发与失败冷却 |
| `_auto_follow_state` / `_auto_follow_stop` | 自动关注状态 |
| `_prefetch_state` | 预取状态（interval/pages/max_illusts/标签列表等） |
| `_queued_downloads` / `_download_progress` / `_download_cancellations` / `download_executor` | 下载队列/进度/取消/线程池 |
| `_search_tasks` / `_search_tasks_lock` / `SEARCH_TASK_TTL` | 异步搜索任务 |
| `_rate_limit_store` / `_rate_limit_cleanup_counter` | 内存限流器 |

### helpers.py（工具与查询）

- `_scan_local_downloads`、`_build_orphan_dicts`、`_page_sort_key`、`_get_download_dir`、`_extract_ext`
- `_proxy_thumb`、`_original_to_resized`、`_fmt_num`、`_safe_int`、`_fetch_original_urls`
- `_pid_in_clause`、`query_cached_tag`
- `_delete_illust_files`、`_next_collection_position`、`_compute_move_position`

### middleware.py

- `_rate_limit`、`_get_csrf_token`、`_get_json_body`、`_csrf_required`
- `_AUTH_EXEMPT_PATHS`、`_AUTH_EXEMPT_PREFIXES`、`_is_authed`、`_require_login`、`_safe_next`、`_security_headers`

### background.py

- `_auto_follow_worker`、`_prefetch_one_tag`、`_prefetch_refresh_bookmarks`、`_prefetch_capacity_cleanup`、`_prefetch_loop`
- `_collect_other_tag_pids`、`_remove_pids_from_search_caches`、`_start_prefetch_thread`、`_shutdown_background_threads`
- `_download_illust`
- `start_background_threads()`（新建组装函数：启动 auto_follow 线程 + prefetch 线程 + 下载 executor 初始化；app.py import 时调用保持时序）

### routes_*.py（Blueprint 迁移映射）

| 模块 | 路由 |
|------|------|
| routes_search | `/search`、`/api/search/status/<task_id>`、`/api/cache/items`、`/api/cache/tags`、`/api/cache/items/<pid>/delete`、`/api/following` |
| routes_gallery | `/detail/<pid>`、`/api/detail/<pid>`、`/gallery`、`/api/gallery`、`/api/gallery/tags`、`/api/gallery/<pid>`(DELETE)、`/api/gallery/batch-delete`、`/thumb/<b64>`、`/api/image/<pid>/<index>`、`/api/favorite/<pid>`、`/api/open-dir`、`/api/illust/<pid>/collections` |
| routes_download | `/download/<pid>`、`/api/download/batch`、`/download/cancel/<pid>`、`/download/reset/<pid>`、`/download_status/<pid>`、`/api/download/status/batch`、`/download_file/<pid>`、`/downloads`、`/api/downloads` |
| routes_prefetch | `/api/prefetch/config`、`/api/prefetch/tags`、`/api/prefetch/status`、`/api/prefetch/refresh` |
| routes_collections | `/api/collections` 全部（含 items、batch、move） |
| routes_settings | `/login`(GET/POST)、`/settings`、`/api/settings`、`/api/settings/unlock`、`/api/blocked-tags`、`/api/auto-follow/status`、`/api/auto-follow/config` |

注：`_cancel_download_internal` 随 routes_download 迁移；`_settings_*` 辅助随 routes_settings 迁移。

## 迁移步骤（每步独立 commit + 全量回归）

1. **Step 1**：`runtime.py` + `helpers.py` 创建，app.py 改为 import（行为平移）
2. **Step 2**：`middleware.py` 创建
3. **Step 3**：`background.py` 创建 + `start_background_threads()` + app.py 调用
4. **Step 4**：6 个 `routes_*.py` Blueprint 创建，app.py 注册
5. **Step 5**：收尾——app.py 瘦身核对、模块头注释、新增 `docs/architecture.md`

## 明确不做（本期）

- `create_app()` 应用工厂、包结构（`__init__.py`）、数据库/模型改动、API 行为改动、前端改动、新依赖、状态类化（第二期）

## 验证

- 每步：`powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q` → 预期 208 passed / 4 环境失败（`test_test_setup.py` 沙箱限制），**无新增失败**
- 每日步：`python -m py_compile` 全部 .py
- 最终：`gunicorn -w 1 --timeout 300 -b 127.0.0.1:8000 app:app` 启动验证 + 浏览器冒烟（搜索/图库/下载/缓存/设置/收藏夹/登录 7 类页面与 API）
- 冒烟检查 `import app` 后线程启动日志（auto_follow / prefetch 行为与重构前一致）