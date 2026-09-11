# Pixiv Viewer 深度架构与风险审计报告

> 审计基线：`main` 分支 @ `615cdf0`；运行模型 `gunicorn -w 1 --threads 8` + SQLite WAL；单用户自部署。
> 审计方法：全部源码逐行阅读（app/config/runtime/models/middleware/helpers/fetcher/background/routes_*/migrations/tests/scripts/templates/static）+ 6 路并行专项审计（下载并发 / 预取搜索状态机 / 安全渗透 / 性能内存 / 测试覆盖 / 上游依赖）。
> 证据等级：**CONFIRMED**（源码直证）/ **HIGH CONFIDENCE**（调用链推导）/ **POSSIBLE**（需运行时验证）/ **SPECULATION**（推测，不列入严重问题）。
> 引用位置用「符号名（文件:行号）」，行号仅供参考，可能随重构漂移。
> **修复进展（2026-09-11 追加）**：P0（S1–S7b）与 P1（S8–S17）已实施并验证，逐条记录见 `docs/superpowers/plans/2026-09-10-risk-audit-fixes.md` §七 / §八 / §33。本报告 §1–§32 的**评分、严重度与结论保持审计当时（基线 `615cdf0`）的口径不变**，仅在受本次修复覆盖的条目后追加「✅ 已修复 / ⚠️ 部分缓解」标注（`<提交号>` 指向修复提交）；未标注的发现一律视为未处理。完整台账见 §33。

---

## 1. Executive Summary

1. **总体判断：这是一个设计有纪律、文档质量高、测试量可观（约 340 用例）的个人项目**，架构（单进程 + 内存状态 + SQLite WAL + 后台线程）完全适配"单人自部署"目标，且经过多轮真实重构与回归加固——不是草台代码。
2. **下载系统是当前最大的真实风险区**：`_download_illust` 的取消/重置/重复提交之间存在确认的竞态窗口，最坏产生"DB=done 但文件已被删除"且**没有任何 UI 入口能恢复**（触发被 done 挡回、取消被状态守卫挡回）。
3. **默认安全姿态不安全**：`ACCESS_PASSWORD` 留空=全站免认证，且 `python app.py` 绑定 `0.0.0.0`；一旦公网直连，Pixiv Cookie、下载内容、`/api/open-dir`（本机目录操作）全部暴露，且 `ProxyFix` 无条件信任 `X-Forwarded-For` 会瓦解最后一道 IP 判定。
4. **TLS 校验默认关闭 + 请求全局携带 PHPSESSID Cookie + 跟随重定向不过滤**：在"能看见你网络流量"的攻击者面前（公共 WiFi、运营商、被控代理），Pixiv 会话令牌可被 302 重定向窃走；代理配置（当前 settings.json 走 `http://127.0.0.1:7890`）会放大这一面。
5. **预取系统的持久化状态机（v4 退避/熔断/三层淘汰）是项目里设计最成熟的部分**，容量上限在"所有行都可删"的假设下必然收敛；但**未分类异常（OperationalError/KeyError/JSONDecodeError）仍能从刷新 pass 冒泡，连带跳过当轮容量清理**，上限保护存在未完全闭环的旁路。
6. **数据一致性风险集中在"跨系统事务"**：文件写→DB commit、DB commit→文件删、SearchCache JSON 与 Illust 行的两段式更新——全部存在崩溃/并发窗口，多数可自愈，少数（幽灵引用、幻影 done）不收敛。
7. **性能在 1 万条规模完全无虞，10 万条开始出现秒级查询，100 万条出现分钟级与内存尖峰**；确认的内存级泄漏只有一处（`fetcher._fill_last_attempt` 只增不删，百万级约 100MB 慢泄漏）。
8. **测试数量多但覆盖严重偏科**：最危险的下载引擎全函数、下载路由、`/thumb` 代理、`/api/image`、settings 写入面**全部零测试**；并发竞态类只有限流器与 token bucket 有测试。
9. **上游 Pixiv API 完全不可控**：403 同时在列表端被归类为"认证失效"（用户看到误导性的"Cookie 过期"）而在详情端被归类为"限流"；任何 endpoint/schema 变化都会静默降级（空结果、详情 None），失败隔离不错但**没有针对意外的形式校验**。
10. **一年内最可能的三个真实故障点**（详见 §32）：下载状态与文件不一致（并发竞态）、公网暴露下的账号/本机安全、规模增长后的查询退化。

---

## 2. Overall Risk Score

| 维度 | 得分 | 理由 |
|---|---|---|
| 架构 | **7.5/10** | 模块边界清晰、依赖单向、`-w 1` 语义被严格执行并文档化；扣分在 app 命名空间测试契约形成的隐式耦合与 import 期副作用 |
| 稳定性 | **6/10** | 后台线程无监督无心跳；下载引擎异常路径可静默卡死（`downloading` 残留）；预取刷新一轮最长 ~20 分钟阻塞容量清理 |
| 安全 | **4.5/10** | 默认免认证 + `0.0.0.0` + `SSL_VERIFY=False` + 重定向跟随带 Cookie + XFF 无条件信任 + 密钥文件无权限控制；但认证/CSRF/XSS/路径遍历的"已启用部分"质量很高 |
| 并发 | **5.5/10** | Check-then-act 竞态集中在下载引擎（幻影 done、队窗静默丢失）；其余共享状态（限流/缩略图失败表/任务表）已按约定加锁且被测试 |
| 数据一致性 | **6/10** | 大部分"两段式"更新可自愈；但取消竞态（不可恢复不一致）、幽灵引用（永不自愈）确认存在 |
| 性能 | **7/10** | 1 万条规模实测优秀（json_each 24.8ms 等）；扣分在 10 万+ 全表扫描集群、ZIP 整包内存、令牌桶串行化导致的单搜最长 13 分钟 |
| 测试 | **6.5/10** | 340 用例、关键状态机覆盖扎实；但下载引擎/下载路由/thumb/图片服务/settings 零覆盖，CSRF 矩阵只锁了 8/27 端点 |
| 可维护性 | **7/10** | 文档（AGENTS/architecture/maintenance）罕见地好；扣分在"测试补丁 seam"复杂度、注释与代码三处失步、无 lint/类型检查 |
| 部署 | **5.5/10** | 一键 gunicorn+systemd 可行；但备份仅拷主库文件（WAL 数据可能丢失）、无恢复演练、`.env`/settings 权限无保护、升级靠人工 |

---

## 3. Top 10 风险

| 排名 | 风险 | 严重度 | 概率 | 影响 | 证据 | 建议 |
|---|---|---|---|---|---|---|
| 1 | 下载取消/reset 竞态 → DB=done 但文件被删，且 UI 无恢复入口 | High | Medium | 数据不一致、用户困惑 | CONFIRMED | reset 改条件更新；commit 前复查取消态 |
| 2 | 默认免认证 + `0.0.0.0` + XFF 伪造 → 远程访问 Pixiv 账号数据、触发 `/api/open-dir` 打开任意目录（Windows UNC→NTLM 泄露） | Critical | Low-Med | 账号接管面、本机泄露 | CONFIRMED（链路） | 部署前强制设密码；反代剥 XFF；禁 0.0.0.0 直跑 |
| 3 | `SSL_VERIFY=False` + 跟随重定向 + 全局 Cookie 头 → on-path 窃取 PHPSESSID / 盲 SSRF | Critical | Medium | Pixiv 账号接管、内网探测 | CONFIRMED（链路） | SSL_VERIFY 默认 True；/thumb 重定向复检；下载 URL 校验 |
| 4 | 下载引擎 `safe_commit` 异常被线程池静默吞掉 → 永久卡 `downloading`，不可观测 | High | Low | 单作品死锁直至重启 | CONFIRMED | 包异常→置 failed+日志 |
| 5 | 预取刷新未分类异常冒泡 → 当轮 `_prefetch_capacity_cleanup` 被跳过，上限失效 | High | Low-Med | 容量无界膨胀 | CONFIRMED | refresh 加宽 except 不冒泡；清理移出共用 try |
| 6 | 重复提交/重下同一 pid → 重下失败删除上次成功文件 | High | Low-Med | 数据丢失（可重下） | HIGH CONFIDENCE | 入口复查 status/加唯一任务键（✅ `2e14c66`） |
| 7 | 下载"队窗"（queued→首 commit）内被容量清理删行 → 下载静默消失无日志 | Medium | Medium | 下载丢失 | HIGH CONFIDENCE | 清理并入 `_queued_downloads` 判定；缺行留痕 |
| 8 | 限流覆盖不全：列表类请求（search/discovery/follow）绕过令牌桶；403 在列表端误报"认证失效" | Medium | High | 触发 Pixiv 封禁、误导排障 | CONFIRMED | 列表端补限流；403 分类统一（⚠️ 403 已修 `dde7221`；列表端限流**未做**） |
| 9 | 数据规模增长：10 万+ 行全表扫描（tags DISTINCT、`_db_pids_cache`）、`_fill_last_attempt` 无界增长、ZIP 整包内存 | Medium | High（时间尺度） | 查询秒级、内存 100MB+ | CONFIRMED（源码推断） | 补索引、周期压缩、ZIP 落盘（✅ 压缩 `ad7ad4e` / ZIP 落盘 `70dded0`；补索引**未做**） |
| 10 | 上游 Pixiv API 变化（endpoint/schema/403 策略）导致全功能静默降级，无告警 | High | Medium | 空结果/下载失效 | HIGH CONFIDENCE | 关键路径错误分类+告警；schema 校验点 |

---

## 4. Critical / High 问题逐条深挖

### [HIGH] 下载取消/reset 竞态：幻影成功（DB=done 而文件已删）

**结论**：`_download_illust` 在"取消检查"与"最终提交"之间存在非原子窗口，reset 插入其中会产生 DB 与文件系统的永久不一致，且 UI 无恢复入口。

**证据**
```text
文件：background.py
函数：_download_illust
位置：L649-704（取消检查点 L652/L685，最终 commit L704 之前无第二次取消复查）
文件：routes_download.py
函数：_cancel_download_internal
位置：L92-110（reset=True：先删磁盘目录、置 download_status=None、写 failed 日志）
```

