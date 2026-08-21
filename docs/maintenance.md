# Pixiv Viewer — 个人自用维护指南

> 面向单人自部署：单 Worker、SQLite WAL、进程内后台线程。不做多实例/多用户扩展。
> 所有命令假设：开发在 Windows（`powershell`/`venv`），生产在 Linux（`gunicorn`/`systemd`）。

---

## 1. 三类存储（务必分清）

| 存储 | 位置 | 内容 | 生命周期 |
|---|---|---|---|
| **预取数据库缓存** | `instance/pixiv.db`（`Illust` + `SearchCache`） | 预取标签累积的作品元数据 + 收藏数 | 由预取循环管理，容量上限 **10000 条**，超出按收藏数低优先删除 |
| **缩略图磁盘缓存** | `instance/image_cache/` | `/thumb/<base64>` 代理的图片 | 自动，TTL 7 天 |
| **原图下载文件** | `downloads/<pixiv_id>/` | 手动/自动下载的原图画质文件 | 由下载管理页 + `pixiv-cleanup.sh` 管理 |

> ⚠️ `pixiv-cleanup.sh` **只清理 downloads/ 下的已下载原图**，与预取数据库的 10000 条容量控制**完全无关**。

---

## 2. 开发启动（Windows）

```powershell
# 初始化
python -m venv venv ; venv\Scripts\activate
pip install -r requirements-dev.txt

# 开发（自动重载）
flask run --debug
```

> 需要访问 Pixiv：`cookies.txt`（根目录或 `/etc/pixiv-viewer/cookies.txt`）放 `PHPSESSID=xxx`。
> 本机 HTTP 调试：如启用 `COOKIE_SECURE`（默认 true），设置 `COOKIE_SECURE=false`（环境变量或 `.env`）。

---

## 3. 测试

```powershell
# 默认测试（离线，不需要真实 Cookie，走 mock/monkeypatch）
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q

# 未来若新增真实 Pixiv 集成测试：必须 @pytest.mark.integration + live_pixiv_required
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -m integration
```

> `scripts/run_tests.ps1` 是包装脚本：生成确定性临时目录（沙箱环境自动回退工作区内 `.pytest-tmp` 并加载沙箱插件），真实环境用 `%LOCALAPPDATA%`。可直接传文件/参数（等价 pytest）。
> 依赖复现：`pip install -r requirements-lock.txt`。

---

## 4. 数据库备份 / 迁移

- **结构迁移**：启动时由 `migrations/runner.py` 按 `PRAGMA user_version` 版本化执行；**升级前自动备份**数据库到 `instance/backups/<db>.YYYYMMDDTHHMMSS.bak`。
- **手动备份**（推荐定期）：

```bash
# Linux
mkdir -p instance/backups && cp instance/pixiv.db instance/backups/pixiv.db.$(date +%Y%m%dT%H%M%S).bak
# 为一致性，可先停服务再拷贝（WAL 模式下热拷贝基本安全，但停服最稳）
```

- 回退：若迁移出现问题，用最新 `.bak` 覆盖后重启（记得把 `PRAGMA user_version` 已推进的版本一并回退或删除备份文件——最简单是整库恢复旧备份 + 旧代码）。

---

## 5. 预取容量检查

预取数据库缓存上限默认 `prefetch_max_illusts = 10000`（settings.json / 设置页）。

```bash
# 当前预取来源作品数
sqlite3 instance/pixiv.db "SELECT COUNT(*) FROM illusts WHERE prefetch_source=1;"
# 各标签缓存条数
sqlite3 instance/pixiv.db "SELECT tag, total, status, cached_at FROM search_cache;"
# 手动触发某标签刷新（Web 设置页/缓存页也有按钮）
curl -X POST http://127.0.0.1:8080/api/prefetch/refresh -H "X-CSRF-Token: <从页面获取>" -d '{"tag":"<标签>"}'
```

> 容量清理规则：超出上限 → 每轮预取后删除**收藏数最低**的未下载、未收藏预取作品；已下载/下载中/已收藏保护。拉取满 1 天的作品还会刷新一次"最终收藏数"，< 10 的自动删除。

---

## 6. 下载清理（可选 cron）

仅清理 30 天前下载的、收藏数 < 100 的**已下载原图**文件，并把作品标记 `cleaned`。

```bash
# 手动
scripts/pixiv-cleanup.sh
# 带覆盖（测试/非常规部署）
PIXIV_DB=/path/pixiv.db PIXIV_DOWNLOADS=/path/downloads scripts/pixiv-cleanup.sh
# cron 安装
sudo cp scripts/pixiv-cleanup.sh /etc/cron.weekly/pixiv-cleanup && sudo chmod +x /etc/cron.weekly/pixiv-cleanup
```

> 安全：脚本只删除 `DOWNLOADS` 目录内文件（路径经 realpath 校验）；不再依赖不存在的 `deleted_records` 表。
> 验证：`tests/test_cleanup_script.ps1`（需 bash + sqlite3，建议在 Linux 跑）。

---

## 7. 生产部署（Linux + systemd 示例）

```bash
git pull
pip install -r requirements-lock.txt    # 首次或依赖更新后
sudo systemctl restart pixiv-viewer     # 服务名以实际 unit 为准
```

- **必须 `gunicorn -w 1`**：进程内状态（下载锁、预取、搜索任务、限流、自动关注）不支持多 worker。
- 更新代码后重启**必须整进程重启**（`systemctl restart`），不能只 `kill -HUP`（`--preload` 下不重载代码）。
- 服务日志：`journalctl -u pixiv-viewer -f | grep prefetch`（预取/清理）。
