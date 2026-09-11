# Pixiv Viewer 高风险修复计划（P0 / P1）

> 来源：《Pixiv Viewer 深度架构与风险审计报告》（`docs/risk-audit-report.md`，2026-09-10）。
> 日期：2026-09-10。状态：**P0（S1–S7b）已实现、已提交、已推送，并在真实部署上验证；P1（S8–S18）待实施**。
> P0 的提交清单、测试增量、逐步证伪证据与部署验证见文末「七、P0 实施与验证结果」。
> 硬约束：不重写；不改"单人自部署 + Flask + SQLite + gunicorn -w 1"总体架构；最小修改优先；每步必须有测试；不顺手做无关重构；不为优雅改变已稳定行为。

## 0. 前置约束（贯穿全部步骤）

1. 不引入新依赖（`requirements-lock.txt` 不变）。
2. 不新增 schema 版本（不动 `user_version`、不改 v1–v4 迁移函数体）。
3. 不改 `-w 1` 单进程语义与内存状态设计。
4. 不动 app 命名空间测试契约（`app.py` 的 from-import 导出清单、延迟 `import app` 模式）。
5. 每步一个独立提交，Conventional Commits + 中文描述。
6. 每步测试必须"能证伪"：回退该步 diff 时，新增用例至少有 1 条失败。

---

## 一、最终修复顺序

顺序原则：**P0 全部先行 → P1；同文件步骤相邻**（减少反复触碰同一函数的冲突与回归风险）；**测试基建隔离前置**到"测试补齐"之前。

| 步骤 | 优先级 | 问题 | 主要文件簇 | 可合并提交 | 状态 |
|---|---|---|---|---|---|
| S1 | P0 | 下载取消/reset 竞态（幻影 done） | background.py / routes_download.py | — | ✅ `81300ec` |
| S2 | P0 | download safe_commit 异常 → downloading 卡死 | background.py / routes_download.py | — | ✅ `1f510d5` |
| S3 | P0 | 下载 queued 窗口丢失（+ `_queued_downloads` 并发快照） | runtime.py / background.py / routes_download.py | — | ✅ `981628e` |
| S4 | P0 | 预取异常导致容量清理被跳过 | background.py | 可与 S2 合并（不同函数，建议独立） | ✅ `4a5dd97` |
| S5 | P0 | SQLite WAL 备份缺陷 | migrations/runner.py | — | ✅ `b55541b` |
| S6 | P0 | 公网部署安全姿态 | app.py / routes_gallery.py / routes_settings.py / docs/maintenance.md | — | ✅ `6e446e3` |
| S7a | P0 | SSL_VERIFY 默认值（探测已完成：代理透传）+ 下载 URL 校验 | config.py / background.py / scripts/check_tls.py / docs/maintenance.md | 与 S6 顺序执行 | ✅ `e5b73b8` |
| S7b | P0 | /thumb 越界重定向拒绝 + 自动发现机制 | config.py / runtime.py / routes_gallery.py / helpers.py | 复用 S16 的 _atomic_write_json（未合并前内联） | ✅ `7fea3b5` |
| S8 | P1(前置) | 测试基建隔离（密钥/settings/缓存目录） | config.py / routes_gallery.py / tests/conftest.py | — | ⬜ 待实施 |
| S9 | P1 | 重复下载保护 | background.py / routes_download.py | — | ⬜ 待实施 |
| S10 | P1 | SearchCache 并发一致性 | background.py | — | ⬜ 待实施 |
| S11 | P1 | thumb 信号量饥饿 | runtime.py / routes_gallery.py | — | ⬜ 待实施 |
| S12 | P1 | 后台线程 heartbeat | background.py / routes_prefetch.py | 可与 S11 合并 | ⬜ 待实施 |
| S13 | P1 | Pixiv 403 分类 | fetcher.py | — | ⬜ 待实施 |
| S14 | P1 | `_fill_last_attempt` 内存增长 | fetcher.py | 可与 S13 合并 | ⬜ 待实施 |
| S15 | P1 | ZIP 内存问题 | routes_download.py / config.py | — | ⬜ 待实施 |
| S16 | P1 | settings.json 原子写 | helpers.py / routes_settings.py / routes_prefetch.py / config.py | — | ⬜ 待实施 |
| S17 | P1 | secret 文件权限 | config.py / app.py | 可与 S16 合并 | ⬜ 待实施 |
| S18 | P1 | 下载/图片/settings 测试补齐 | tests/（新增 3 文件 + conftest） | 收口步骤 | ⬜ 待实施 |

**依赖关系**：
- S1/S2/S3/S9 都改 `_download_illust` / `routes_download.py` → 顺序执行，不并行。
- S8 是 S18 的前置（否则新增测试会写真实 `instance/`）。
- S15 依赖 S3 的快照锁（同文件，顺带使用）。
- S4 与 S10 都改 `background.py` 但函数不重叠。

---

## 二 / 三 / 四、每步：文件 · 核心方案 · 测试

### S1（P0）下载取消/reset 竞态：幻影 done

**文件**：`background.py`（`_download_illust`）、`routes_download.py`（`_cancel_download_internal`）

**核心方案**（不改锁归属、不改 `finally` 清理语义）：
1. `_cancel_download_internal(reset=True)` 改为**条件更新裁判**：
   - `status == 'downloading'` 时执行 `UPDATE illusts SET download_status=NULL WHERE pixiv_id=:p AND download_status='downloading'`；
   - `rowcount == 1` → 本次 reset 赢，继续删目录文件 + 写 `failed` 日志（原行为）；
   - `rowcount == 0` → worker 已提交终态（done/failed）→ **不删文件、不写 failed**，返回 409 + "下载已结束，请刷新"（修掉"reset 删掉刚完成文件"的另一方向不一致）。
   - 仅 queued（`status != 'downloading'` 且 `is_queued`）→ 保持现状（置 None + 删目录 + flag）。
2. `_download_illust` 成功路径：在现有取消检查之后、组装 done 之前，**同一 session 内重读该行**并复查 `pixiv_id in download_cancellations`；若标记已置或 `fresh.download_status != 'downloading'` 或行不存在 → 走既有取消分支（删本次文件、置 None、写 `cancelled` 日志）。
3. 提交后再复查一次标记：若在"复查→commit"窗口内被置位 → **补偿**：删本次文件 + `UPDATE illusts SET download_status=NULL WHERE pixiv_id=:p AND download_status='done'`（条件更新，避免误伤） + `cancelled` 日志。
4. 失败路径同样把写入改成条件更新（`WHERE download_status='downloading'`）：`rowcount == 0` 时说明 reset 已接管，跳过重复删文件与重复日志。

**测试**（新增 `tests/test_download.py`）：
- `test_reset_during_final_commit_no_phantom_done`：用 `threading.Event` 卡住 worker 于"取消检查之后、commit 之前"，先跑 reset 再放行 → 断言最终 `download_status is None`、磁盘无文件、**无 `action='done'` 日志**（回退修复时此用例必须失败）。
- `test_reset_after_worker_done_does_not_delete_files`：worker 已完成 → reset 返回 409 且文件仍在。
- `test_reset_when_queued_still_cancels`：queued-only 行为回归。
- `test_download_failure_after_reset_skips_double_cleanup`：失败路径 `rowcount==0` 分支。

---

### S2（P0）safe_commit 异常 → downloading 卡死

**文件**：`background.py`（`_download_illust`）、`routes_download.py`（`trigger_download`）

**核心方案**：
1. 新增私有工具 `_rescue_commit(db, pixiv_id, *, action, message)`：`safe_commit` 失败 → `logger.error` + `db.rollback()` + 尽力把该行置 `failed` 并写 `DownloadLog`（再次失败只记日志，不冒泡）。`_download_illust` 的 4 个提交点（起始 / 无原图 / 失败 / 完成）统一改用它。
2. `trigger_download` 的 `downloading` 分支增加**幽灵检测**：`pixiv_id not in _download_progress` **且** `pixiv_id not in _queued_downloads` → 判定为残留状态 → 置 `None` + `DownloadLog('failed','检测到残留 downloading 状态，已自动复位')` → 继续正常下载流程。
   - 说明：worker 存活期间必然在 `_download_progress` 中；`L613-623` 毫秒窗口误判无害（worker 随后会重新置 `downloading`）。

**测试**：
- `test_download_commit_failure_marks_failed`：monkeypatch `models.safe_commit` 首次抛 `OperationalError` → 最终 `failed` + 有失败日志，且**不是** `downloading`。
- `test_trigger_download_recovers_ghost_downloading`：预置 `downloading` 且无 progress/queued → POST 返回 accepted 且进入下载。
- `test_ghost_detection_keeps_live_download`：progress 中有条目 → 仍返回"下载中"，不复位。

---

### S3（P0）下载 queued 窗口丢失（+ 队列集合并发快照）

**文件**：`runtime.py`、`background.py`、`routes_download.py`

**核心方案**：
1. `runtime.py` 新增 `_download_queue_lock = threading.Lock()`；`_queued_downloads` 的**所有写点**（`trigger_download`、`batch_download`、`_cancel_download_internal`、`_download_illust` 的 discard、`_auto_follow_worker`）用锁包住；**读点**统一改为 `with _download_queue_lock: snapshot = set(_queued_downloads)`（顺带消除 `api_downloads` 的 `list(set)` 并发 `RuntimeError`）。
2. `_prefetch_capacity_cleanup` 与 `_refresh_bookmarks_pass` 的保护判定加入 `pid in queued_snapshot`（各自取一次快照）→ 消除"queued→首个 commit"窗口内被容量清理删行导致的**静默丢失**。
3. `_download_illust` 行缺失分支（查不到 Illust 行）改为写 `DownloadLog('failed','作品行已被清理，下载未执行')` 后 return，避免无痕消失。

**测试**：
- `test_capacity_cleanup_keeps_queued_download`、`test_refresh_pass_keeps_queued_download`。
- `test_download_missing_row_logs_failed`。
- `test_queued_snapshot_consistent_under_concurrency`：多线程反复 add/discard，同时反复取快照，迭代 N 次不抛 `RuntimeError`。
- `test_api_downloads_survives_concurrent_queue_mutation`（可选）。

---

### S4（P0）预取异常导致容量清理被跳过

**文件**：`background.py`（`_refresh_bookmarks_pass`、`_prefetch_loop`、`_prefetch_one_tag`）