**调用链**
```text
用户点击取消/重置
↓
_cancel_download_internal：删文件 + DB=None + download_cancellations.add(pid)
↓（与下面并行）
worker 已通过 L685 的取消检查（读到 False）
↓
worker 计算 file_size、写 local_paths='done'、safe_commit
↓
DB = done，磁盘文件已被 reset 删除 → 幻影成功
```

**触发条件**：reset 请求恰好落在 worker 通过 L685 检查之后、L704 commit 之前（毫秒级窗口，多页作品因 `os.path.getsize` 求和会略微加宽）。

**实际后果**
- 数据：文件删除但 DB 记 done、file_size=0、DownloadLog='done'。
- 数据库：`download_status='done'`、`local_paths` 指向不存在的文件。
- 用户体验：图库显示"已下载"卡片、图片全 404；`trigger_download` 因 done 分支（routes_download.py:32-33）直接返回"已下载"；`_cancel_download_internal` 因状态守卫（L86-87）返回"未在下载中"——**唯一恢复途径是图库删除接口**。

**为什么当前实现会产生**：状态转移没有做成条件更新（compare-and-swap），且失败路径没有文件与 DB 的对账。

**修复建议（最小）**：worker 在 L704 提交前把取消复查与提交放进同一临界区——最简单是把 reset 侧改为条件 UPDATE：`UPDATE illusts SET download_status=NULL WHERE pixiv_id=:p AND download_status='downloading'`（rowcount=1 才执行文件删除），worker 侧 commit 前再查一次 `download_cancellations`（把"检查→提交"缩到最小窗口）并在提交后若发现取消则回滚式置 None。

**测试建议**：`test_cancel_race_reset_vs_worker_final_commit`：Barrier 卡住 worker 于 L685 之后，先跑 reset，再放行，断言最终 DB 状态与磁盘一致。

**证据等级：CONFIRMED（竞态窗口源码直证；实际触发需精确时序）**

---

### [CRITICAL] 公网直连时的安全链：默认免认证 + 0.0.0.0 + XFF 伪造 + open-dir

**结论**：`ACCESS_PASSWORD` 默认为空；`app.py __main__` 绑定 `0.0.0.0`；`ProxyFix(x_for=1)` 无条件信任 `X-Forwarded-For` 头；`/api/open-dir` 仅校验 `request.remote_addr`。该链路对公网可达的部署意味着任意访客可调用 open-dir 打开服务器任意现有目录（Windows 下 `os.startfile` 支持 UNC 路径 → NTLM 凭据泄露），并可无限旋转 XFF 绕过登录限流爆破口令。

**证据**：`app.py:71-72`（ProxyFix）、`app.py:151-152`（0.0.0.0）、`middleware.py:65`（限流 key 用 remote_addr）、`routes_gallery.py:517-535`（open-dir 仅查 remote_addr + `os.startfile`/`subprocess.Popen(['xdg-open', path])`）。`/csrf-token` 在 `_AUTH_EXEMPT_PATHS`（middleware.py:98），无认证即可取得 CSRF token——CSRF 不构成该链的屏障。

**触发条件**：部署者未设 ACCESS_PASSWORD（默认！）且服务对公网/局域网可达；或反代未过滤 XFF。

**后果**：Pixiv 账号数据（搜索、详情、收藏、下载原图）可被任何访客读取/下载（消耗主人的 Pixiv 配额并暴露 R18 浏览记录）；open-dir 可让攻击者触发主机打开任意目录（Windows UNC → 泄露 NTLM hash；Linux xdg-open 打开目录低危害）；登录墙（若启用）可被 XFF 旋转绕过枚举。

**修复建议**：① 生产部署文档明确"公网必须设 ACCESS_PASSWORD"，并考虑启动时检测"无口令 + 非 loopback 绑定"打一条 ERROR 日志；② 反代层（Caddy/nginx）剥离或重写 XFF（ProxyFix 信任第一跳的前提是反代强制覆盖该头）；③ `/api/open-dir` 判定增强为"remote_addr 为 loopback 且 XFF 经 ProxyFix 还原前后一致"（或直接只允许 `X-Real-IP` 由反代重写）；④ `python app.py` 的 host 默认改 `127.0.0.1`。

**证据等级：CONFIRMED（链路每一环都在源码中；可利用性依赖部署方式）**

---

### [CRITICAL] TLS 不校验 + 重定向跟随 + 全局 Cookie 头 → PHPSESSID 泄露 / 盲 SSRF

**结论**：`SSL_VERIFY=False` 为默认（config.py:112），`build_pixiv_session` 给**所有**请求（含 `i.pximg.net` 图片、`/thumb` 代理与下载引擎）手工设置 `Cookie: PHPSESSID=...` 头（fetcher.py:304）；所有请求默认跟随重定向（requests 默认 `allow_redirects=True`，`/thumb` 未复检白名单，routes_gallery.py:79-90 对重定向目标无条件放行）。

**触发条件**：攻击者处于客户端与 Pixiv 之间的网络路径（公共 WiFi、恶意代理——注意当前用户 settings.json 配置了 `proxy: http://127.0.0.1:7890`，代理是明文 HTTP）；或 TLS 终止在不可信点上。

**后果**：① 302 重定向到任意主机时，手工设置的全局 Cookie 头随重定向请求一并发出 → PHPSESSID 被窃取 → Pixiv 账号接管；② 重定向到内网地址（如 `http://169.254.169.254/...`）构成 SSRF（`/thumb` 受限于初始 URL 前缀，但重定向目标不设限）；③ 下载引擎 `session_obj.get(url)`（background.py:659）对 `original_urls` 无 scheme/host 校验，是纵深缺口（当前 URL 只能来自 Pixiv 响应，无远程污染途径）。

**修复建议**：① `SSL_VERIFY` 默认改 `True`（AGENTS 已注明生产可设）；② `/thumb` 与下载引擎的 session 配置 `allow_redirects=False` 或重定向后重新校验 host 白名单；③ 对 `original_urls`/图片 URL 增加 host 白名单校验（`urlparse(url).netloc == 'i.pximg.net'` 或允许镜像站配置）。

**证据等级：CONFIRMED（配置与行为均直证；实际攻击需要网络路径上的攻击者）**

---

### [HIGH] 下载引擎 safe_commit 异常被线程池静默吞掉 → 永久卡 downloading

**结论**：`_download_illust` 内 4 处 `safe_commit`（L633/642/682/704）抛异常（如 SQLITE_BUSY 超 10s）时，异常穿过 ThreadPoolExecutor 的 worker 边界，仅被 Python 线程池底层记录一行日志；DB 状态停留在已提交的 `downloading`，`trigger_download` 因 downloading 分支（routes_download.py:35-36）拒绝重新触发，普通取消（reset=False）只加取消标记不恢复状态。恢复仅靠 `_reset_stuck_downloads`（重启）或用户手动 reset。

**为什么会产生**：状态写入失败没有对应的失败处理分支；线程池天然吞异常。

**修复建议（最小）**：给 4 个提交点包一层 `except Exception`：记录日志 + 尽力 `safe_commit` 一次 `download_status='failed'` + DownloadLog('failed', '状态写入失败')；`_reset_stuck_downloads` 保持兜底。

**证据等级：CONFIRMED（异常路径源码直证，触发需 DB 写失败，概率低）**

---

### [HIGH] 预取刷新未分类异常冒泡 → 当轮容量清理被跳过（上限失效旁路）

**结论**：`_refresh_bookmarks_pass` 的异常兜底只捕获 `PixivAuthError` 与 `FileNotFoundError`（background.py:411-418）。`safe_commit` 的 `OperationalError`、`_get_illust_detail` 内 `data['body']` KeyError、`resp.json()` JSONDecodeError、`int(body.get('userId'))` ValueError（fetcher.py:573-584）均未捕获 → 冒泡到 `_prefetch_loop` 的 `except Exception`（L563-565）→ **本轮 `_prefetch_capacity_cleanup()` 与 `last_check` 被跳过**。设计文档明确"容量清理必执行"是上限不变式的前提，此路径破坏它。

**修复建议**：`_refresh_bookmarks_pass` 加宽 `except Exception`（记日志 + `stats['aborted']='unknown'`，不冒泡——与 auth 同款语义）；或将 `app._prefetch_capacity_cleanup()` 移出与 refresh 共用的 try 块。

**证据等级：CONFIRMED（调用链完整追踪）**

---

## 5. 并发问题汇总

| # | 问题 | 文件/符号 | 等级 | 关键结论 |
|---|---|---|---|---|
| C1 | 取消检查→提交非原子（幻影 done） | background.py `_download_illust` L685→L704 | CONFIRMED | 见 §4-1 |
| C2 | 锁竞争失败静默丢弃新任务 | `_download_illust` L615-616 `lock.acquire(blocking=False)` 失败即 return，无日志 | HIGH CONFIDENCE | 用户收到 accepted 但任务从未执行；`_queued_downloads` 由旧任务 finally 清掉 |
| C3 | `setdefault→acquire` 双窗口 | L613-615 | POSSIBLE | `_release_download_lock` 的 `is` 比较挡住大部分；理论窗口极小 |
| C4 | 重复提交同一 pid | routes_download `trigger_download`/`batch_download` | CONFIRMED | 锁防双执行但有"返回 accepted 实为 no-op"与"重下失败毁旧文件"两个后果 —— ✅ 已修复：`2e14c66`（排队判定与入队同锁 + `_download_illust` 的 done 守卫 + 提交失败把 pid 撤出队列） |
| C5 | 下载队窗静默丢失 | routes_download.py L45-46 → background.py L626-628 | HIGH CONFIDENCE | 容量清理在 queued→首 commit 窗口删行，worker 查不到行静默 return |
| C6 | `api_downloads` 无锁遍历 `_queued_downloads`（`list(set)` 可抛 changed size） | routes_download.py L205 | POSSIBLE | 违反 AGENTS 自己定的容器遍历加锁约定 |
| C7 | 限流器锁 | middleware.py `_check_rate_limit` | 已修复✓ | 整事务持锁 + 并发爆破测试（test_auth 40 轮）——正面案例 |
| C8 | 缩略图信号量饥饿 | runtime.py `_thumb_sem`（12）> `--threads 8` | HIGH CONFIDENCE | 冷缓存双标签页 12+ 并发时请求线程可全部阻塞在信号量上，全站 API 冻结（断网时放大到 30s） —— ✅ 已修复：`9c71903`（`_thumb_sem.acquire` 加 `THUMB_SEM_TIMEOUT`=15s 上限，超时 503） |
| C9 | `_search_tasks` 读无锁 | routes_search.py `search_status` | 可接受✓ | 写方最后置 status 的约定成立，除 §9-2 的 error 字段写序瑕疵 |
| C10 | `_last_fetch_stats` 互相覆盖 | fetcher.py | 已知可接受✓ | 仅影响展示统计 |

