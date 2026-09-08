# Pixiv Viewer — 智能体指南

Flask Web 应用，通过 Pixiv 内部 Ajax API（非官方）搜索/浏览/下载 Pixiv 插画。单人自部署服务。

**本仓库无 README —— 本文件是唯一的工程入口文档**，涵盖命令、架构、约定。设计工作流见文末「设计任务（opendesign）」。

**技术栈**：Python（开发/测试实际运行 3.13，语法下限 3.9+）/ Flask 3.1 / SQLAlchemy 2.0 / SQLite(WAL) / Bootstrap 5.3 / 原生 ES2020 JS（无构建步骤）/ requests / gunicorn / pytest。无 linter、无类型检查、无打包配置。

> 引用位置时用**符号名**而非行号（行号随重构腐化）。需要行号时自行 grep。

---

## 命令

```bash
# 初始化开发环境
python -m venv venv && venv\Scripts\activate && pip install -r requirements-dev.txt

# 可复现部署（使用已验证的精确版本）
pip install -r requirements-lock.txt

# 开发
flask run --debug

# 默认测试（离线；不需要真实 Cookie。完整一轮 270 用例约 15s，见文末「测试」）
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q

# 跑单个文件 / 单条用例 / 按关键字（run_tests.ps1 是 pytest 透传包装，pytest 参数原样可用）
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 tests/test_models.py -q
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 "tests/test_models.py::TestIllustToDict" -q
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q -k "prefetch and capacity"

# 真实 Pixiv 集成测试（必须显式标记 integration + live_pixiv_required fixture）
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -m integration

# 生产部署（必须 -w 1 —— 内存状态是进程级的；--threads 见下）
gunicorn -w 1 --threads 8 --timeout 300 -b 127.0.0.1:8000 app:app
```

---

## 架构

| 文件 | 作用 |
|------|------|
| `app.py` | 组装入口（Flask app / 配置 / 注册 7 个 Blueprint / 启动后台线程）+ 4 个页面路由（`/`、`/cache`、`/csrf-token`、`/favicon.ico`） |
| `config.py` | 常量、`.env` 与 `instance/settings.json` 覆盖；`SETTINGS_KEYS` 是**设置键的唯一来源**（config 与设置页共用） |
| `models.py` | SQLAlchemy ORM：`Illust`、`BlockedTag`、`DownloadLog`、`Collection`、`CollectionItem`、`SearchCache`；`init_db` / `get_session` / `safe_commit` / `get_favorite_pids` |
| `runtime.py` | 进程内存状态（`-w 1` 单进程常驻）：扫描/TTL 缓存、预取状态、下载队列与进度、限流存储、异步搜索任务 |
| `helpers.py` | 纯工具函数与库内查询：下载目录扫描、URL/展示工具、`query_cached_tag`、收藏夹位置计算、文件删除 |
| `middleware.py` | 认证 / CSRF / 限流 / 安全头；app 级钩子（`before_app_request` / `after_app_request`）随 `middleware_bp` 注册 |
| `background.py` | 后台线程与下载引擎：自动关注、预取循环、下载执行器、`start_background_threads()`（幂等）、`_reset_stuck_*` |
| `fetcher.py` | Pixiv API 封装：Cookie 认证、搜索、作品详情、令牌桶限流、后台详情补全 |
| `routes_search.py` | `/search`、搜索任务状态、缓存浏览 `/api/cache/*`、`/api/following` |
| `routes_gallery.py` | 图库、详情页、图片服务、缩略图代理 `/thumb/<b64>`、收藏 API、`/api/open-dir` |
| `routes_download.py` | 下载触发/状态/取消/批量、下载管理页 |
| `routes_prefetch.py` | 预取管理 API（config / tags / status / refresh） |
| `routes_collections.py` | 收藏夹全部路由（items / batch / move） |
| `routes_settings.py` | 登录、设置读写、屏蔽标签、自动关注控制 |
| `templates/*.html` | 8 个 Jinja2 模板（搜索、图库、下载管理、详情、设置、设置解锁、登录、缓存浏览） |
| `static/` | `app.js`（共享工具）+ `page-<name>.js`（按页入口，各模板显式引入）+ `lightbox.js` + `style.css` + `vendor/bootstrap-5.3.3/` |
| `scripts/` | `run_tests.ps1`（pytest 包装：确定性临时目录 + 沙箱插件）、`sandbox_pytest_shim.py`、`pixiv-cleanup.sh`（仅清理已下载原图，与预取容量无关）、`_inspect_db.py` |
| `migrations/` | `runner.py`（按 `PRAGMA user_version` 版本化执行，**升级前自动备份**）+ `versions.py` |
| `pixiv-api-http-main/` | 内置第三方 Node.js Pixiv API 参考实现，**仅作接口格式对照，不参与运行** |
| `docs/` | `architecture.md`（模块地图 + 测试补丁契约，改动前必读）、`maintenance.md`（运维手册）、`superpowers/{plans,specs}/`（近期变更设计文档） |

