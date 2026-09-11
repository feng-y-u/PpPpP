# findings.md — 源码分析发现（持续追加）

## Phase 0 盘点
- 项目本身无 README；AGENTS.md 是唯一工程入口文档（32,976 B）。
- 核心后端文件：app.py(152 行) config.py(167) models.py(258) runtime.py(80) helpers.py(426) middleware.py(147) background.py(731) fetcher.py(1373) + 7 个 routes 模块（search 343 / gallery 576 / download 231 / prefetch 212 / collections 248 / settings 243）。
- 测试 13 个文件；模板 8 个；static JS 10 个 + style.css + vendor/bootstrap-5.3.3。
- migrations：runner.py(53 行, PRAGMA user_version 版本化 + 备份) + versions.py(139 行, v1-v4)。
- scripts：run_tests.ps1 / sandbox_pytest_shim.py / pixiv-cleanup.sh / _inspect_db.py。
- 依赖：requirements.txt(4 依赖) / requirements-dev.txt(+pytest) / requirements-lock.txt(22 精确版本)；pytest.ini（marker: integration）。
- docs/：architecture.md（模块地图+测试契约） + maintenance.md（运维手册）+ superpowers/{specs,plans}/ 约 20 个设计文档。
- 运行数据：instance/{pixiv.db(+wal/shm), settings.json, .secret_key, .cursor_secret, backups/pixiv.db.20260826T142945.bak}；downloads/ 5 个作品；cookies.txt；image_cache/。
- 干扰目录（排除）：venv/、.git/、.btx/、.pytest-tmp/、.test-tmp/、.worktree-patch/、opendesign/、pixiv-api-http-main/（第三方参考实现）。

## Phase 1 核心后端精读要点

### 技术栈（确认）
- Python 3.13（venv 实际运行；语法下限 3.9+ `from __future__ import annotations`）；Flask 3.1.3；SQLAlchemy 2.0.51；SQLite（WAL、busy_timeout=10000、synchronous=NORMAL）；requests 2.x；gunicorn 26（仅 Linux 生产）；pytest 9.1.1；Bootstrap 5.3.3（本地 vendor）；原生 JS ES2020。
- 无 linter/formatter/类型检查/打包配置（pyproject.toml、setup.py 均无）。

### 入口与启动（app.py）
- 模块级副作用：logging.basicConfig → werkzeug 日志降 WARNING（防 Cookie 泄露）→ Flask app → ProxyFix(x_for=1,x_proto=1) → SECRET_KEY（instance/.secret_key，空则重生）→ session 加固（HTTPOnly/SameSite=Lax/Secure=COOKIE_SECURE/7 天）→ 注册 7 个 Blueprint → mkdir downloads/CACHE_DIR → enforce_image_cache_limit(force=True)（启动兜底清缓存）→ init_db() → _reset_stuck_downloads()/_reset_stuck_prefetch() → start_background_threads() → atexit 注册 shutdown。
- 4 个页面路由：/、/cache、/csrf-token、/favicon.ico(204)。__main__ 分支 app.run(host=0.0.0.0, port=5000, debug=False)。
- app 命名空间测试补丁契约：大量 from-import 再导出（search_by_tag 等 20+ 符号）；业务代码函数体内 `import app` 延迟引用。

### config.py
- import 时副作用：读 instance/.cursor_secret（游标 HMAC 密钥，不存在则生成）→ 手写 .env 解析（setdefault，不覆盖已有环境变量）→ Cookie 路径（Linux 优先 /etc/pixiv-viewer/cookies.txt）→ 常量定义 → settings.json 覆盖 SETTINGS_KEYS（16 个键 → 常量名映射，唯一来源）。
- 关键常量：PIXIV_BASE_URL、SEARCH_PAGES=10、PER_PAGE=60、DETAIL_TIMEOUT=(10,30)、DETAIL_MAX_RETRIES=2、FETCH_DETAIL_WORKERS=5、PREFETCH_*（interval 3600/pages 3/max 10000/backoff 86400/force done 14d/abort streak 3/batch 300/evict 3d）、MEDIUM_IMAGE_SIZE=600、THUMB_CONCURRENCY=12、IMAGE_CACHE_MAX_BYTES=1GB/TARGET_RATIO=0.9/CLEANUP_INTERVAL=300s、DOWNLOAD_MAX_WORKERS=2、PAGE_DOWNLOAD_INTERVAL=3、MAX_BOOKMARKS_DEFAULT=0、ITEMS_PER_PAGE=24、AUTO_FOLLOW_INTERVAL=600、PROXY=''、SSL_VERIFY=False、SETTINGS_PASSWORD/ACCESS_PASSWORD（环境变量）、COOKIE_SECURE 默认 true。