---

## 6. 数据一致性问题

1. **DB=done 文件缺失（幻影成功）**：§4-1。不可自愈，需人工删稿重下。CONFIRMED。
2. **文件删了 DB 仍 done**（用户手动删文件、pixiv-cleanup.sh 与 app 并发）：`serve_image`/`download_file` 有 404 兜底但 DB 状态不自动修正，图库长期显示"已下载"空卡。POSSIBLE（行为确认，触发看用户）。建议在下载管理页做文件对账。
3. **SearchCache 幽灵引用（永不自愈）**：手动刷新 merge 与删除路径跨 session 读改写同一 JSON 列（background.py `_prefetch_one_tag` L189-201 vs `_remove_pids_from_search_caches` L226-238），WAL last-writer-wins 无冲突检测；已删 pid 被写回 → `total`/`filtered_total` 长期不一致（查询按 INNER 语义静默跳过）。CONFIRMED（路径）/ POSSIBLE（触发）。建议进程级锁或 CAS。**✅ 已修复：`cb64ff7`（`background._search_cache_guard` 罩住合并段与删除改列段；幽灵引用用例在回退修复后实测 `illust_ids=[9202, 9201]`，即已删 pid 被写回，修复后消失。删除侧 `commit` 仍在锁外，残留见 §33）**
4. **容量清理两段式（清引用 commit → 删行 commit）间崩溃**：行保留、其他标签引用已丢——依赖"下轮重新出现在结果页"才自愈，非永久不一致。HIGH CONFIDENCE。
5. **失败路径先删文件后提交**（background.py L671-682）：文件已删、commit 失败 → DB=downloading + 文件缺失的不一致方向。建议统一"先提交 failed 再清理文件（清理失败仅告警）"。
6. **`_reset_stuck_downloads` 无条件整目录删除**：多页作品已完成页在崩溃重启时被一并删除（无续传设计，有意为之但值得记录）；重下中崩溃会把上一轮成功文件一起删。CONFIRMED（设计行为）。
7. **`file_size` 删除后不归零**：`_delete_illust_files` 不改 file_size，图库仍显示旧大小。CONFIRMED（展示级）。
8. **`prefetch_tags_delete` 缺 DownloadLog 保护**（与容量清理不一致）：用户有 cancelled/failed 记录的预取作品在删标签时被连带删行，而容量清理会保护它。CONFIRMED。修复：复用 `_is_user_owned`。

---

## 7. 下载系统问题（专项）

除 §4 已列外：

| # | 问题 | 等级 |
|---|---|---|
| D1 | `download_cancellations` 标记由"旧 worker 的 finally"清理：旧 worker 已退出而新任务未启动时，标记残留期新任务被静默跳过（L618-622） | HIGH CONFIDENCE（时序上界毫秒，实际影响小） |
| D2 | reset 与 worker 各自写日志（failed + cancelled 两条） | CONFIRMED（噪音级） |
| D3 | 取消生效延迟 ≤ 单页最慢请求（10s 连接超时 + 60s 读超时），页面间才有检查点（L652）——超长单页无法快速取消 | HIGH CONFIDENCE（设计权衡） |
| D4 | `download_file` ZIP：整包 `BytesIO`（50 页×10MB ≈ 500MB 常驻请求线程，8 线程可乘 N 倍）；L164-181 之间文件消失 → TOCTOU 500 —— ✅ 已修复：`70dded0`（总大小超 `ZIP_MEMORY_THRESHOLD_BYTES` 时落临时文件流式发送；打包途中消失的文件跳过其余照常，全部消失返回 404「文件已丢失」而不是空 zip 或 500。清理机制见计划文档 S15「实现偏差」） | HIGH CONFIDENCE |
| D5 | 下载引擎每任务 `build_pixiv_session` 新建 + 关闭（非池化）：单任务多页 OK，但 batch 下载 N 个作品 = N 次 TCP+TLS 握手 | HIGH CONFIDENCE（性能） |
| D6 | `download_status='failed'` 无自动重试；批量下载个别失败静默 | CONFIRMED（UX） |

**下载系统状态机**（现状）：
```text
none →(trigger)→ queued →(worker 启动)→ downloading →(全部页成功)→ done
                                                    ↘(任一页失败)→ failed
                                                    ↘(取消检查点命中)→ none（删部分文件）
重启: downloading →(reset_stuck)→ none（删全部文件）
```
**缺陷**：queued 无持久化（重启即丢但无痕）；done 无文件对账；downloading 无超时。

---

## 8. 预取系统问题（专项）

- **状态机整体稳健**：idle→fetching→done/error 全路径可恢复（抢占 UPDATE 保证 error 可重试）；`_reset_stuck_prefetch` 只重置 fetching 是正确的。
- **P-1 fetching 残留路径**：`_prefetch_one_tag` 的 except 分支内 `safe_commit` 再抛（L204-209）→ 状态停留在已提交的 fetching → 该标签**每轮被永久跳过直到重启**。CONFIRMED（路径）/ POSSIBLE（概率）。建议：except 内状态写用 try/except 兜底。
- **P-2 刷新未分类异常冒泡跳容量清理**：§4-5，CONFIRMED。
- **P-3 幽灵引用**：§6-3，CONFIRMED（路径）。
- **P-4 标签复活**：`POST /api/prefetch/tags` DELETE 与预取循环竞争——循环读完标签列表后建行，删除操作被静默撤销（background.py L155-158）。CONFIRMED（路径）/ POSSIBLE。
- **P-5 refresh 409 TOCTOU**：路由查 status 与起线程之间循环线程可能已抢占，手动刷新静默 no-op 仍返回 `refreshing`（routes_prefetch.py L205-211）。CONFIRMED（UX 误导）。
- **P-6 部分预取失败泄漏 prefetch_source 行**：翻页途中失败→已置标记但不在任何 SearchCache.illust_ids → 不可见但计容量。CONFIRMED（路径），层 2/3 会收敛。
- **P-7 吞吐模型**：每轮=所有标签预取（无标签数上限）→ 刷新 300 条（fill 桶 20/min 理论 15min、与后台补全挤占最坏 ~30min）→ 容量清理；单轮最长可超过 1 小时（`_prefetch_loop` 无耗时上限），期间 UI `running=true`。HIGH CONFIDENCE（源码推断）。建议给每轮加时间预算。
- **P-8 容量收敛性**：上限不变式成立（tier3 兜底有测试）；唯一例外是受保护行自身超上限（设计豁免）。刷新 backlog 收敛依赖入库速率 < 7200/天，长期超速时表现为"高位稳态"而非失控。HIGH CONFIDENCE。
- **P-9 `_prefetch_capacity_cleanup` 全表 ORM 加载 + 每轮 2 次 commit**：10k 行可接受，100k 行明显。见 §11。
- **P-10 docstring 与代码相悖**：L480-481"宁可删新入的低收藏作品"与 tier1（已刷新优先淘汰）行为相反——文档误导维护者。CONFIRMED（文档）。

---

## 9. 搜索任务问题（专项）

1. **S-1 running 任务永不清理**：`_cleanup_search_tasks` 只清终态超 TTL；`BaseException` 穿透 `except Exception` 后 `finally` 仍写 `finished_at` → 任务以 `running` 态永久滞留（每任务 KB 级，无功能影响；前端靠 `searchGeneration` 静默失效 + 404 提示重搜）。CONFIRMED（路径）/ SPECULATION（概率——请求全有超时，无已知挂死点）。
2. **S-2 error 字段写序瑕疵**：`_submit_search_task` 的异常分支先置 `status='error'` 再写 `task['error']`（L97-106），与 L224 注释"最后置 status"的视图一致性声明矛盾；轮询可能读到 `error=None` 的 502。CONFIRMED（影响极小）。
3. **S-3 游标安全**：HMAC(CURSOR_SECRET) 签名 + 24h 过期 + 服务器端编码——**不可伪造、不可篡改**（`decode_cursor` 双校验，fetcher.py:57-72）。replay 是设计内行为（分页恢复）。✓
4. **S-4 游标参数无条件覆盖新请求参数**（routes_search.py L155-160）：粘贴旧游标 URL 时用户新筛选条件被静默忽略。前端正常流程已防（改条件丢游标）。POSSIBLE。
5. **S-5 skip_count 无钳制**：合法签名的超大 skip 会使 `paginated_search` 逐页整页跳过至 `_MAX_SCAN_PAGES`，每次翻页=10 次上游请求+空结果。POSSIBLE（需手工编码游标）。建议路由层钳制。
6. **S-6 游标重放 + Pixiv 分页漂移 → 缺件窗口**：漂移期间页内容收缩时，切断页偏移不进入 next_skip（仅同页累加，L254-255）→ 重复展示 + 前端去重吞掉 → 部分作品被永久跳过。POSSIBLE。前端去重逻辑已是兜底。
7. **S-7 搜索线程每任务新建**：`threading.Thread` 每搜索一个线程 + `_fetch_details_parallel` 每页新建 5-worker executor（每页 5 次新 TLS 握手，跨搜索不复用）。性能点，见 §11。
8. **S-8 `_submit_search_task` 取消策略正确**：只 set running 任务的 event；在途请求完成入库（下次免重拉）；预算 thread-local 隔离有测试。✓

---

## 10. 安全问题（专项）

