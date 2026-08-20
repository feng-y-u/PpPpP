# Pixiv Viewer — 智能体指南

Flask Web 应用，通过 Pixiv 内部 Ajax API（非官方）搜索/浏览/下载 Pixiv 插画。单人自部署服务。

**技术栈**: Python 3.9+ / Flask 3.1+ / SQLAlchemy 2.0 / SQLite (WAL) / Bootstrap 5.3 / 原生 JS / requests / gunicorn / pytest。无构建流程、无 linter、无类型检查。

---

## 命令

```bash
# 初始化
python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt

# 开发
flask run --debug

# 默认测试（离线，不要求真实 Pixiv Cookie）
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q

# 未来的真实 Pixiv 集成测试（必须显式标记 integration，并使用 live_pixiv_required fixture）
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -m integration

# 生产部署（必须 -w 1 — 见下）
gunicorn -w 1 --timeout 300 -b 127.0.0.1:8000 app:app
```

---

## 架构

| 文件 | 作用 |
|------|------|
| `app.py` | Flask 入口：所有路由、下载引擎、后台任务、CSRF、限流 |
| `fetcher.py` | Pixiv API 封装：Cookie/OAuth 认证、搜索、作品详情 |
| `models.py` | SQLAlchemy ORM：Illust、BlockedTag、DownloadLog、Collection、CollectionItem |
| `config.py` | 常量、环境变量覆盖、`instance/settings.json` 导入时覆盖 |
| `templates/*.html` | 8 个 Jinja2 模板（搜索、图库、下载管理、详情、设置、设置解锁、登录、缓存浏览） |
| `static/` | `app.js`、`style.css`、`vendor/bootstrap-5.3.3/` |
| `scripts/` | `pixiv-cleanup.sh`（cron 磁盘清理，30 天 / 收藏 < 100） |
| `pixiv-api-http-main/` | 内置的第三方 Node.js Pixiv API 参考实现（含 `search/no-premium.js` 等），作为接口格式对照参考，不参与运行 |
| `docs/superpowers/` | 近期变更的设计文档（plans/specs）：分页重设计、安全加固、收藏夹排序。改动前先读相关 spec |

无 `__init__.py` — 模块直接导入。无 `setup.py`/`pyproject.toml`。

---

## 关键注意事项

### 进程与状态
- **Gunicorn 必须用 `-w 1`**：以下状态在进程内存中 — `_auto_follow_state`、`download_locks`、`download_cancellations`、`_queued_downloads`、`_download_progress`、`_search_tasks`、`_rate_limit_store`、`_prefetch_state`。多 worker 不共享。详见 `app.py:170-181` 注释。
- **限流是每个 worker 的内存计数器**：`_rate_limit` 装饰器按 IP 保存时间戳，`-w 1` 时正常工作。用于 `POST /login`（`app.py:664`）和 `/api/settings/unlock`（`app.py:1976`）。

### 配置与重启
- **settings.json 需重启服务器**：`config.py` 在导入时读取 `instance/settings.json`。通过 Web UI 修改后需重启进程生效。
- **config.py 在 import 时执行所有副作用**：读取 `.env`、`settings.json`、生成 `CURSOR_SECRET`、设置全局常量。测试需要 import 前覆盖 `config.DATABASE_PATH`（见 `tests/conftest.py`）。
- **密钥自动生成**：首次启动时写入 `instance/.secret_key` 和 `instance/.cursor_secret`。删除会使所有会话/游标失效。
- **`.env` 文件支持**：`config.py` 自动加载根目录 `.env`，用 `os.environ.setdefault`（不覆盖已有环境变量）。
- **搜索预取配置**：`PREFETCH_INTERVAL` / `PREFETCH_PAGES` / `PREFETCH_MAX_ILLUSTS`（`config.py`，settings.json 键 `prefetch_interval`/`prefetch_pages`/`prefetch_max_illusts`）。interval 运行时经 `POST /api/prefetch/config` 立即生效；其余需重启生效。

### 认证
- **Cookie 认证**：手动创建 `cookies.txt`，存放 `PHPSESSID=xxxxx` 或纯 token。过期会静默返回空结果。
- **Linux 上优先读 `/etc/pixiv-viewer/cookies.txt`**，否则读项目根目录。
- **全局访问密码**：`ACCESS_PASSWORD`（环境变量或 settings.json 的 `access_password`）非空时启用全站登录墙 —— `before_request` 钩子拦截未认证请求，页面 302 到 `/login`，API/POST 返回 401。**留空 = 免认证**（本机默认）。登录态存 session（`authed`），7 天有效；`POST /login` 限流 5 次/分钟 + 失败延迟 1 秒。`COOKIE_SECURE` 控制 Session Cookie 仅 HTTPS 传输（默认 true，本地 HTTP 调试需设 `COOKIE_SECURE=false`）。

