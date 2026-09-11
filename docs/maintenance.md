# Pixiv Viewer — 个人自用维护指南

> 面向单人自部署：单 Worker、SQLite WAL、进程内后台线程。不做多实例/多用户扩展。
> 所有命令假设：开发在 Windows（`powershell`/`venv`），生产在 Linux（`gunicorn`/`systemd`）。

---

## 1. 三类存储（务必分清）

| 存储 | 位置 | 内容 | 生命周期 |
|---|---|---|---|
| **预取数据库缓存** | `instance/pixiv.db`（`Illust` + `SearchCache`） | 预取标签累积的作品元数据 + 收藏数 | 由预取循环管理，容量上限 **10000 条**，超出按更新后的最终收藏数低优先删除（未完成最终刷新的暂不淘汰） |
| **缩略图磁盘缓存** | `instance/image_cache/` | `/thumb/<base64>` 代理的图片 | 自动，TTL 7 天 |
| **原图下载文件** | `downloads/<pixiv_id>/` | 手动/自动下载的原图画质文件 | 由下载管理页 + `pixiv-cleanup.sh` 管理 |

> ⚠️ `pixiv-cleanup.sh` **只清理 downloads/ 下的已下载原图**，与预取数据库的 10000 条容量控制**完全无关**。

---

## 1b. 实例目录重定向（`PIXIV_INSTANCE_DIR`）

上面三类存储里的"实例数据"默认都在仓库内的 `instance/`。设 `PIXIV_INSTANCE_DIR` 可以把**整个实例目录**搬到别的位置：`.cursor_secret` / `.secret_key` / `settings.json` / `pixiv.db`（+WAL/SHM）/ `image_cache/` / `backups/` / `thumb_redirect_hosts.json` 都由 `config._instance_dir` 单点派生，跟着一起走。

```bash
# Linux：systemd unit 里用 Environment=PIXIV_INSTANCE_DIR=/srv/pixiv-data ，或写进 .env
PIXIV_INSTANCE_DIR=/srv/pixiv-data gunicorn -w 1 --threads 8 --timeout 300 -b 127.0.0.1:8000 app:app
```

```powershell
# Windows 开发机
$env:PIXIV_INSTANCE_DIR = 'D:\pixiv-data' ; flask run --debug
```

- **必须在 `import config` 之前设置**（环境变量与 `.env` 都可以，`.env` 在实例目录派生之前加载）。`config.py` 在 import 时就派生好全部路径并生成密钥，**事后改环境变量无效**。
- **不设置时行为不变**：仍是仓库内 `instance/`（生产默认姿态）。
- **覆盖值不可用时 import 直接失败，故意不回落到默认目录**。这一条是刻意的：静默回落意味着测试或部署会悄悄读写真实实例数据（历史上测试就是往真实 `instance/` 里写密钥的）。所以路径写错要当"启动报错"处理，不要期待它自己纠正；目录不存在会被自动创建，而指向一个**文件**则启动即报错。
- 典型用途：多实例共用一份代码（各用各的数据目录）、把数据放到更大的盘或独立分区、自动化测试隔离（`tests/conftest.py` 就是靠它在 `import config` 之前把整轮测试重定向到临时目录）。
- **迁移已有的 `instance/`**：新目录不会自动搬。停服后把 `instance/` 整个拷过去（数据库连 `-wal`/`-shm` 一起拷，或先停服让 WAL checkpoint —— 见第 4 节），再设变量重启。
- **换了目录就等于换了密钥**：`.secret_key` / `.cursor_secret` 若不在新目录里会被重新生成 → 所有登录会话与搜索游标失效，用户需要重新登录。要保留登录态就把这两个文件一起拷过去。

```bash
# 改过配置后值得跑一次：确认当前进程实际用哪个目录
python -c "import config; print(config._instance_dir); print(config.DATABASE_PATH)"
```

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

## 5b. 缩略图磁盘缓存（`instance/image_cache/`）

`/thumb/<base64>` 代理下来的图片（网格缩略图、灯箱中图）缓存于此。**有容量上限**，超出后自动按修改时间从旧到新淘汰，无需手动干预。

```bash
# 查看占用与文件数
du -sh instance/image_cache && ls instance/image_cache | wc -l

# 调整上限（默认 1 GB）：改 config.py 的 IMAGE_CACHE_MAX_BYTES 后重启。
# 淘汰的代价只是下次访问回源一次，所以磁盘宽松时建议调大，以减少对 Pixiv 的请求。
```

- 淘汰规则见 `AGENTS.md`「目录」一节：只删本缓存自己写的文件，且是"最旧写入优先"而非严格 LRU。
- 要彻底清空时停服删掉整个目录即可，下次访问会自动重建（代价是一轮回源）。

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