**已确认覆盖完备（正面）**：
- 认证墙：61 个路由全核对，豁免仅 `/login`、`/favicon.ico`、`/csrf-token`、`/static` 前缀，无漏网端点。✓
- CSRF：27 个修改型端点 100% 挂 `_csrf_required`，装饰顺序正确，`hmac.compare_digest` 恒定时间；SameSite=Lax + 自定义头双重防线。✓
- 前端 XSS：48 处 innerHTML 逐点核毕，escHtml/escAttr 使用正确，模板无 `|safe`，`tojson` 安全，未发现可利用注入点（page-downloads.js:155 的 action label 未转义但动作枚举服务端固定）。✓
- Open redirect：`_safe_next` 拒 `//`、`\`、控制字符，无确认绕过。✓
- 会话：HttpOnly + SameSite=Lax + Secure(默认) + 7 天，签名 cookie 无 fixation 面。✓

**已确认问题**：
1. XFF 伪造链（§4-2）——含登录限流 5/min 被旋转 XFF 绕过（仅剩 1s sleep）；`/api/settings/unlock` 连 1s 延迟都没有（routes_settings.py:158-170）。CONFIRMED。
2. TLS/重定向/Cookie 链（§4-3）。CONFIRMED。
3. 密钥文件权限：`.secret_key`/`.cursor_secret` 生成无 chmod（Linux 默认 0644）；`.cursor_secret` 截断/短密钥文件**直接使用**（config.py:12-19 只处理不存在，不校验长度，与 app.py:76-84 的空文件处理不一致）。CONFIRMED。**✅ 已修复：`578c0c1`（`config._load_or_create_secret(path, min_len=32)` 统一长度校验 + `os.chmod(0o600)`，`.secret_key` 与 `.cursor_secret` 共用同一助手）**
4. settings.json 明文存放两把口令；损坏回退默认 → **已部署的登录墙静默失效**（config.py:152-166 捕获后忽略一切，`ACCESS_PASSWORD` 恢复为空）。CONFIRMED（低概率路径，安全姿态降级）。**⚠️ 仅部分缓解：`4c7c783` 把 settings.json 改为原子写（同目录 tmp → `fsync` → `os.replace`）并在损坏时留 `settings.json.corrupt.bak` —— 这让"被写坏"的概率大幅下降且有现场可查，但"损坏即回退默认值"的语义**未改**：手工改坏这个文件时登录墙仍会静默失效。明文存口令也未处理。**
5. 默认免认证 + `0.0.0.0` 绑定（§4-2）。CONFIRMED。
6. 路径遍历：`local_paths` 无远程污染途径（仅 `_download_illust` 写固定格式），当前不可达；`download_file` 文件名消毒未处理 Windows 保留名（CON/NUL）与首尾点空格（功能级）。CONFIRMED（纵深缺口）/ POSSIBLE（利用）。
7. SSRF 面：`/thumb` 白名单可靠（startswith 精确前缀），但重定向目标不校验（合并入 §4-3）；`original_urls` 下载直连无校验（纵深）。CONFIRMED（缺口）/ POSSIBLE（利用）。
8. Cookie 注入防护：`api_settings_post` 剔除 `[\r\n\t\x00-\x1f\x7f]`——完备。✓

---

## 11. 性能问题

（除标注外均为**源码推断**；AGENTS.md 已有实测的单独标注）

1. **标签搜索 min_bookmarks>0 冷查**：每页 60 条全新作品 × 详情桶 45/min → 单页最坏 ~80s、10 页 ~13min（`paginated_search` + `_process_items` 非 defer 路径）。这是当前"搜索慢"的最大单点；预算机制只给了作者搜索。建议：给标签搜索同样启用 detail 预算（或 min_bookmarks 过滤改为"列表 tags 粗筛 + 详情按需"二级策略）。
2. **预取与前台搜索共享 45/min 详情桶**：`_prefetch_one_tag` 未传 limiter（走 `_detail_limiter`），每小时数百～数千条详情与用户搜索互相拖慢。建议预取显式传 `_fill_limiter`（或独立桶）。
3. **令牌桶总闸互抢**：detail(45) + fill(20) 峰值 > total(60)，同跑时搜索掉到 ~40/min、刷新一轮拉长到 ~20min。HIGH CONFIDENCE。
4. **全表扫描集群（规模退化主因）**：
   - `api_gallery_tags`/`api_cache_tags` 的 `DISTINCT json_each` 全表（1M 行推断 2-5s）；
   - `_db_pids_cache` 每 30s 全表列扫描（1M 行 ≈1s + 40-80MB 集合尖峰；AGENTS 注明"宁可重复不可脏数据"是有意取舍）；
   - `_prefetch_capacity_cleanup` / 刷新候选查询全表 ORM 加载 + `prefetch_source` 无索引；
   - `query_cached_tag` 的排序列 `bookmark_count`/`upload_date` 无索引 → 全量排序。
   - 建议：补 `(prefetch_source)`、`(prefetch_source, prefetch_refresh_at)`、`(bookmark_count)` 索引；tags DISTINCT 物化为标签计数表或限制扫描行数。
5. **每搜索新建 executor 与线程**：跨搜索不复用 → 每页 5 次 TLS 握手 + 线程创建抖动。建议 fetcher 级常驻 `ThreadPoolExecutor`。
6. **`_refresh_bookmarks_pass` 300 条串行 + 300 次独立事务**：建议批量提交（~10 个 commit）与独立线程解耦（勿再阻塞容量清理）。
7. **`_scan_local_downloads`**：AGENTS 实测 500 目录 43.8ms（75% 在 isfile），有 30s TTL 兜底——当前规模不值得改。✓
8. **`enforce_image_cache_limit`**：1GB 缓存全目录扫描按 mtime 排序，写路径节流 5min——可接受；1M 文件量级需警惕。✓

---

## 12. 内存问题

1. **`fetcher._fill_last_attempt` 只增不删（确认的慢泄漏）**：后台补全每次尝试写时间戳，无任何删除路径；10 万 pid ≈10MB、100 万 ≈100MB。建议周期性压缩（保留最近 N 天或改 LRU）。**✅ 已修复：`ad7ad4e`（`_FILL_ATTEMPT_MAX_ENTRIES = 1000`，在既有 `_fill_lock` 段内清理远超节流窗口的条目；窗口内条目一律保留，故是"有界"而非"硬顶"）**
2. **`download_file` ZIP 整包内存**：峰值≈文件总和（不翻倍，但从 BytesIO 常驻请求线程）；建议超过阈值（如 200MB）时改用临时文件。**✅ 已修复：`70dded0`（`config.ZIP_MEMORY_THRESHOLD_BYTES = 200MB`，超阈值走临时文件 + 流式发送；阈值内仍走原内存路径，行为不变）**
3. **`_db_pids_cache` 30s 尖峰**：1M 行 ≈40-80MB 集合。可改流式分批。
4. **`_get_illust_detail` 峰值 ~2.5MB**（5 worker 并发，无问题）。✓
5. **`_thumb_failed`/`_SEARCH_CACHE`(64)/`_USER_PROFILE_CACHE`(64)/`_detail_error_samples`(20)**：全部有界或自限。✓
6. **`_search_tasks`**：终态 600s 清理 + running 泄漏（§9-1，KB 级）。✓ 可接受。
7. **`_download_progress`**：无泄漏（提前 return 路径未设置，finally 必 pop）。✓

---

## 13. 文件系统问题

1. **`_reset_stuck_downloads` 整目录删除**（§6-6，与续传矛盾，属设计决策，建议文档注明）。
2. **临时文件**：`/thumb` 原子写（唯一 tmp → `os.replace`）设计正确；`os.replace` 后 `.meta` 单独写存在半程（mimetype 回退 jpeg，可接受）。✓
3. **孤儿文件**：删除接口支持无 DB 行孤儿（已修复）；`_scan_local_downloads` 会把孤儿渲染为卡片——有专用删除路径。✓
4. **settings.json 非原子写**：`open(...,'w')` 直接覆盖，断电/崩溃 → 损坏 → 回退默认（含口令丢失、登录墙失效）。建议写临时文件 + `os.replace`。**✅ 已修复：`4c7c783`（`helpers._atomic_write_json`：同目录 `<path>.tmp` → `flush`+`fsync` → `os.replace`，失败路径清掉残留 tmp 且原文件字节不变；`api_settings_post` 与 `prefetch_config_post` 两个写入点都改用它）**
5. **磁盘满**：写入无预检，失败路径大多有 `except OSError` 兜底（`_download_illust` 失败分支会清理）；`/thumb` 写失败降级流式返回。可接受。✓
6. **路径穿越**：无远程污染途径（§10-6）。✓
7. **mtime 语义**：命中缓存不刷 mtime 保持 ETag 稳定——正确权衡。✓

---

## 14. 数据库问题

1. **外键**：仅 `CollectionItem.collection_id` 有 FK；`DownloadLog.pixiv_id`/`CollectionItem.pixiv_id` 无 FK 是**有意设计**（孤儿支持），不构成问题。✓ 删 Collection 时手动级联删 items（routes_collections.py:77）。✓
2. **唯一约束**：`illusts.pixiv_id` UNIQUE ✓；`collection_items` 复合 UNIQUE ✓；`collections.name` UNIQUE ✓；`blocked_tags.tag` UNIQUE ✓。入库冲突由 `INSERT ON CONFLICT DO NOTHING` 容忍（已修复的经典坑）。✓
3. **事务边界**：跨系统（文件/DB）两段式更新无补偿，见 §6。
4. **迁移**：
   - 幂等性：所有版本函数均按列存在性跳过。✓
   - 中途失败：`engine.begin()` 包裹，DDL 可回滚、`user_version` 不推进（有测试）。✓
   - **备份缺陷（CONFIRMED）**：`backup_database` 只 `shutil.copy2` 主库文件（migrations/runner.py:25），不 checkpoint WAL、不复制 `-wal`——若上次进程崩溃留有 WAL 数据，该备份丢失最近事务。建议备份前 `PRAGMA wal_checkpoint(TRUNCATE)` 或使用 SQLite backup API/`VACUUM INTO`。
   - **备份无保留策略**：`instance/backups/` 只增不删。
   - **降级重建路径**（SQLite<3.35 `rebuild_illusts_table`）有数据保留测试。✓
5. **并发**：WAL + busy_timeout=10s + synchronous=NORMAL 配置正确；`safe_commit` 不重试、失败即 rollback 抛出的语义与文档一致（`max_retries` 参数是死参数，误导读者）。写串行下 1M 行容量清理大 DELETE 最可能踩 10s 锁超时。建议大 DELETE/大 UPDATE 分批。
6. **连接数上界 ~14-20 常驻**：`get_session` 每线程 1 连接（SingletonThreadPool），后台线程数有限，无泄漏。✓

---

## 15. 上游 Pixiv API 风险

1. **Endpoint 变化的影响面**：`/ajax/search/illustrations`、`/ajax/illust/{id}`、`/ajax/discovery/artworks`、`/ajax/user/{id}/profile/all`、`/ajax/follow_latest/illust`、`i.pximg.net` 图片。任何 schema 变化 → 各解析点（`_parse_tags`、`_extract_original_urls`、`illustManga.data` 路径）已有宽容的 None/缺省降级，**不会崩溃但会静默空结果/缺字段**（作品可用但 original_urls 空 → 下载不可用）。失败隔离做得好，但**无 schema 校验与告警**——Pixiv 改版后用户只会看到"搜不到"。
2. **403 语义分裂**：列表端点把 403 归类为 `PixivAuthError`（fetcher.py:1088/1156/1290/1330）→ 前端显示"Cookie 已过期"；详情端把 403 当限流退避。而项目自己的注释说"并发 3 即触发 403"——**403 更可能是限流而非认证**，用户排障会被误导。建议 403 与 401 分开归类。**✅ 已修复：`dde7221`（`fetcher._warn_403`；四处列表端点 403 → warning「疑似限流/风控」+ 按既有失败形态返回空结果，401 仍抛 `PixivAuthError`；详情端 403 退避语义未动）**
3. **Cookie 失效语义**：`PixivAuthError` → 搜索任务 error(auth)→401；预取刷新只中止不标记（正确）；下载引擎不检测认证错误（图片 URL 无需 Cookie）✓；自动关注失败静默 continue——**没有主动通知"Cookie 过期"的机制**。
4. **限流覆盖**：详情/profile 双桶+总闸 ✓；**列表请求（search/discovery/follow_latest）零限流**（仅翻页间 sleep1s）——高频翻页/自动关注 10 页连点可触发 403。确认的合规漏洞。
5. **重试策略**：连接失败 fail-fast、限流 3s/9s 退避、404 不重试、401 上报——策略精细且有测试。✓（风险在"两层重试叠加"已被注释与测试锁住）。
6. **`popular_d` 排序需 Premium**：非 Premium 静默空结果——用户层面误导，建议文档提示。

---

## 16. 后台线程问题

| 线程 | 死亡方式 | 系统是否知道 | 恢复 |
|---|---|---|---|
| `_auto_follow_worker` | 外层 try/except Exception 兜底；仅 BaseException 能杀 | 无心跳/无 is_alive 检查 | 不会自动重启（`start_background_threads` 幂等守卫反而阻止重启） |
| `_prefetch_loop` 线程 | `_run` 无 try 兜底；`_prefetch_loop` 内部有 except | 无 | 同上，`running=False` 只是状态展示，线程死了没人知道 |
| 下载线程池 | worker 异常被池吞（静默） | 部分由 DB 状态可见 | `_reset_stuck_downloads` 仅重启时 |
| 搜索任务线程 | 有完整 except（除 BaseException） | 任务状态可见 ✓ | 无 |
| 后台补全 daemon | 有 try/except + finally 清 `_filling_ids` ✓ | 日志 | 依赖下次触发 |

**核心缺口**：后台线程无 supervisor / heartbeat / last_run 告警。`_prefetch_state['last_check']` 存在但没有任何"超过 X 小时未更新→告警"的消费方；线程死后 UI 仍显示一切正常（`/api/prefetch/status` 的 running 只是运行时标志）。建议：给 `_prefetch_loop` 的 `_run` 加 try 兜底 + 状态机记录 error；prefetch/auto-follow 的 last_check 在状态 API 里暴露"延迟"。

**✅ 部分修复：`df7cd94`** —— `background.get_background_health()` 保留线程引用并回答"线程还在不在"，`/api/prefetch/status` 新增 `prefetch_alive` / `auto_follow_alive` / `stale`（阈值 `2*interval+600`，`interval<=0` 恒 False）/ `last_error`（"非空"= 最近一轮就有问题）。**但没有加 supervisor 或自动重启，`_start_prefetch_thread` 内 `_run` 仍无兜底 try** —— `_prefetch_loop` 之外的异常仍能打死线程，区别只在于现在看得出来（详见 §33 遗留第 1 条）。

---

## 17. 部署与运维问题

1. `python app.py` 绑定 0.0.0.0 + 默认无认证（§4-2）。P0。
2. **`systemctl restart` 安全性**：in-flight 下载被 `download_executor.shutdown(wait=False)` 放弃，文件半写 + DB=downloading → 重启 `_reset_stuck_downloads` 清理 ✓（幂等设计正确）；WAL 保证 DB 不损坏 ✓。但**重启会丢失 queued（内存）任务且无日志**。
3. **升级**：迁移自动备份+版本化 ✓；settings.json 向后兼容 ✓；但 6.3 注释/10.7 docstring 等文档失步会误导部署排障。
4. **恢复演练**：无。备份的"可恢复性"未被任何测试/文档流程验证（尤其 WAL 缺陷 §14-4 使备份可能不完整）。
5. **`--threads 8` 与 `_thumb_sem=12` 的线程饥饿**（§5-C8）：建议 CONCURRENCY ≤ 线程数或 acquire 带超时。**✅ 已修复：`9c71903`（`runtime.THUMB_SEM_TIMEOUT`，超时返回 503 而不是无限期占住请求线程）**
6. **日志**：werkzeug 压到 WARNING 防 Cookie 泄露 ✓；应用日志无分级路由（全进 journald——可接受）。
7. **`pixiv-cleanup.sh`**：路径越界保护（realpath 校验）✓、pid 数字校验 ✓、sqlite3 并发错误被 `|| true` 静默吞掉（失败静默）；Windows 环境（当前开发机）无法直接运行 bash 脚本。

---

## 18. 灾难恢复（场景推演）

| 场景 | 发生什么 | 用户看到 | DB 状态 | runtime | 文件 | 自动恢复 | 人工干预 | 建议 |
|---|---|---|---|---|---|---|---|---|
| A. SQLite 损坏 | 查询抛 OperationalError | 大量 500/降级 | 损坏 | 不变 | 不变 | 部分（tags 降级） | 恢复备份 | 备份+定期 `PRAGMA integrity_check`；备份修复 WAL 缺陷 |
| B. 磁盘满 | 写失败 OSError | 下载 failed、缓存降级流式 | downloading 残留 | 线程活 | 半写被清理 | 部分 | reset/重启 | 下载失败分支先提交 failed（§4-4） |
| C. 进程 OOM | 杀进程 | 服务挂 | downloading/fetching 残留 | 全丢 | 半写 | 重启时重置 ✓ | 无 | 无 |
| D. kill -9 | 同 C | 服务挂 | downloading 残留 | 全丢 | 半写 | 重启时重置并**删除全部已下载页**（含完成页） | 无 | 接受（文档化）；或改"保留完成页" |
| E. Cookie 过期 | PixivAuthError | 搜索 401、预取中止、自动关注静默 | 不变 | 不变 | 不变 | 否 | 更新 cookies.txt | 状态 API 主动告警 |
| F. Pixiv 403 | 详情端退避、列表端误报 auth | "Cookie 已过期"误导 | 不变 | 退避/标记 | 不变 | 自动（退避） | 无 | 403/401 分离（§15-2，✅ `dde7221`） |
| G. Pixiv 429 | 详情退避 3s/9s、刷新熔断 3 连 | 搜索变慢 | 不变 | 熔断计数 | 不变 | 自动 ✓ | 降低并发配置 | 已完善 |
| H. 网络断开 | 连接失败 fail-fast | 搜索空结果、下载 failed | 下载 failed ✓ | 线程继续 | 清理 ✓ | 自动 | 无 | 已完善（重试收敛 10s） |
| I. 下载中断电 | killing | 见 D | downloading | 丢 | 半写 | 重启重置 | 无 | 接受 |
| J. 用户手动删文件 | DB=done 文件无 | 图库空卡、灯箱 404 | done 不变 | 不变 | 缺 | **否** | 手动处理 | 增加文件对账/重下入口提示 |
| K. settings.json 损坏 | 读取回退默认 | 密码墙消失、设置重置 | 不变 | 不变 | 损坏文件保留 | 部分（回退） | 重建 | 原子写 + 损坏时保留 .bak（✅ `4c7c783`；回退默认值的语义仍未改） |
| L. 迁移中途失败 | 事务回滚，user_version 不推进 | 启动失败/重试 | 不变 | — | 备份已生成 | 重试即可 ✓ | 可用 .bak 回退（未验证流程） | 补"恢复演练"测试 |

---

## 19. 测试缺口（按 P0-P3）

**P0**（不测=数据损坏/安全）：
1. `_download_illust` 全函数零测试（锁去重、3 个取消检查点、失败清理、finally、reset 交错、双提交、safe_commit 异常）。连"happy path 写文件+done"都没有。
2. 下载路由 6 端点 + `_cancel_download_internal` 零测试；这些 POST 端点的 CSRF 403 用例全部缺失（CSRF 矩阵只覆盖 8/27）。
3. `_auto_follow_worker`、`start_background_threads` 幂等、`_shutdown_background_threads`、`_reset_stuck_downloads` 零测试。
4. `/api/settings` 写入面（cookie 写失败 500、损坏 JSON 回退、写失败 500、控制字符剔除）零测试。
5. 迁移：partial DDL 回滚、备份恢复流程、备份失败中止、版本合法性校验——均无测试。
6. **测试隔离缺陷（CONFIRMED）**：`import config` 会向**真实** `instance/` 写 `.cursor_secret`；`import app` 写 `.secret_key`、对**真实 image_cache** 执行强制容量清理、建真实 downloads/；真实 settings.json 的覆盖（如 access_password）泄漏进整个测试进程（设了密码的机器上跑测试会全挂）。运行 `run_tests.ps1` 前请知情。**✅ 已修复：`4e49cf2`（conftest 在 `import config` **之前**强制设置 `PIXIV_INSTANCE_DIR` 到临时目录，密钥 / settings.json / pixiv.db / image_cache / 重定向发现表全部跟着走；A/B 实验——临时移走真实密钥后跑全量，旧代码会在真实 `instance/` 重新生成，修复后同一实验零写入）**

**P1**：`/thumb`（白名单 403/冷却 502/命中不刷 mtime/原子写降级）、`/api/image` 两分支、`_fetch_original_urls` 惰性路径、容量清理 tier2 created_at 3 天阈值分支（当前表达式只被 refresh_failed_at 路径覆盖）、`_prefetch_capacity_cleanup` 的 upload_date 破平局、游标 24h 过期分支、图库 json_each 损坏降级、孤儿卡片构建（`_db_pids_cache` 窗口）。

**P2**：`_page_sort_key`、`_compute_move_position` 单条目边界、`_pid_filter` 空数组、`/download_file` zip 打包、`/api/downloads` 聚合。

**P3**：`_safe_next` 更多变体、`_original_to_resized` 正则边界、`_extract_ext`。

**测试体系正面**：限流并发爆破（40 轮 Barrier）、令牌桶跨线程、搜索取消/预算线程隔离、预取刷新状态机 62 例、容量三层回归、乐观锁 rebalance——这些是"最危险代码"之外覆盖最好的部分。

---

## 20. 技术债务

1. **app 命名空间测试契约（71+ 符号）**：所有路由/后台代码必须函数体内 `import app` 延迟引用、app.py 顶部 from-import 再导出、删任何 import 前要 grep tests——这是开发者（含 AI）最容易踩的雷区，也是"改一个地方必须同步改五个地方"的典型。
2. **import 期副作用**：config.py（读 .env/settings.json/写密钥）、app.py（建目录、清缓存、`init_db`、`_reset_stuck_*`、起线程）全部在 import 时执行——测试依赖"import 前覆盖 config.DATABASE_PATH"这一脆弱时序。
3. 死代码/死参数：`safe_commit(max_retries)`、`_rebuild_illusts_table` ORM 侧包装、6.3 过时注释、10.7 docstring 矛盾、AGENTS.md 用例数过期（270 → 340）。
4. 无 linter / 类型检查 / CI：全部靠人工纪律（AGENTS 约定 + 测试兜底）。
5. `_detail_error_samples` 上限 20 种 message 的采样机制是聪明的运维折中，但依赖人工更新关键词清单（已提供观测入口，未自动化）。
6. 批量删除/批量下载/批量收藏的 API 形状不一致（`ids` vs `pixiv_ids`）。

---

## 21. 当前架构优点（不只有批评）

1. **架构-目标匹配**：单进程 + 内存状态 + SQLite WAL 对单人自部署是**正确选择**；`-w 1 --threads 8` 语义被反复文档化并严格执行。
2. **启动自愈设计**：`_reset_stuck_downloads` / `_reset_stuck_prefetch` 把"崩溃残留"变成幂等重启语义——这是项目最成熟的生命周期设计。
3. **入库冲突容忍**：`INSERT ... ON CONFLICT DO NOTHING` + 按 pid 回查赢家行，消除了并发窗口炸整批事务的经典坑。
4. **预取持久化状态机（v4）**：退避/熔断/force_done/三层淘汰/观测统计——设计深度超出同类项目平均水平，且有测试锁住。
5. **重试策略收敛**：连接 fail-fast、限流退避、404 不重试、双层叠加被注释和测试双重锁死（避免 62s 陷阱）。
6. **连接池复用约定**：get_pooled_session 线程内复用 + cookie mtime 感知重建 + 快失败重试。
7. **安全默认项**：CSP/HttpOnly/SameSite/CSRF 全覆盖/escHtml 纪律/`_safe_next`/`_get_json_body` 宽容解析——"已启用部分"质量高。
8. **前端无构建 + ES2020 上限纪律**：对 AI 维护非常友好（无转译链）。
9. **文档体系**：AGENTS/architecture/maintenance/spec 分层，仓库无 README 但文档完整度罕见。

---

## 22. 必须修复（不修=数据丢失/安全/核心故障/无法恢复）

| # | 问题 | 类型 |
|---|---|---|
| 1 | §4-1 取消竞态幻影 done（reset 改条件更新 + commit 前复查） | 数据一致性 |
| 2 | §4-4 safe_commit 异常卡 downloading（4 个提交点加失败兜底） | 核心功能 |
| 3 | §4-5 刷新未分类异常冒泡跳容量清理（宽 except / 清理移出共用 try） | 资源失控 |
| 4 | §4-2 公网部署姿态：默认没口令就报警、禁 0.0.0.0 直跑文档化、反代剥 XFF、open-dir 判定强化 | 安全 |
| 5 | §4-3 SSL_VERIFY 默认 True + /thumb 与下载 URL 重定向/主机校验 | 安全 |
| 6 | C5 下载队窗：容量清理并入 `_queued_downloads` 判定 + 缺行留痕日志 | 数据丢失 |
| 7 | §14-4 迁移备份 WAL checkpoint（备份前 `PRAGMA wal_checkpoint(TRUNCATE)` 或 backup API） | 灾难恢复 |

## 23. 建议修复（稳定性/性能/可维护性）

1. 重下/重复提交防呆：`_download_illust` 入口复查 `download_status == 'done'`（或引入 Task 唯一 ID）；重下前先删旧 local_paths 记录。
2. `prefetch_tags_delete` 复用 `_is_user_owned`（与容量清理对齐）。
3. SearchCache JSON 读改写加进程级锁（与 `_search_tasks_lock` 同款）；merge 改事务内重读+CAS。
4. `_prefetch_one_tag` except 分支状态写兜底；`_prefetch_loop` 每轮兜底重置超龄 fetching。
5. 搜索任务 running 状态加超时清理（如 30min）；`_submit_search_task` 错误分支先写 error 字段再置 status。
6. 游标 skip_count 路由层钳制（如 ≤ 500）。
7. `_thumb_sem` acquire 加超时（503 降级）或 THUMB_CONCURRENCY ≤ 线程数。
8. 补索引：`(prefetch_source)`、`(prefetch_source, prefetch_refresh_at)`、`bookmark_count`。
9. `_fill_last_attempt` 周期性压缩；`download_file` 大包改临时文件（阈值约 200MB）。
10. 预取显式用 `_fill_limiter`（不抢前台桶）；搜索任务加时间预算。
11. settings.json 原子写（tmp+replace）+ 损坏时保留 .bak；`.secret_key`/`.cursor_secret` chmod 0600 + 长度校验重生成。
12. 日志改进：403 分类分离（限流 vs 认证）；自动关注线程 try 兜底。
13. 后台线程 heartbeat：`/api/prefetch/status` 暴露 `last_check` 延迟，超阈值置 warning。
14. 测试隔离：conftest 重定向 `.cursor_secret`/`.secret_key`/settings.json/image_cache 到临时目录。

## 24. 当前可以不修

- 严格 LRU 替换 mtime 淘汰（AGENTS 权衡成立）。
- `-w 1` 扩展为多 worker 共享存储（单人场景无收益，成本高）。
- 详情 schema 校验告警（收益低，成本高——保留静默降级即可，先修 403 分类）。
- `_compute_move_position`/`_page_sort_key` 边界（有测试缺失但功能正确）。
- ZIP 文件名 Windows 保留名处理、`file_size` 删除后归零（展示级）。
- 迁移备份保留策略（先修 WAL 缺陷，加上限即可）。
- 完美 LRU、多标签并行预取、搜索缓存多级化。

---

## 25. 修复路线图

```text
P0（立即，1-2 周）
问题：取消竞态幻影 done / safe_commit 卡死 / 刷新冒泡跳清理 / 队窗静默丢 / 备份 WAL 缺陷
原因：状态迁移无 CAS、异常路径无兜底、跨系统事务无补偿
影响：数据不一致不可恢复、上限失效、下载丢失、备份不可信
修复：reset 条件更新 + commit 前复查；4 个提交点失败兜底置 failed；refresh 宽 except；
      清理并入 _queued_downloads；备份前 wal_checkpoint
