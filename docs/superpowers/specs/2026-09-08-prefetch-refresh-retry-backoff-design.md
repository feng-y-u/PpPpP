# 预取最终收藏数刷新：失败退避与永久失败清理设计

- 日期：2026-09-08
- 状态：已实现（2026-09-08，见文末「验证结果」）
- 相关代码：`background.py`、`fetcher.py`、`models.py`、`migrations/versions.py`、`config.py`、`tests/test_prefetch.py`、`tests/test_fetcher.py`

## 背景与问题（真实案例）

服务器 9/2 将 `fetch_detail_workers` 从 3 改到 5，触发 Pixiv 403 级联后，暴露了 `_prefetch_refresh_bookmarks` 的三个结构性缺陷：

1. **无限重试占坑（head-of-line blocking）**：候选 =「未刷新 + 最老优先」，每轮 100 条。永久失败的作品（作者已删除/非公開/无权限）每次都被选入、每次失败、永远留在队首 —— 100 个名额被毒区块固定占用，后面的作品每天只推进几十条。实测：队首 ~90 条死作品卡住 5713 条大墙。
2. **失败无状态**：`detail is None → continue` 静默跳过，不留任何标记，无法区分「下次值得重试」与「永远没戏」；同一批死作品每轮白耗限流与退避时间。
3. **未刷新积压顶破上限**：未刷新的作品被容量清理豁免（防误删高收藏新作）。当「永久失败 + 刷新吞吐不足」让未刷新的量**单独**超过 `prefetch_max_illusts` 时，`illusts` 行数将永久突破上限（服务器已出现 5713/10000 未刷新的倒计时）。

## 目标

为「刷新失败」建立持久化状态机（存 DB，天然抗重启），保证：

1. 队列结构性不可能再被死作品堵死 —— 失败有退避，每条最多每 24h 尝试一次；
2. 确定的死作品（404 / 删除类报错）被识别并当场出清；
3. 长期失败有最终兜底，不会无限累积顶破容量上限。

## 需求（分三层，全部落库）

### 第一层：失败退避标记（核心）

- `illusts` 新增可空列 `refresh_failed_at DATETIME`：最近一次刷新失败时间。
- 候选条件改为：

  ```
  prefetch_source=1 AND prefetch_refresh_at IS NULL
  AND created_at < now-1天
  AND (refresh_failed_at IS NULL OR refresh_failed_at < now - PREFETCH_REFRESH_BACKOFF)
  ORDER BY created_at ASC LIMIT 100
  ```

- 失败（暂时性）→ 写 `refresh_failed_at = now`；成功 → 写 `prefetch_refresh_at` 并清空 `refresh_failed_at`。
- 常量 `PREFETCH_REFRESH_BACKOFF = 86400`（秒，24h）。纯常量不进 settings.json（调优参数，非用户设置）。

### 第二层：永久失败识别与出清

- `fetcher._get_illust_detail` 新增可选参数 `return_dead: bool = False` 与模块哨兵 `DEAD_DETAIL`：
  - HTTP 404 → **立即返回、不再按一般错误重试**：`DEAD_DETAIL if return_dead else None`；
  - `error:true` 且 message 命中删除类关键词（`削除`/`删除`/`被删除`/`不存在`/`非公開`/`非公开`/`not found`/`not exist`）→ 同上；
  - 其余（403/429/5xx/连接错误/R18 权限类如「年龄确认」）→ 维持 `None`（暂时性，走第一层退避）。
  - 哨兵只在**直接调用方**（`_prefetch_refresh_bookmarks` 直调 `_get_illust_detail`）可见；`_fetch_details_parallel` 等搜索路径不传 `return_dead`，语义不变。
  - 404 对所有调用方都改为立即返回：重试 404 是纯浪费（资源已不存在）。
- 刷新遇 `DEAD_DETAIL`：
  - 未下载、未收藏 → 删除 Illust 行 + 从所有 SearchCache 摘引用（复用 `_remove_pids_from_search_caches`），记日志；
  - 已下载 / 下载中 / 已收藏 → 保留行，写 `prefetch_refresh_at` 退出队列（作品仍可浏览/下载）。

### 第三层：长期失败兜底（强制完成）

- `refresh_failed_at` 早于 `now - PREFETCH_REFRESH_FORCE_DONE` 的未刷新作品 → 直接写 `prefetch_refresh_at = now`、清空失败标记，退出刷新队列（每轮单独一遍扫描，不占候选名额）。
- 常量 `PREFETCH_REFRESH_FORCE_DONE = 14 * 86400`（秒，14 天）。
- 之后由容量清理按现有规则处理：收藏数为入库快照（多接口不带收藏数时通常为 0），超上限时**最先**被淘汰；已下载/已收藏保护不变。
- 理由：14 天 × 每日重试仍失败 ≈ 永久不可达；不兜底则「未刷新 + 豁免淘汰」无限累积，迟早顶破容量上限。

## 取舍与边界

