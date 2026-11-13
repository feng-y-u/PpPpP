# PpPpP — Pixiv 个人美术工作站

自部署的 Pixiv 浏览 / 缓存 / 下载工具：通过 Pixiv 内部 Ajax API 搜索作品，把喜欢的画师和标签**预取进本地 SQLite**，再用一个「打开就有内容」的首页浏览。单人使用，不需要数据库服务，也不需要前端构建。

> 非官方实现，接口随时可能变化。请仅用于个人自用，遵守 Pixiv 的服务条款。

## 功能

| 模块 | 能做什么 |
|---|---|
| 首页 | 最近 48 小时热门（按收藏数排序，不足一屏自动放宽到 7 天）；继续下滑从预取缓存里随机推荐收藏数 > 500 的作品 |
| 搜索 | 按标签 / 作者 / 关注画师搜索，异步任务 + 游标分页；可过滤 R-18 与自定义屏蔽标签 |
| 缓存浏览 | 查看预取标签累积下来的作品，库内过滤、排序、分页 |
| 图库 | 浏览已下载作品，单条 / 批量删除（含无 DB 记录的孤儿文件） |
| 详情 | 多图浏览、原图 / 中图候选链、上一作 / 下一作 |
| 下载 | 单张或批量加入队列，进度轮询、取消、失败重试 |
| 预取 | 配置标签（支持同义词合并），后台定时拉取；收藏数定期刷新、按容量自动淘汰 |
| 自动关注 | 定时检查关注画师的新作，可自动下载 |
| 备份 | SQLite 滚动快照（`/api/backups`），迁移前另有一次性备份 |
| 设置 | 全站访问密码、Cookie、代理、页大小、预取/关注节奏等 |

所有 Pixiv 请求都经由一个适配层发出，带令牌桶限流（避开 403）与 Cookie 失效识别；图片走本地缓存代理，不会每次刷新都打图床。

## 环境要求

- Python 3.9+（3.9.25、3.12.14、3.13.13 均完成验证；Linux 3.12.3 可直接使用）
- Windows / Linux 均可
- 一个能用的 Pixiv Cookie（见下）

## 快速开始

```bash
git clone https://github.com/feng-y-u/PpPpP.git
cd PpPpP
python -m venv venv
# Windows
venv\Scripts\activate
# Linux / macOS
. venv/bin/activate

pip install -r requirements.txt
```

在仓库根目录创建 `cookies.txt`（已在 `.gitignore` 中，不会被提交）：

```text
PHPSESSID=你的_pixiv_session_id
```

然后启动：

```bash
flask run --debug
# 或
python app.py
```

打开 <http://127.0.0.1:5000>。首次启动会自动创建 `instance/`（数据库、密钥、设置、图片缓存都在里面）。

> 本地用 HTTP 调试时如果设了访问密码，需要 `COOKIE_SECURE=false`，否则登录态不会回传。

## 配置

配置有两层，都是可选的：

- **`.env`**（环境变量）：在 import 时读取，适合放部署相关的东西。
- **`instance/settings.json`**：设置页写入。**改完需要重启服务器才生效**（`prefetch_interval` 例外，保存后立即生效）。

常用项：

| 键 | 说明 |
|---|---|
| `PIXIV_INSTANCE_DIR` | 把整个实例目录（DB / 密钥 / settings / 图片缓存）搬到别处；必须在启动前设置 |
| `ACCESS_PASSWORD` | 全站访问密码；留空 = 仅本机（loopback）免认证，其他来源会被 403，公网部署务必设置 |
| `ALLOW_UNAUTHENTICATED_PUBLIC` | 默认 `false`；确实要在无密码状态下接受公网访问时设为 `true`（风险自负） |
| `TRUSTED_PROXIES` | 可信反代 CIDR，逗号分隔；默认 `127.0.0.0/8,::1/128`。只有来自这些地址的 `X-Forwarded-For` 才会被采信 |
| `LOCAL_NETWORKS` | 免认证认可的“本机/内网”CIDR，逗号分隔；默认仅 loopback。要无密码访问内网时显式加入，例如 `127.0.0.0/8,192.168.1.0/24` |
| `COOKIE_SECURE` | 默认 `true`；本地 HTTP 调试设为 `false` |
| `PROXY` | HTTP/SOCKS5 代理，如 `http://127.0.0.1:7890` |
| `SSL_VERIFY` | 默认 `true`；仅在代理做 TLS 拦截时关闭，先跑 `python scripts/check_tls.py` 判定 |
| `ITEMS_PER_PAGE` | 每页作品数（默认 24，首页 / 图库 / 缓存页共用） |

完整列表见 `pixivviewer/config.py` 的 `SETTINGS_KEYS`，也可以直接在设置页里改。

## 生产部署

内存状态（预取 / 关注 / 下载队列 / 限流）是**进程级**的，所以 gunicorn 必须单 worker：

```bash
gunicorn -w 1 --threads 8 --timeout 300 -b 127.0.0.1:8000 app:app
```

建议放在 nginx / Caddy 后面，并在反代层开 gzip；公网部署前请对照 `docs/maintenance.md` 的「公网部署检查清单」（访问密码、TLS、`/api/open-dir` 限制等）。

## 目录结构

```text
app.py                 # 唯一入口：建 Flask、注册蓝图、启动后台线程
pixivviewer/           # 应用包
  config.py            # 常量与配置（import 时读 .env / settings.json）
  models.py            # SQLAlchemy ORM + SQLite
  pixiv_client.py      # Pixiv 适配层（端点、认证、限流）
  fetcher.py           # 业务层（过滤、缓存、分页、入库）
  background.py        # 预取、自动关注、下载引擎
  routes/              # 6 个 Blueprint
  templates/ static/   # 页面与前端资源（原生 JS，无构建）
tests/                 # pytest（离线）
scripts/               # 测试包装、TLS 诊断、样本抓取
migrations/            # 轻量迁移（PRAGMA user_version）
```

## 开发与测试

```bash
pip install -r requirements-dev.txt

# Windows（会准备确定性临时目录与沙箱插件）
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q

# Linux / macOS
pytest -q
```

当前基线：**484 passed / 2 skipped / 0 failed**（486 collected），约 20s。测试完全离线，不需要真实 Cookie。Python 3.9.25 完成 import/启动/关键测试，3.12.14 与 3.13.13 完成全量测试；发布质量门槛与已知技术债见 [QUALITY_BASELINE.md](QUALITY_BASELINE.md)。

更多工程约定（模块依赖、测试补丁 seam、性能与并发注意事项）见 [AGENTS.md](AGENTS.md)、[docs/architecture.md](docs/architecture.md)；发布质量基线见 [QUALITY_BASELINE.md](QUALITY_BASELINE.md)；运维手册（备份恢复、TLS、公网清单）见 [docs/maintenance.md](docs/maintenance.md)。

## 已知限制

- Pixiv 非官方接口，Cookie 过期或接口变动会表现为"搜索无结果"，需要重新配置 Cookie。
- 首页只聚合本地库：内容多少取决于预取标签、自动关注和手动搜索的覆盖。
- 只支持单用户、单进程；没有多用户权限体系。
- 数据目录（`instance/`、`downloads/`）和 `cookies.txt` 都不进版本库，迁移机器时记得单独备份。

## 许可证

仓库目前未附带 LICENSE 文件；如需再分发，请先补充许可证。