文件：background.py、routes_download.py、migrations/runner.py
模块：下载引擎、预取循环、迁移
测试：test_cancel_race_reset_vs_worker_final_commit、test_download_illust_safe_commit_busy、
      test_refresh_unknown_exception_still_runs_cleanup、test_backup_checkpoints_wal、队窗用例
风险：中（改动集中在 3 个文件，均有测试锚点）
```

```text
P1（近期，1 个月内）
问题：部署安全姿态（XFF/0.0.0.0/open-dir/无密码报警）、SSL_VERIFY+重定向、
      重下毁文件、prefetch_tags_delete 保护、SearchCache 锁、thumb 线程饥饿、
      索引、_fill_last_attempt 压缩、settings 原子写、后台线程寿命
方案：见 §23
测试：下载引擎全套补测（§19 P0-1/2）、/thumb 端点测试、settings 写入面测试、
      tier2 created_at 阈值测试、测试隔离修复
```

```text
P2（中期重构，1-3 个月）
问题：app 命名空间测试契约逐步收敛（先文档化 + 校验脚本，再渐进去耦合）、
      import 期副作用收敛（config 惰性化）、_prefetch_loop 时间预算/独立刷新线程、
      搜索任务 executor 常驻化、标签搜索 detail 预算、容量清理全表扫描改分页