**核心方案**：
1. `_refresh_bookmarks_pass` 在既有 `except PixivAuthError` / `except FileNotFoundError` **之后**新增 `except Exception`：记 `stats['aborted']='unknown'` + `logger.exception` + **不冒泡**（与 auth 同款语义）。
2. `_prefetch_loop` 把 `_prefetch_refresh_bookmarks()` 单独 try/except（仅记日志），随后**无条件**调用 `_prefetch_capacity_cleanup()` → 落实"每轮容量清理必执行"不变式。
3. `_prefetch_one_tag` 的 except 分支内状态写加 try/except 兜底（防 `safe_commit` 再抛导致 `fetching` 残留永久跳过；同源 3 行改动）。

**测试**：
- `test_refresh_generic_exception_does_not_bubble`：monkeypatch 详情解析抛 `KeyError` → `aborted=='unknown'` 且函数正常返回。
- `test_prefetch_loop_runs_cleanup_when_refresh_raises`：monkeypatch `_prefetch_refresh_bookmarks` 抛 `RuntimeError` → 断言 cleanup 仍被调用一次。
- `test_prefetch_one_tag_status_write_failure_does_not_raise`。

---

### S5（P0）SQLite WAL 备份缺陷

**文件**：`migrations/runner.py`

**核心方案**：
1. `run_migrations` 在 `backup_database(...)` **之前**执行 `PRAGMA wal_checkpoint(TRUNCATE)`（经 `engine.connect()`；非 WAL 库无副作用），保证主库文件包含全部已提交事务。
2. `backup_database` 复制主库后，若同目录存在 `-wal` / `-shm`，一并复制为同名前缀（belt & braces）；保持"仅 pending 时才备份""唯一时间戳命名"的既有行为。
3. 不做保留策略（见"不应该修改"）。

**测试**：
- `test_backup_captures_wal_uncheckpointed_data`：WAL 模式写入行且不 checkpoint → 走 `run_migrations`（有 pending）→ 用 sqlite3 打开 `.bak` 断言行存在（回退修复时必须失败）。
- `test_backup_still_works_on_non_wal_db`。
- `test_no_backup_when_no_pending`（既有行为回归）。

---

### S6（P0）公网部署安全姿态

**文件**：`app.py`、`routes_gallery.py`、`routes_settings.py`、`docs/maintenance.md`

**核心方案**：
1. `app.py __main__`：默认绑定改 `os.environ.get('HOST','127.0.0.1')` + `PORT`（不再默认 `0.0.0.0`）。
2. `app.py` 启动时若 `ACCESS_PASSWORD` 为空 → `logger.warning('ACCESS_PASSWORD 未设置：全站免认证，仅限本机/可信内网使用')`。
3. `api_open_dir` 判定增强：`remote_addr` 为 loopback **且** 请求**不存在** `X-Forwarded-For` 头才允许；否则 403（经反代的请求一律拒绝，防 XFF 伪造绕过）。
4. `settings_unlock` 失败分支补 `time.sleep(1)`（与 `login_submit` 对齐，减缓爆破）。
5. `docs/maintenance.md` 增加"公网部署检查清单"（必须设 ACCESS_PASSWORD；反代必须剥离/重写 XFF；open-dir 在反代部署下不可用；禁止 `python app.py` 直跑公网）。

**测试**：
- `test_open_dir_rejects_forwarded_request`（带 XFF → 403，即使 ProxyFix 已把 remote_addr 还原成 127.0.0.1）。
- `test_open_dir_allows_direct_localhost`（无 XFF + 127.0.0.1 → 200，mock `os.startfile`）。
- `test_unknown_unlock_delay_applied`（monkeypatch `time.sleep` 断言调用）。
- `test_main_default_host_is_loopback`（monkeypatch `app.app.run` 断言 host 参数）。
- `test_startup_warns_without_access_password`（caplog）。

---

### S7a（P0）SSL_VERIFY 默认值（探测已完成）+ 下载 URL 校验（Cookie 泄露面收口）

**前置探测结论（2026-09-10 实测，已执行）**

| 探测项 | 结果 | 含义 |
|---|---|---|
| `127.0.0.1:7890` TCP | 可达 | 代理在运行 |
| `i.pximg.net` **经代理** `verify=True` | **成功**，叶证书 issuer = `Google Trust Services`（CN=WR1） | 公共 CA 直签，未被替换 |
| `www.pixiv.net` **经代理** `verify=True` | **成功**，issuer = `Google Trust Services`（CN=WE1） | 同上 |
| `www.cloudflare.com` 直连 vs 经代理的叶证书 SHA-256 | **完全相同**（`cf80aa757e806acf`） | **代理是纯 CONNECT 透传，不做 TLS 拦截**（决定性对照） |
| 应用真实栈（requests + proxies + `verify=True`） | `i.pximg.net` 返回 404（假 URL，TLS 成功）、`www.pixiv.net` 返回 200 | 应用代码路径在 `verify=True` 下可直接工作 |
| 直连 Pixiv（不走代理） | TimeoutError | 该网络环境本就必须走代理（代理是出口，不是拦截层） |

**结论**：本机代理**不做 MITM**，`SSL_VERIFY` 默认改 `True` 在本机**零影响**。用户已确认接受该默认值。

**文件**：`config.py`、`background.py`（`_download_illust`）、`scripts/check_tls.py`（新增，只读诊断）、`docs/maintenance.md`

**核心方案**：
1. `config.py`：`SSL_VERIFY` 默认改 `True`（`os.environ.get('SSL_VERIFY','true').lower() != 'false'`），保留 `SSL_VERIFY=false` 作为 TLS 拦截型代理的**显式逃生门**。
2. `app.py`（启动期）与 `fetcher`：当 `SSL_VERIFY is False` 时 `logger.warning('TLS 校验已关闭：流量可被链路上任何中间人读取/篡改（仅在你的代理做 TLS 拦截时需要）')` —— 让"关校验"从静默默认变成显式可见状态。
3. 新增 `scripts/check_tls.py`（只读诊断，无副作用）：对 `PIXIV_BASE_URL` 与 `i.pximg.net` 分别打印"直连/代理 × verify True/False"的握手结果、叶证书 SHA-256 与 issuer；MITM 判定规则 = `verify=True` 失败（不可信 CA）**或** 经代理与直连的叶证书指纹不一致；退出码 0=可安全开启校验 / 1=疑似拦截（建议保留 `SSL_VERIFY=false`）。网络或代理变更后可重跑，作为 S7a 的**常驻验收工具**。
4. `config.py` 新增 `IMAGE_HOST_ALLOWLIST = frozenset({'i.pximg.net'})`（注释说明镜像站需自行添加）。
5. `_download_illust`：下载前对每个 URL 做校验 —— 硬性拒绝（非 `https` / 私网 / 非 443 / 含 userinfo）→ 直接置 `failed` + 日志"非法图片地址"、**不发起请求**；host 在白名单内 → 走现有 pooled（带凭据）会话；host 为**公网 https 但不在白名单** → 走**无凭据会话**（复用 S7b 第 8 条的共享判定函数）继续下载，避免 Pixiv 换图床域名时下载中断、同时不泄露 PHPSESSID。下载请求统一加 `allow_redirects=False`。
6. `fetcher.build_pixiv_session` 的全局 Cookie 头**不改**（配合本步重定向收紧 + 校验开启已闭环，见"不应该修改"）。
7. `docs/maintenance.md`：记录"如何判断代理是否做 TLS 拦截"（探测脚本用法 + 判定规则）与"何时才允许设 `SSL_VERIFY=false`"。

**测试**：
- `test_ssl_verify_defaults_true`（默认值断言）。
- `test_startup_warns_when_ssl_verify_disabled`（caplog 断言 warning）。
- `test_download_rejects_non_allowlisted_host`（`http://169.254.169.254/...` → 失败且 `session.get` 未被调用）、`test_download_rejects_http_scheme`。
- `test_download_does_not_follow_redirect`（图片 URL 返回 302 → 判定失败，不跟随）。
- `test_check_tls_script_mitm_verdict`（mock 握手结果：指纹不一致 → 退出码 1；一致 → 0）。

**专项验收**：在本机跑 `python scripts/check_tls.py` → 输出 PASS（`verify=True` 成功且 issuer 为公共 CA）；随后把 `SSL_VERIFY` 按新默认（True）实跑一次**冒烟三连**：搜索一次、图库打开一页缩略图、下载一个作品——全部正常（这是对"默认值改动不破坏本机可用性"的直接验收）。

---

### S7b（P0）/thumb 越界重定向：拒绝 + **自动发现机制**

**设计目标**：既不允许任意重定向（防 SSRF / 防 Cookie 外泄），又能在 Pixiv 图床将来切到新 CDN 域名时**自动恢复**，而不是永久 502 等人改配置。

**文件**：`config.py`、`runtime.py`、`routes_gallery.py`、`helpers.py`、`app.py`（如需 seam）

**核心方案（判定顺序不可颠倒）**：
1. **初始 URL 白名单保持不变**：`/thumb/<b64>` 入参仍必须 `https://i.pximg.net/` 前缀 —— 自动发现**只作用于重定向目标**，不扩展入口（不新增 SSRF 面）。
2. 请求 `allow_redirects=False`；3xx 时解析 `Location`（支持相对路径 → 以原 URL 为基准拼接）。
3. **关键事实（决定整套设计）**：`fetcher.build_pixiv_session()` 挂的是**会话级 Cookie 头**——`s.headers.update({'Cookie': f'PHPSESSID={_cookie_value}'})`（fetcher.py:304），requests 会把它发给**任意主机**（只有紧随其后的 `s.cookies.set(..., domain=_pixiv_hostname)` 是主机作用域的）。因此"跟随到白名单外主机"等价于**把 PHPSESSID 交给第三方** —— 重定向目标必须按凭据分级。
4. **凭据分级（A/B 两级，均只跟随一次，再遇 3xx → 502，不递归）**：
   - **A 级（带凭据）**：目标 host ∈ `config.IMAGE_HOST_ALLOWLIST` → 用 `get_pooled_session()`（带 Cookie）跟随一次。
   - **B 级（无凭据，自动发现路径）**：目标 host 是**公网 https**（通过第 5 条硬性校验）但不在白名单 → 用**无 Cookie 会话**跟随一次（`build_pixiv_session()` 后 `headers.pop('Cookie', None)`；建议在 `fetcher` 提供按线程缓存的 `get_pooled_session(with_cookie=False)` 变体），只带 UA/Referer/Accept-Language；**并写入发现表**。
   - B 级额外要求：响应 `Content-Type` 以 `image/` 开头，否则 502（防止经重定向向本站缓存投递任意 HTML/JSON）。