### models.py（6 表 ORM）
- Illust（index: pixiv_id unique, user_id, (download_status, created_at)）：pixiv_id/title/user_id/user_name/tags(JSON Text)/page_count/bookmark_count/bookmark_updated_at/upload_date/thumb_url/original_urls(JSON)/local_paths(JSON, nullable)/download_status/downloaded_at/file_size/prefetch_source/prefetch_refresh_at/refresh_failed_at/created_at。tags_list/original_urls_list/local_paths_list property；to_dict() 不输出 local_paths。
- BlockedTag（tag unique）、DownloadLog（pixiv_id indexed, action, message, created_at）、SearchCache（tag 主键, illust_ids JSON, cached_at, status, error, total）、Collection（name unique, description, timestamps）、CollectionItem（collection_id FK, pixiv_id indexed, position float, UniqueConstraint(collection_id,pixiv_id)）。
- init_db：create_all → run_migrations(MIGRATIONS) → 无条件再跑 repair_illust_schema + add_illust_refresh_failed_at 兜底。
- safe_commit：失败 rollback 后原样抛出（不内部重试）；get_session；get_favorite_pids（'我的收藏' CollectionItem 集合）。

### runtime.py（进程内存状态，-w 1 语义）
- _scan_cache(30s)、_thumb_sem(12)/_thumb_failed/_THUMB_FAIL_COOLDOWN=30s、_db_pids_cache(30s)、_auto_follow_state/_auto_follow_stop、_prefetch_state（含 refresh_stats）、download_executor(2)/download_cancellations/_queued_downloads/_download_progress、_rate_limit_store、_search_tasks/_search_tasks_lock/SEARCH_TASK_TTL=600s。

### middleware.py
- _check_rate_limit（加锁读-判-记-清一体，5 次/60s 默认）、_rate_limit 装饰器、_get_csrf_token（session 内 token_hex(16)）、_get_json_body（silent 解析，非 dict 返回 {}）、_csrf_required（hmac.compare_digest X-CSRF-Token）。
- 认证：_is_authed（延迟 import app 读 ACCESS_PASSWORD）、before_app_request _require_login（豁免 /login /favicon.ico /csrf-token /static；API/POST 401，页面 302 登录）。
- _safe_next 防开放重定向（拒绝 //、\、控制字符）。
- after_app_request 安全头：nosniff、X-Frame-Options DENY、Referrer-Policy no-referrer、CSP（script-src 'self'、style-src unsafe-inline、img-src 'self' data:、frame-ancestors none、base-uri、form-action）。