无 `__init__.py` — 模块直接导入。无 `setup.py` / `pyproject.toml`。

### 加载顺序

依赖单向、无循环 import：

```
config / runtime / helpers（叶子）→ middleware → background → routes_* → app.py（组装）
```

`app.py` import 时序：`init_db()` → `_reset_stuck_downloads()` / `_reset_stuck_prefetch()`（清理残留状态）→ `start_background_threads()` → `atexit.register(_shutdown_background_threads)`。

### 扩展点：新增路由

路由全部挂在 Blueprint 上，共 7 个（`middleware_bp` / `search_bp` / `gallery_bp` / `download_bp` / `prefetch_bp` / `collections_bp` / `settings_bp`）。新增接口时：在对应的 `routes_*.py` 里加路由 → `app.py` 注册该 Blueprint（已注册则无需改动）。**如果新接口的某个依赖将来可能被测试 monkeypatch，该符号必须在 `app.py` 顶部用 from-import 再导出一次**，然后业务代码在函数体内 `import app` 延迟引用 `app.<符号>` —— 见文末「测试契约」。

---

## 变更流程：spec / plan 先行

非平凡改动（新功能、跨模块重构、性能改造）走 `docs/superpowers/` 两阶段：

1. **spec** → `docs/superpowers/specs/`：写清需求、决策与取舍边界。
2. **plan** → `docs/superpowers/plans/`：写清实施步骤。
3. **实现**，然后**回写 spec/plans 标记"已实现 + 验证结果"** —— 这一步不是形式主义，git log 里 `docs: spec 标记已实现并记录验证结果` 一类的提交就是回写记录。

小修（单文件 bugfix、文案、注释）不需要走这套流程。

---

## 提交与分支约定

- **Commit message**：Conventional Commits 前缀 + 中文描述。本仓库实际用过的前缀：`feat:` / `fix:` / `docs:` / `refactor:` / `perf+fix:`（性能与修复混合时）。示例：`fix: 删除接口支持无 DB 记录的孤儿作品（按 downloads 目录删 + 记 DownloadLog）`。
- **分支**：主线 `main`；功能用 `feature/<slug>`，重构用 `refactor/<slug>`。改动在分支上完成后合回 `main`。

---

## 代码风格

仓库无 linter / formatter / 类型检查，以下约定靠人工保持一致：

- **Python**：模块顶部 `from __future__ import annotations`（可放心写 `dict | None` 这类注解）；注释与日志用中文；函数和公开常量写 docstring，说明「为什么」而非复述代码。
- **前端 JS**：**无构建步骤，浏览器直接加载源文件**，所以语法上限 = 目标浏览器原生支持的范围。按代码现状，实际上限是 **ES2020**：`const/let`、箭头函数、模板字符串、async/await 已普遍使用，可选链 `?.` 与空值合并 `??` 也已大量使用（如 `static/page-gallery.js`）。**不要引入需要转译的语法**：ESM `import`、`class` 私有字段 `#x`、装饰器、顶层 await。另外 CSP 是 `script-src 'self'`（无 `unsafe-inline`），**不允许内联 `<script>`，也不允许 `eval` / `new Function`**。

---

## 关键注意事项

### 进程与状态

- **Gunicorn 必须用 `-w 1`，但建议加 `--threads 8`**：内存状态（`_auto_follow_state`、`_prefetch_state`、`_queued_downloads`、`_download_progress`、`download_cancellations`、`_search_tasks`、`_rate_limit_store`、`_scan_cache`、`_db_pids_cache`、`_thumb_failed`、`download_locks`）是**进程级**的 —— 多 worker 不共享，所以 `-w` 必须为 1；而线程共享同一进程内存，所以 `--threads N` 在保持单进程语义的前提下提供并发。
  缺省（sync worker、无 `--threads`）时 gunicorn **一次只处理一个请求**：一页 24 张缩略图会严格串行加载，这是图库首屏慢的主要来源之一。共享状态的线程安全已审计（见文末「并发」），可安全开启。