方案：模块内重构，保持 -w 1 语义与测试契约
```

```text
P3（长期演进）
问题：多 worker 支持（若未来要）、真实 CI（GitHub Actions + 离线测试套件）、
      Pixiv schema 校验告警层、文件对账任务（DB done ↔ 磁盘存在性）、
      SQLite 备份自动化（cron + integrity_check + 保留策略）
```

---

## 26. 大重构警告

**不建议任何形态的"重写"**：当前架构与目标匹配，全部严重问题都可以用 ≤3 个文件的局部修改解决。结构性改造只建议两处渐进式推进：测试契约去耦合与 import 副作用收敛（P2）。

## 27. AI 可维护性（本项目特殊关注点）

1. **隐式契约最危险**：`app.<符号>` 延迟导入约定违反常规直觉——AI"清理 import 死代码"会摧毁整个测试套件（仓库已发生过一次死 import 事件，靠文档与人工才修复）。
2. **禁止改动项没有机器强制**：AGENTS 里"不要改 v2 迁移"、"重试不要两处同时放开"、"入库必须 ON CONFLICT"等约束全靠模型读文档——建议固化进 CI 断言（迁移文件哈希、重试常量断言）。
3. **三处文档失步**（6.3 注释、10.7 docstring、AGENTS 270 用例数）说明"文档与代码同步"机制在细节层失效——AI 会把错误注释当规范。
4. **最容易被 AI 改坏的三个核心函数**：`_download_illust`（竞态微妙、零测试）、`_refresh_bookmarks_pass`（状态机复杂、冒泡语义关键）、`paginated_search`（cursor 步长契约、跨页去重）。**先补测试再让 AI 动它们**是这个仓库最重要的工程纪律。

---

## 28. 隐藏 Bug 核查汇总

| 类别 | 结论 |
|---|---|
| A→B→A 状态反转 | 未发现（下载 none→done 无回路；fetching→error 可重试是设计） |
| 重复执行 | 下载双提交有锁防执行但无防误报（C4）；预取抢占 UPDATE 防双跑 ✓ |
| stale state | 取消标记由旧 worker 清理的新任务可见期（D1，毫秒级）；`_scan_cache` 已按"先 data 后 ts"修复 ✓ |
| cleanup race | `_release_download_lock` 只删自己的锁 ✓；容量清理快照窗口（POSSIBLE，毫秒级） |
| resurrection | reset 后取消标记残留会静默丢弃新任务（§7-D1，概率低但路径存在） |
| phantom success | **确认存在**：取消竞态（§4-1）、重下失败删旧文件（C4） |
| phantom failure | 未发现（下载失败都有日志；`_reset_stuck_downloads` 的 failed 日志是准确语义） |
| silent failure | **确认存在**：safe_commit 异常、队窗删行、403 限流误报认证、后台线程死亡、pixiv-cleanup.sh 的 sqlite 错误被 `\|\| true` 吞掉 |

---

## 29. 方法声明

- 所有严重问题均带"文件+符号（行号）"证据与调用链；未编造任何基准数字（性能数字均为源码推断或引用 AGENTS 实测，已在文中标注）。
- SPECULATION 级发现未列入严重问题清单。
- 文档描述（AGENTS/maintenance/spec）只作背景，所有结论以源码为准；发现 3 处文档与代码失步。

---

## 30. 代码符号索引（按模块）

```text
核心状态：runtime.py（_scan_cache/_thumb_failed/_db_pids_cache/_prefetch_state/
          _queued_downloads/_download_progress/download_cancellations/download_executor/
          _search_tasks/_rate_limit_store）