### fetcher.py（1373 行，核心）
- 认证：_load_cookie（mtime 缓存，PHPSESSID= 前缀或纯 token）；build_pixiv_session（UA/Referer/Cookie/PROXY/SSL_VERIFY/Retry total=1 connect=0, status_forcelist 429,500,502,503）；get_pooled_session/reset_pooled_session（threading.local 连接池，Cookie mtime 变自动重建）。
- 游标：encode_cursor/decode_cursor（urlsafe b64 + HMAC-SHA256 签名，CURSOR_SECRET）。
- 限流：_TokenBucket（全局间隔锁）；三级桶：DETAIL_RATE_PER_MINUTE=45（前台）、FILL_RATE_PER_MINUTE=20（后台）、TOTAL_RATE_PER_MINUTE=60（总闸）。
- 详情重试分类（_get_illust_detail）：连接错误立刻放弃（RETRYABLE_GLOBAL_DETAIL）；403/429 递增退避 3s/9s；其他退避 1s；401 → PixivAuthError；404/删除关键词 → DEAD_DETAIL（return_dead 模式）；未识别报错采样 _record_detail_error。
- 详情预算 threading.local：_budget_begin/_budget_consume/budget_exhausted；USER_SEARCH_DETAIL_BUDGET_PAGES=2。
- 取消机制：SearchCancelledError + _cancel_begin/_cancel_end/_cancelled（threading.local）。
- _fetch_details_parallel：ThreadPoolExecutor(FETCH_DETAIL_WORKERS) + early_stop 流式过滤 + 连接池复用 + CancelledError 处理。
- 入库：_insert_new_illusts 用 SQLite `INSERT ... ON CONFLICT DO NOTHING` 冲突容忍 + 按 pid 回查赢家行。
- _process_items：去重→过滤（blocked/min_bookmarks/hide_r18）→defer/同步详情→入库；后台补全 _background_fill_details/_kick_background_fill（_fill_limit 20/min、_FILL_ATTEMPT_INTERVAL=300s、_filling_ids 去重）。
- 搜索 API：search_by_tag（/ajax/search/illustrations，s_mode=s_tag）、browse_discovery（/ajax/discovery/artworks）、search_by_user（profile/all + 详情全量同步 + ITEMS_PER_PAGE 切片 + detail_budget）、fetch_following（/ajax/follow_latest/illust）、_get_user_profile_ids 缓存(600s)。paginated_search 游标驱动分页（pixiv_page/skip_count/created_at）。
- 内存缓存：_SEARCH_CACHE OrderedDict（TTL 30s、max 64）、_USER_SEARCH_CACHE_TTL=600s + _blocked_fingerprint。
- 其他：_original_to_resized（img-original → c/600x600 img-master _master1200）、_parse_tags/_parse_date/_extract_original_urls/_is_blocked/_is_r18/R18_TAGS。

### background.py（731 行）
- _reset_stuck_downloads（删残留文件+DownloadLog failed）/ _reset_stuck_prefetch（fetching→done）。
- _auto_follow_worker：fetch_following 最多 10 页 → 去重 → 查重 → 新作品入库 → 可选自动下载（先 commit 再 submit）。
- 预取：_prefetch_one_tag（原子抢占 status='fetching' → search_by_tag(min_bookmarks=1, date_d, all) 每页翻转 prefetch_source → 累积合并 illust_ids）；_prefetch_loop（遍历标签 → 刷新收藏数 → 容量清理）；_prefetch_refresh_bookmarks/_refresh_bookmarks_pass（满 1 天刷新最终收藏数；<10 且未保护删除；DEAD_DETAIL 删除/保留；失败退避 refresh_failed_at；全局失败 streak=3 中止；PixivAuthError/FileNotFoundError 只中止不冒泡；stats 结构落 _prefetch_state['refresh_stats']）；reset_prefetch_refresh（tag 或 pixiv_id，json_each 下推）；_prefetch_capacity_cleanup（三层淘汰：已刷新→推不动(失败过/超 3 天)→兜底；保护=下载中/已下载/_is_user_owned）；_is_user_owned（CollectionItem 或 DownloadLog action in start/failed/cancelled/done/deleted）。
- 下载引擎：_download_illust（download_locks 去重、取消检查、progress、分页间隔 3s、无 original_urls 置空不固化 done、失败删文件记 failed、取消删文件记 cancelled、完成记文件数+字节）；_release_download_lock 只删自己的锁。
- start_background_threads（幂等：_auto_follow_thread 守卫）+ _shutdown_background_threads（atexit）。

