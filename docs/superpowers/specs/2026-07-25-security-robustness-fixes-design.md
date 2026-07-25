# 安全与健壮性修复 — 设计文档

日期：2026-07-25
状态：已批准
前置：2026-07-25 全面技术审查报告（对话上下文）

---

## 1. 背景与目标

全面技术审查发现 4 项 P0 与约 16 项 P1 问题。本设计覆盖**全部 P0 + 全部 P1** 的修复方案。

关键背景事实：

- **部署形态：公网暴露**（用户确认）。当前应用除 settings 页外无任何认证，`__main__` 绑定 `0.0.0.0`——这是最高风险项。
- 单人自部署场景：认证方案需简单（单密码），不引入用户系统。
- 测试策略：修复 + 关键新测试（非全量 TDD）。
- 组织方式：**地基先行 + 独立批次**——先做 3 件公共地基，再按子系统分 3 批独立交付，每批可单独验证合并。

## 2. 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 修复范围 | P0（4 项）+ 全部 P1（约 16 项），不含 3.3 理想架构重构 |
| 认证方案 | 全局访问密码（`ACCESS_PASSWORD`）+ `/login` 页 + session，留空则免认证（向后兼容本机用户） |
| `/api/open-dir` | 仅 `remote_addr == 127.0.0.1` 放行，公网形态下功能保留但不可用 |
| ZIP 流式化 | 引入 `zipstream-ng` 依赖，真流式打包 |
| 测试 | 修复 conftest P0 + 为关键改动逻辑补测试（游标分页、认证流、safe_commit、Session 工厂） |
| 实施顺序 | Phase 0 地基 → 批次 A 数据正确性 → 批次 B 性能 → 批次 C 前端交付 |

## 3. Phase 0：地基（约 1 天）

### 3.1 conftest.py 修复（P0-1，最高优先）

**问题**：`conftest.py:10` 模块顶部 `from models import ...` 触发 `models.py` 顶层 `create_engine`，绑定生产库路径；fixture 内的 `config.DATABASE_PATH` 覆盖无效。`clean_db` 实际在生产库上全表 DELETE。

**方案**：

1. `tests/conftest.py` 顶部（任何 `models`/`app` import 之前）：
   ```python
   import config
   config.DATABASE_PATH = os.path.join(tempfile.gettempdir(), f'pixiv_test_{os.getpid()}.db')
   config.AUTO_FOLLOW_INTERVAL = 0
   ```
2. fixture 清理时同时删除 WAL/SHM 文件（`db_path + '-wal'`、`'-shm'`）。
3. **附带动作（手动）**：检查生产库污染——`BlockedTag` 中 `'dupe'`、`'delete-me'`、`'csrf-test-*'` 垃圾数据，及收藏/下载记录是否曾被清空，向用户报告。

### 3.2 Session 工厂统一（P0-2）

**问题**：`_download_illust`（app.py:279-284）与 `thumb_proxy`（app.py:749-754）裸建 `requests.Session()`，漏配 `PROXY`；代理用户搜索正常但下载/缩略图全失败。

**方案**：

- `fetcher._build_session()` 重命名为公共工厂 `build_pixiv_session()`，保留 `_build_session` 别名向后兼容。
- 三处调用方统一复用：
  | 调用方 | 现状 | 改后 |
  |---|---|---|
  | `fetcher.py` 内部各处 | `_build_session()` | `build_pixiv_session()` |
  | `app.py:_download_illust` | 裸 Session | 工厂（PROXY/UA/Referer/verify 齐全） |
  | `app.py:thumb_proxy` | 裸 Session | 工厂 |
- 测试：mock `config.PROXY` 验证工厂产出的 session 带 proxies。

### 3.3 全局认证 + 安全基线（P0-4，公网核心防线）

**认证机制**：

- 新配置项 `ACCESS_PASSWORD`（环境变量优先，settings.json 可覆盖），**留空 = 免认证**（本机用户零影响）。
- 新增 `/login`（GET 页面 + POST 校验）：校验通过设 `session['authed'] = True`。
- `before_request` 钩子：未认证时——页面请求重定向 `/login?next=<path>`；`/api/*`、`/search` 等 JSON 端点返回 401。放行名单：`/login`、`/static/*`、`/favicon.ico`。
- 密码校验用 `hmac.compare_digest`；登录端点复用 `_rate_limit`（5 次/分钟）+ 失败 1s 延迟。
- CSRF token 比较同步改 `hmac.compare_digest`（app.py:397）。
- 旧 `SETTINGS_PASSWORD` 保留兼容读取；全局密码启用后，设置页复用全局会话，`settings_unlock` 流程降级为直通。

