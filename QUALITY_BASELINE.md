# 工程质量基线（QUALITY_BASELINE）

本文件是 Pixiv Viewer 的**发布质量契约**：它把审计结论、修复记录和测试覆盖缺口收敛成可执行的检查项。
面向维护者与发布者，不替代 `README.md`（使用说明）和 `AGENTS.md`（工程约定）。

- 基线日期：2026-11-13
- 适用范围：单人自部署、单进程 gunicorn、SQLite(WAL)、原生 ES2020 前端
- 当前状态：**本轮 P1/P2 已修复；无发布阻塞项**；审计遗留 P2/P3 见 §5
- 变更记录：
  - 2026-11-11 第一轮：审计修复 P1-1～P1-5、P2-1/P2-3/P2-4，并修复真实联调发现的搜索广告容器问题
  - 2026-11-11 第二轮：Python 3.9 兼容、设置原子校验、历史非法配置清洗、代理/XFF 信任边界、静态/登录/CSRF 豁免顺序，补齐 7 条高风险测试
  - 2026-11-13 第三轮：修复首页无限加载卡顿（lazyLoad observer 复用/reset、卡片合成层、图片异步解码），首页第一屏与随机池排除已下载作品，并实现首页候选/普通/老作品 2/7/30 天分级收藏数刷新

---

## 1. 支持的 Python 版本

| 项 | 结论 |
|---|---|
| 声明/实际支持 | **3.9+** |
| 开发/测试实测 | 3.9.25（真实 import + HTTP 启动 + 38 条关键测试）、3.12.14（性能修复前全量 482 passed / 2 skipped）、3.13.13（当前全量 488 passed / 2 skipped） |
| 生产目标环境 | Linux Python **3.12.3** 属于支持范围；同一 minor 已在 3.12.14 全量验证，建议在目标机再跑一次 §4.1 |
| 3.9 兼容处理 | `config.py`/`runtime.py`/`migrations/runner.py` 补 `from __future__ import annotations`；ORM 的 `Mapped[datetime \| None]` 改为 `Mapped[Optional[datetime]]`（SQLAlchemy 在 3.9 会求值注解） |
| 依赖锁定 | `requirements-lock.txt` 使用 3.9 可安装的精确版本：click 8.1.8、greenlet 3.2.4、gunicorn 23.0.0、pytest 8.4.2、requests 2.32.5、urllib3 2.6.3；`exceptiongroup`/`tomli` 仅在 Python <3.11 安装 |
| 开发依赖 | `requirements-dev.txt` 固定 `pytest>=8,<9`（pytest 9 的 iniconfig 2.3 要求 Python>=3.10） |

**发布规则**：同一个 `requirements-lock.txt` 必须在 3.9、3.12、3.13 都能安装；只在 3.13 上跑过全量测试、却宣称 3.9 支持，视为未验证。Linux 上两条 Windows `chmod` skip 会实际执行，测试数会不同于 Windows 基线。

---

## 2. 核心业务链路

| 链路 | 入口 | 关键模块 | 数据/外部依赖 | 必须保持的不变式 |
|---|---|---|---|---|
| 搜索 | `GET /search` → `/api/search/status/<id>` | `routes/search.py` → `fetcher.paginated_search` → `pixiv_client` | Pixiv Ajax、`Illust`、`SearchCache` | 异步任务终态只能是 `done/partial/error/cancelled`；详情预算与取消必须生效 |
| 首页 | `GET /`、`GET /api/home`、`POST /api/home/more` | `routes/home.py` → `helpers.query_home_feed/query_home_random` | 本地 `Illust` | 首页第一屏**不发 Pixiv 请求**；时间窗用 JST 口径；随机推荐去重 |
| 图库 | `GET /gallery`、`GET /api/gallery` | `routes/gallery.py` | `Illust` + `downloads/` 扫描缓存 | 页大小跟随 `ITEMS_PER_PAGE`；删除后扫描缓存立即失效 |
| 详情/图片 | `GET /detail/<pid>`、`GET /api/detail/<pid>`、`GET /api/image/<pid>/<i>` | `routes/gallery.py`、`helpers` | `Illust`、本地文件、运行中搜索快照 | 未落库的预览只能走内存快照；`/api/image` 只认已下载库行 |
| 缩略图代理 | `GET /thumb/<b64>` | `routes/gallery.py` → `pixiv_client.get_pooled_session` | `i.pximg.net`、`instance/image_cache` | 入口白名单、3xx 只跟随一次、凭据分级、7 天磁盘缓存 |
| 下载 | `POST /download/<pid>`、`/api/download/batch` | `routes/download.py` → `background._download_illust` | Pixiv 原图、`downloads/` | 同一 pid 不重复排队；取消后能重新下载；终态用 CAS 写 |
| 预取 | 设置页标签 → `_prefetch_loop` | `background.py`、`fetcher.py` | Pixiv、`SearchCache`、`Illust` | 入库不停；容量三层淘汰；失败退避状态机 |
| 自动关注 | 设置页开关 → `_auto_follow_worker` | `background.py`、`fetcher.fetch_following` | Pixiv 关注列表 | `new_pids` 是新增计数唯一口径；自动下载必须先有原图地址 |
| 设置/认证 | `/login`、`/api/settings`、`/api/prefetch/config` | `routes/settings.py`、`middleware.py`、`config.py` | `settings.json`、`cookies.txt`、session | 设置先校验后落盘；Cookie 只写 `COOKIE_PATH`；CSRF 全覆盖 |