- **启动即重置**：`_reset_stuck_downloads()` 清除所有 `downloading` 状态并删除残留文件；`_reset_stuck_prefetch()` 把 `SearchCache.status='fetching'` 改回 `done`（否则预取抢占逻辑会让该标签被永久跳过）。
- **限流是每 worker 的内存计数器**：`_rate_limit` 装饰器按 IP 保存时间戳，用于 `POST /login` 与 `/api/settings/unlock`。
- 关键 TTL：`SEARCH_TASK_TTL=600s`、`_SCAN_CACHE_TTL=30s`、`_DB_PIDS_CACHE_TTL=30s`、`_THUMB_FAIL_COOLDOWN=30s`、缩略图并发上限 `config.THUMB_CONCURRENCY`（默认 12）。

### 配置与重启

- **settings.json 需重启服务器**：`config.py` 在 import 时读取 `instance/settings.json` 覆盖全局常量。Web UI 修改后需重启才生效；例外是 `prefetch_interval`（经 `/api/prefetch/config` 或设置页保存后**立即生效**）。
- **config.py 在 import 时执行所有副作用**：读取 `.env`、`settings.json`、生成 `instance/.secret_key` 与 `instance/.cursor_secret`。测试必须在 import 前覆盖 `config.DATABASE_PATH`（见 `tests/conftest.py`）。删除密钥文件会使所有会话/游标失效。
- **`.env` 支持**：用 `os.environ.setdefault`（不覆盖已有环境变量）。
- 新增设置键**只改 `config.SETTINGS_KEYS`**，设置页白名单与默认值由它派生。

### 认证

- **Cookie 认证**：手动创建 `cookies.txt`，存放 `PHPSESSID=xxxxx` 或纯 token。Linux 上优先读 `/etc/pixiv-viewer/cookies.txt`。过期会静默返回空结果。
- **全局访问密码**：`ACCESS_PASSWORD` 非空时启用全站登录墙 —— `before_app_request` 拦截未认证请求，页面 302 到 `/login`，API/POST 返回 401。**留空 = 免认证**。`POST /login` 限流 5 次/分钟 + 失败延迟 1 秒。登录态存 session（`authed`），7 天有效。
- **`COOKIE_SECURE` 默认 true**：本地 HTTP 调试必须设 `COOKIE_SECURE=false`（环境变量或 `.env`），否则登录态不回传。
- **旧 `SETTINGS_PASSWORD` 流程仍保留**：已全局登录则直通设置页，否则走设置解锁页。
- `_AUTH_EXEMPT_PATHS = {'/login', '/favicon.ico', '/csrf-token'}`，`/static` 前缀豁免。
- `/api/open-dir` 仅允许 `remote_addr` 为 `127.0.0.1` / `::1`。

### API 行为

