# Pixiv Viewer — 智能体指南

## 这是什么

Flask Web 应用，通过 Pixiv 内部 Ajax API（非官方）搜索/浏览/下载 Pixiv 插画。单人自部署服务，面向低配服务器（4C/4GB/40GB/3Mbps）。

**技术栈**：Python 3.9+ / Flask 3.1+ / SQLAlchemy 2.0 / SQLite (WAL) / Bootstrap 5.3 / 原生 JS。无构建流程、无 linter、无类型检查。

---

## 命令

```bash
# 初始化
python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt

# 开发
flask run --debug

# 测试（pytest，需要有效的 cookies.txt 或 Pixiv 凭据才能通过集成测试）
pytest -v

# 生产部署（必须 -w 1 — 见注意事项）
gunicorn -w 1 --timeout 300 -b 127.0.0.1:8000 app:app

# 可选：pixiv-api-http（Node.js Express 代理，支持 SNI 绕过）
cd pixiv-api-http-main && npm install && npm start  # → :1145
```

没有 linter/类型检查/格式化命令。

---

## 架构

| 文件 | 作用 |
|------|------|
| `app.py` | Flask 入口：所有路由、下载引擎、后台任务、CSRF、限流 |
| `fetcher.py` | Pixiv API 封装：Cookie/OAuth 认证、按标签/用户/关注搜索、作品详情 |
| `models.py` | SQLAlchemy ORM：Illust、BlockedTag、DownloadLog、Collection、CollectionItem |
| `config.py` | 常量、环境变量覆盖、`instance/settings.json` 导入时覆盖 |
| `templates/*.html` | 7 个 Jinja2 模板（搜索、图库、批量、下载管理、详情、设置、设置解锁） |
| `static/` | `app.js`（124 行）、`style.css`（177 行）、`vendor/bootstrap-5.3.3/` |
| `scripts/` | `pixiv-cleanup.sh`（cron 磁盘清理，30 天 / 收藏 < 100） |

无 `__init__.py` — 模块直接导入。无 `setup.py`/`pyproject.toml`。

---

## 关键注意事项

- **Gunicorn 必须用 `-w 1`**：下载状态（`_download_progress`、`_bulk_tasks` 等）在进程内存中。多个 worker 不共享。详见 `app.py:93-104` 注释。
- **settings.json 需要重启服务器**：`config.py` 在导入时读取 `instance/settings.json`。通过 Web UI 修改后需重启进程生效。
- **Cookie 热加载**：`fetcher.py` 每次 API 调用检查 `cookies.txt` 的 mtime — 刷新 Cookie 无需重启。Cookie 过期会静默返回空结果。文件支持 `PHPSESSID=xxxxx` 或纯 `xxxxx` 格式。
- **OAuth 认证也可用**：设置 `PIXIV_USERNAME`/`PIXIV_PASSWORD` 环境变量使用 Pixiv OAuth Bearer Token（通过 refresh_token 自动续期，无需维护 Cookie）。客户端凭证（`MOBrBDS8blbauoSck0ZfDbtuzpyT`/`lsACyCD94FhDUtGTXi3QjcFE2uP2qW`）是硬编码在 `fetcher.py:49-50` 的 Pixiv 公开应用常量。
- **`popular_d` 排序需要 Pixiv Premium**：非 Premium 账号静默返回空结果。无查询时的发现页也默认使用 `popular_d`。
- **所有 Pixiv 图片请求需要 `Referer: https://www.pixiv.net/`** 否则返回 403。缩略图代理 `/thumb/<base64_url>` 处理此问题。
- **没有数据库迁移**：启动时 `SQLAlchemy create_all()`。`init_db()` 有针对特定列（`file_size`、`description`、`is_favorite`、`favorited_at`）的临时 `ALTER TABLE` 逻辑。其他 schema 变更需手动处理。
- **5 分钟清理批量下载**：完成的批量任务 300 秒后从 `_bulk_tasks` 移除（`threading.Timer`）。
- **启动时重置卡死下载**：`_reset_stuck_downloads()` 在导入时清除所有 `downloading` 状态并删除残留文件。
- **空查询 → 发现页**：没有搜索关键词时，`/search` 回退到 `browse_discovery()` 而不是 `search_by_tag()`。
- **所有 POST 接口需要 CSRF**：`X-CSRF-Token` 请求头（从 `GET /csrf-token` 或页面内嵌获取）必须携带。缺失/错误返回 403。通过 `app.py` 的 `_csrf_required` 装饰器实现。
- **限流是每个 worker 的内存计数器**：`_rate_limit` 装饰器按 IP 保存时间戳。`-w 1` 时正常工作；多 worker 各自计数。当前仅用于 `/api/settings/unlock`。
- **SSL 验证默认关闭**：`config.py` 中 `SSL_VERIFY = False`。部分系统上 Pixiv 的证书链可能失败。生产环境如已安装 CA 证书可设为 `True`。
- **密钥自动生成**：首次启动时写入 `instance/.secret_key`。删除此文件会使所有会话失效（用户被登出）。
- **收藏夹基于 Collection 模型**：`is_favorite` 是计算字段，取决于是否在"我的收藏"收藏夹中。切换收藏会添加/移除该收藏夹。`init_db()` 在启动时迁移旧的 `is_favorite=True` 记录。

---

## 设计任务 — 使用 OpenDesign 工作流

当用户提出 UI 设计、原型、幻灯片、设计系统或品牌设计需求时：

1. 检查现有设计系统：`./opendesign/design-systems/*/`
2. 结构化提问（受众、语气、精细度、格式、变体数量）
3. 输出到 `./opendesign/mockups/<task-slug>/`，附带 `manifest.json`
4. 设计约束：无滥用渐变、不用 emoji 当图标、避免 Inter/Roboto/Arial、触控目标 ≥44px