### 路由层（7 Blueprint，注册顺序 middleware→search→gallery→download→prefetch→collections→settings）
- routes_search：POST /search 异步任务（task_id，提交即取消在途任务；游标 24h 过期；ps 步长校验；detail_budget=24×2）；GET /api/search/status/<id>（404 TASK_LOST；error→401/502）；/api/cache/items（query_cached_tag 分页）、/api/cache/tags（json_each DISTINCT 500）、POST /api/cache/items/<pid>/delete；/api/following。
- routes_gallery：/thumb/<b64>（白名单 i.pximg.net、磁盘缓存 md5+ext+.meta、并发信号量 12、失败冷却 30s、快失败重试一次、原子写 tmp+rename、enforce_image_cache_limit）；/api/image/<pid>/<index>（DB 行→downloads 目录，ETag 7 天）；/detail/<pid>（惰性取原图+related 6 件+medium/original proxied）；/api/detail/<pid>；/gallery 页面；/api/gallery（本地扫描+DB 过滤 json_each+孤儿补全+fill_ids 后台补全；分片 IN 500）；/api/gallery/tags；DELETE /api/gallery/<pid>（含孤儿）；/api/gallery/batch-delete；/api/illust/<pid>/collections；/api/open-dir（仅 127.0.0.1/::1）；/api/favorite/<pid> GET/POST。
- routes_download：POST /download/<pid>（惰性拉原图→入队）；/api/download/batch；/download/cancel|reset/<pid>；/download_status/<pid>；/api/download/status/batch；/download_file/<pid>（单文件直发/多文件 ZIP_STORED 内存缓冲，标题净化）；/downloads 页面；/api/downloads（active/queued/completed 30/logs 50）。
- routes_prefetch：/api/prefetch/config GET/POST（写 settings.json 后同步内存）；/api/prefetch/tags GET/POST/DELETE（删除时保留其他标签引用/已下载/已收藏）；/api/prefetch/status（running/last_check/refresh stats/pending_refresh/failed_backoff/detail_errors）；/api/prefetch/refresh-reset POST；/api/prefetch/refresh POST（线程触发 _prefetch_one_tag）。
- routes_collections：/api/collections CRUD（GROUP BY 计数防 N+1）；items GET/POST/DELETE（position=_next_collection_position 1000 步长）；items/batch POST/DELETE；items/<pid>/move（_compute_move_position 二分割点/边界 ±1000/rebalance；乐观锁 UPDATE ... AND position=:op，失败 409）。
- routes_settings：/login GET/POST（限流 5 次/min + 失败 sleep 1s + hmac 比对 + session authed 7 天）；/api/auto-follow/status|config；/api/blocked-tags GET/POST/DELETE（clear_search_cache）；/settings 页（_settings_locked：全局登录直通，否则 SETTINGS_PASSWORD 流程）；/api/settings/unlock（限流同登录）；/api/settings GET（脱敏 _password/cookie 返回 ''）/POST（cookie 写 cookies.txt+控制字符剔除+立即更新内存；设置键白名单 _SETTINGS_DEFAULTS=SETTINGS_KEYS 排除密码与 cookie_secure；prefetch_* 保存后同步内存立即生效）。

### migrations
- runner.py：run_migrations 校验版本唯一递增 → PRAGMA user_version → 有 pending 则备份（instance/backups/<db>.<ts>.bak）→ 逐版本事务执行 + 写 user_version。backup_database 时间戳唯一（重名加计数器）。
- versions.py：MIGRATIONS = v1 migrate_collection_positions（补 position REAL + 回填计数器×1000）、v2 migrate_illust_schema（补 file_size/downloaded_at/bookmark_updated_at/prefetch_source/prefetch_refresh_at；删 description/is_favorite/favorited_at；SQLite<3.35 重建表保留 PK/UNIQUE/NOT NULL/DEFAULT；索引保证）、v3 repair_illust_schema（=v2 幂等）、v4 add_illust_refresh_failed_at。LATEST_SCHEMA_VERSION=4。

### 依赖（requirements）
- 直接依赖仅 4 个：Flask>=3.1,<3.2、SQLAlchemy>=2.0,<2.1、requests>=2.32,<3、gunicorn>=23,<27；dev +pytest>=8,<10。lock 22 个精确版本（Flask 3.1.3、SQLAlchemy 2.0.51、requests 2.34.2、gunicorn 26.0.0、pytest 9.1.1、Werkzeug 3.1.8 等）。
- 注意：requirements-dev.txt 的 pytest>=8,<10 与 lock 的 pytest==9.1.1 一致；gunicorn 26 在 requirements 直接依赖 vs AGENTS.md 说 gunicorn>=23,<27 一致。