5. **硬性拒绝条件（优先于 A/B 两级，永不放行）**：非 `https`、含 userinfo（`@`）、host 为空、端口非 443/空、`localhost` / `.local` / `.internal` 结尾、host 为 IP 字面量且属 loopback / 私网 / link-local / ULA（`127/8`、`10/8`、`172.16/12`、`192.168/16`、`169.254/16`、`::1`、`fc00::/7`、`fe80::/10`）→ `abort(502)` + 计入 `runtime._thumb_redirect_rejected`，**不记录发现、不放行**。
6. **自动发现 = 观测 + 单调收敛，无冷启动**：B 级首次成功跟随即写入 `runtime._thumb_redirect_hosts[host] = {count, first_seen, last_seen, sample_url, last_content_type}`；每个 host **首次**发现打一条 `logger.warning`，其后仅累加计数（防刷屏）。**不做自动提升白名单**——因为 B 级本身已能无凭据正常取图，白名单只决定"是否携带凭据"，不需要为了可用性放宽信任。人工提升（确认为 Pixiv 官方 CDN 且希望带凭据访问）只需往 `config.IMAGE_HOST_ALLOWLIST` 加一行。
7. **观测与人工控制**：
   - `GET /api/thumb/redirect-hosts` → `{static: [...], discovered: [{host, count, first_seen, last_seen, last_content_type}], rejected: {host: count}}`。
   - `DELETE /api/thumb/redirect-hosts`（需 CSRF）→ 清空发现表。
   - 发现表落盘 `instance/thumb_redirect_hosts.json`（仅观测用途、重启不丢），复用 `helpers._atomic_write_json`（S16 提供；未合并前本步内联同款 tmp + `os.replace`）。
   - `config.THUMB_REDIRECT_DISCOVERY`（默认 `True`）：设 `False` → B 级**不跟随**（仅记录 + 502），回到"只允许白名单内重定向"的纯拒绝行为。
8. **与下载引擎复用同款判定（S7a 第 5 条据此调整）**：把"公网 https 校验 + 无凭据会话"抽成单一函数，下载引擎复用 —— 下载 URL 的 host 若不在白名单，**用无凭据会话 + 公网校验继续下载**（而不是直接失败）；这样 Pixiv 换图床域名时**下载不中断**，同时**不把 PHPSESSID 发给新域名**。
9. **状态与锁**：`runtime._thumb_redirect_hosts` / `_thumb_redirect_rejected` 新增，配 `_thumb_redirect_lock`；计数的"读→改→写"整体在锁内（AGENTS 并发约定：容器遍历与读-判定-写都要加锁）。

**为什么仍然安全**：跨域重定向**从不携带凭据**（B 级会话已剥离 Cookie 头），因此即使 `SSL_VERIFY=false` 的机器上被 MITM 篡改重定向，攻击者拿不到 PHPSESSID；SSRF 的真正危害（内网 / 云元数据端点）被第 5 条硬性条件封死，且该条件**优先于所有白名单**；第 4 条的 `image/*` 约束阻止第三方内容以本站缓存形式落地。

**与"自动提升"方案的区别（为什么选这个）**：阈值提升方案会在新 CDN 出现时产生"前 N 次 502"的冷启动（用户可见短暂空图），而 502 与"新域名"在日志里难以区分；无凭据跟随方案**零冷启动**，且把"信任"严格限定为"不带任何秘密地取一张图"，安全边界更清晰。

**测试**（`tests/test_thumb.py`）：
- `test_thumb_follows_redirect_within_static_whitelist_with_cookie`（A 级：第二次请求**带** Cookie）。
- `test_thumb_follows_cross_host_redirect_without_cookie`（B 级：公网 https 目标 → 跟随成功，断言第二次请求**不含 Cookie / PHPSESSID**，且发现表已记录该 host）。
- `test_thumb_rejects_cross_host_non_image_content_type`（B 级目标返回 `text/html` → 502）。
- `test_thumb_rejects_redirect_private_or_insecure_targets`：`https://169.254.169.254/...`、`http://127.0.0.1/x`、`http://[::1]/x`、`https://user@host/x`、`https://host:8080/x`、`http://pub.example/x` → 全部 502，`rejected` 计数增加、发现表**不**新增。
- `test_thumb_redirect_initial_url_whitelist_unchanged`：`/thumb/<b64 of https://evil.example/x>` → 403（发现机制不影响入口判定）。
- `test_thumb_redirect_no_recursive_follow`（跟随一次后再 3xx → 502）。
- `test_thumb_redirect_discovery_disabled_never_follows_cross_host`（`THUMB_REDIRECT_DISCOVERY=False`）。
- `test_thumb_redirect_hosts_discovered_persisted_and_reset`（落盘 → 重载仍可见；`GET` 列表 + `DELETE` 清空 + DELETE 的 CSRF 403 用例）。
- `test_download_uses_cookieless_session_for_non_allowlisted_host`（下载侧复用：断言无 Cookie 头且下载成功）。
- `test_download_rejects_non_public_host`（私网目标 → 失败且**未发起**请求）。

---

### S8（P1 前置）测试基建隔离

**文件**：`config.py`、`routes_gallery.py`、`tests/conftest.py`

**核心方案**：
1. `config.py`：`_instance_dir` 改为 `os.environ.get('PIXIV_INSTANCE_DIR') or os.path.join(BASE_DIR,'instance')`；`_settings_path`、密钥路径全部从它派生。
2. `routes_gallery.CACHE_DIR` 改为从 `config` 的实例目录派生（路径值不变，仍是 `instance/image_cache`）。
3. `tests/conftest.py`：在 `import config` **之前**把 `PIXIV_INSTANCE_DIR` 指到临时目录，并断言"测试进程不再写真实 `instance/`"。

**测试**：
- `tests/test_test_setup.py` 增加 `test_instance_dir_isolated_in_tests`（断言 `config._instance_dir` 为临时目录、真实 instance 未被写入）。
- 既有隔离用例保持全绿。

**已实现 + 验证结果（2026-09-11，提交 `4e49cf2`）**：改了 `config.py`（`_instance_dir` 由 `PIXIV_INSTANCE_DIR` 派生、`.env` 加载前移到派生之前、`DATABASE_PATH`/`_settings_path` 改为从它派生）、`app.py` / `routes_gallery.py` / `routes_settings.py`（原先各自拼一份 `BASE_DIR/instance`，全部改为派生）、`tests/conftest.py`（在 `import config` **之前**强制设置该变量，**刻意不用 `setdefault`** —— 外部环境变量不得把测试指向真实实例目录）、`AGENTS.md`。新增 5 例：全部派生路径的隔离断言、默认分支（把 `config.py` 复制到临时目录后 exec，`BASE_DIR` 随 `__file__` 走）、覆盖分支（四条路径全部跟着走且不再创建默认目录）、覆盖值指向文件时必须 import 即失败、conftest 顺序守卫；全量 392 passed。**证伪为行为级（强）**：回退 4 个生产文件后 3 例失败（覆盖不生效、指向文件时静默回落、隔离断言失败）；另做 A/B 实验 —— 临时移走真实的 `.secret_key`/`.cursor_secret` 后跑全量，旧代码在**真实** `instance/` 里重新生成了这两个文件，修复后同一实验对真实 `instance/` **零写入**。**边界**：覆盖值不可用时**故意不回落**默认目录（不然测试/部署会静默读写真实实例数据）；本步未动 `config.py` import 期执行副作用的整体设计。

---

### S9（P1）重复下载保护

**文件**：`background.py`、`routes_download.py`

**核心方案**：
1. `trigger_download`：在 done/downloading 检查之后补 `pid in queued_snapshot` → 返回 `{'status':'queued'}`（不再重复 submit）。
2. `batch_download`：queued 的 pid 计入 `skipped`。
3. `_download_illust` 起始：若 `illust.download_status == 'done'` → 记日志后 return（防未来调用方从 done 触发重下、进而在失败时删掉上次成功文件）。**删除图库文件后的重下（status=None）仍照常可用**。

**测试**：
- `test_trigger_download_queued_returns_queued_not_duplicate`、`test_batch_download_skips_queued`。
- `test_download_illust_noop_when_done`。
- `test_redownload_after_delete_still_works`（回归）。

**已实现 + 验证结果（2026-09-11，提交 `2e14c66`）**：改了 `routes_download.py`（`trigger_download` 在 done/downloading 判定之后补排队判定、判定与入队放进同一把 `_download_queue_lock`、`batch_download` 的 queued pid 计入 skipped、两个入队点在 submit 失败时把 pid 撤出队列）、`background.py`（`_download_illust` 取到行后若已是 `done` 就记日志返回）。新增 7 例：排队中重复触发 / 批量跳过排队 / 并发触发恰好提交一次（6 线程 × 8 轮 barrier 同发）/ done 守卫不发请求也不删文件不写 start / 删除后重下回归 / 单条与批量提交失败都不残留队列。**证伪为行为级**：回退 `routes_download.py` + `background.py` 后 6/7 失败（"删除后重下"两侧都通过，它是回归守卫）；恢复后 `tests/test_download.py` 36 passed、全量 399 passed。**边界**：`download_locks` 的 `is` 比较语义、`_release_download_lock` 与 queued 不持久化（重启即丢）均未改；入队失败时的补偿只是把 pid 撤出队列，不重试。

---

### S10（P1）SearchCache 并发一致性

**文件**：`background.py`

**核心方案**：新增模块级 `_search_cache_guard = threading.Lock()`，把 `_prefetch_one_tag` 的 merge 段（读→改→写 `illust_ids`）与 `_remove_pids_from_search_caches` 整体纳入锁内（调用方不变）。**不改** merge"只增不减"语义、不改事务边界、不做 CAS 改造。

**测试**：
- `test_search_cache_merge_and_remove_serialized`：多线程反复 merge + 删除同一 pid，断言最终 `illust_ids` 不含已删 pid（无幽灵引用）。
- `test_search_cache_lock_scope_excludes_network`（白盒：断言 merge 锁段内不发起网络调用）。

