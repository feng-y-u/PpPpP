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

# 测试（需要有效的 cookies.txt 才能通过集成测试）
pytest -v

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
| `templates/*.html` | 8 个 Jinja2 模板（搜索、图库、批量、下载管理、详情、设置、设置解锁、登录） |
| `static/` | `app.js`、`style.css`、`vendor/bootstrap-5.3.3/` |
| `scripts/` | `pixiv-cleanup.sh`（cron 磁盘清理，30 天 / 收藏 < 100） |

无 `__init__.py` — 模块直接导入。无 `setup.py`/`pyproject.toml`。

---

## 关键注意事项

### 进程与状态
- **Gunicorn 必须用 `-w 1`**：以下状态在进程内存中 — `_auto_follow_state`、`download_locks`、`download_cancellations`、`_queued_downloads`、`_download_progress`、`_bulk_tasks`。多 worker 不共享。详见 `app.py:171-179` 注释。
- **限流是每个 worker 的内存计数器**：`_rate_limit` 装饰器按 IP 保存时间戳，`-w 1` 时正常工作。当前仅用于 `/api/settings/unlock`。

### 配置与重启
- **settings.json 需重启服务器**：`config.py` 在导入时读取 `instance/settings.json`。通过 Web UI 修改后需重启进程生效。
- **config.py 在 import 时执行所有副作用**：读取 `.env`、`settings.json`、生成 `CURSOR_SECRET`、设置全局常量。测试需要 import 前覆盖 `config.DATABASE_PATH`（见 `tests/conftest.py`）。
- **密钥自动生成**：首次启动时写入 `instance/.secret_key` 和 `instance/.cursor_secret`。删除会使所有会话/游标失效。
- **`.env` 文件支持**：`config.py` 自动加载根目录 `.env`，用 `os.environ.setdefault`（不覆盖已有环境变量）。

### 认证
- **Cookie 认证**：手动创建 `cookies.txt`，存放 `PHPSESSID=xxxxx` 或纯 token。过期会静默返回空结果。
- **Linux 上优先读 `/etc/pixiv-viewer/cookies.txt`**，否则读项目根目录。
- **全局访问密码**：`ACCESS_PASSWORD`（环境变量或 settings.json 的 `access_password`）非空时启用全站登录墙 —— `before_request` 钩子拦截未认证请求，页面 302 到 `/login`，API/POST 返回 401。**留空 = 免认证**（本机默认）。登录态存 session（`authed`），7 天有效；`POST /login` 限流 5 次/分钟 + 失败延迟 1 秒。`COOKIE_SECURE` 控制 Session Cookie 仅 HTTPS 传输（默认 true，本地 HTTP 调试需设 `COOKIE_SECURE=false`）。

### API 行为
- **`popular_d` 排序需 Pixiv Premium**：非 Premium 账号静默返回空结果。`/search` 路由默认排序为 `date_d`（`app.py:518`），空查询回退到 `browse_discovery()` 时也使用该默认值。
- **所有 Pixiv 图片请求需 `Referer: https://www.pixiv.net/`**，否则 403。缩略图代理 `/thumb/<base64_url>` 处理此问题。
- **游标分页 305 秒过期**（`app.py:546`）：翻页游标包含时间戳，超时后客户端需重新搜索。
- **`PIXIV_BASE_URL`** 可改为代理/镜像地址（`config.py:48`）。

### 数据库
- **没有迁移系统**：启动时 `SQLAlchemy create_all()` + `init_db()` 中的手动 `ALTER TABLE` 逻辑（`models.py:203`）处理五列：`file_size`、`description`、`is_favorite`、`favorited_at`、`downloaded_at`。其他 schema 变更需手动处理。
- **写入必须用 `safe_commit()`**（`models.py:32`）而不是直接 `db.commit()`：它带重试处理 `database is locked`。
- **获取 session 用 `get_session()`**（`models.py:255`），不要直接创建 `Session(engine)`，除非在 `init_db()` 等启动逻辑中。
- **启动时重置卡死下载**：模块导入时 `_reset_stuck_downloads()` 清除所有 `downloading` 状态并删除残留文件（`app.py:144-169`）。
- **收藏夹基于 Collection 模型**：`Illust.is_favorite` 列存在但语义上由"我的收藏"收藏夹中的 CollectionItem 驱动。切换收藏会添加/移除该收藏夹。`init_db()` 迁移旧的 `is_favorite=True` 记录到默认收藏夹。

### 请求与中间件
- **所有 POST 接口需 CSRF**：`X-CSRF-Token` 请求头（从 `GET /csrf-token` 或页面内嵌获取）。缺失/错误返回 403。`_csrf_required` 装饰器实现（`app.py:400`）。
- **上传限制 1MB**：`app.config['MAX_CONTENT_LENGTH']`（`app.py:66`）。
- **Werkzeug 请求日志被设为 WARNING** 级别以防止 Cookie 泄露到日志（`app.py:47`）。

### 下载
- **5 分钟清理批量任务**：完成的批量任务 300 秒后从 `_bulk_tasks` 移除（`threading.Timer`，`app.py:1210`）。
- **SSL 验证默认关闭**（`config.py` 中 `SSL_VERIFY = False`）。生产环境如已安装 CA 证书可设为 `True`。

### 目录
- **`instance/`**：`.secret_key`、`.cursor_secret`、`pixiv.db`（+ WAL/SHM）、`settings.json`、`image_cache/`（缩略图代理磁盘缓存）。整个目录在 `.gitignore` 中。
- **`downloads/`** 和 **`cookies.txt`** 也在 `.gitignore` 中。

---

## 测试

- 测试文件：`tests/test_app.py`（路由/API/CSRF）、`tests/test_auth.py`（认证）、`tests/test_models.py`（模型）
- `conftest.py` 在 **import app 之前**覆盖 `config.DATABASE_PATH` 为临时文件并设 `AUTO_FOLLOW_INTERVAL=0`
- 需要有效 `cookies.txt` 才能通过集成测试（涉及真实 Pixiv API 调用的测试）
- `clean_db` fixture 在每次测试前清空所有表

---

## 设计任务

详细工作流见 `CLAUDE.md`。摘要：检查 `./opendesign/design-systems/*/` → 输出到 `./opendesign/mockups/<task-slug>/` 附带 `manifest.json`。约束：无滥用渐变、不用 emoji 当图标、避免 Inter/Roboto/Arial、触控目标 >= 44px。