### 配置（当前 instance/settings.json 实际值，非敏感）
proxy=http://127.0.0.1:7890、download_max_workers=1、per_page=60、search_pages=10、max_bookmarks_default=100、auto_follow_interval=600、auto_follow_download=false、fetch_detail_workers=5、medium_image_size=600、items_per_page=24（无密码类键——settings.json 不含 access/settings 密码）。

## 架构结论（分析）
- 分层：Web/路由层(routes_*) → 服务/封装层(fetcher/background/helpers) → 数据层(models) → SQLite；横切中间件(middleware)；进程内存状态层(runtime)。
- 依赖单向无环：config/runtime/helpers(叶子) → middleware → background → routes_* → app.py。
- 线程模型：gunicorn -w 1 --threads 8（多线程共享进程内存；共享状态锁约定见 AGENTS.md）。
- 数据流（图片）：浏览器 → /thumb 代理 → i.pximg.net（磁盘缓存 instance/image_cache）；搜索：/search(task) → 后台线程 fetcher → Pixiv Ajax → 存 illusts → 前端轮询 status；下载：POST /download → executor 线程 → 写 downloads/<pid>/ → DownloadLog。
- 外部系统：Pixiv Web Ajax API（非官方）；无其他第三方服务。无消息队列/缓存中间件（内存缓存+SQLite 自实现）。

## 待补充（子代理报告回收后再记）
- tests 体系细节、前端细节、脚本/运维细节。

## 实测环境事实（2026-xx 只读检查 instance/pixiv.db）
- PRAGMA user_version=3（v4 尚未应用）、journal_mode=wal、sqlite 3.50.4。
- 表行数：blocked_tags=0、collection_items=0、collections=1（=「我的收藏」）、download_logs=44、illusts=1456、search_cache=1。
- illusts 实际列缺 refresh_failed_at（证明该库没有被 v4 代码 init 过；当前代码 init_db 会补列+迁移到 v4，并在迁移前自动备份）。
- 路由总数：61 个（app 4 + collections 11 + prefetch 8 + download 9 + gallery 13 + settings 12 + search 6 处? 复核：search 6、gallery 13、download 9、prefetch 8、collections 10、settings 11、app 4 = 61）。→ 准确计数：app.py 4、routes_search 6、routes_gallery 13、routes_download 9、routes_prefetch 8、routes_collections 10、routes_settings 11，合计 61。

## 测试体系子代理结论（699f2a5a）
- 12 个 pytest 文件约 270 用例；conftest.py 于 import config 后、models/app 前覆盖 DATABASE_PATH；clean_db 每用例清表。
- 全库无 integration 标记用例——pytest.ini 的 marker 与 live_pixiv_required 是死代码；当前 100% 离线 mock。
- 打补丁模式：@patch('app.*') 10 处 + monkeypatch 25+ 处，验证了 app 命名空间契约。
- 覆盖薄弱/缺失：routes_download 零测试、_download_illust/_reset_stuck_downloads 零测试、/thumb 与 /api/image 零测试、settings POST 写盘零测试、迁移备份还原未测、自动关注未测、TTL 缓存未断言。
- 工程问题：8 个 sleep/轮询（flaky 风险）、TestRateLimitConcurrency 40 轮 × 8 线程、TestIllustToDict 精确键集断言脆弱、test_csrf_changes_per_session 命名与断言相反、重复辅助代码（_get_token/_FakeSession）、AGENTS.md 对 test_helpers 描述漂移。
- test_cleanup_script.ps1 验证 pixiv-cleanup.sh 契约（Linux bash+sqlite3）。