**已实现 + 验证结果（2026-09-11，提交 `cb64ff7`）**：改了 `background.py`（新增模块级 `_search_cache_guard`；`_prefetch_one_tag` 的合并段"读 → 合并 → 那次提交"与 `_remove_pids_from_search_caches` 的改列段整体入锁）。锁**不**罩搜索/详情等网络阶段（否则预取的网络耗时会把删除请求一起拖住），空 pid 列表提前返回不拿锁，事务边界不动（删除侧仍不 commit，由调用方提交）。新增 4 例：幽灵引用（用事件把合并卡在"已读到旧值、尚未提交"那一刻，另一线程在该窗口里删除并提交）、锁范围不含网络阶段（用记录 owner 线程的探针锁断言，`Lock.locked()` 是所有线程共享的视图、分不清谁在锁里）、删除侧必须持同一把锁、空列表不拿锁。**证伪为行为级（强）**：仅回退 `background.py` 后前 3 例失败，幽灵引用用例实测值是 `illust_ids=[9202, 9201]` —— 已删的 9201 被写回；恢复后 `tests/test_prefetch.py` 59 passed、全量 403 passed。**残留（如实记录）**：删除侧的 commit 仍在锁外，最终值取决于两个全列写（`UPDATE ... SET illust_ids=?`）的提交次序；极端情况下合并这次提交可能等锁超时而**丢失一次合并**（有日志与下一轮重试，不再留下幽灵引用）。彻底线性化要把两侧 commit 都收进锁内或改 CAS，会动事务边界，不在本次范围。

---

### S11（P1）thumb 信号量饥饿

**文件**：`runtime.py`、`routes_gallery.py`

**核心方案**：`runtime.py` 新增 `THUMB_SEM_TIMEOUT = 15.0`；`thumb_proxy` 用 `if not _thumb_sem.acquire(timeout=THUMB_SEM_TIMEOUT): abort(503)` + `try/finally` 释放，替代 `with _thumb_sem`。其余（冷却、缓存、重试、mtime 语义）一律不变。**不改** `THUMB_CONCURRENCY` 默认值。

**测试**：
- `test_thumb_returns_503_when_semaphore_exhausted`（占满名额 + monkeypatch 超时 → 503 且无网络请求）。
- `test_thumb_releases_semaphore_after_failure`。

**已实现 + 验证结果（2026-09-11，提交 `9c71903`）**：改了 `runtime.py`（新增 `THUMB_SEM_TIMEOUT = 15.0`，**调用时读取**，便于测试与将来调整）、`routes_gallery.py`（改为 `if not _thumb_sem.acquire(timeout=runtime.THUMB_SEM_TIMEOUT)` → 记 warning 并返回 503，取图段用 `try/finally` 归还名额）。15s 远大于正常单张耗时（通常 < 2s），只在图床确实卡住时触发。新增 3 例：槽位耗尽返回 503 且不发起取图请求（等待上限取自 `runtime` 常量，并用永不发名额的替身锁死"必须带 timeout"这条契约）、成功与失败两条路径都归还名额（真 `Semaphore(1)` 验证）、6 个并发请求 + 槽位 2 时并发峰值不超上限且名额全部归还。**证伪为行为级（强）**：仅回退 `routes_gallery.py` + `runtime.py` 后，槽位耗尽用例失败在「不得用 `with` 获取信号量（没有等待上限）」—— 旧实现正是无上限阻塞获取；另两例是回归防线，两侧都通过。恢复后 `tests/test_thumb.py` 23 passed、全量 406 passed。**边界**：`THUMB_CONCURRENCY` 默认值、失败 URL 冷却、磁盘缓存键与 7 天 mtime 语义、原子写、重定向凭据分级全部未动；前端对非 200 本来就占位/重试，503 与既有 502 同属"这张图暂时没有"。

---

### S12（P1）后台线程 heartbeat

**文件**：`background.py`、`routes_prefetch.py`

**核心方案**：
1. `background.py` 保存 `_prefetch_thread` 引用，新增 `get_background_health()` → `{prefetch_alive, auto_follow_alive, last_check, stale, last_error}`。
2. `_prefetch_loop` 的 except 分支记录 `_prefetch_state['last_error']`。
3. `/api/prefetch/status` 增加 `alive`/`stale`/`last_error` 字段（`stale = last_check 为空或早于 2*interval+600`；`interval<=0` 时 `stale=False`）。不新增页面 UI 改动（前端不消费也不报错）。

**测试**：
- `test_prefetch_status_reports_alive_and_stale`、`test_prefetch_status_not_stale_when_disabled`。
- `test_prefetch_loop_records_last_error`。

**已实现 + 验证结果（2026-09-11，提交 `df7cd94`）**：改了 `background.py`（`_start_prefetch_thread` 保留 `_prefetch_thread` 引用、新增 `get_background_health()`、`_prefetch_loop` 四个 except 分支写 `last_error` 并用本地 `round_error` 标志在整轮干净收尾时清空 —— 于是"`last_error` 非空"= 最近一轮就有问题，而不是历史上某轮出过问题）、`routes_prefetch.py` 与 `runtime.py`（status 新增 `alive`/`auto_follow_alive`/`stale`/`last_error`；`stale` 阈值 `2*interval+600`，`interval<=0` 时恒为 False）。新增 4 例：单轮异常留痕且下一轮干净收尾清空、标签列表读不出来也留痕且不打死线程、status 的四种时序（超阈值 stale / 阈值内不 stale / 线程死掉但数据新（只有 alive 能暴露）/ `interval>0` 但从未跑完一轮）、`interval=0` 时不判 stale；既有 status 字段集断言同步纳入新字段（合约变更，其余断言原样保留并补了新字段断言）。证伪：回退 `background.py` + `runtime.py` + `routes_prefetch.py` 后 5 例失败（新字段缺失、`last_error` 未定义），恢复后 96 passed、全量 410 passed。**如实说明**：本项是**新增可观测面**，证伪表现为"字段缺失/行为未定义"，不是修掉某个既有错误值；本步**没有**加 supervisor 或自动重启，`_start_prefetch_thread` 内 `_run` **仍无兜底 try** —— `_prefetch_loop` 之外的异常仍能打死线程，区别只在于 `prefetch_alive` 现在看得出来（审计报告 §16 建议里的"给 `_run` 加 try 兜底"未做）。

---

### S13（P1）Pixiv 403 分类

**文件**：`fetcher.py`

**核心方案**：把 `if status in (401, 403): raise PixivAuthError` 的 4 处（`search_by_tag`、`browse_discovery`、`_get_user_profile_ids`、`fetch_following`）改为 **401 → `PixivAuthError`**；**403 → `logger.warning('疑似限流/风控')` 并按既有失败形态返回空结果**（列表返回 `([], False)`、profile 返回 `[]`）。`_get_illust_detail` 的 403 退避语义**不变**；不新增重试次数（避免与既有重试策略叠加）。

**测试**：
- `test_search_tag_403_returns_empty_not_auth_error`、`test_search_tag_401_raises_auth_error`。
- `test_fetch_following_403_returns_empty`、`test_user_profile_403_returns_empty`。

**已实现 + 验证结果（2026-09-11，提交 `dde7221`）**：改了 `fetcher.py`（新增 `_warn_403(api)`；`search_by_tag` / `browse_discovery` / `_get_user_profile_ids` / `fetch_following` 四处改为 **401 → `PixivAuthError`**、**403 → warning「疑似限流/风控（非认证失效）」后按既有失败形态返回空结果**）。原有的 `logger.error(f'XXX API failed: ...')` 与返回形态一字未动，告警是额外一条。新增 9 例：四处 403 返回空结果且只发一次请求（不重试）、同样四处 401 仍抛 `PixivAuthError`、详情路径 403 仍退避重试且绝不上报认证失效。**证伪为行为级（强）**：仅回退 `fetcher.py` 后**恰好**那 4 个 403 用例失败（旧代码抛 `PixivAuthError`），401 与详情回归用例两侧都通过；恢复后 `tests/test_fetcher.py` 77 passed、全量 419 passed。**边界**：`_get_illust_detail` 的 403/429 退避语义与 `RETRYABLE_GLOBAL_DETAIL` 哨兵、`DETAIL_MAX_RETRIES`、三级令牌桶常量、`_is_auth_error(msg)` 的 JSON 报错文本判定均未动；**列表类请求仍然零限流**（审计报告 §15-4 不在本次范围）。

---

### S14（P1）`_fill_last_attempt` 内存增长

**文件**：`fetcher.py`

**核心方案**：在 `_background_fill_details` 的既有锁段内，当 `len(_fill_last_attempt) > 1000` 时清理 `now - ts >= _FILL_ATTEMPT_INTERVAL * 2` 的条目（保留窗口足以维持节流语义）。不改节流常量、不改 `_filling_ids` 语义。

**测试**：
- `test_fill_attempt_map_pruned_when_large`、`test_fill_attempt_recent_kept`。

**已实现 + 验证结果（2026-09-11，提交 `ad7ad4e`）**：改了 `fetcher.py`（新增 `_FILL_ATTEMPT_MAX_ENTRIES = 1000`；在 `_background_fill_details` 既有的 `_fill_lock` 段内，**仅当** `len(_fill_last_attempt) > 1000` 时清理 `now - ts >= _FILL_ATTEMPT_INTERVAL * 2` 的条目）。只清"远超节流窗口"的条目是关键：这类条目留着的话下一轮判定 `now - ts >= _FILL_ATTEMPT_INTERVAL` 也必然通过，删掉不改变任何节流行为。新增 4 例：表超上限时过期条目被清而窗口内条目保留、未超上限时一条都不清（清理不每轮扫全表）、清理不放宽节流（窗口内作品仍被跳过且不刷新时间戳）、8 线程并发补全时判定与清理互斥且不抛 `dictionary changed size`。**证伪为行为级**：仅回退 `fetcher.py` 后 2 例失败（旧代码不清理），另 2 例"不得放宽节流"两侧都通过（它们是用来看住修复别做过头）；恢复后 `tests/test_fetcher.py` 81 passed、全量 423 passed。**边界**：清理**只在超上限时触发**，窗口内（300s）条目一律保留 —— 这张表仍可能短暂超过 1000，是"有界"而非"硬顶"；实测 10 万条目约 10.0 MB（其中 dict 本体 5.0 MB，`sys.getsizeof` 实测）；节流常量、`_filling_ids` 语义、`_fetch_details_parallel` 与 DB 写入流程未动。