- **关键词集合保守**：「年龄确认」等 R18 权限类 message 不在删除关键词内，按暂时性每日重试、**绝不删除** —— 用户 Cookie 升级后可自然恢复。关键词为模块级 frozenset，可按服务器日志（`Detail API error for ...` 行）实测报文微调。
- **不修改已发布迁移**：v1/v2/v3 函数与 MIGRATIONS 元组顺序不动；`refresh_failed_at` 由新 v4 迁移补列（幂等 `ALTER TABLE ADD COLUMN`），全新库由 `create_all()` 直接建全。`repair_illust_schema`（v3 及启动兜底）仍只补 v2 的列集，不扩。
- **force-done 后不再刷新收藏数**：若作品后来可刷新（如 Cookie 升级），它不再进刷新队列；但仍可浏览/下载，且只在超上限时按低收藏优先淘汰 —— 与「未刷新豁免」相比是净改进。
- **fav_ids 快照语义**：刷新循环沿用现状，循环开始前取一次收藏集合。

## 影响面

| 文件 | 改动 |
|---|---|
| `migrations/versions.py` | 新增 v4 `add_illust_refresh_failed_at`，追加进 `MIGRATIONS` |
| `models.py` | `Illust` 加 `refresh_failed_at` 列 |
| `config.py` | `PREFETCH_REFRESH_BACKOFF`、`PREFETCH_REFRESH_FORCE_DONE` 常量 |
| `fetcher.py` | `DEAD_DETAIL` 哨兵、`_is_permanently_removed_message`、`_get_illust_detail(..., return_dead=False)`、404 立即返回 |
| `background.py` | `_prefetch_refresh_bookmarks`：候选条件 + 失败写标记/成功清标记 + DEAD 分支 + force-done 扫描 |
| `tests/test_prefetch.py` | 刷新用例适配新语义 + 新增退避/DEAD/force-done 用例 |
| `tests/test_fetcher.py` | `TestDetailRetryPolicy` 新增 404/删除类用例 |

## 验收

1. 刷新失败的作品在退避期内不再入选（测试）；
2. 404 / 删除类死作品（未下载未收藏）被当场删除并同步摘除缓存引用（测试）；
3. 刷新失败满 14 天被强制标记完成（测试）；
4. 全量测试通过（`run_tests.ps1 -q`，约 270 例）；
5. 服务器部署后：迁移 v4 自动执行（升级前自动备份），新逻辑下死作品不再占坑。

## 验证结果（2026-09-08 · 第一批）

- 实现落地：迁移 v4 `add_illust_refresh_failed_at`（幂等补列）、`Illust.refresh_failed_at`、
  `config.PREFETCH_REFRESH_BACKOFF` / `PREFETCH_REFRESH_FORCE_DONE`、fetcher
  `DEAD_DETAIL` 哨兵 + `_is_permanently_removed_message` + `_get_illust_detail(return_dead=)`、
  `_prefetch_refresh_bookmarks` 三层状态机。实施细节一处调整：**aged（force-done）扫描放在
  候选查询之前**，否则本轮会把 aged 行选入候选白跑一次详情请求。
- 定向测试：`TestPrefetchRefreshBookmarks`（新增 6 例：退避标记/退避窗口/退避过期重试/
  DEAD 删除/DEAD 已下载保留/DEAD 已收藏保留/force-done）、`TestDetailRetryPolicy`
  （新增 4 例：404 立即返回、404 哨兵、删除类 message、年龄确认类不判死）、
  `test_migrations.py`（补 `refresh_failed_at` 列断言）—— 25 例全绿。
- 全量：`run_tests.ps1 -q` → **276 passed**；`test_test_setup.py` 4 例失败为环境性
  （子进程 PowerShell 被沙箱拦截），基线（stash 对比）同样失败，与本次改动无关。

## 迭代修订（2026-09-08 · 第二批：认证错误、限流熔断、文档同步）

### 动机（第一批实现后复盘出的三个缺口）

1. **认证失效会让整轮刷新中断，并连带跳过容量清理**：`_prefetch_refresh_bookmarks`
   内 `PixivAuthError` 冒泡到 `_prefetch_loop` 的兜底 → `_prefetch_capacity_cleanup()`
   不执行 → **10000 上限彻底失效**，日志只有一句笼统的「循环异常」。
2. **限流被当成单作品失败**：403/429/连接错误是账户级/环境级状态，却给每条被尝试的
   作品写 24h 退避 —— 冤枉整队列（Pixiv 恢复后还要多等一天），且每条白烧 3s+9s 退避。
3. **AGENTS.md 未同步**新状态机与迁移 v4。

### 需求

**需求 2 · 全局配置类错误不中断循环、不写退避标记**
- `_prefetch_refresh_bookmarks` 内部捕获 `fetcher.PixivAuthError`（401/认证类 message）
  与 `FileNotFoundError`（cookies.txt 缺失）：记 error 日志、**中止本轮**、**不写
  `refresh_failed_at`**（全局问题不该记在作品头上，Cookie 修好后下一轮自动继续）；
- 异常不再冒泡 → `_prefetch_loop` 的容量清理照常执行；
- `session` 关闭加 `None` 守卫（`build_pixiv_session()` 本身可能抛错）。