## 脚本精读（已亲自读）
- run_tests.ps1：沙箱探测（TEMP 含 dsh-）→ 临时根解析（PIXIV_TEST_TMP > 沙箱 .pytest-tmp > LOCALAPPDATA\pixiv-viewer-test-tmp）→ 每次新 run-<pid>-<guid> basetemp → 沙箱时设 PIXIV_DSH_SANDBOX=1 + PYTHONPATH=scripts + -p sandbox_pytest_shim → venv\Scripts\python.exe -m pytest。exit 透传。
- sandbox_pytest_shim.py：pytest_configure 时剥 os.mkdir 的 mode 参数（沙箱把 mode 映射成 ACL 导致 WinError 5）。
- pixiv-cleanup.sh：只清 downloads/ 已下载原图（30 天前 + bookmark_count<100 + status=done），python 内联删除（realpath 越界保护），置 download_status='cleaned'/local_paths=NULL；不碰 SearchCache；可用 PIXIV_DB/PIXIV_DOWNLOADS 覆盖；logger 记日志。

## Phase 2 架构分析（主代理，源码依据）

### 分层与依赖（单向无环，源码 import 验证）
```
config ─┐
runtime ┼→ helpers ─┐
models ─┤           ├→ middleware → background → routes_search/gallery/download/prefetch/collections/settings → app.py
fetcher ┘           │
（fetcher ← helpers/background；models ← 几乎所有模块）
```
- 叶子：config / runtime / models / fetcher（fetcher 依赖 config+models）。
- helpers 依赖 config/models/fetcher/runtime。
- middleware 依赖 runtime（限流 store）+ 函数体内延迟 import app（认证位）。
- background 依赖 fetcher/helpers/runtime/models。
- routes_* 依赖 middleware/helpers/runtime/background/models/fetcher，模块间互不 import。
- app.py 最后组装：7 个 Blueprint 注册顺序 middleware_bp 最先（app 级钩子先生效）。

### 请求生命周期（源码综合）
1. 请求 → ProxyFix（还原客户端 IP）→ 7 Blueprint 的 before_app_request（middleware._require_login 认证墙：豁免 /login /favicon.ico /csrf-token /static；未认证：API/POST 401、页面 302 login）。
2. 路由分发（61 个路由）→ 视图函数内参数校验（白名单枚举）→ 业务逻辑（fetcher/background/helpers）→ DB 读写（get_session/safe_commit）。
3. POST 接口先过 _csrf_required（X-CSRF-Token 头，hmac.compare_digest）；登录/unlock 再叠 _rate_limit（5 次/min）+ 失败 sleep 1s。
4. 响应 → after_app_request 安全头（CSP/nosniff/DENY/no-referrer）→ 返回。

### 核心异步流程
- 搜索：GET /search → _submit_search_task（取消所有在途任务）→ daemon 线程 fn() = paginated_search(标签搜索/发现/作者搜索) → fetcher 走 Pixiv Ajax → _insert_new_illusts 入库 → task 状态 done/error/cancelled → 前端轮询 /api/search/status/<id>（TTL 600s 清理）→ 渲染。
- 预取：后台线程 _prefetch_loop（interval 默认 3600s）→ 遍历 SearchCache 标签 _prefetch_one_tag（fetching 原子抢占）→ search_by_tag 入库+prefetch_source=1 → _prefetch_refresh_bookmarks（满 1 天刷最终收藏数）→ _prefetch_capacity_cleanup（三层淘汰）。
- 自动关注：_auto_follow_worker（interval 600s）→ fetch_following 最多 10 页 → 新作品入库 → 可选自动下载入队。
- 下载：POST /download/<pid> → download_executor.submit(_download_illust) → 锁去重（download_locks）→ 逐页流式下载（PAGE_DOWNLOAD_INTERVAL=3s）→ 写 downloads/<pid>/ → DownloadLog → 进度 _download_progress。
- 图片：/thumb/<b64> → 白名单校验 → 磁盘缓存命中（md5 名+meta）→ 未命中实时拉 i.pximg.net（信号量 12 + 失败 30s 冷却 + 快失败重试）→ 原子写缓存 → 7 天 Cache-Control。

### 数据流（图片/元数据/状态三维）
- 元数据：Pixiv Ajax → fetcher 解析 → illusts 表 → to_dict() → JSON API → JS → DOM。
- 图片字节：i.pximg.net → /thumb 代理（磁盘缓存 image_cache/）→ 浏览器；原图：下载引擎 → downloads/<pid>/ → /api/image 本地发送。
- 状态：进程内存 runtime（下载队列/进度/搜索任务/限流/预取状态）↔ DB 持久状态（download_status/prefetch_* 列/SearchCache.status）。