---

### S15（P1）ZIP 内存问题

**文件**：`routes_download.py`、`config.py`

**核心方案**：
1. `config.py` 新增 `ZIP_MEMORY_THRESHOLD_BYTES = 200 * 1024 * 1024`。
2. `download_file`：计算总大小；≤ 阈值沿用现有 `BytesIO` 路径（行为不变）；> 阈值改用 `tempfile.NamedTemporaryFile(delete=False)` 写 zip，`send_file(..., as_attachment=True)` + `after_this_request` 删除临时文件（失败仅日志）。
3. zip 循环内 `except OSError: continue`（TOCTOU 兜底）；若无任何条目 → 404 "文件已丢失"。

**测试**：
- `test_download_file_zip_memory_below_threshold`、`test_download_file_zip_tempfile_above_threshold`（monkeypatch 阈值变小）。
- `test_download_file_skips_disappeared_file`、`test_download_file_tempfile_removed_after_request`。

**实现偏差（已实现 + 已验证，2026-09-10，提交 `70dded0`）**：清理临时文件**不能**用本方案写的 `after_this_request`，也不能用 `Response.call_on_close`。实测结论：`send_file()` 产出的响应是 `direct_passthrough`，Werkzeug 的 `get_app_iter()` 在该模式下**直接返回 body（文件包装器）本身**，服务器全程不会调用 `Response.close()` —— 所以
1. `after_this_request` 执行太早（响应体还没发、文件句柄还开着），Windows 上 `os.remove` 必然失败（WinError 32）；
2. `Response.call_on_close` 的回调**永远不会被执行**（没人调用那个 Response 的 close），每个大包漏一份几百 MB 临时文件。
实际落地：把清理挂在 body 上（`routes_download._DeletingBody` 转发 `send_file` 的 body），覆盖「读完 EOF」「`close()`（客户端断开 / HEAD / 416 空 body）」两条路径，另加两个兜底：`send_file` 抛异常（Range 不可满足 → 416）当场删、`200/206` 之外的响应（304 类）先关句柄再当场删。**206 必须排除在「无内容」之外** —— 它会发送内容且此刻句柄仍开着，误判会导致删除失败且下载拿不到数据（实现过程中踩到过，已由 `test_download_file_partial_range_still_works_and_cleans` 看住）。测试从计划里的 4 个扩到 11 个（含 HEAD / 416 / 206 / 304 四种响应形态）。

**已实现 + 验证结果（2026-09-11，提交 `70dded0`）**：改了 `config.py`（新增 `ZIP_MEMORY_THRESHOLD_BYTES = 200 * 1024 * 1024`）、`routes_download.py`（+170：`_write_zip_entries` / `_total_bytes` / `_remove_temp_zip` / `_close_body_chain` / `_DeletingBody` 与两条发送路径）、`tests/test_app.py`（+221）。新增用例清单：阈值内不落临时文件且包内容正确、超阈值落临时文件且包内容一致、响应关闭后临时文件消失、读完整包后消失、HEAD 不漏文件、Range 不可满足(416)不漏文件、Range 正常(206)仍可用且不漏文件、非内容响应(304)不漏文件、打包途中文件消失则跳过其余照常、全部消失则 404 且不残留、单文件仍直接返回原图。**证伪分两级如实记录**：① 只回退 `routes_download.py` + `config.py` 时 11 例中 10 例失败，但失败原因是**新 seam 不存在**（`AttributeError: ... has no attribute 'tempfile'`）—— **seam 缺失型、证据偏弱**；② 因此补做**机制级证伪**（保留 seam，把阈值判断改成恒真以强制走内存路径），8 例以 `assert 0 == 1` 失败，证明它们确实在盯新分支而不是只碰到 seam。恢复后 `tests/test_app.py` 71 passed、全量 434 passed。**遗留边界**：清理依赖 WSGI 关闭 body 的契约（另有 EOF 自清理兜底）；大包改吃临时目录空间（`/tmp` 若是 tmpfs 仍算内存，但只占一份而不是"整包 + 读缓冲"）；磁盘满时 `OSError` 会清理并 500；阈值常量改动需重启；**计划批准的行为变化** —— 所有条目都消失时由"空 zip 200"改为 404「文件已丢失」。

---

### S16（P1）settings.json 原子写

**文件**：`helpers.py`、`routes_settings.py`、`routes_prefetch.py`、`config.py`

**核心方案**：
1. `helpers.py` 新增 `_atomic_write_json(path, data)`：同目录写 `path.tmp` → `os.replace(tmp, path)` → `finally` 清理残留 tmp。
2. 两个写入点（`api_settings_post`、`prefetch_config_post`）改用它；**保持**"先写盘成功再更新内存"的既有顺序与 500 错误语义。
3. `config.py` 读取失败分支：把损坏文件复制为 `settings.json.corrupt.bak`（不存在时）后回退默认——回退行为不变，仅增加可恢复证据。

**测试**：
- `test_settings_post_atomic_no_tmp_left`、`test_settings_post_write_failure_keeps_old_bytes`（mock `os.replace` 抛 `OSError` → 500 且原文件字节不变）。
- `test_prefetch_config_atomic_write`。
- `test_corrupt_settings_backed_up_on_load`。

**已实现 + 验证结果（2026-09-11，提交 `4c7c783`）**：改了 `helpers.py`（新增 `_atomic_write_json`：同目录写 `<path>.tmp` → `flush` + `fsync` → `os.replace`，`finally` 里无论成败清掉残留 tmp，异常原样抛出由调用方决定错误码；tmp 特意放**同目录**，跨设备时 `os.replace` 会退化成复制+删除、就不原子了）、`routes_settings.py` 与 `routes_prefetch.py`（两个写入点改用它，调用顺序与错误语义一字未改 —— 仍是"先写盘成功，再更新内存"，失败仍 500 且不更新 `_prefetch_state`；顺带删掉因改动变成死引用的 `import os`）、`config.py`（新增 `_backup_corrupt_settings`，读取失败时把损坏文件复制为 `settings.json.corrupt.bak`，**仅在副本不存在时**；备份失败只记日志）。新增 11 例，分布在 `tests/test_helpers.py`（4：替换内容不留 `.tmp`、目录缺失自动创建、`os.replace` 失败时旧字节完整保留、序列化失败时旧文件不动）、`tests/test_app.py`（2，**此前 `POST /api/settings` 零覆盖**）、`tests/test_prefetch_api.py`（2，守住"先写盘成功再更新内存"这条既有约定）、`tests/test_test_setup.py`（3，复用既有的 `_load_config_probe` 独立 exec `config.py` 的 seam）。**证伪**：回退 4 个生产文件后 11 例中 7 例失败，其中 **4 例是 seam 缺失型**（`helpers._atomic_write_json` 不存在）**证据偏弱、如实标注**；另 3 例为**行为型**且抓到具体原因 —— 两个路由用例 `assert 200 == 500`（旧代码不经过 `os.replace`，注入的失败根本不生效）、配置用例「损坏文件必须留一份副本」。恢复后全量 445 passed。**遗留边界**：每次保存多一次 `fsync`（用户手动低频操作，代价可忽略）；`SIGKILL` 落在写 tmp 与 replace 之间会残留 `settings.json.tmp`（无害半成品，读取侧只认 `settings.json`，下次写入覆盖）；`settings.json.corrupt.bak` 不自动清理，与 `settings.json` 同目录同权限（内含可能的密码类键，不额外扩大暴露面）；**损坏时仍回退默认值** —— "设置页下次保存会用默认值覆盖"的语义未改，只是多了 `.bak` 可恢复。

---

### S17（P1）secret 文件权限

**文件**：`config.py`、`app.py`

**核心方案**：
1. `config.py` 抽取 `_load_or_create_secret(path, min_len=32)`：文件不存在**或长度不足**时重新生成，写入后 `os.chmod(path, 0o600)`（`OSError` 忽略，兼容 Windows）——同时修掉"截断 `.cursor_secret` 被直接使用"。
2. `app.py` 的 `.secret_key` 读写改走同一助手，消除"仅空文件才重生成"的不一致。

**测试**：
- `test_short_secret_regenerated`、`test_empty_secret_regenerated`（回归）。
- `test_secret_file_mode_0600`（`skipif win32`）。

**已实现 + 验证结果（2026-09-11，提交 `578c0c1`）**：改了 `config.py`（新增 `_load_or_create_secret(path, min_len=32)` 与 `_restrict_secret_file(path)`：文件缺失**或长度不足**时用 `secrets.token_hex(32)` 重新生成并写回，长度足够则原样使用；无论走哪条分支最后都 `os.chmod(path, 0o600)`，失败静默）、`app.py`（`.secret_key` 改走同一助手，删掉重复的写盘分支与因此变成死引用的 `import secrets`）、`tests/test_auth.py`（`TestSecretFiles`，9 例）。测试覆盖：截断文件与空文件都重生成且写回、首次生成不产生"内容过短"告警噪声而真出现截断时必须有告警、长度足够的密钥原样保留（稳定性守卫）、写入后权限 0600 且已有文件被放宽成 0644 后下次启动收紧、三条分支都确实请求了 `0o600`（用 `os.chmod` 替身盯权限位，避免"只在 Linux 上才验证"）、`chmod` 抛 `OSError` 不影响启动、`app.py` 与 `config.py` 共用同一助手（AST 确认模块级确实调用了它 —— 该文件没有可重跑的 seam，重跑等于建第二个 Flask 应用并起后台线程；并核对运行态 `app.config['SECRET_KEY']` 与文件内容一致、两条密钥长度都 ≥ 32）。**证伪分两级如实记录**：① 回退 `config.py` + `app.py` 后 8 例失败，但其中 7 例是 **seam 缺失型**（`AttributeError: module 'config' has no attribute '_load_or_create_secret'`）—— **证据偏弱**；② 因此补做**机制级证伪**（保留 seam，把 `min_len` 默认值改成 0、`_restrict_secret_file` 改成 no-op），拿到行为级原因：`assert 'abc' != 'abc'`（3 字符密钥被原样采用）、`assert 0 == 3`（一次 chmod 都没发生）、`assert '内容过短' in ''`，并暴露 `min_len=0` 时缺失文件会返回空密钥且不落盘（说明该参数同时守着"文件必须被创建"）。恢复后 `tests/test_auth.py` 40 passed / 1 skipped、全量 453 passed / 1 skipped。**遗留边界**：长度不足时重新生成会使**该部署既有会话与游标失效**（有意取舍：弱密钥比登出危险得多）；`0600` 只在 POSIX 有意义，Windows 上断言按 `skipif` 跳过，本机**只验证了"确实请求了 0o600"**，POSIX 端到端权限未在本机证实（部署后可用 `ls -l instance/.secret_key` 复核）；`chmod` 失败静默。