**Session 加固**（`app.config`）：

- `SESSION_COOKIE_HTTPONLY = True`
- `SESSION_COOKIE_SAMESITE = 'Lax'`
- `SESSION_COOKIE_SECURE`：新配置项 `COOKIE_SECURE`，默认 `True`（公网应全程 HTTPS）；本地 HTTP 调试可关
- `PERMANENT_SESSION_LIFETIME = timedelta(days=7)`

**ProxyFix**：`app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)`——反代后 `remote_addr` 为真实 IP，限流器恢复有效；`x_proto` 供未来 HTTPS 判定。

**`/api/open-dir`**：仅 `request.remote_addr in ('127.0.0.1', '::1')` 放行，否则 403。

**安全响应头**（`after_request`）：

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- CSP 宽松版（过渡期）：`default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:`——批次 C 完成后收紧 `script-src 'self'`。

**测试**：未登录 401/重定向、登录成功、错误密码 403 + 限流 429、open-dir 非本机 403。

## 4. 批次 A：数据与正确性（约 1.5 天）

| # | 项 | 方案 | 涉及文件 |
|---|---|---|---|
| A1 | `safe_commit` 假重试 | 新增事务级重试：装饰器/上下文管理器 `retryable_transaction(session)`，重试**整个事务块**（失败后 rollback 再重跑函数体），最多 3 次、退避 1s/2s。`safe_commit` 保留为兼容封装。优先迁移下载引擎、批量下载、收藏切换等高频写路径 | models.py, app.py, fetcher.py |
| A2 | `detail_page` 同步阻塞外部 API | 缺 `original_urls` 时改调 `_kick_background_fill` 异步补 + 模板渲染"详情加载中"占位（前端轮询 `/api/detail` 刷新）；加 10 分钟负缓存（内存 set + 时间戳）防重复触发 | app.py, detail.html |
| A3 | `_scan_local_downloads` 全量扫盘 | 模块级 TTL 缓存（5s）+ `_download_illust` 完成、`_delete_illust_files` 执行后主动失效 | app.py |
| A4 | N+1 查询 | `list_collections`：`LEFT JOIN` + `GROUP BY` 一次查出 item_count；`delete_collection`：批量 UPDATE 受影响 pid 的 `is_favorite`（一次 SQL：`UPDATE illusts SET is_favorite = EXISTS(...)`），去掉逐 pid 新 session | app.py |
| A5 | 时区丢失 | 约定**存储 naive UTC**：写入前统一 `astimezone(timezone.utc).replace(tzinfo=None)`；`to_dict()` 输出时附加 `Z` 后缀，前端 `new Date()` 解析即正确。存量数据已是 UTC 字符串，无需迁移 | models.py, fetcher.py, app.py |
| A6 | `local_paths` 绝对路径 | 改存相对 `DOWNLOAD_DIR` 的相对路径，读取处（serve_image/download_file/gallery）统一 `_abs_path()` 拼接；`init_db` 加一次性迁移：存量绝对路径转相对 | models.py, app.py |
| A7 | cleanup 脚本孤儿表 | 删除 `INSERT INTO deleted_records` 语句（表无定义、无消费者）；脚本内路径改为相对脚本位置推导（`SCRIPT_DIR/../instance/pixiv.db`），可用环境变量覆盖 | scripts/pixiv-cleanup.sh |
| A8 | 配置双轨收敛 | ① `_SETTINGS_DEFAULTS['fetch_detail_workers']` 与 `config.py` 对齐为 5；② 数值配置（`download_max_workers`、`auto_follow_interval`、`fetch_detail_workers`、`items_per_page`、`medium_image_size`）改为**读取时生效**——消费方每次从 `_load_settings()` 读，不再 import 时定型，Web 改完免重启；③ `config.py` 的 settings.json 覆盖保留作启动兜底，注释标注"仅进程级常量" | config.py, app.py, fetcher.py |

**A1 测试**：mock session 首次 commit 抛 `OperationalError('database is locked')`，验证重跑函数体后成功且数据正确。

## 5. 批次 B：性能与健壮性（约 1 天）