### 线程模型（AGENTS.md + 源码确认）
- Gunicorn -w 1 --threads 8：多线程共享进程内存；共享可变容器加锁清单：_rate_limit_store(_rate_limit_lock)、_thumb_failed(_thumb_failed_lock)、_search_tasks(_search_tasks_lock)、download_locks(_download_locks_guard)、_queue...（GIL 内原子）、fetcher 内 _SEARCH_CACHE/_user_profile/_filling_ids/_detail_error_samples 均有锁。
- 已知可接受无锁：_last_fetch_stats 覆盖、_scan_cache/_db_pids_cache 重复重建。
- 关键：init_db 后先 reset stuck 再起线程；atexit 停线程。

### 配置流
.env（setdefault）→ config 常量 → settings.json 覆盖（import 时，需重启；prefetch_interval 例外经 API 立即生效）→ 设置页读写（_SETTINGS_DEFAULTS 白名单）→ /api/prefetch/config 写盘+同步内存。

## 前端子代理结论（03fc7905）
- 模板↔脚本一一对应：每模板底部 app.js+page-*.js；全部含 csrf-token meta 与内联 <style>；settings_unlock/login 无 bootstrap bundle。
- app.js 工具：$/$$/escHtml/escAttr/proxyThumb/fmtSize/fmtNum/showToast/pvCache/triggerDownload+pollDl/lazyLoad/renderInChunks/全局 load·error 捕获（pvCache 在 app.js:44）。
- 搜索轮询 2s 递归 + searchGeneration 代数防竞态（page-index.js::11/237/320）；CURSOR_EXPIRED 24h TTL 恢复。
- lightbox.js：扁平图片级导航（moveFlat/locate），discover() 经 /api/detail 探测 local/medium_urls；/api/image 由后端填充。
- 缓存：pvCache localStorage {ts,value}；pv_search_state 30min+24h 兜底；pv_gallery_* 30min；sessionStorage r18 偏好/pv_detail_seq。
- 约束合规：ES2020、无内联执行脚本（唯一 CSP 隐患：page-gallery.js::311 内联 onclick 死代码）、XSS 防护使用一致。
- 主要问题：重复代码（导航/两版 renderCard/骨架屏）、page-downloads 静默 catch、键盘无焦点守卫、a11y 缺 aria-label、1s 常驻 setInterval。
- style.css：粉色调设计令牌、640/480 响应式、prefers-reduced-motion 降级。

## 测试套件实测（2026-xx 后台实际运行 scripts/run_tests.ps1 -q）
- 结果：**309 passed, 4 failed, 4 warnings, 16.95s**（共 313 项）。
- 4 个失败全在 tests/test_test_setup.py 的临时根回退用例（test_temp_root_*）：内部 spawn powershell.exe 执行 `[IO.Path]::GetTempPath()` 取 stdout，DSH 沙箱下子进程管道输出捕获受限（EPERM 类）→ CalledProcessError。属沙箱环境限制，非产品代码缺陷；真实 Windows 环境应全绿。
- 302 通过即 AGENTS.md「270 用例约 15s」的自述已被超越（用例数随功能增长），实测 ~17s。
- 4 warnings：urllib3 InsecureRequestWarning（测试内 mock 的 127.0.0.1 HTTPS，预期内）。