- **`popular_d` 排序需 Pixiv Premium**，非 Premium 静默返回空结果。`/search` 默认排序 `date_d`，空查询回退 `browse_discovery()` 时也用它。
- **搜索是异步的**：`GET /search` 立即返回 `task_id`，后台线程拉取，前端轮询 `/api/search/status/<task_id>`。任务存于 `_search_tasks`，访问 status 时顺带清理过期任务；游标含时间戳，**24 小时过期**。空页去重与死游标作废由前端处理。
- **提交新搜索会取消所有在途搜索任务**（`_submit_search_task` 置位旧任务的 `cancel_event`，单人应用同时只该有一个搜索在跑，旧任务继续拉详情只会烧令牌桶拖慢新搜索）。fetcher 侧取消机制：`SearchCancelledError` + `_cancel_begin/_cancel_end/_cancelled`（与详情预算同款 `threading.local`，预取/后台补全线程不受影响），检查点在 `paginated_search` 翻页前后与 `_fetch_details_parallel` 每个 worker 发请求前；**在途请求照常处理完并入库**（下次搜索命中 `existing_map` 免重拉），未发起的直接跳过。任务终态：`done` / `error` / `cancelled`（cancelled 返回 200）。前端用搜索代数（`searchGeneration`）让旧任务的轮询静默失效。
- **所有 Pixiv 图片请求需 `Referer: https://www.pixiv.net/`**，否则 403。所有 Pixiv 请求**必须经 `fetcher.build_pixiv_session()`** 构造 session，禁止裸建 `requests.Session()`。
- **缩略图代理 `/thumb/<base64_url>`**：仅允许 `https://i.pximg.net/` 白名单，磁盘缓存 7 天 + 失败 URL 冷却，防刷新时打爆图床。
- **热点路径必须复用连接池**：`/thumb` 与 `_fetch_details_parallel` 走 `fetcher.get_pooled_session()`（线程内复用 Session），**不要在这些循环里调 `build_pixiv_session()`**。原因见文末「连接复用」。
- **详情 API 三级令牌桶**：`DETAIL_RATE_PER_MINUTE=45`（前台搜索）、`FILL_RATE_PER_MINUTE=20`（后台补全）、`TOTAL_RATE_PER_MINUTE=60`（总闸）。
- **详情拉取的重试是分类的**：连接错误立即放弃、限流（403/429）退避重试 —— 详见文末「重试策略」，不要在两处同时放开。
- **`PIXIV_BASE_URL`** 可改为代理/镜像地址。
- **预取缓存**：手动在设置页配置预取标签，后台线程按 `prefetch_interval` 用宽松参数（min_bookmarks=1、date_d、R18 不过滤）预取，写入 `Illust`（`prefetch_source=1`）+ `SearchCache`。**`/search` 永远走实时 Pixiv，不命中缓存**；预取结果由独立 `/cache` 页浏览（`GET /api/cache/items`，库内过滤排序分页）。预取作品入库满 1 天刷新一次"最终收藏数"，< 10 且未下载未收藏的删除；超出 `prefetch_max_illusts`（默认 10000）按最终收藏数低优先清理（已下载/下载中/已收藏保护）。
- **最终收藏数刷新有持久化失败状态机**（`illusts.refresh_failed_at`，迁移 v4）：暂时性失败写时间戳、退避 `PREFETCH_REFRESH_BACKOFF`（24h）期内不再入选——**这是防"永久失败的死作品每轮占满 100 个名额"的关键，不要退回"失败即静默 continue"**；404 / 删除类报错返回 `fetcher.DEAD_DETAIL`，未下载未收藏的当场删除（已下载/已收藏标记完成保留）；限流/连接错误返回 `fetcher.RETRYABLE_GLOBAL_DETAIL`，**不记在作品头上**，连续 `PREFETCH_REFRESH_ABORT_STREAK`（3）条即中止本轮；认证失效（`PixivAuthError`）/ Cookie 缺失只中止本轮、不写标记、**不冒泡**（冒泡会让 `_prefetch_loop` 跳过容量清理、上限失效）；失败满 `PREFETCH_REFRESH_FORCE_DONE`（14 天）强制标记完成，交容量清理淘汰。**保护判定统一走 `background._is_user_owned()`**（收藏夹 + 用户操作类 `DownloadLog`，含失败/取消待重试；缓存清理自己写的 `action='prefetch_deleted'` 不算保护）——用户点过下载的作品不当缓存垃圾。**入库节流阀**：`_prefetch_loop` 在入库前看未刷新积压，达到 `prefetch_max_illusts × PREFETCH_INTAKE_PAUSE_RATIO`（0.8）就跳过入库（只跑刷新+清理），回落到 `PREFETCH_INTAKE_RESUME_RATIO`（0.6）才恢复（滞回）——正面处理"入库速率 > 刷新吞吐"。观测与手动干预：`GET /api/prefetch/status` 除运行态外还返回 `refresh`（上一轮结构化统计：processed/ok/deleted_low/deleted_dead/kept_dead/failed_transient/failed_global/force_done/aborted/at）、`pending_refresh`（未完成刷新数，长期积压=吞吐跟不上入库）、`failed_backoff`（退避中数量）、`intake_paused`、`detail_errors`（未命中删除关键词的详情报错样本 message→次数，用于核对关键词清单），设置页「搜索预取」卡片展示；`POST /api/prefetch/refresh-reset`（`{tag}` 或 `{pixiv_id}`，必须指定范围）清空刷新完成/失败标记把作品放回队列，用于 Cookie 权限修复后救回被强制完成/永久退避的作品，下一轮预取生效。

### 数据库