| # | 项 | 方案 | 涉及文件 |
|---|---|---|---|
| B1 | ZIP 内存炸弹 | 引入 `zipstream-ng`：`ZipFile.from_local_file(...)` 逐文件流式 yield，`Response(stream, mimetype='application/zip')`。requirements.txt 加依赖 | app.py, requirements.txt |
| B2 | `_bulk_tasks` 无界 | 并发上限 2（running 任务数 ≥2 时 `/api/bulk/start` 返回 409）；log 改 `deque(maxlen=200)`；`bulk_status` 返回 `list(log)`（天然截断） | app.py |
| B3 | thumb 缓存无界 + 写竞态 | ① 写入改 `临时文件 + os.replace()` 原子改名，消并发截断；② 容量上限 1GB：写入路径惰性检查（每 50 次写入触发一次），超限按 mtime 删最旧直到 < 800MB | app.py |
| B4 | 前端轮询风暴 | 删除 `app.js:pollDl` 单卡片轮询；页面级单一轮询（2.5s）`/api/downloads`（index/downloads 页）或 `/api/download/status/batch`（gallery 页），用返回状态统一刷新所有卡片按钮 | static/app.js, templates |
| B5 | `_rate_limit` 竞态 | 加模块级 `threading.Lock` 保护 records 读写 | app.py |

## 6. 批次 C：前端与交付（约 1.5 天）

| # | 项 | 方案 | 涉及文件 |
|---|---|---|---|
| C1 | 内联 JS 抽离 | 6 个页面内联 `<script>` 抽到 `static/page-{index,gallery,downloads,detail,settings,bulk}.js`；`csrf_token` 等注入改 `<meta name="csrf-token">` 或 `data-*`；模板只留 HTML + `<script src>` | templates/*, static/ |
| C2 | CSP 收紧 | `script-src 'self'`（C1 完成后）；`style-src` 保留 `'unsafe-inline'`（内联 style 量大，二期处理） | app.py |
| C3 | `btoa` 非 Latin-1 | `proxyThumb()` 改 `btoa(unescape(encodeURIComponent(url)))` | static/app.js |
| C4 | action label 未转义 | `renderLogs` 回退分支 `a.label` 走 `escHtml` | downloads.html |
| C5 | renderCard 三处复制 | 公共部分（escHtml/escAttr/badge 构造/下载按钮状态机）沉淀 `app.js`；页面差异留在各自 page-*.js。适度 DRY，不强行统一 DOM 结构 | static/app.js |
| C6 | 关键新测试 | ① `paginated_search` 游标推进（跨页 skip_count 累加）单测；② 认证流程测试（见 3.3）；③ A1 重试测试；④ Session 工厂 PROXY 测试；⑤ thumb 缓存原子写测试 | tests/test_app.py 等 |
| C7 | 文档 | `AGENTS.md`：认证机制、免重启配置清单、zipstream-ng 依赖、thumb 缓存上限；新增"公网部署 checklist"（HTTPS 反代 / COOKIE_SECURE / ACCESS_PASSWORD / DB 备份） | AGENTS.md |

## 7. 验证标准（每批次 DoD）

1. `pytest -v` 全绿（含批次内新增测试）。
2. 手动冒烟五页面走查：搜索 → 详情 → 下载 → 图库 → 设置。
3. 批次内无 TODO/FIXME 残留。
4. 每批次独立 commit（或多 commit）可独立回退。

## 8. 风险与回退

| 风险 | 缓解 |
|---|---|
| A6 路径迁移出错导致已下载文件"丢失" | 迁移前先备份 `pixiv.db`；迁移逻辑同时兼容绝对/相对两种存量值，读路径兜底 |
| 全局认证上线后把自己锁在外面 | `ACCESS_PASSWORD` 留空即免认证；首启未设置时 `/login` 直接放行并提示设置 |
| A8 免重启改造引入读取不一致 | 数值消费方收敛为单一读取函数 `get_setting(key)`，禁直接读模块常量 |
| 内联 JS 抽离破坏页面行为 | 逐页抽离逐页冒烟；C2 收紧 CSP 放最后单独一步 |
| 测试套件历史上污染过生产库 | Phase 0.1 附带检查并向用户报告 |

## 9. 明确不做（YAGNI）

- 不做 3.3 理想架构重构（蓝图拆分 / create_app 工厂 / service 层）。
- 不引入 Alembic 迁移框架（沿用现有 init_db 手动迁移模式扩展）。
- 不做用户系统 / 多密码 / OAuth。
- 不处理样式 CSP（`style-src 'unsafe-inline'` 二期）。
- 不替换 SQLite / 不加 Redis。