- **必须 `gunicorn -w 1`，并建议加 `--threads 8`**：进程内状态（下载锁、预取、搜索任务、限流、自动关注）**不支持多 worker**，但线程共享同一进程内存，因此 `--threads` 在保持单进程语义的前提下提供并发。

- **为什么要 `--threads`**：不给 `--threads` 时 gunicorn 的 sync worker **一次只处理一个请求**，一页 24 张缩略图会严格串行加载 —— 这是图库首屏慢的主要来源之一。开启后图片可并发拉取，共享状态的线程安全已审计（见 `AGENTS.md`「并发：`--threads` 下的共享状态约定」）。

  ```bash
  gunicorn -w 1 --threads 8 --timeout 300 -b 127.0.0.1:8000 app:app
  ```

- 更新代码后重启**必须整进程重启**（`systemctl restart`），不能只 `kill -HUP`（`--preload` 下不重载代码）。
- 服务日志：`journalctl -u pixiv-viewer -f | grep prefetch`（预取/清理）。

---

## 8. 公网部署检查清单

单人自用的默认姿态是"本机 loopback"，本节只针对**把服务挂到公网或公司内网**的部署。

**必须做的**

1. **设置 `ACCESS_PASSWORD`**（`.env` 或环境变量）。留空 = **全站免认证**：任何人可搜索、下载、改设置。启动日志会打印一行
   `ACCESS_PASSWORD 未设置：全站免认证，仅限本机/可信内网使用；公网部署必须设置` 作提醒。
2. **只监听 loopback，由反代对外**：

   ```bash
   gunicorn -w 1 --threads 8 --timeout 300 -b 127.0.0.1:8000 app:app
   ```

   **禁止 `python app.py` 直跑公网**：它默认只绑 `127.0.0.1`（要改监听地址用 `HOST`/`PORT` 环境变量），但它终究是开发服务器，没有反代的超时/并发/压缩保护。
3. **反代必须剥离或重写 `X-Forwarded-For`**：

   ```nginx
   proxy_set_header X-Forwarded-For $remote_addr;   # 覆盖掉客户端自带的值
   ```

   应用侧 `ProxyFix(x_for=1)` 按这个头还原真实客户端 IP，**限流与 `/api/open-dir` 的本机判定都依赖它**。若反代原样透传客户端自带的 XFF，访问者就能自称 `127.0.0.1`。
4. **`COOKIE_SECURE=true`**（默认已是 true）：HTTPS 下才回传登录态；纯 HTTP 调试必须显式设 `false`，否则登录后立刻掉线。
5. **证书与压缩都在反代上做**：Flask 自身不 gzip（`/api/gallery?limit=50` 约 25 KB），TLS 也由反代终结。

**反代部署下的行为边界**

- **`/api/open-dir` 不可用**：只要请求带 `X-Forwarded-For` 就返回 403（包括"反代把 XFF 改写成 127.0.0.1"的情况）。这是刻意的 fail-closed —— 该功能能在服务器上打开本地目录，经代理转发时无法区分"本机浏览器"和"远程伪造"。
- 需要在服务器本机上用这个功能时：在服务器自己的浏览器里打开 `http://127.0.0.1:8000`（不经反代）。

**自查命令**

```bash
# 1) 免认证告警是否出现（未设 ACCESS_PASSWORD 时）
grep 'ACCESS_PASSWORD 未设置' /var/log/pixiv-viewer.log

# 2) 伪造本机身份打 open-dir：应当 403 而不是 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/api/open-dir \
  -H 'X-Forwarded-For: 127.0.0.1' -H "X-CSRF-Token: $TOKEN" \
  -H 'Content-Type: application/json' -d '{"path":"/"}'

# 3) 未登录访问：API 应 401，页面应 302 到 /login
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/blocked-tags
```

---

## 9. TLS 校验与代理（`SSL_VERIFY`）

**默认 `SSL_VERIFY=true`**（`config.py`）。只有"你的代理确实在做 TLS 拦截（用自签根证书解密流量）"时才允许关掉它 —— 关闭校验意味着链路上任何中间人（代理、公司网关、公共 WiFi）都能读取并篡改你与 Pixiv 之间的流量。

**怎么判断当前环境该不该关**：

```bash
python scripts/check_tls.py            # 只读诊断：只做 TLS 握手读证书，不发业务请求、不写文件
python scripts/check_tls.py --timeout 15   # 网络慢时放宽
```

判定规则与退出码：

| 退出码 | 含义 | 该怎么做 |
|---|---|---|
| 0 | 至少一条链路 `verify=True` 成功；若直连与代理都成功，两者叶证书指纹一致 | 保持默认 `SSL_VERIFY=true` |
| 1 | 所有 `verify=True` 都失败而 `verify=False` 能连通；或直连/代理叶证书指纹不一致 | 疑似 TLS 拦截或本机缺 CA，见下 |
| 2 | 两种方式都连不上（网络不可达） | 先修网络再跑 |