下载：    background._download_illust / _reset_stuck_downloads / download_locks /
          _release_download_lock；routes_download.trigger_download / batch_download /
          _cancel_download_internal / download_file
预取：    background._prefetch_one_tag / _prefetch_loop / _prefetch_refresh_bookmarks /
          _refresh_bookmarks_pass / _prefetch_capacity_cleanup / _is_user_owned /
          reset_prefetch_refresh；routes_prefetch.*
搜索：    routes_search._submit_search_task / _cleanup_search_tasks / search /
          search_status；fetcher.paginated_search / _process_items / _fetch_details_parallel /
          search_by_tag / search_by_user / browse_discovery
安全：    middleware._require_login / _csrf_required / _rate_limit / _safe_next /
          _security_headers；routes_gallery.thumb_proxy / api_open_dir / serve_image；
          routes_settings.login_submit / settings_unlock / api_settings_post
数据：    models.Illust / SearchCache / DownloadLog / Collection / CollectionItem /
          safe_commit / init_db；migrations.runner.run_migrations / backup_database
```

---

## 31. 附：审计执行记录

- 主干源码逐模块精读：app/config/models/runtime/middleware/helpers/fetcher/background/routes_*（全部）/migrations（runner+versions）/scripts（run_tests.ps1、pixiv-cleanup.sh、sandbox_pytest_shim）/tests（12 文件全量统计与抽查）/templates+static（XSS 面抽查+48 处 innerHTML 核验）/requirements-lock。
- 并行专项审计 ×5：下载引擎并发、预取/搜索状态机（含一次 SQLAlchemy 行为实证）、安全渗透（61 路由/27 CSRF 端点/48 innerHTML 全核）、性能/内存/规模（10 项）、测试覆盖（340 用例逐文件映射）。
- 现场事实核查：git 历史（最近 40 条，多轮重构+spec/plan 回写纪律）；instance/ 实况（backups/image_cache/密钥文件存在、settings.json 内容含 proxy=http://127.0.0.1:7890）；.gitignore 覆盖 instance/cookies.txt/downloads/.env ✓。
- 上游 API 公开信息检索：未获得 2025-2026 年 Pixiv /ajax/* 变更的公开报道，相关风险按源码行为推断（§15）。

---

## 32. 最终判断

> **这个项目目前最大的三个真实风险是什么？如果我继续使用一年，最可能在哪里出问题？**

**1. 下载系统的并发状态错乱（最可能实际发生）**。`_download_illust` 的取消/重置/重复触发/队窗竞态（§4-1、§7）意味着：**"下载了但文件消失"或"状态显示 done 实际没文件"这类不一致会在你频繁使用取消、重置、批量下载功能时被真实触发**——窗口不是理论性的，而是每个多页作品下载页间间隔、每张取消点击都在经过它。修复成本低（条件更新 + 复查），不修则一年内大概率遇到至少一次"图库空卡"。

**2. 公网/局域网暴露下的安全链（一旦暴露就是灾难）**。默认免认证 + `0.0.0.0` 直跑 + `SSL_VERIFY=False` + 重定向跟随带 PHPSESSID + XFF 伪造——目前你在设置里配的 `proxy: http://127.0.0.1:7890` 本身也是明文 HTTP 链路。**只要服务对网络可达或代理不可信，你的 Pixiv 账号、浏览记录、下载内容都在风险面内**；一年内最可能的场景不是"被攻击"，而是"随手映射到公网/局域网后用手机访问一次"。

**3. 使用一年后的规模退化（不会坏，但会肉眼可见地变慢）**。预取 10000 条上限 + 下载数百作品 + 1GB 缩略图缓存的规模下，全表扫描类查询（tags 列表、容量清理、`_db_pids_cache`）开始秒级、`_fill_last_attempt` 无声吃内存、后台补全/预取刷新与搜索抢令牌桶让搜索越来越慢。**这是唯一"可预见、可提前修"的风险**：补三个索引 + 一个周期性压缩就能把退化点推迟一个数量级。

**一句话结论**：架构健康、工程纪律优秀、但下载引擎的竞态与默认安全姿态是两颗"定时炸弹"——前者会在日常使用中炸（数据不一致），后者会在暴露网络时炸（账号与数据泄露）；一年后的体验瓶颈是规模查询退化而非功能损坏。**按 P0 清单先修 7 项，这个项目就足够安全稳定地再跑一年。**

---

## 33. P1 修复台账（S8–S20，2026-09-11）

本节是**追加**的修复状态记录，不改动 §1–§32 的审计口径与评分。P0（S1–S7b）的验证记录在 `docs/superpowers/plans/2026-09-10-risk-audit-fixes.md` §七，此处只列 P1（S8–S18）中被本报告命中的发现；各步的完整方案、测试清单与遗留边界见该计划文档对应小节与 §八。§33.3 另记两条**原审计未命中、由 S18 补测跑出来**的缺陷。

| 步骤 | 修复的发现（本报告位置） | 提交 | 验证方式（测试 / 证伪结论） |
|---|---|---|---|
| S8 | §19-6 测试隔离缺陷（import `config`/`app` 会写真实 `instance/`） | `4e49cf2` | 新增 5 例（隔离断言 / 默认分支 / 覆盖分支 / 覆盖值指向文件必须 import 即失败 / conftest 顺序守卫）；**行为级证伪**：回退 4 个生产文件后 3 例失败，外加"临时移走真实密钥后跑全量 → 真实 `instance/` 零写入"的 A/B 实验 |
| S9 | §3 Top10-6 重复提交 / 重下毁旧文件、§5-C4、§28 phantom success 的 C4 半 | `2e14c66` | 新增 7 例（含 6 线程 × 8 轮 barrier 并发恰好提交一次）；证伪：回退后 6/7 失败（"删除后重下"两侧都通过，是回归守卫） |
| S10 | §6-3 SearchCache 幽灵引用（永不自愈）、§8 P-3 | `cb64ff7` | 新增 4 例；**行为级证伪**：回退 `background.py` 后幽灵引用用例实测 `illust_ids=[9202, 9201]` —— 已删 pid 被写回 |
| S11 | §5-C8 缩略图信号量饥饿、§17-5 | `9c71903` | 新增 3 例；**行为级证伪**：回退后失败在「不得用 `with` 获取信号量（没有等待上限）」 |
| S12 | §16 核心缺口（后台线程无心跳、死亡不可知） | `df7cd94` | 新增 4 例（另同步 status 字段集断言）；证伪表现为"字段缺失 / 行为未定义" —— 属**新增可观测面**，不是行为修复，证伪强度天然弱 |
| S13 | §15-2 403 语义分裂、§3 Top10-8 的 403 半、§18-F | `dde7221` | 新增 9 例；**行为级证伪（强）**：回退后**恰好**那 4 个 403 用例失败（旧代码抛 `PixivAuthError`） |
| S14 | §12-1 `_fill_last_attempt` 只增不删、§3 Top10-9 的压缩半 | `ad7ad4e` | 新增 4 例；证伪：回退后 2 例失败，另 2 例"不得放宽节流"两侧都通过 |
| S15 | §7-D4 ZIP 整包内存 + TOCTOU 500、§12-2 | `70dded0` | 新增 11 例（此前 `/download_file` 零覆盖）；证伪分两级：回退后 10 例失败但属 **seam 缺失型（证据偏弱）**，补做机制级证伪得 8 例 `assert 0 == 1` |
| S16 | §13-4 settings.json 非原子写、§18-K；§10-4 部分缓解 | `4c7c783` | 新增 11 例（含此前零覆盖的 `POST /api/settings`）；证伪：7/11 失败，其中 4 例 seam 型（偏弱）、3 例行为型（两个路由用例 `assert 200 == 500` + 配置用例"损坏文件必须留一份副本"） |
| S17 | §10-3 密钥文件权限与长度校验、§23-11 后半 | `578c0c1` | 新增 9 例（1 例 `skipif win32`）；证伪：8 例失败但 7 例 seam 型（偏弱），补做机制级证伪得 `assert 'abc' != 'abc'`、`assert 0 == 3` |
| S18 | §19 测试缺口（下载引擎/路由、`/thumb`、`/api/image`、settings 写入面、CSRF 全覆盖）、§20-3；另发现 §33.3 两条 | 见计划文档 §8.1 | 新增 55 例（含新文件 `tests/test_settings_api.py`、29 端点 CSRF 矩阵 + 静态对账）；**行为级证伪**：摘掉一个 `@_csrf_required` → 恰好对应用例失败；§33.3 两条各有失败先行的证据 |