---

### S18（P1）下载 / 图片 / settings 测试补齐（收口）

**文件**：`tests/test_download.py`、`tests/test_thumb.py`、`tests/test_settings_api.py`、`tests/conftest.py`

**核心方案**：
1. `test_download.py`：`_download_illust` happy path（写文件 + `done` + `file_size` + 日志）、无原图分支、失败清理分支、中途取消分支；6 个下载路由的 happy + 异常 + **CSRF 403**；`/api/downloads` 聚合。
2. `test_thumb.py`：白名单 403、失败冷却 502、缓存命中不发网络且 mtime 不变 + `max_age`、原子写降级、信号量 503；`/api/image` 的 DB 路径 / 目录兜底 / 404 三分支。
3. `test_settings_api.py`：GET 脱敏、POST happy、cookie 控制字符剔除、cookie 写失败 500、settings 写失败 500。
4. 参数化 **CSRF 矩阵**：`test_all_mutating_endpoints_require_csrf` 覆盖全部修改型端点（当前 27 个），防止未来漏挂装饰器。

**测试（本步即测试）**：验收要求见 §六；目标：总用例数 ≥ 420、离线全绿、单轮 < 30s。

**状态：已完成（2026-09-11）。** 补齐了三个零覆盖区并做了 CSRF 参数化矩阵；过程中**发现并修复了两个此前无人知道的缺陷**（见下）。

**实现偏差（与上面"核心方案"的差异，如实记录）**：
1. **矩阵规模是 29 个端点而不是 27**：S11/S16 之后修改型路由共 29 个（collections 8、download 4、gallery 5、prefetch 5、search 1、settings 6）。矩阵不只有手写清单，还加了 `test_mutating_endpoint_matrix_is_complete` 做**静态对账**（从 `routes_*.py` 抓 `@bp.route(..., methods=[...])` 与矩阵比对，新增路由会被这条先拦住）——只写清单的话，将来"清单忘了加"和"装饰器忘了挂"是同一类静默风险。
2. **settings 用例落在新文件 `tests/test_settings_api.py`**，并把 S16 那两个 settings 写盘用例从 `test_app.py` **迁入**该文件（避免同一契约两处维护）；`test_app.py` 只留 CSRF 契约。
3. **`tests/conftest.py` 未改动**：实际不需要新夹具（`clean_db` / `client` 已够用，`cookies.txt` 落点用改写 `routes_settings.__file__` 的方式隔离）。
4. **两个新缺陷的修复超出"只加测试"的边界，但必须做**（否则只能删掉失败的断言 —— 那正是本阶段明令禁止的）：见下"顺带修复"。

**顺带修复（S18 补测发现，失败先行）**：
- **设置页保存预取配置从未生效**：`routes_settings.api_settings_post` 把 settings.json 的**长键**（`prefetch_interval`/`prefetch_pages`/`prefetch_max_illusts`）直接写进 `_prefetch_state`，而 `background` 的预取循环读的是**短键**（`interval`/`pages`/`max_illusts`）—— 等于写进三个没人读的键，`AGENTS.md` 里"经设置页保存后立即生效"的说明自 Blueprint 拆分重构（`73a8f0e`）起就不成立。修法：改用 `routes_prefetch._PREFETCH_SETTINGS_KEYS` 映射（单一来源，不再复制一份键表）。**证伪**：改回旧逻辑后 `test_prefetch_keys_apply_immediately` 失败于 `assert 0 == 321`。
- **排队中取消会永久毁掉该作品的下载**：`background._download_illust` 的 `session_obj = None` 位于"取消标记检查"**之后**，而该检查处有一条提前 `return`；于是 `finally` 首行就抛 `UnboundLocalError`，其后所有清理（`_download_progress.pop`、`lock.release()`、`_release_download_lock`、`download_cancellations.discard`、`_queued_downloads.discard`）**全部跳过** → 该作品的下载锁永远不放、取消标记永远留着，之后每次触发都在 `lock.acquire(blocking=False)` 处**静默**跳过（用户看到"已加入下载队列"但永远不动），进度条目也永久挂在下载管理页。修法：把两个 session 的初始化提到取消检查之前。**证伪**：把初始化放回原位后 `test_download_cancelled_before_start_does_nothing` 失败于 `UnboundLocalError: cannot access local variable 'session_obj'`；修复后该用例还额外断言"取消过的作品必须能重新下载"（症状级守卫）。

**已实现 + 验证结果（2026-09-11，提交见 §8.1 表）**：新增 **55 例**：
- `tests/test_settings_api.py`（新文件，13 例）：GET 脱敏（密码类与 `cookie` 一律回空，且响应体里搜不到明文）、缺文件回默认值、损坏文件不 500、锁定态 GET/POST 双 403 且不写盘、POST 只合并已知键、原子写不留 `.tmp`、替换失败 500 且旧字节不变 + 内存态不漂移、`prefetch_*` 保存即生效（回归守卫）、Cookie 单行写入、控制字符剔除（`\r\n\t\0` 不能造成第二行注入）、纯控制字符 400、空 Cookie 不动文件、cookies.txt 写失败 500 **且 settings.json 一个字节都不写**。`cookies.txt` 落点用改写 `routes_settings.__file__` 隔离，并在收尾**兜底断言仓库根目录的真实 cookies.txt 逐字节没变**（路由是用 `__file__` 推项目根，不是 `config.COOKIE_PATH`，没有可直接 patch 的路径变量）。
- `tests/test_app.py`（+30 例）：29 个修改型端点的 CSRF 矩阵（缺头一律 403 且错误文案一致）+ 1 例静态对账。**证伪**：临时摘掉 `/api/prefetch/refresh-reset` 的 `@_csrf_required` 后，恰好对应用例失败于「POST /api/prefetch/refresh-reset 未受 CSRF 保护」。
- `tests/test_download.py`（+6 例）：中途取消（第 1 页下完后取消 → 状态复位、半成品目录清掉、记 `cancelled`、不固化 `done`、无残留状态）、排队中取消（不起请求、不写日志、且删除锁/标记/进度条目，另加"之后仍能正常下载"的症状级守卫）、`/download/cancel` 的 happy 与 400/404、`/download_status/<pid>` 与 `/api/download/status/batch`（含 400 与"库里没有的 pid 给 none"）、`/downloads` 页面、`/api/downloads` 四段聚合（active 带 `_download_progress` 进度、queued、completed、logs）。
- `tests/test_thumb.py`（+8 例）：缓存命中不发网络且 mtime 不变 + `Content-Type` 从 `.meta` 回放 + `max-age=604800`、`.meta` 缺失按 jpeg 兜底、失败冷却期内不再发请求且过期后恢复、成功后清冷却、原子写失败降级为直接转发响应且不留缓存与 `.tmp`、`/api/image` 的 DB 路径 / 目录兜底（含页号排序 `_p10` 在 `_p2` 之后）/ 404 三分支（目录不存在、index 越界、DB 有记录但文件已删）。

**记错与纠正（如实记录）**：写冷却用例时我最初断言"首次失败总共 1 次出站请求"，实际 `_thumb_request` 对非超时连接失败会重建连接池**重试一次**（既有设计），首次失败本身就是 2 次调用 —— 是**我的断言写错**而不是代码错，已改为断言"冷却期内请求数不再增长"。

**测试结果**：全量 **513 例（508 passed / 1 skipped / 4 failed，17.10s）**；4 例失败为预先存在的 Windows 沙箱子进程检查，1 例 skip 为 S17 的 POSIX 权限断言。§六 的"≥ 420 例、离线全绿、单轮 < 30s"中，用例数与耗时达标；"全绿"受那 4 个**既存、与本阶段无关**的沙箱用例影响，未变绿 —— **不宣称全绿**。

**遗留边界**：CSRF 矩阵只证明"缺头时 403"，**不证明各端点的业务授权逻辑**（如收藏夹归属校验）；下载引擎的多 worker 抢同一 pid、前端 JS、`/api/image` 的 Range/ETag、令牌桶时序、真实旧库上的迁移升级仍未覆盖；`test_settings_api.py` 里 `cookies.txt` 的隔离依赖改写模块 `__file__`（路由若改用 `config.COOKIE_PATH` 更干净，但那会改变 Windows/Linux 的写入目标，属行为变更，未做）。

---

## 五、明确"不应该修改"（本阶段一律不动）