**退出码 1 时怎么办**（按优先级）：

1. **首选：把代理的根证书装进系统信任库**（Windows"证书管理器 → 受信任的根证书颁发机构"，Linux 放 `/usr/local/share/ca-certificates/` 后 `update-ca-certificates`），然后保持 `SSL_VERIFY=true`。
2. 确实无法安装（例如只想临时跑）：设 `SSL_VERIFY=false`（环境变量或 `.env`），并接受"流量可被链路上任何人读取/篡改"。**该状态会在启动日志里出现告警**：`TLS 校验已关闭（SSL_VERIFY=false）…`。
3. 判断依据不明确时**不要**关校验 —— 关掉只是让失败消失，并没有解决问题。

> 本机实测记录（2026-09-10，代理 `http://127.0.0.1:7890`）：
> `scripts/check_tls.py` 退出码 **0**。`www.pixiv.net` 与 `i.pximg.net` 经代理 `verify=True` 握手均成功，issuer 为 `Google Trust Services`（`WE1` / `WR1`，即公共 CA 直签），叶证书 SHA-256 前 16 位分别为 `baaff5d5e06af2d2` / `eded7a031557e60`；直连两条链路均不可达（该网络环境必须走代理出网）。另有一项决定性对照：`www.cloudflare.com` 直连与经代理的叶证书指纹完全相同（`cf80aa757e806acf`）—— 代理是纯 CONNECT 透传，不做 TLS 拦截。
> 网络或代理变更后请重跑脚本，不要凭记忆沿用结论。

**图片主机白名单（`config.IMAGE_HOST_ALLOWLIST`）**：决定"访问某主机时是否允许携带 Pixiv 凭据"。官方图床 `i.pximg.net` 在白名单内；下载引擎遇到**白名单外的公网 https** 主机（例如 Pixiv 将来换 CDN）会改用**无凭据会话**继续下载 —— 下载不中断，同时不会把 `PHPSESSID` 交给第三方。自建图片镜像时把域名加进这个集合即可带凭据访问；无论是否白名单，非 https / 内网地址 / 云元数据端点 / 带 userinfo / 非 443 端口的地址一律拒绝且不发起请求，图片地址发生重定向也一律判失败（不跟随）。

**`/thumb` 缩略图代理的重定向策略（与下载引擎同一套判定）**：入口仍然只接受 `https://i.pximg.net/`；图床返回 3xx 时**不交给 requests 自动跟随**，而是按凭据分级跟随一次：

| 目标 | 行为 |
|---|---|
| 白名单内主机（A 级） | 带凭据连接池跟随一次（官方图床内部跳转属正常） |
| 白名单外的公网 https（B 级） | 用**无凭据**连接池跟随一次，且响应必须是 `Content-Type: image/*`，成功后才记入发现表 |
| 非 https / 内网 / 云元数据 / 带 userinfo / 非 443 | 502，**不发起请求**，计入被拒计数 |
| 跟随后仍是 3xx（嵌套重定向）、3xx 但缺 `Location` | 502，不再跟随 |

**为何这么设计**：`fetcher.build_pixiv_session()` 挂的是**会话级** `Cookie` 头，requests 会把它发给任意主机 —— 所以"跟随到白名单外主机"等价于把 `PHPSESSID` 交给第三方。分级的原则是**白名单只决定"是否携带凭据"，不决定"能否访问"**，于是既不会因为图床换域名而整页缩略图全挂，也不会泄露凭据。

**发现表（观测，不自动生效）**：

```bash
# 看当前静态白名单、自动发现的图片主机与被拒主机计数
curl -s localhost:8000/api/thumb/redirect-hosts | python -m json.tool

# 清空发现表（内存 + instance/thumb_redirect_hosts.json）
curl -s -X DELETE localhost:8000/api/thumb/redirect-hosts -H "X-CSRF-Token: <token>"
```

发现表**不会**自动变成白名单：B 级本来就取得到图，没有放宽信任的必要。若日志出现

```
/thumb 发现新的图片主机 img-cdn.example.net（已用无凭据方式成功取图）……
```

且确认那是 Pixiv 官方 CDN，再手工把域名加进 `config.IMAGE_HOST_ALLOWLIST`（需要重启）以恢复携带凭据访问。`THUMB_REDIRECT_DISCOVERY=false` 可关闭 B 级跟随（跨域重定向一律 502，回到纯拒绝行为）；发现表落盘在 `instance/thumb_redirect_hosts.json`，删掉即从空表开始。