## 运维/脚本/迁移子代理结论（961db8a0）+ 主代理复核
- 依赖策略：区间元约束（requirements.txt 4 项）+ requirements-lock.txt 22 项精确 pin；dev 仅 +pytest。
- run_tests.ps1 / sandbox_pytest_shim.py / pixiv-cleanup.sh / _inspect_db.py 细节已在前文脚本精读一致，无出入。
- 迁移：backup 只在有 pending 迁移时触发；v1-v4 语义与主代理读源码一致；init_db 无条件补跑 repair+v4。
- docs/superpowers：specs 11 + plans 11；发现 08-12/08-13 三份 spec 标「待实现」但代码已落地（回写纪律未执行）。
- opendesign/ = 设计工作流产物（mockups + design-systems），不参与运行；pixiv-api-http-main/ = Dituon 的 Node.js 参考实现（express ^5 beta 等 4 依赖），仅接口对照。
- PpPpP的收藏夹方案.md = 收藏夹设计（两张表+联合唯一索引+分数差值拖拽排序），项目 v1 迁移采纳其排序思想。
- 0001-*.patch：外部补丁引入 _reset_stuck_prefetch（已并入启动序列）。
- .workbuddy/memory/ = agent 会话记忆（.gitignore）。
- 部署：无 Docker/CI/Makefile/pyproject/setup.py（确认）；生产 = git pull + lock 安装 + gunicorn -w 1 --threads 8 -b 127.0.0.1:8000 app:app；systemd 仅 maintenance.md 示例。
- 运维风险：日常无自动备份（仅迁移时）；settings.json 重启生效（prefetch_interval 例外）；密钥删除即会话/游标全失效；cookies.txt Linux 优先 /etc/pixiv-viewer/cookies.txt（设置页写入的是项目根 cookies.txt —— 差异点，见问题清单）；SSL_VERIFY 默认 False。

## 主代理补充验证（detail.html CSP 疑点已澄清）
- templates/detail.html:336-344 是 `<script type="application/json" id="detailData">{{...|tojson}}</script>` 数据块（非执行脚本，CSP script-src 'self' 放行），后面才是 `<script src="/static/page-detail.js">` —— 前端子代理「无执行型内联 script」结论与源码一致。
- 61 个路由全部 regex 验证过（见工具输出）。

## Phase 4 问题清单（初稿，正式版进文档）
1. High：SSL_VERIFY=False 默认（config.py:112）——公网部署中间人风险；建议装 CA 后 True。
2. Medium：设置页更新 Cookie 写项目根 cookies.txt（routes_settings.py:205-212），Linux 生产 COOKIE_PATH 优先 /etc/pixiv-viewer/cookies.txt（config.py:37-40）→ 重启后设置页写入的 Cookie 失效。
3. Medium：download_file 多页 ZIP 用 BytesIO 全量内存（routes_download.py:180-191），ZIP_STORED 下大图合集会吃数百 MB 内存。
4. Medium（测试体系）：routes_download 全链路、下载引擎 _download_illust、/thumb 与 /api/image、settings 写盘、迁移备份还原均无测试。
5. Medium（运维）：无日常自动备份（仅迁移前）；迁移备份无限累积无清理。
6. Low：test_test_setup 4 用例依赖 powershell.exe 子进程取 GetTempPath，沙箱下失败（实测 309/4）。
7. Low：fetcher._process_items to_refetch 对永久失败作品每次搜索都会重拉详情（有令牌桶但重复请求）。
8. Low：_last_fetch_stats 并发搜索互相覆盖（AGENTS.md 已知可接受）。
9. Info：integration marker/live_pixiv_required 是死代码（零用例）。
10. Info：docs/superpowers 三份 spec 未回写「已实现」。
11. Info：.pytest-tmp 等临时目录堆积、无清理机制。
12. Info：当前实例库 user_version=3（v4 未应用），下次启动自动迁移+备份。
13. Low：/api/open-dir 依赖 ProxyFix 的 XFF 单跳信任（服务仅绑 127.0.0.1 时无实际暴露）。
14. Low：page-gallery.js:311 内联 onclick 死代码（CSP 阻止），有 closest('a') 兜底。
15. Low：test_models TestIllustToDict 精确键集断言脆弱；test_csrf_changes_per_session 命名与断言相反。
16. Info：CSRF token 同会话恒定（设计），跨会话变化；豁免 /csrf-token 无风险（须 session）。
17. Low：settings.json 覆盖与设置页保存存在两套写盘路径（routes_settings POST 与 routes_prefetch POST），prefetch 键由 设置页 与 /api/prefetch/config 双入口写（内存同步逻辑各写一份——routes_settings.py:237-240 与 routes_prefetch.py:56-67 逻辑重复）。

## 待办
- [ ] Phase 5：32 节文档生成（docs/technical-documentation.md）
- [ ] 最终自检 28 项