- **写入必须用 `safe_commit()`，不要直接 `db.commit()`**。注意其语义：**失败时 `rollback()` 后原样抛出，不做内部重试**（重试只会得到一次空提交、静默掩盖数据丢失）。`PRAGMA busy_timeout=10000` 提供 10 秒等锁窗口。
- **获取 session 用 `get_session()`**，不要直接 `Session(engine)`（`init_db()` 等启动逻辑除外）。
- **轻量迁移系统**：启动时 `create_all()` 后由 `migrations/runner.py` 按 `PRAGMA user_version` 顺序执行；当前版本 v1 `migrate_collection_positions`（补 `collection_items.position` 并回填）、v2 `migrate_illust_schema`（补 `file_size`/`downloaded_at`/`bookmark_updated_at`/`prefetch_source`/`prefetch_refresh_at`，DROP `description`/`is_favorite`/`favorited_at`）、v3 `repair_illust_schema`、v4 `add_illust_refresh_failed_at`（补 `refresh_failed_at` 刷新失败退避时间戳）。`init_db()` 在迁移后**无条件再跑一次** `repair_illust_schema` 兜底，并额外幂等调用一次 `add_illust_refresh_failed_at`（v4 列不在 v2 的列集里，用于覆盖外部改动丢列）。SQLite < 3.35 时用重建表策略保留 PK/UNIQUE/NOT NULL。**新增 schema 变更必须追加新版本，不得修改已发布版本。**
- **收藏语义完全由 Collection 驱动**：切换收藏即在"我的收藏"收藏夹增删 `CollectionItem`。`Illust.is_favorite` 列已废弃删除，不要再依赖；判断收藏用 `models.get_favorite_pids()`。
- `Illust.to_dict()` **不输出 `local_paths`**（磁盘绝对路径），前端取图走 `/api/image/<pid>/<index>`。
- `tags` / `original_urls` / `local_paths` 是 JSON 文本列，读写走 `*_list` property。库内标签过滤用 SQLite `json_each()`；单条损坏 JSON 会抛 `OperationalError`，图库/缓存查询都有"降级跳过标签过滤"的兜底。

### 请求与中间件