1. **架构与进程模型**：`-w 1` 语义、SQLite WAL、进程内状态设计、Blueprint 划分、`start_background_threads()` 幂等守卫、`atexit` 注册顺序、`gunicorn` 命令与 systemd unit。
2. **app 命名空间测试契约**：`app.py` 的 from-import 导出清单、函数体内延迟 `import app` 模式（去耦合是 P2 议题，本阶段零改动）。
3. **fetcher 的重试与限流**：连接类 fail-fast、urllib3 `Retry(total=1, connect=0)`、详情 403/429 退避 3s/9s、404 不重试；令牌桶速率常量（45/60/20）与 `_TokenBucket.wait()` 实现。
4. **入库去重**：`_insert_new_illusts` 的 `ON CONFLICT DO NOTHING` + 按 pid 回查赢家行。
5. **迁移历史**：v1–v4 函数体；本次不新增 schema 版本、不改 `user_version` 语义；`init_db` 的"create_all → migrations → repair"顺序不变。
6. **缓存的无锁策略**：`_scan_cache` / `_db_pids_cache` 的"先 data 后 ts"与允许并发重复重建；`_thumb_failed` 的冷却时长与清理方式。
7. **下载锁与取消标记归属**：`download_locks` / `_release_download_lock` 的 `is` 比较、`download_cancellations` 由 worker `finally` 清理的语义（S1 只在提交点增加复查与补偿）。
8. **`build_pixiv_session` 的全局 Cookie 头**：不改（它的"会话级 Cookie 发给任意主机"语义被 S7b 用**跨域无凭据会话**回避：白名单外目标永不带 Cookie；改动工厂本身会牵连搜索/详情/下载/缩略图全部路径的行为）。
9. **预取淘汰语义**：三层淘汰的 tier 排序与判定、`PREFETCH_*` 常量默认值、"入库永不停"的设计取舍。
10. **`/thumb` 既有语义**：命中缓存不刷 mtime、`LOCAL_IMAGE_MAX_AGE`、失败冷却值、原子写方式；**入口 URL 白名单（`https://i.pximg.net/` 前缀）与缓存键（URL 的 md5 + `.meta`）也不得被 S7b 的自动发现机制改变**（自动发现只作用于重定向目标）。
11. **前端**：`templates/` 与 `static/` 本次零改动（S12 只加 API 字段）。
12. **已正确项**：`_safe_next`、`safe_title` 消毒、CSP/安全头、CSRF 装饰器实现、`_get_json_body`、`pixiv-cleanup.sh`、`_page_sort_key`。
13. **既有测试断言**：不改动既有用例的期望值来"迁就"新行为；若某步确实需要调整既有断言，必须在提交说明中单独列出并说明理由。

---

## 六、验收标准

### 通用验收模板（每一步都要满足）

1. `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q` **全绿**，用例数 ≥ 上一步基线（只增不减）。
2. **证伪检查**：`git stash` 该步的源码改动（保留测试）后重跑，新增用例中**至少 1 条失败** → 证明测试真正锁住了该问题。
3. 无新增未捕获异常/ERROR 日志（与上一步离线跑基线对比）。
4. **行为不变声明**：除该步目标行为外，接口的响应码、字段、状态值、日志文案均不变（由既有用例断言兜底）。
5. **文档同步**：行为有变化时更新 `AGENTS.md` 对应小节（必要时 `docs/architecture.md` / `docs/maintenance.md`）。
6. 提交格式：Conventional Commits + 中文（如 `fix: 下载取消竞态——reset 改条件更新并补提交前复查`），提交说明包含"验证结果"（跑了什么、多少用例、结论）。

### 分步专项验收

| 步骤 | 专项验收（除通用项外） |
|---|---|
| S1 | 竞态用例在"回退修复"时必须失败；reset 的 409 分支被覆盖；下载 happy path 用例全绿 |
| S2 | 提交失败后状态为 `failed` 而非 `downloading`；幽灵自动复位不误伤活跃下载 |
| S3 | 容量清理/刷新均保护 queued pid；快照并发用例迭代 ≥ 200 次无异常；`/api/downloads` 不再有 `RuntimeError` 风险 |
| S4 | refresh 抛出任意异常后，`_prefetch_capacity_cleanup` 仍被调用一次（用 mock 计数断言）|
| S5 | 用 sqlite3 打开 `.bak` 能读到 WAL 中未 checkpoint 的行；无 pending 时不产生备份（回归） |
| S6 | 带 XFF 的 open-dir 一律 403；直连 localhost 仍可用；`python app.py` 默认 127.0.0.1；unlock 失败有延迟 |
| S7a | `scripts/check_tls.py` 输出 PASS（verify=True 成功 + issuer 为公共 CA）；改为默认 True 后本机冒烟三连（搜索 / 缩略图一页 / 下载一个）全部正常；非法图片 URL 不发起请求即失败 |
| S7b | 白名单内重定向**带凭据**跟随；跨域公网 https 重定向**无凭据**跟随且成功取图（断言无 PHPSESSID）+ 发现表记录；私网/非 https/带 userinfo/非 443 端口目标永不放行且不计入发现；跨域非 `image/*` 响应 502；入口白名单不受影响；不递归跟随；`THUMB_REDIRECT_DISCOVERY=False` 时跨域只记录不跟随；下载侧同款规则（非白名单 host 用无凭据会话） |
| S8 | 断言 `config._instance_dir` 指向临时目录；测试运行后真实 `instance/` 无新增/修改文件 |
| S9 | queued 重复提交不再产生第二个任务；done 状态不可被重下覆盖；删除后重下仍可用（回归） |
| S10 | 并发 merge/remove 收敛无幽灵引用（用例迭代 ≥ 200 次） |
| S11 | 占满信号量时 /thumb 返回 503 且无网络请求；异常路径信号量被释放 |
| S12 | status 暴露 alive/stale/last_error；禁用预取时不报 stale |
| S13 | 403 不再映射为"Cookie 过期"提示；401 仍映射 auth 错误 |
| S14 | 大 map 被压缩到阈值附近；近期条目保留（节流语义不变） |
| S15 | 小作品仍走内存路径（行为不变）；大作品走临时文件且请求后已清理；文件消失不再 500 |
| S16 | 写成功后无 `.tmp` 残留；`os.replace` 失败时原文件字节不变；损坏文件被备份 |
| S17 | 截断密钥被重生成；POSIX 下权限 0600；Windows 下不报错 |
| S18 | 总用例 ≥ 420；CSRF 矩阵覆盖全部修改型端点；下载引擎/thumb/settings 三个零覆盖区各有 ≥ 5 条用例 |

### 收口验收（S18 完成后）

1. 全量离线测试全绿 + 真实环境冒烟：启动 `gunicorn -w 1 --threads 8`，手工验证"搜索一次 / 下载一次 / 图库一页缩略图 / 设置页保存一次 / 预取状态页"。
2. 回写 `docs/superpowers/plans/2026-09-10-risk-audit-fixes.md` 各步骤状态与验证结果（仓库既有纪律）。
3. 更新 `docs/risk-audit-report.md` 中对应问题的状态（已修复/已缓解），保留未处理项（P2/P3）与"暂不修"清单。

---

## 七、P0 实施与验证结果（S1–S7b，2026-09-10 ~ 2026-09-11）

**结论**：P0 的 8 步全部实现、独立提交、推送远程，并已在真实部署（`pixiv-viewer.service`）上验证。P1（S8–S18）的实施与验证结果见 §八（其中 S18 进行中）。

**基线数字**：测试函数 309 → **371**（+62）；全量离线收集 **391 例 / 387 passed**。
`tests/test_test_setup.py` 的 4 例失败为**预先存在**（Windows 沙箱 ConstrainedLanguage 下子进程 PowerShell 检查），已用 `git stash` 回退本阶段全部改动复现同一失败，与本阶段无关。

### 7.1 提交与测试增量

| 步骤 | 提交 | 新增测试函数 | 触及的测试文件 |
|---|---|---|---|
| S1 | `81300ec` | 7 | `tests/test_download.py` |
| S2 | `1f510d5` | 6 | `tests/test_download.py` |
| S3 | `981628e` | 5 | `test_download.py`、`test_prefetch.py` |
| S4 | `4a5dd97` | 4 | `tests/test_prefetch.py` |
| S5 | `b55541b` | 3 | `tests/test_migrations.py` |
| S6 | `6e446e3` | 7 | `tests/test_auth.py` |
| S7a | `e5b73b8` | 17 | `test_download.py`、`test_fetcher.py`、`test_auth.py`、新增 `tests/test_tls_config.py` |
| S7b | `7fea3b5` | 13 | 新增 `tests/test_thumb.py` |

### 7.2 逐步证伪证据（已按「通用验收模板」第 2 条执行）

方法统一为：`git stash push -- <该步源码文件>`（保留测试）→ 跑该步用例 → 期望出现失败 → `git stash pop` → `grep` 确认修复标记已恢复。

| 步骤 | 回退修复后的失败形态（行为级） |
|---|---|
| S1 | `test_reset_racing_worker_never_leaves_phantom_done`、`test_reset_during_final_write_leaves_no_phantom_done` 失败：出现 DB=`done` 而文件已被 reset 删除的幻影态 |
| S2 | `test_download_terminal_commit_failure_resets_to_failed`、`test_download_start_commit_failure_aborts_without_touching_state` 失败：提交异常后状态停在 `downloading`；`test_ghost_detection_keeps_live_download` 失败：幽灵复位误伤活跃下载 |
| S3 | `test_download_missing_row_logs_failed`、`test_queued_download_access_always_under_lock`、`test_capacity_cleanup_keeps_queued_download` 失败：queued 窗口内的作品行被缓存清理删除且无任何记录 |
| S4 | `test_prefetch_loop_runs_cleanup_when_refresh_raises`、`test_prefetch_loop_runs_cleanup_when_tag_raises` 失败：`_prefetch_capacity_cleanup` 的 mock 计数为 0 → 容量上限失效 |
| S5 | `test_backup_captures_wal_uncheckpointed_data` 失败：用 `sqlite3` 打开 `.bak` 读不到仍在 WAL 中的已提交行；`test_backup_copies_wal_when_checkpoint_fails` 用 `raising=False` 让失败成为行为级而非结构性 |
| S6 | `test_rejects_spoofed_loopback_via_xff`（伪造 XFF 打 `/api/open-dir` 得到 200）、`test_dev_server_bind_defaults_to_loopback`、`test_unlock_failure_applies_delay`（`sleeps == []`）失败 |
| S7a | 9 例非法地址用例全灭，其中 `http://169.254.169.254/latest/meta-data/` 报 **`assert 'done' == 'failed'`** —— 旧代码把云元数据端点当图片下载成功并标记 `done`；`test_download_does_not_follow_redirect`、`test_ssl_verify_defaults_to_true` 同时失败；凭据分级用例因旧代码无该 API 报 `AttributeError`（结构性，已在报告中如实标注） |
| S7b | A/B 探测（同一替身会话分别跑新旧代码）：旧代码初始请求 `allow_redirects=True`，且**跨域第二个出站请求携带 `cookie=PHPSESSID=secret`**；新代码为 `allow_redirects=False` + `cookie=NONE` + 发现表记录 `img-cdn.example.net` |

### 7.3 真实部署验证（2026-09-11，服务器 `pixiv-viewer.service`）