**验证口径**：全量离线收集 **513 例（508 passed / 1 skipped / 4 failed，17.10s）**。4 例失败为**预先存在**（`tests/test_test_setup.py` 的 Windows 沙箱 PowerShell 子进程检查，P0 阶段已用 `git stash` 复现同款），与本阶段无关；1 例 skip 为 S17 的 POSIX 权限断言。P0 结束时的基线为 391 例，各步提交说明声称的新增用例合计 122（P1 的 S8–S17 为 67，S18 为 55），与实测一致。

### 33.1 明确"仍未处理"的发现（避免被误读为已修）

1. **§15-4 列表类请求零限流**（search / discovery / follow_latest）：S13 只修了 403 的**分类**，没有给列表端点补令牌桶。
2. **§10-4 的"损坏 → 回退默认值 → 登录墙静默失效"**：S16 只降低"被写坏"的概率并保留 `.bak`，回退语义未改；`settings.json` 明文存口令也未处理。
3. **§11-4 / §12-3 / §14-4**：缺索引与全表扫描、`_db_pids_cache` 30s 尖峰、备份保留策略 —— 均未动。
4. **§16 的 supervisor / 自动重启**：S12 只让线程死亡变得可观测，没有让它不可能发生（S20 把这份可观测面接到了设置页界面上，**仍然没有** supervisor）。
5. **§19 的测试缺口**：S18 已补齐三个零覆盖区（下载引擎与下载路由、`/thumb` 与 `/api/image`、settings 写入面）并加了 29 个修改型端点的 CSRF 矩阵；**仍未覆盖**：各端点的业务授权逻辑（矩阵只证明"缺头 403"）、下载引擎多 worker 抢同一 pid、前端 JS、`/api/image` 的 Range/ETag、令牌桶时序、真实旧库上的迁移升级。
6. **§20-3 `AGENTS.md` 用例数过期**（原文记 270）：已订正（同时补上此前遗漏的 `scripts/check_tls.py`、`instance/thumb_redirect_hosts.json`、`_thumb_redirect_lock` 与新增的 `tests/test_settings_api.py`）。

### 33.2 修复自身的遗留边界（影响"能不能算修好了"）

- **S10**：删除侧 `commit` 仍在锁外 → 极端情况下合并可能等锁超时而丢一次合并（不再产生幽灵引用）。
- **S12**：`_start_prefetch_thread` 内 `_run` 仍无兜底 try，线程仍可能静默死亡（只是现在看得见）。
- **S14**：清理只在表超过 1000 条时触发、节流窗口内条目一律保留 → 是"有界"而非"硬顶"。
- **S15**：临时文件清理依赖 WSGI 关闭 body 的契约（另有 EOF 自清理）；大包改占临时目录空间；`ZIP_MEMORY_THRESHOLD_BYTES` 改动需重启。
- **S16**：`SIGKILL` 落在写 tmp 与 `os.replace` 之间会残留 `settings.json.tmp`；`settings.json.corrupt.bak` 不自动清理。
- **S17**：密钥长度不足时会重新生成 → 该部署既有会话与游标失效（有意取舍）；`0600` 的 POSIX 端到端权限**未在本机证实**（Windows 上断言 skip，只验证了"确实请求了 0o600"）。

### 33.3 S18 补测发现并修复的两个缺陷（原审计未命中）

这两条是"补测试"这一步的副产品：原审计（§1–§32）没有命中它们，是本阶段新增用例跑出来的**真实生产缺陷**，修法与证伪如下。两处都是"把初始化/映射摆到正确位置"，未改任何语义分支。

1. **设置页保存预取配置从未生效**（`routes_settings.api_settings_post`）。保存 `prefetch_*` 时把 settings.json 的**长键**（`prefetch_interval` / `prefetch_pages` / `prefetch_max_illusts`）直接写进 `_prefetch_state`，而 `background` 的预取循环读的是**短键**（`interval` / `pages` / `max_illusts`）—— 等于写进三个没人读的键。`AGENTS.md` 里"prefetch_interval 经设置页保存后立即生效"的说明自 Blueprint 拆分重构起就不成立；受影响的是"改了预取间隔却要等到重启"这一交互，**不会**丢数据。修法：改用 `routes_prefetch._PREFETCH_SETTINGS_KEYS` 映射（单一来源）。**证伪**：改回旧逻辑后 `tests/test_settings_api.py::TestSettingsPost::test_prefetch_keys_apply_immediately` 失败于 `assert 0 == 321`。
2. **排队中取消会让该作品永久无法下载**（`background._download_illust`）。`session_obj = None` 位于"取消标记检查"**之后**，而该检查处有一条提前 `return`；于是 `finally` 首行抛 `UnboundLocalError`，其后所有清理（`_download_progress.pop`、`lock.release()`、`_release_download_lock`、`download_cancellations.discard`、`_queued_downloads.discard`）**全部跳过**：下载锁永远不放、取消标记永远留着，之后每次触发都在 `lock.acquire(blocking=False)` 处**静默**跳过（前端仍显示"已加入下载队列"），进度条目也永久挂在下载管理页。触发姿势很常见 —— 下载队列排队时点"取消"。修法：把两个 session 的初始化提到取消检查之前。**证伪**：把初始化放回原位后 `tests/test_download.py::test_download_cancelled_before_start_does_nothing` 失败于 `UnboundLocalError: cannot access local variable 'session_obj'`；修复后该用例另加"取消过的作品必须能重新下载"的症状级守卫。

### 33.4 S18 之后的两个跟进项（S19 / S20）

这两条是**收口阶段之后**才处理完的：S19 来自本文档之外的分析文档发现（`docs/technical-documentation.md` §20.2），S20 是 §16 那条"状态 API 主动告警"建议的落地补完。两者都不改既有语义分支，改动范围分别在"一个路径来源"与"一个响应字段 + 一行界面文案"。

1. **S19 设置页 Cookie 落点与读取路径不一致**（`routes_settings.api_settings_post`）。写侧按 `__file__` 推项目根，读侧用 `config.COOKIE_PATH`（Linux 上存在 `/etc/pixiv-viewer/cookies.txt` 时优先它）。那种部署里设置页写的是**没人读的文件**：进程内靠直接赋值 `fetcher._cookie_value` 看着生效、重启后旧 Cookie 复辟；且 `get_pooled_session()` 的失效戳盯的也是 `COOKIE_PATH`，那个 mtime 从未变过，新 Cookie **连当期都不对已缓存的连接池生效**（后一点原审计未写出来）。修法：落点改用 `app.COOKIE_PATH`（`config.COOKIE_PATH` 的再导出），写失败时 500 并把实际路径写进错误信息（此前会静默写进一个无害文件然后假装成功）。**证伪**：改回旧的 `__file__` 推导后 `tests/test_settings_api.py` 4 例失败（新用例在 `COOKIE_PATH` 处 `FileNotFoundError`、写失败用例变成 `assert 200 == 500`、夹具的"仓库根真实 cookies.txt 未被改写"断言触发）。**副产物**：`test_fetcher.py` 有 3 例走真实 `_fetch_details_parallel → build_pixiv_session() → _load_cookie()`，此前默默依赖开发者本机存在真实 `cookies.txt`（干净 checkout 上会 `FileNotFoundError`），已改用临时文件夹具；`_isolate_cookies_txt` 的收尾也从"只断言"改为"先无条件还原再断言"（旧版在一次证伪跑动中真的把仓库根的真实 Cookie 覆盖成了测试 token —— 该文件在 `.gitignore` 里，没有任何副本可恢复）。**遗留**：Cookie 写入仍非原子（`open(...,'w')`，理论上可读到半截内容后按 mtime 缓存住这个坏值）。
2. **S20 自动关注"静默停止"在界面上看不见**（§16 的"状态 API 主动告警"只做了一半）。S12 给 `background.get_background_health()` 加了 `auto_follow_alive`，但它只出现在 `/api/prefetch/status`，而设置页既不读那个键、也不读 `/api/auto-follow/status`（该路由返回 `_auto_follow_state`，**同样没有任何前端调用者**）—— 于是"自动关注线程死了"或"每轮都在失败"这两种静默失效，只能在日志里发现。修法：`/api/auto-follow/status` 增补 `alive`（取自 `get_background_health()`，与预取状态页里的同名字段**同源**，只是视角不同），设置页「自动关注」卡片新增一行状态（运行中/已停止 + **运行中的**间隔 + 上次成功检查 + 该轮新作品数）。刻意返回**副本**而非把 `alive` 塞进 `_auto_follow_state`（该 dict 由自动关注线程与 `/api/auto-follow/config` 共用，派生值会污染运行态）。界面文案按真实语义措辞：`last_check` 只在**成功拉到关注列表并处理完一轮**时更新（拉不到任何作品的那轮直接 `continue`），所以"陈旧"既可能是没有新作品、也可能是每轮都在失败 —— 不能写成"上次轮询时间"。**证伪**：① 去掉 `alive` → 4 例失败（`KeyError: 'alive'` ×3 + 运行态污染用例）；② 把 `alive` 写死成 `True` → 两个"线程已停止"用例失败于 `assert True is False`（证明它读的是真实线程引用而非常量）；③ 删掉页面加载时的调用 → 前端接线用例失败。**遗留**：仍无 supervisor / 自动重启（§33.1 第 4 条不变）；自动关注**没有** `last_error`（出错只写日志、不写 state），所以"刚失败过"与"关注列表本来没新作品"在界面上仍不可区分。