**全局数据纪律**：
- 写入必须走 `safe_commit()`；新作品必须走 `_insert_new_illusts()` 的冲突容忍写入。
- 进程级内存状态只允许 `gunicorn -w 1`；并发请求由 `--threads` 提供。
- `tags`/`original_urls`/`local_paths` 只通过 `*_list` 属性读写。

---

## 3. 安全边界

| 边界 | 机制 | 检查点 |
|---|---|---|
| 全站访问 | `ACCESS_PASSWORD` 登录墙；session 绑定凭据代次；7 天有效期 | 公网部署必须设密码 |
| 免认证公网守卫 | 无密码且客户端不在 `LOCAL_NETWORKS` → 403；`ALLOW_UNAUTHENTICATED_PUBLIC=true` 才放行 | **默认只认 loopback**，见 SEC-01 |
| 可信反代 | `TRUSTED_PROXIES` 内的直连对端才采信 `X-Forwarded-For` | 反代在私网时必须显式加 CIDR，否则 XFF 不被采信 |
| CSRF | 所有修改型 POST 要求 `X-CSRF-Token` | 新增 POST 必须挂 `_csrf_required` |
| 登录限流 | 5 次/分钟，按 `_client_ip()` 取 IP；失败延迟 1 秒 | 公网/私网不可信对端伪造 XFF 不得换桶 |
| Cookie | `cookies.txt` 只存 `PHPSESSID=...`；同目录 tmp + 原子替换 | 文件权限建议 600；绝不能存访问密码 |
| 图片代理 | `/thumb` 仅接受 `i.pximg.net` 入口；3xx 按凭据分级跟随一次；只缓存位图 | 非 https/内网/云元数据/userinfo/非 443 一律拒绝；SVG 也拒绝 |
| 图片下载 | `check_image_url` + `IMAGE_HOST_ALLOWLIST`；白名单外使用无凭据 Session | 不把 Cookie 发给第三方主机 |
| 本机功能 | `/api/open-dir` 仅本机；带 `X-Forwarded-For` 直接 403 | 反代后该功能不可用是刻意 fail-closed |
| 密钥/日志 | `.secret_key`/`.cursor_secret` 自动生成并收紧权限；Werkzeug 请求日志 WARNING | 不把 Cookie/密码写进日志或仓库 |

**SEC-01（已修复的信任边界）**：
`middleware._client_ip()` 不再把"私网"自动当成可信：
- 只有直连对端落在 `config.TRUSTED_PROXIES`（默认 `127.0.0.0/8,::1/128`）时才采信 ProxyFix 还原的 XFF；
- 免认证范围由 `config.LOCAL_NETWORKS`（默认同样只有 loopback）决定；
- Docker 网桥（`172.17.x`）、普通内网（`192.168/10.x`）默认都不放行，也不能靠自带 `XFF: 127.0.0.1` 伪装成本机。

部署要求：
- 公网：必须设 `ACCESS_PASSWORD`；反代必须覆盖写 XFF，并把反代 CIDR 加入 `TRUSTED_PROXIES`。
- Docker `-p`：来源是网桥私网，默认被拒绝；直接设 `ACCESS_PASSWORD`，不要把网桥 CIDR 加进 `LOCAL_NETWORKS`。
- 内网无密码：显式设 `LOCAL_NETWORKS`（例如 `127.0.0.0/8,192.168.1.0/24`），并确认该网段可信。

---

## 4. 必须通过的测试

### 4.1 发布门槛

```bash
# 离线全量（不读真实 Cookie）
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q
```

**当前基线（Python 3.13.13）：490 collected = 488 passed / 2 skipped / 0 failed，约 20s。**
2 条 skip 是 Windows 上 POSIX `chmod` 语义不可用，不是失败；Linux（含 fuyan 的 3.12.3）上这两条会实际执行，结果数会不同。

