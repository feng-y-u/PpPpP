# Pixiv Viewer — 智能体指南

## 这是什么

Flask Web 应用，通过 Pixiv 内部 Ajax API（非官方）搜索/浏览/下载 Pixiv 插画。单人自部署服务。

**技术栈**：Python 3.9+ / Flask 3.1+ / SQLAlchemy 2.0 / SQLite (WAL) / Bootstrap 5.3 / 原生 JS / requests / gunicorn / pytest。无构建流程、无 linter、无类型检查。

---

## 命令

```bash
# 初始化
python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt

# 开发
flask run --debug

# 测试（需要有效的 cookies.txt 或 Pixiv 凭据才能通过集成测试）
pytest -v

# 生产部署（必须 -w 1 — 见注意事项）
gunicorn -w 1 --timeout 300 -b 127.0.0.1:8000 app:app
```

没有 linter/类型检查/格式化命令。

---

## 架构

| 文件 | 作用 |
|------|------|
| `app.py` | Flask 入口：所有路由、下载引擎、后台任务、CSRF、限流 |
| `fetcher.py` | Pixiv API 封装：Cookie/OAuth 认证、搜索、作品详情 |
| `models.py` | SQLAlchemy ORM：Illust、BlockedTag、DownloadLog、Collection、CollectionItem |
| `config.py` | 常量、环境变量覆盖、`instance/settings.json` 导入时覆盖 |
| `templates/*.html` | 7 个 Jinja2 模板（搜索、图库、批量、下载管理、详情、设置、设置解锁） |
| `static/` | `app.js`、`style.css`、`vendor/bootstrap-5.3.3/` |
| `scripts/` | `pixiv-cleanup.sh`（cron 磁盘清理，30 天 / 收藏 < 100） |

无 `__init__.py` — 模块直接导入。无 `setup.py`/`pyproject.toml`。

---

## 关键注意事项

- **Gunicorn 必须用 `-w 1`**：以下状态在进程内存中 — `_auto_follow_state`、`download_locks`、`download_cancellations`、`_queued_downloads`、`_download_progress`、`_bulk_tasks`。多个 worker 不共享。详见 `app.py:157-168` 注释。
- **settings.json 需要重启服务器**：`config.py` 在导入时读取 `instance/settings.json`。通过 Web UI 修改后需重启进程生效。
- **认证方式**：手动创建 `cookies.txt`，存放 `PHPSESSID=xxxxx` 或纯 token。Cookie 过期会静默返回空结果。
- **`popular_d` 排序需要 Pixiv Premium**：非 Premium 账号静默返回空结果。`/search` 路由默认排序为 `date_d`（`app.py:427`），空查询发现页 `browse_discovery()` 签名默认 `popular_d` 但路由传入的 sort_order 默认为 `date_d`。
- **所有 Pixiv 图片请求需要 `Referer: https://www.pixiv.net/`** 否则返回 403。缩略图代理 `/thumb/<base64_url>` 处理此问题。
- **没有数据库迁移**：启动时 `SQLAlchemy create_all()`。`init_db()`（`models.py:203`）有针对五列的临时 `ALTER TABLE` 逻辑：`file_size`、`description`、`is_favorite`、`favorited_at`、`downloaded_at`。其他 schema 变更需手动处理。
- **5 分钟清理批量下载**：完成的批量任务 300 秒后从 `_bulk_tasks` 移除（`threading.Timer`，`app.py:1084`）。
- **启动时重置卡死下载**：`_reset_stuck_downloads()` 在模块导入时清除所有 `downloading` 状态并删除残留文件（`app.py:130-155`）。
- **空查询 → 发现页**：没有搜索关键词时，`/search` 回退到 `browse_discovery()` 而不是 `search_by_tag()`（`app.py:462-463`）。
- **所有 POST 接口需要 CSRF**：`X-CSRF-Token` 请求头（从 `GET /csrf-token` 或页面内嵌获取）必须携带。缺失/错误返回 403。通过 `_csrf_required` 装饰器实现。
- **限流是每个 worker 的内存计数器**：`_rate_limit` 装饰器按 IP 保存时间戳。`-w 1` 时正常工作。当前仅用于 `/api/settings/unlock`。
- **SSL 验证默认关闭**：`config.py` 中 `SSL_VERIFY = False`。部分系统上 Pixiv 的证书链可能失败。生产环境如已安装 CA 证书可设为 `True`。
- **密钥自动生成**：首次启动时写入 `instance/.secret_key`（`app.py:51-59`）。删除此文件会使所有会话失效。
- **收藏夹基于 Collection 模型**：`is_favorite` 是 Illust 上的 Boolean 列，语义上由"我的收藏"收藏夹中的 CollectionItem 驱动。切换收藏会添加/移除该收藏夹。`init_db()` 迁移旧的 `is_favorite=True` 记录（`models.py:230-252`）。
- **`instance/` 目录**：存放 `.secret_key`、`pixiv.db`（+ WAL/SHM 文件）、`settings.json`、`image_cache/`（缩略图代理磁盘缓存，`app.py:63-64`）。整个目录在 `.gitignore` 中。
- **`.env` 文件支持**：`config.py` 自动加载根目录 `.env` 文件中的 `KEY=VALUE` 环境变量（`os.environ.setdefault`，不覆盖已有变量）。

---

## 设计任务

详细工作流见 `CLAUDE.md`。摘要：检查 `./opendesign/design-systems/*/` → 输出到 `./opendesign/mockups/<task-slug>/` 附带 `manifest.json`。设计约束：无滥用渐变、不用 emoji 当图标、避免 Inter/Roboto/Arial、触控目标 >= 44px。