### API 行为
- **`popular_d` 排序需 Pixiv Premium**：非 Premium 账号静默返回空结果。`/search` 路由默认排序为 `date_d`（`app.py:811`），空查询回退到 `browse_discovery()` 时也使用该默认值。
- **搜索是异步的**：`GET /search` 立即返回 `task_id`，后台线程拉取，前端轮询 `/api/search/status/<task_id>`。任务在 `_search_tasks` 内存字典中，访问 status 时清理过期任务。
- **所有 Pixiv 图片请求需 `Referer: https://www.pixiv.net/`**，否则 403。缩略图代理 `/thumb/<base64_url>` 处理此问题（仅允许 `https://i.pximg.net/` 白名单 URL，磁盘缓存 7 天）。
- **游标分页 24 小时过期**（`app.py:841`）：翻页游标包含时间戳，超时后客户端需重新搜索。空页去重 + 死游标作废由前端处理。
- **`PIXIV_BASE_URL`** 可改为代理/镜像地址（`config.py:48`）。
- **搜索预取缓存**：手动在设置页配置预取标签，后台线程按 interval 用宽松参数（min_bookmarks=1、date_d、R18 不过滤）定时预取并写入 `Illust` + `SearchCache` 表。**`/search` 永远走实时 Pixiv，不命中缓存**；预取结果通过独立 `/cache` 页面浏览（`GET /api/cache/items`，库内按收藏数/排序过滤分页，`query_cached_tag`）。`popular_d` 排序为 `bookmark_count` 降序的库内近似。

### 数据库
- **没有迁移系统**：启动时 `SQLAlchemy create_all()` + `init_db()` 中的手动 `ALTER TABLE` 逻辑（`models.py:240`）负责全部 schema 变更。当前会补加 `file_size`、`downloaded_at`、`bookmark_updated_at`、`prefetch_source`（预取来源标记）列与 `collection_items.position`（拖拽排序），通过 `PRAGMA user_version` 一次性回填 position 初值，并新增 `SearchCache` 表（tag→illust_ids 映射，预取缓存）。**`description` 列已彻底移除**（模型、`to_dict`、fetcher、模板均不再有，init_db 会 DROP）。**`is_favorite` / `favorited_at` 列已废弃，init_db 会将其 DROP**。SQLite < 3.35 时用 `_rebuild_illusts_table`（`models.py:213`）建表重建，保留 PK/UNIQUE/NOT NULL。其他 schema 变更需手动添加类似逻辑。
- **写入必须用 `safe_commit()`**（`models.py:32`）而不是直接 `db.commit()`：它带重试处理 `database is locked`。
- **获取 session 用 `get_session()`**（`models.py:324`），不要直接创建 `Session(engine)`，除非在 `init_db()` 等启动逻辑中。
- **启动时重置卡死下载**：模块导入时 `_reset_stuck_downloads()` 清除所有 `downloading` 状态并删除残留文件（`app.py:143`）。
- **收藏语义完全由 Collection 驱动**：切换收藏会添加/移除"我的收藏"收藏夹中的 CollectionItem。`Illust.is_favorite` 列已废弃删除，不要再依赖。

### 请求与中间件
- **所有 POST 接口需 CSRF**：`X-CSRF-Token` 请求头（从 `GET /csrf-token` 或页面内嵌获取）。缺失/错误返回 403。`_csrf_required` 装饰器实现（`app.py:616`）。
- **上传限制 1MB**：`app.config['MAX_CONTENT_LENGTH']`（`app.py:67`）。
- **Werkzeug 请求日志被设为 WARNING** 级别以防止 Cookie 泄露到日志（`app.py:48`）。

### 下载
- **SSL 验证默认关闭**（`config.py` 中 `SSL_VERIFY = False`）。生产环境如已安装 CA 证书可设为 `True`。

### 目录
- **`instance/`**：`.secret_key`、`.cursor_secret`、`pixiv.db`（+ WAL/SHM）、`settings.json`、`image_cache/`（缩略图代理磁盘缓存）。整个目录在 `.gitignore` 中。
- **`downloads/`** 和 **`cookies.txt`** 也在 `.gitignore` 中。

---

## 测试

- 测试文件：`tests/test_app.py`（路由/API/CSRF）、`tests/test_auth.py`（认证）、`tests/test_models.py`（模型）、`tests/test_fetcher.py`（Pixiv API 封装）、`tests/test_prefetch.py`（预取引擎/容量清理）、`tests/test_search_cache.py`（库内缓存查询）、`tests/test_prefetch_api.py`（预取管理 API）、`tests/test_cache_page.py`（缓存浏览 API/页面）
- `conftest.py` 在 **import app 之前**覆盖 `config.DATABASE_PATH` 为临时文件并设 `AUTO_FOLLOW_INTERVAL=0`（事后覆盖无效，会连到生产库）
- session 级 `app` fixture 结束后调用 `models.engine.dispose()`，否则 Windows 上无法删除临时 .db 文件（WinError 32）
- 当前默认测试通过 mock/monkeypatch 隔离网络，不需要有效 `cookies.txt`。
- 真实 Pixiv 集成测试必须显式使用 `@pytest.mark.integration` 和 `live_pixiv_required` fixture；缺少 Cookie 时测试会 skip。
- `clean_db` fixture 在每次测试前清空所有表

---

## 设计任务

详细工作流见 `CLAUDE.md`。摘要：检查 `./opendesign/design-systems/*/` → 输出到 `./opendesign/mockups/<task-slug>/` 附带 `manifest.json`。约束：无滥用渐变、不用 emoji 当图标、避免 Inter/Roboto/Arial、触控目标 >= 44px。