Python 3.9.25 真实环境已单独验证：
- `python -c "import app"` 成功（59 条 URL 规则）；
- `python app.py` 启动后 `GET /` 返回 200；
- `tests/test_models.py tests/test_pixiv_contract.py` 共 38 passed；
- 完整 3.9 套件受当前 DSH 沙箱的 `tmp_path` ACL 限制未能一次跑完，不能把 3.13 的结果冒充成 3.9 全量结果。

发布前必须满足：
- `0 failed`；
- 3.9 至少完成 import + 启动 + 关键测试（本文件 §4.1 已记录）；
- 新增/修改的模块至少有定向用例；
- 不得把“环境性失败”当成可接受基线；
- 测试不得读写生产库（`tests/conftest.py` 的三道防线不得拆除）。

### 4.2 按修改范围的定向测试

| 修改范围 | 命令 |
|---|---|
| 设置/预取 API | `run_tests.ps1 tests/test_settings_api.py tests/test_prefetch_api.py -q` |
| 认证/限流/公网守卫 | `run_tests.ps1 tests/test_auth.py -q` |
| 图库/详情页大小 | `run_tests.ps1 tests/test_app.py::TestGalleryPageSize -q` |
| 详情页 JS 页大小 | `run_tests.ps1 tests/test_page_detail_js.py -q`（无 node 时 skip） |
| 自动关注 | `run_tests.ps1 tests/test_auto_follow.py -q` |
| 缩略图代理/重定向 | `run_tests.ps1 tests/test_thumb.py -q` |
| 下载/批量上限 | `run_tests.ps1 tests/test_download.py -q` |
| Pixiv 适配层契约 | `run_tests.ps1 tests/test_pixiv_contract.py -q` |

### 4.3 真实链路冒烟（手工，发布前至少一次）

使用**临时 `PIXIV_INSTANCE_DIR`**，不要指向生产 `instance/`：
1. 启动应用，确认 `/` 返回 200；
2. `GET /search` 发起真实标签搜索，轮询到 `done` 且 results 非空；
3. 打开 `/detail/<pid>`，确认 200；
4. 取 `/api/detail/<pid>` 的 `medium_urls[0]`，请求 `/thumb/...`，确认 200 且 `Content-Type: image/*`；
5. 清理临时目录，确认未写生产库。

2026-11-11 实测记录：搜索 24 条、详情 200、`/thumb` 200 `image/jpeg` 303099 bytes；真实适配层搜索 60 条、原图 200 `image/png`。

> `pytest -m integration` 目前 **0 条用例、490 deselected**；在补上真正的 `@pytest.mark.integration` 用例之前，不得把该命令当成发布门槛。

---

## 5. 已知技术债

### 5.1 已关闭（第二轮）

| ID | 原问题 | 处置 | 验证 |
|---|---|---|---|
| TD-01 | Python 3.9 导入失败（PEP 604 注解 / ORM 注解求值 / 依赖锁要求 3.10） | 补 future import；ORM 改 `Optional`；锁文件降至 3.9 可安装版本 | 3.9.25 import、HTTP 启动 200、38 条关键测试 |
| TD-02 | 设置接口先写 Cookie、后校验数值 | `api_settings_post` 先校验全部设置再落盘 | 新增 Cookie+非法数值回归用例 |
| TD-03 | 历史越界 settings.json 卡死设置页 | `_load_settings` 按 `SETTINGS_BOUNDS` 清洗 | GET 回默认值、原样 POST 成功并清盘 |
| TD-04 | Docker/私网/伪造 XFF 绕过公网守卫 | `TRUSTED_PROXIES` + `LOCAL_NETWORKS`，默认 only loopback；私网/公网对端不采信 XFF | 私网/Docker/XFF 矩阵用例 |
| TD-12 | 公网守卫先于豁免判断，`/static`、`/login`、`/csrf-token` 被 403 | 豁免判断移到守卫之前 | 公网无密码豁免断言 |
| TD-14 | 页大小只有源码断言；worker 测试有轮询重复提交风险 | 新增 Node DOM/JS 执行测试；worker mock 改成一次性返回 | `test_page_detail_js.py`、`test_auto_follow.py` |

### 5.2 仍存在的 P2/P3

| ID | 问题 | 状态 |
|---|---|---|
| TD-05 | `GET /api/following` 仍会写库并触发后台补全 | 未处理 |
| TD-06 | `/download_file` 的 200MB 内存 zip 阈值 | 未处理 |
| TD-07 | `-w 1` 仍只靠文档约束，无运行时多 worker 检测 | 未处理 |
| TD-08 | 孤儿卡片/下载页仍下发绝对路径（`local_paths`/`local_dir`） | 未处理 |
| TD-09 | `_kick_background_fill` 仍是每请求一个 daemon 线程 | 未处理 |
| TD-10 | 自动关注自动下载按新作品串行补详情，量大时阻塞 worker | 未处理 |
| TD-11 | 图库 `file_size` 补算与目录扫描未优化 | 未处理 |
| TD-13 | 无 `@pytest.mark.integration` 用例；真实链路只有手工冒烟 | 测试基础设施缺口 |
| TD-15 | Python 3.9 完整套件受当前沙箱 `tmp_path` ACL 限制，未在 3.9 跑全 | 环境限制，需在 CI/正常 Windows 环境补跑 |