| 检查 | 结果 |
|---|---|
| `scripts/check_tls.py` | **退出码 0**：`www.pixiv.net` / `i.pximg.net` 在 `direct` 链路 `verify=True` 均成功，issuer=Google Trust Services WE1 / WR1 |
| 指纹交叉印证 | 开发机（经代理）与服务器（直连）两条独立链路得到**同一组叶证书指纹**（`baaff5d5e06af2d2` / `eded7a031557e60`）→ 进一步支持"链路无劫持" |
| 新默认是否生效 | 运行时 `config.SSL_VERIFY=True`；systemd unit 无 `Environment=` 覆盖、服务器 `.env` 未设该键 → **未使用 `SSL_VERIFY=false` 逃生门**，默认值如期生效 |
| 重启后日志 | 无 SSL 报错、无 traceback；`[prefetch] 后台线程已启动，interval=3600s`；"免认证"告警如期未出现（该部署设了 `ACCESS_PASSWORD`） |
| 真实取图冒烟（开发机，离线探测真实 `i.pximg.net` URL） | 缩略图 `200 / image/jpeg / 21242B`、原图 `200 / image/png / 1085377B`，均 `is_redirect=False` → 证明"禁止跟随重定向"不误伤真实取图 |

### 7.4 本阶段遗留（诚实记录）

1. **`AGENTS.md` 文档同步**：S6/S7a/S7b 改变了公网部署姿态、`SSL_VERIFY` 默认值与 `/thumb` 重定向策略，`AGENTS.md` 对应小节原写"SSL 验证默认关闭"等，**需同步**（本阶段末尾随 `docs:` 提交处理）。
2. **`docs/risk-audit-report.md` 逐条状态回写**（本文件「收口验收」第 3 条）尚未做，留到 S18 收口时一并处理。
3. **已知边界（有意不改，记录在案）**：
   - 下载路径未校验响应 `Content-Type`（`/thumb` 的 B 级重定向路径已校验 `image/*`）；
   - `/thumb` B 级未做图片 magic bytes / 体积上限校验；
   - `check_image_url` 刻意不做 DNS 解析（防 TOCTOU 与每图一次解析开销），域名的信任来自证书校验 + 凭据分级。
4. **`instance/thumb_redirect_hosts.json`** 为新增的观测数据文件（可随时删除）；发现表不参与任何判定，也不自动提升白名单。

---

## 八、P1 实施与验证结果（S8–S18，2026-09-11）

**结论**：S8–S18 十一步全部实现、逐项独立提交，每步都有新增测试与证伪记录（各节末「已实现 + 验证结果」）。**S18 在补测过程中发现并修复了两个此前无人知道的缺陷**（预取配置键映射错位、排队中取消导致下载锁与取消标记永久泄漏），两者都有失败先行的证据，详见该节末。

**基线数字**：P0 结束时（§七）全量离线收集 391 例。各步提交说明声称的新增用例合计 122（67 ＋ S18 的 55），与本次实测的 **513 例（508 passed / 1 skipped / 4 failed）**一致（391＋122＝513）。**口径声明**：本次回写独立复算的只有"全量收集/通过/跳过/失败"这一组数字；各步骤小节里的文件级 `passed` 数与新增用例数**取自该步提交说明**（本会话逐项记录），未逐个重跑复核。**不写"测试函数"口径的数字**：函数级计数未复核，避免两套口径混用。
其中 **4 例失败为预先存在**（`tests/test_test_setup.py` 的 Windows 沙箱 PowerShell 子进程检查，§七 已用 `git stash` 复现同款），本阶段改动前后都是这 4 例；**1 例 skip** 是 S17 新增的 `0600` 权限断言（`skipif win32`，Windows 无 POSIX 权限语义）。
单轮耗时 **17.10s**，满足 §六 的"单轮 < 30s"。

### 8.1 提交与用例增量

| 步骤 | 提交 | 新增用例 | 触及的测试文件 |
|---|---|---|---|
| S8 | `4e49cf2` | 5 | `tests/test_test_setup.py`、`tests/conftest.py` |
| S9 | `2e14c66` | 7 | `tests/test_download.py` |
| S10 | `cb64ff7` | 4 | `tests/test_prefetch.py` |
| S11 | `9c71903` | 3 | `tests/test_thumb.py` |
| S12 | `df7cd94` | 4 | `test_prefetch.py`、`test_prefetch_api.py`（+ 字段集断言同步） |
| S13 | `dde7221` | 9 | `tests/test_fetcher.py` |
| S14 | `ad7ad4e` | 4 | `tests/test_fetcher.py` |
| S15 | `70dded0`（偏差记录 `1b40c7c`） | 11 | `tests/test_app.py` |
| S16 | `4c7c783` | 11 | `test_helpers.py`、`test_app.py`、`test_prefetch_api.py`、`test_test_setup.py` |
| S17 | `578c0c1` | 9 | `tests/test_auth.py` |
| S18 | 见本节「S18」段 | 55 | 新增 `tests/test_settings_api.py`；`test_app.py`、`test_download.py`、`test_thumb.py` |

### 8.2 证伪结论（含"证据偏弱"的如实标注）

- **行为级、证据强**：S8（回退后 3 例失败 + "真实 `instance/` 零写入" A/B 实验）、S9（6/7）、S10（幽灵引用实测 `illust_ids=[9202, 9201]`）、S11（失败在「不得用 `with` 获取信号量（没有等待上限）」）、S13（回退后**恰好**那 4 个 403 用例失败）、S14（2 例失败，另 2 例"不得放宽节流"两侧都通过）。S16 的 3 例、S17 的机制级 7 例也属此类（见下）。
- **seam 缺失型、证据偏弱（如实标注）**：S15（回退后 10 例失败，但原因是新 seam 不存在 `AttributeError: ... has no attribute 'tempfile'`）、S16（7 例中 4 例是 `helpers._atomic_write_json` 不存在）、S17（8 例中 7 例是 `module 'config' has no attribute '_load_or_create_secret'`）。这三步**都补做了机制级证伪**（保留 seam，只改判定/阈值/权限调用）：S15 得到 8 例 `assert 0 == 1`，S16 得到 2 例 `assert 200 == 500` + 1 例「损坏文件必须留一份副本」，S17 得到 `assert 'abc' != 'abc'` / `assert 0 == 3` / `assert '内容过短' in ''`。
- **S12 属新增可观测面**：证伪表现为"字段缺失/行为未定义"，不是修掉某个既有错误值 —— 这类修复的证伪强度天然弱于行为修复，不应按同一标准宣称"已证明修好"。
- **S18 的两处行为级强证据**（补测发现的新缺陷，不是既有修复）：① 预取键映射：把 `routes_settings` 的同步逻辑改回旧的"直接用长键"后，`test_prefetch_keys_apply_immediately` 立即失败于 `assert 0 == 321`；② 排队中取消：把 `session_obj = None` 放回取消检查之后，`test_download_cancelled_before_start_does_nothing` 失败于 `UnboundLocalError: cannot access local variable 'session_obj'`；③ CSRF 矩阵：临时摘掉 `/api/prefetch/refresh-reset` 的 `@_csrf_required` 后，恰好对应用例失败于「POST /api/prefetch/refresh-reset 未受 CSRF 保护」。

### 8.3 本阶段遗留与边界（诚实记录）

1. **S12 只把"后台线程静默死亡"变成可观测**（`prefetch_alive` / `stale` / `last_error`），**未加 supervisor、未自动重启**，`_start_prefetch_thread` 内 `_run` **仍无兜底 try**：`_prefetch_loop` 之外的异常仍能打死线程，只是这次看得见。
2. **S10 的残留**：删除侧 `commit` 仍在锁外，最终值取决于两个全列写的提交次序；极端情况下合并可能等锁超时而丢一次合并（有日志与下一轮重试，不再产生幽灵引用）。
3. **S14 是"有界"而非"硬顶"**：清理只在表超上限时触发，节流窗口内的条目一律保留。
4. **S15 的临时文件清理依赖 WSGI 关闭 body 的契约**（另有 EOF 自清理兜底）；大包改占临时目录空间；阈值常量改动需重启。
5. **S16 只降低"settings.json 被写坏"的概率并保留证据**，未改变"损坏即回退默认值"的语义 —— 审计报告 §10-4 的"损坏 → 登录墙静默失效"因此只是被降低概率，**未被消除**。
6. **S17 的长度校验会重生成密钥** → 该部署既有会话与游标失效（有意取舍）；`0600` 的 POSIX 端到端权限**未在本机证实**（Windows 上断言 skip，只验证了"确实请求了 0o600"）。
7. **不在本阶段范围的审计发现**（仍未修）：列表请求零限流（报告 §15-4）、`_db_pids_cache` 尖峰与缺索引（§11-4 / §12-3）、`settings.json` 明文存口令与损坏回退语义（§10-4）、Windows 保留名（§10-6）、备份保留策略（§14-4）。
8. **S18 补测发现并修复的两个缺陷**（原审计未命中，见报告 §33.3）：① 设置页保存 `prefetch_*` 时把 settings.json 的**长键**写进 `_prefetch_state`，而预取循环读的是**短键** → "保存即生效"自 Blueprint 拆分重构（`73a8f0e`）起就没生效过；② `_download_illust` 的 `session_obj = None` 位于取消检查之后，**排队中被取消**的任务会在 `finally` 首行抛 `UnboundLocalError`，导致锁未释放、取消标记与进度条目泄漏 → 该作品**再也下载不了**（worker 在 `lock.acquire(blocking=False)` 处静默跳过）。两处修法都是"把初始化/映射摆到正确位置"，未改任何语义分支。
9. **S18 仍未覆盖的面**（诚实边界）：下载引擎的并发细节（多 worker 抢同一 pid）、前端 JS 无测试（仓库向来如此）、`/api/image` 的 Range/ETag 行为、`fetcher` 的令牌桶时序、迁移在真实旧库上的升级（只在临时库验证）。**CSRF 矩阵是静态对账 + 请求级 403 双保险**，但它只证明"缺头时 403"，不证明各端点的业务授权逻辑（例如收藏夹归属校验）—— 那需要逐端点用例，本步未做。
10. **审计报告的回写范围**：本次只回写被 S8–S18 覆盖的发现（`docs/risk-audit-report.md` 新增 §33 台账 + 相关条目状态标注）；报告 §1–§32 的评分与结论**保持审计当时的口径**，不据修复结果改写评分。P0（S1–S7b）的状态回写见 §七，不在 §33 重复。