- **所有 POST 接口需 CSRF**：`X-CSRF-Token` 请求头，从 `GET /csrf-token` 或页面内嵌获取；缺失/错误返回 403（`_csrf_required` 装饰器）。
- **上传限制 1MB**（`app.config['MAX_CONTENT_LENGTH']`）。
- **安全头**：CSP（`script-src 'self'`，`style-src` 仍需 `unsafe-inline`，`img-src 'self' data:`）、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`。
- **Werkzeug 请求日志被设为 WARNING** 级别，防 Cookie 泄露到日志。
- 反代后经 `ProxyFix(x_for=1, x_proto=1)` 还原真实客户端 IP（限流与 open-dir 本机判断依赖它）。

### 下载

- **SSL 验证默认关闭**（`SSL_VERIFY = False`）。生产环境已安装 CA 证书时可设为 `True`。
- 下载引擎在 `background.py`：`_download_illust` 用 `download_locks` 去重、支持取消、按 `PAGE_DOWNLOAD_INTERVAL` 在页间间隔；**无 `original_urls` 时不固化为 `done`**，而是置空以便重试。
- 自动关注发现的新作品先 `commit` 再提交下载任务（否则 `_download_illust` 查不到行会静默跳过）。

### 目录

- **`instance/`**：`.secret_key`、`.cursor_secret`、`pixiv.db`（+ WAL/SHM）、`settings.json`、`image_cache/`、`backups/`（迁移前自动备份）。整个目录在 `.gitignore` 中。
- **`image_cache/` 有容量上限**：`config.IMAGE_CACHE_MAX_BYTES`（默认 1 GB）。超出后按 mtime 从旧到新淘汰，落到上限的 90%（`IMAGE_CACHE_TARGET_RATIO`）。淘汰只认本缓存写的文件（32 位 md5 名 + 同名 `.meta`），目录里的其他文件一律不动。扫描受 `IMAGE_CACHE_CLEANUP_INTERVAL`（默认 5 分钟）节流，写入缓存时顺便触发；启动时额外强制跑一次。
  注意这是"最旧写入优先"而非严格 LRU：命中缓存**不**刷新 mtime，否则 ETag 会跟着变、让浏览器那 7 天的本地缓存整体失效。
- **`downloads/`** 和 **`cookies.txt`** 也在 `.gitignore` 中。
- **`CACHE_DIR` 单点定义在 `routes_gallery.py`**，`app.py` 从那里 import，不要另写一份路径。

---

## 测试

- 测试文件：`tests/test_app.py`（路由/API/CSRF/收藏契约/**作者搜索预算与游标步长**）、`test_auth.py`（认证/限流/安全头）、`test_models.py`（模型/迁移）、`test_migrations.py`（迁移 runner/备份）、`test_helpers.py`（下载目录扫描等纯工具函数）、`test_fetcher.py`（API 封装/限流/收藏数补全/**重试策略**/**连接池复用**/**作者搜索切片与结果缓存**/**详情预算**）、`test_prefetch.py`（预取引擎/容量清理）、`test_search_cache.py`（库内缓存查询）、`test_prefetch_api.py`（预取管理 API）、`test_cache_page.py`（缓存浏览 API/页面）、`test_test_setup.py`（测试环境自校验）。
- `conftest.py` 在 **import app 之前**覆盖 `config.DATABASE_PATH` 为临时文件，并设 `AUTO_FOLLOW_INTERVAL=0` / `PREFETCH_INTERVAL=0`（事后覆盖无效，会连到生产库）。
- session 级 `app` fixture 结束后调用 `models.engine.dispose()`，否则 Windows 上无法删除临时 .db 文件（WinError 32）。
- `clean_db` fixture 在每次测试前清空所有表，并重置 `_scan_cache['ts']` / `_db_pids_cache['ts']`。
- 真实 Pixiv 集成测试必须显式使用 `@pytest.mark.integration` 和 `live_pixiv_required` fixture；缺少 Cookie 时 skip。
- `run_tests.ps1` 内部直接调 `venv\Scripts\python.exe`，跑测试**不需要先 activate venv**。它只做两件额外的事：把 `TEMP/TMP` 指到确定性临时根，并在沙箱下加载 `scripts/sandbox_pytest_shim.py`（剥掉 `os.mkdir` 的 `0o700` mode）。本地直接 `venv\Scripts\python.exe -m pytest` 也能跑，但在沙箱环境会踩 WinError 5。

### 最重要的约定：app 命名空间是测试补丁 seam

`app.py` 特意用 from-import 把被测试 monkeypatch 的符号**再导出到 app 命名空间**。路由/后台模块若需读取**可能被测试补丁的 app 命名空间符号**，必须在函数体内 `import app` 延迟导入并限定 `app.<符号>`，禁止模块顶部 `from app import <符号>`（循环 import，且看不到补丁）。

**删除 `app.py` 中任何 from-import 前，先 `grep "app\.<名>" tests/` 核对。** 完整契约表见 `docs/architecture.md`「测试契约」一节。

### 规则：详情/下载类用例必须预置 `original_urls`

`/detail/<pid>` 与 `POST /download/<pid>` 在 `original_urls` 为空时，会惰性调 `_fetch_original_urls()` 走真实网络。**构造这类用例的 `Illust` 时务必预置 `original_urls_list`**，否则离线环境下单次拉取就会拖慢几十秒。

> 历史坑：`test_detail_page_reflects_favorite_membership` 曾因漏填该列，让整套用例从 15s 涨到 **140s**（独占 89%）。修复方式是给用例补 `original_urls_list` + 收敛重试（见下）。

### 并发：`--threads` 下的共享状态约定

生产以 `gunicorn -w 1 --threads 8` 运行，请求由多线程并发处理。共享可变状态必须遵守以下约定（2026-08-29 已按此审计并修复）：

- **容器遍历要加锁**。清理逻辑多为 Python 层推导式（每条之间有字节码边界，可被其他线程抢入），期间被改动会抛 `RuntimeError: dictionary changed size during iteration`。已知并已加锁：`_rate_limit_store`（`_rate_limit_lock`）、`_thumb_failed`（`_thumb_failed_lock`）。
- **"读 → 判定 → 写"必须整体在锁内**。拆成多步时并发请求会各自读到未计入对方的中间状态。限流器是安全控制，这点尤其致命（曾可被并发爆破绕过）。
- **TTL 缓存先写数据、再写时间戳**。反序会留下"时间戳已刷新、数据仍是旧值"的窗口；`_db_pids_cache` 原先甚至在整条 SQL 查询期间都保持着这个窗口，会让已下载作品被误判成孤儿。
- **注销资源时只删自己那份**。`download_locks` 的 pop 必须比对锁对象本身，否则会把并发新任务的锁删掉（见 `background._release_download_lock`）。
- 键集合固定、只改值的 dict（`_auto_follow_state`、`_prefetch_state`）可无锁读写：`jsonify` 遍历时不会有 size change。

**已知可接受（未加锁）**：`fetcher._last_fetch_stats` 并发搜索时可能互相覆盖，仅影响前端展示的"详情拉取统计"；`_scan_cache` / `_db_pids_cache` 并发重建时会重复扫盘/查表（宁可重复，也不要返回脏数据）。

### 性能：已量化的几条约定

改动这些地方前先看数字（8000 件作品的实测基线）：

- **按一批 id 过滤作品用 `json_each`，不要拼分块 `IN`**。`_pid_filter` 把整个 id 数组作为**一个**绑定参数下推；分块 `IN` 在 8000 个 id 时会生成 16k 个绑定参数。实测 `query_cached_tag` 64.2ms → 24.8ms（纯 SQL 部分 64ms → 7ms，快 8.8 倍）。
- **别在循环里发查询**。`list_collections` 曾对每个收藏夹单独 `COUNT`，20 个收藏夹 5.0ms；改成一次 `GROUP BY` 后整条 HTTP 路由 0.9ms。
- **`/api/image` 必须带 `max_age`**。Flask 的 `SEND_FILE_MAX_AGE_DEFAULT` 默认为 `None`，此时 `send_file` 发的是 `Cache-Control: no-cache` —— 已下载原图每次打开灯箱都要发一趟 304 重新校验。已设 `LOCAL_IMAGE_MAX_AGE`（7 天，与 `/thumb` 一致；重新下载会产生新 mtime，ETag 随之变化）。
- **压缩交给反代**。Flask 自身不 gzip，`/api/gallery?limit=50` 响应体约 25 KB。生产前面有 nginx/Caddy 时在那里开 gzip/brotli，不要在应用层加。
- **按作者搜索是唯一走"全量同步详情"的搜索路径，改动前务必先读**。它是 `search_by_user` 传 `illust_factory=_illust_from_detail` 且不传 `defer_details`，因为过滤条件（`hide_r18` / `min_bookmarks`）**必须拿到详情的 tags 才能判定**，而 `profile/all` 只给 id —— 所以过滤必然发生在拉详情之后。对比之下 `search_by_tag` 的 `defer = defer_details or (min_bookmarks == 0)`，列表接口自带 tags/thumb，默认一条详情都不拉。
  三道闸把成本压在可控范围内，**不要绕过任何一道**：
  1. **切片用 `ITEMS_PER_PAGE`（24）而不是 `PER_PAGE`（60）**。`PER_PAGE` 是标签搜索从 Pixiv 上游"白拿"的页大小（一次 HTTP 回来 60 条，多拿不花额外请求）；这里每多切一条就多一次详情请求，60 是纯浪费。注意这会改变游标里 `pixiv_page` 的步长，故游标携带 `ps` 字段，步长对不上时 `routes_search` 会丢弃游标重新搜索（不是报错，是重搜）。
  2. **单次搜索的详情总预算**：`paginated_search(..., detail_budget=N)`。因为 `early_stop` 只数"通过过滤"的条数，筛选严格时一页可能一条都不通过，会一直翻页扫满 `_MAX_SCAN_PAGES`。预算用 `threading.local` 存（搜索任务跑在自己的线程里，天然隔离），只给作者搜索启用，其余路径默认 0（不限）。
  3. **响应缓存**：`search_by_user` 用独立的 `_USER_SEARCH_CACHE_TTL`（600s），远长于标签搜索的 30s —— 后者成本是 1 次 HTTP，前者一页要发整页详情请求，30 秒会在用户看完这一屏之前就失效。代价是新鲜度，所以**缓存键必须带上 `_blocked_fingerprint(blocked)`**，否则改完屏蔽标签要等十分钟才见效；预算中途耗尽的残缺结果与拉不到作品列表的情况都**不写入**缓存。

  **放宽令牌桶是错的**：45/60 每分钟是为绕开 403 实测定的（`fetcher.py` 顶部注释：并发 3 即触发 403）。`_TokenBucket.wait()` 持锁 sleep，`FETCH_DETAIL_WORKERS` 提再高也不会更快（实测单次详情 1.333s）。要提速只能从"少发请求"入手。

规模变大后才需要看的：

- `_scan_local_downloads` 冷扫描 500 个作品目录（1500 文件）约 **43.8ms**，其中 75% 是逐文件 `os.path.isfile()`；换 `os.scandir`（复用 dirent 的 `is_file()`）可快 1.6 倍。有 30 秒 TTL 缓存兜底，当前 `downloads/` 规模很小，暂不值得改。
- `/api/gallery` 的 `to_dict()` 会带上 `original_urls` / `created_at` / `downloaded_at`，而图库网格和灯箱都不消费它们（约占单条体积的 17%）。要裁剪得给 `to_dict()` 加参数，且 `test_models.py::TestIllustToDict` 断言了完整字段集，改动需同步。

### 连接复用：热点路径必须用 `get_pooled_session()`

`build_pixiv_session()` 每次都会新建 `HTTPAdapter` → 新的 urllib3 `PoolManager`，`close()` 后连接池销毁。在循环里对每个 URL / 每个作品调用它，等于**每次请求都重做一次 TCP + TLS 握手**。

实测（本地 HTTP 服务器，无 TLS）：30 次请求 —— 每请求新建 Session = **30 条 TCP 连接**；复用一个 Session = **1 条**。真实环境每条连接还要额外付 1~2 个 RTT 的 TLS 握手，图片越小这笔开销占比越高（图库首屏、灯箱、搜索批量拉详情都踩在这里）。

- 用 `fetcher.get_pooled_session()`：按线程缓存 Session，同线程跨请求复用连接；Cookie 文件 mtime 变化时自动重建。
- 不跨线程共享（`threading.local`），因为 `requests.Session` 不保证线程安全。
- 复用连接被对端单方面关闭时，调 `fetcher.reset_pooled_session()` 丢弃重建。`/thumb` 已内置"快失败重试一次"逻辑（超时类不重试，避免又变成 10s+ 的等待）。

### 重试策略：连接错误 fail fast，限流才退避

`_get_illust_detail` 的重试分三类，语义不同，**不要无脑加重试次数**：

| 错误 | 行为 | 理由 |
|---|---|---|
| 连接类（`requests.ConnectionError`：超时/拒绝/DNS/代理） | **立即返回 `None`，不重试** | 几乎必然重复失败，重试只是空等满 `DETAIL_TIMEOUT` |
| 限流（`403` / `429`） | 递增退避 3s / 9s 后重试 | 暂时性，等待后可能恢复 |
| 其他（`5xx`、读取超时等） | 退避 1s 后重试 | 可能瞬时抖动 |

同时 `build_pixiv_session()` 的 urllib3 适配层设了 `Retry(total=1, connect=0, ...)`：429/5xx 与读取错误在传输层重试一次，但**连接错误不重试**。

两层都放开时，10s 的连接超时会被放大成 **62s**（3 次 attempt × 2 次连接 × 10s ＋ 退避）。收敛后断网单次拉取为 **10s**。改动任一处前先想清楚会不会把重试又叠回去。

> `/api/gallery` 里 `_kick_background_fill` 派生的 daemon 线程也会拉详情，但它不阻塞响应；离线时其报错日志出现在 `N passed` 汇总之后，只是收尾噪声，不计入耗时。

---

## 前端约定

- 无构建步骤：`app.js` 提供共享工具（`$`、`escHtml`、`proxyThumb`、`fmtSize`、`pvCache` 等），每个模板在底部显式引入自己的 `page-<name>.js`。
- CSRF token 从页面 `<meta name="csrf-token">` 读取，POST 统一带 `X-CSRF-Token` 头。
- 搜索结果在前端缓存 30 分钟（`SEARCH_CACHE_TTL`），游标状态恢复兜底 24 小时（与后端游标 TTL 对齐）。

---

## 设计任务（opendesign）

设计类需求（UI 设计、原型、幻灯片、设计系统、品牌设计等）走 opendesign 工作流，**不要按普通前端任务处理**。

**核心原则**：以设计师身份产出，HTML 是输出媒介；有品味有观点，但受上下文约束；不做模板工。

**工作流**：

1. 检查现有设计系统：扫描 `./opendesign/design-systems/*/`
2. 需求收集：对模糊任务做结构化提问（受众、语气、fidelity、输出格式、变体数量等）
3. 收集上下文：读取选中的设计系统、UI kit、代码库、品牌参考
4. 规划：写出简短计划，明确审美选择
5. 构建：输出到 `./opendesign/mockups/<task-slug>/`，并生成 `manifest.json`
6. 校验：fork 校验子代理检查输出是否符合需求
7. 总结：只讲 caveats 和下一步

**设计规范**：

- 无渐变滥用，无 emoji 当图标，无圆角左彩色边框卡片
- 不手绘复杂 SVG，用带等宽标签的占位符
- 避免 Inter / Roboto / Arial 等过度使用的字体
- 触控目标 ≥ 44px；deck 文字 ≥ 24px（1920×1080）
- 占位符标记优于手绘近似

**入口**：用户会说 `/opendesign 设计一个XX页面`、`/opendesign 做一个品牌幻灯片`、`/opendesign 从代码提取设计系统`。技能文件位置：`C:\Users\FLOW\.claude\plugins\marketplaces\manalkaff-opendesign\skills\`。