---

## 6. 发布前检查清单

### 6.1 代码与版本

- [ ] `git diff --check` 无空白错误；无临时目录、`.live_*`、`.review_*` 残留。
- [ ] `python -V` 满足 3.9+；3.9 锁文件已安装并完成 import/启动验证。
- [ ] `requirements-lock.txt` 未引入未验证的新依赖；3.9 与 3.13 两套环境都能安装。
- [ ] 没有删除既有功能或改变未声明 API 的响应形状；新增 400/403 已在发布说明中列出。

### 6.2 测试

- [ ] 全量 `run_tests.ps1 -q`：**0 failed**（当前 488 passed / 2 skipped）。
- [ ] 改动模块的定向用例通过。
- [ ] 3.9 环境 import + 启动 + 关键测试通过；不要把 3.13 全量结果冒充成 3.9。
- [ ] 真实链路冒烟通过（见 §4.3），且使用临时实例目录。
- [ ] 确认 `pytest -m integration` 的 0 条现状已记录；不要把空跑当通过。

### 6.3 安全姿态

- [ ] 公网部署已设 `ACCESS_PASSWORD`；本地调试才允许留空。
- [ ] `ALLOW_UNAUTHENTICATED_PUBLIC` 保持 `false`，除非明确接受公网裸奔。
- [ ] `TRUSTED_PROXIES` 只包含真实反代地址；反代已覆盖写 XFF；否则 XFF 不被采信。
- [ ] `LOCAL_NETWORKS` 保持 loopback，除非明确要无密码访问某个内网段；不要把 Docker 网桥加进去。
- [ ] `COOKIE_SECURE=true`（HTTPS）；本地 HTTP 调试才临时设 `false`。
- [ ] `SSL_VERIFY=true`；只有代理做 TLS 拦截且已跑 `scripts/check_tls.py` 时才关闭。
- [ ] `cookies.txt` 位置是 `config.COOKIE_PATH`，内容只有 `PHPSESSID=...`，权限 600；不含访问密码。
- [ ] `/api/open-dir` 在反代后为 403；本机直连才可用。

### 6.4 运行与数据

- [ ] gunicorn 使用 `-w 1 --threads 8 --timeout 300`。
- [ ] 启动日志无 `ACCESS_PASSWORD 未设置`（公网）与 `TLS 校验已关闭` 告警。
- [ ] 数据库迁移前已生成备份；`PRAGMA integrity_check` 通过。
- [ ] `image_cache` 容量、`downloads/` 磁盘余量已检查。
- [ ] `/api/thumb/stats`、`/api/prefetch/status`、`/api/auto-follow/status` 可正常读取。
- [ ] 回滚路径明确：停服 → 恢复 DB 快照 → 回滚代码 → 重启 → 冒烟。

### 6.5 发布说明

- [ ] 写清新行为：设置 0 值/越界值会 400；无密码公网默认 403；`ALLOW_UNAUTHENTICATED_PUBLIC` 为逃生阀。
- [ ] 写清未解决项：TD-05～TD-11、TD-13、TD-15 作为已知问题列出；本轮已关闭项不再写成"待修"。

---

## 附：基线与审计记录

- 2026-11-11 第一轮：P1-1 自动关注新增计数/自动下载、P1-2 设置 0 值边界、P1-3 详情页页大小、P1-4 XFF 伪造限流、P1-5 无密码公网守卫、P2-1 `/thumb` Content-Type、P2-3 原图连接池、P2-4 批量上限；真实联调修复搜索广告容器。
- 2026-11-11 第二轮：Python 3.9 兼容（future import + ORM Optional + 依赖锁）、设置先校验后落盘、历史非法配置清洗、`TRUSTED_PROXIES`/`LOCAL_NETWORKS` 信任边界、静态/登录/CSRF 豁免顺序、SVG 重定向拒绝、page-detail DOM/JS 测试、真实 `fetch_following` + worker 集成、worker 轮询去重。
- 回归用例：第一轮新增 13 条，第二轮新增 7 条，本轮新增 6 条（lazyLoad observer 复用、首页卡片合成层、首页排除已下载第一屏/随机池、列表 bookmarkCount 刷新、分级刷新优先级/间隔）；全量基线 477 → 490 collected（488 passed / 2 skipped）。
- 完整审计背景见 `docs/risk-audit-report.md`；本轮修复的设计记录见 `docs/superpowers/`（如已归档）。