**需求 3 · 连续全局失败熔断**
- fetcher 新增第二个哨兵 `RETRYABLE_GLOBAL_DETAIL`：`return_dead=True` 时，限流
  （403/429 重试耗尽）与连接错误返回它；其余暂时性失败仍返回 `None`（5xx 等按
  单作品退避处理，不触发熔断）；
- 刷新循环维护连续计数：遇该哨兵 → 计数 +1、**不写退避标记**；达到
  `PREFETCH_REFRESH_ABORT_STREAK`（3）→ 记 warning、`break` 中止本轮，剩余候选
  留待下一轮；非该哨兵的失败重置计数；
- 取舍：熔断后队首最多重复尝试 2 条/轮（成本可忽略），换来 403 风暴时不再把整队列
  刷上「冤枉」的 24h 退避、也不再每轮白烧退避时间。

**需求 5 · 文档同步**
- AGENTS.md「预取缓存」补独立条目说明失败状态机（退避 / 永久失败出清 / 全局失败熔断 /
  认证失效不冒泡 / 14 天强制完成）与四个常量；
- AGENTS.md「轻量迁移系统」补 v4 `add_illust_refresh_failed_at`。

### 验证结果（第二批）

- 定向：`tests/test_prefetch.py`（+4 例：认证失效中止且不写标记、`_prefetch_loop`
  在认证失效后仍执行容量清理、连续全局失败熔断、普通失败重置连续计数）+
  `tests/test_fetcher.py::TestDetailRetryPolicy`（+3 例：403 耗尽 → 全局哨兵、
  连接错误 → 全局哨兵、5xx 仍 → None）→ 46 passed；
- 全量：`run_tests.ps1 -q` → **283 passed**，4 例环境性失败（同第一批基线）。

## 迭代修订（2026-09-08 · 第三批：可观测性与手动重置入口）

### 动机（复盘遗留缺口）

- **刷新完全不可观测**：成功/失败/删除各多少、是否熔断，只有日志能看，`/api/prefetch/status`
  里什么都没有——这次加了状态机却没有配套观测面；
- **没有任何手动重刷/重置入口**：`refresh_failed_at` 只能改库清空；被 14 天规则
  强制完成的作品，即使 Cookie 权限修好也不会再回到刷新队列。

### 需求 4 · 可观测性

- `runtime._prefetch_state` 新增固定键 `refresh_stats`（键集合仍在初始化时固定，
  之后只改值，符合无锁读约定）；
- `background._prefetch_refresh_bookmarks` 拆为「入口（统计）+ `_refresh_bookmarks_pass`
  （逻辑）」：无论正常结束、无候选提前返回还是中止，`finally` 都把整份统计写入
  `refresh_stats`，字段：`processed` / `ok` / `deleted_low` / `deleted_dead` /
  `kept_dead` / `failed_transient` / `failed_global` / `force_done` / `aborted`
  （`''` | `rate_limit` | `auth` | `cookie_missing`）/ `at`；
- `GET /api/prefetch/status` 新增 `refresh`（上一轮统计）、`pending_refresh`
  （未完成刷新的预取作品数——长期积压即"刷新吞吐跟不上入库"）、`failed_backoff`
  （退避中数量）；
- 设置页「搜索预取」卡片新增一行健康信息（未完成刷新 / 退避中 / 最近一轮明细），
  页面加载与增删标签后刷新。

### 需求 6 · 手动重置入口

- `background.reset_prefetch_refresh(tag=None, pixiv_id=None) -> int`：清空指定范围的
  `prefetch_refresh_at` 与 `refresh_failed_at`（把作品放回刷新队列），返回受影响条数；
  **必须指定范围**（tag 或 pixiv_id），否则返回 0，避免误伤全库；只作用于
  `prefetch_source=1`；标签范围用 `json_each` 下推整条 id 数组（单个绑定参数），
  不拼万级 `IN`（同 `helpers._pid_filter` 的既有约定）；
- `POST /api/prefetch/refresh-reset`（CSRF）：body `{tag}` 或 `{pixiv_id}`；
  两者皆缺 → 400，tag 不存在 → 404；经 `app.reset_prefetch_refresh` 调用（测试 seam，
  `app.py` from-import 再导出）；
- 设置页标签徽章内新增 ⟳ 按钮（`stopPropagation`，与"点击标签名删除"不冲突），
  点击确认后调用接口并提示"已重置 N 条（下一轮预取生效）"。

### 取舍

- 重置**只清标记、不立即触发刷新**：生效时机是下一轮预取（≤ `prefetch_interval`）。
  这样避免与预取线程并发跑两轮刷新（重复详情请求、互相覆盖统计）。
- 重置后能否真的成功，仍由下一轮的真实请求决定；死作品会被第二层再次清理。

### 验证结果（第三批）

- 定向：`tests/test_prefetch.py`（+2 例统计、+4 例 reset）、
  `tests/test_prefetch_api.py`（状态接口字段与健康计数、reset 的 tag/pixiv_id/400/404/403）
  → 74 passed；
- 前端：`node --check static/page-settings.js` 通过（无构建步骤，语法上限 ES2020）；
- 全量：`run_tests.ps1 -q` → **294 passed**，4 例环境性失败（同基线）。