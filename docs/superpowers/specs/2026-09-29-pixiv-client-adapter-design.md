# Pixiv 适配层（Client/Adapter）设计

## 状态
已实现并验证：全量离线测试 **599 passed / 2 skipped**（基线 550/2，新增 49 条契约用例全绿）；
`grep '/ajax/' fetcher.py` 无输出；`fetcher` 不再直接引用任何 Pixiv payload 字段名。

验证命令：`powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q`
（沙箱下需让脚本走工作区临时根：先 `$env:TEMP=...\Temp\dsh-manual`，否则它会回退到只读的
`LOCALAPPDATA`，让 200+ 条用 `tmp_path` 的用例在 setup 阶段 `PermissionError` —— 与本次改动无关。）

## 背景与问题
本项目走 Pixiv **非官方内部 Ajax API**（`/ajax/illust/…` 等），认证是 `PHPSESSID` Cookie。
接口路径、参数名、响应信封（`error`/`message`/`body`）与 payload 字段名都由 Pixiv 单方面决定，
随时可能变。

当前 `fetcher.py`（1433 行）同时承担两件事，且**协议细节散落在业务流水线之间**：

| 协议细节 | 现状位置 |
|---|---|
| 5 个 Ajax 端点的 URL 拼装 | `search_by_tag` / `browse_discovery` / `_get_user_profile_ids` / `fetch_following` / `_get_illust_detail` **各自内联**（5 处） |
| `error`/`message`/`body` 信封判定 | 4 处重复的 `if X.get('error')` 分支 |
| 请求异常分类（401 上报认证 / 403 记限流告警） | 5 处重复 |
| payload 字段名（`illustTitle`/`userId`/`updateDate`/`thumbnails.illust`/`isLastPage`/`metaPages`…） | 分散在 `_get_illust_detail`、`_illust_from_item`、`_process_items`、`fetch_following`、`browse_discovery` |
| Cookie / Session / 连接池 / 令牌桶 | 与搜索缓存、入库、分页逻辑同处一文件 |

后果：Pixiv 改一个字段，要在业务层里逐处找；没有"接口形态"的回归网，
只能在用户报障后才发现响应结构变了。

## 目标
1. 新建独立适配层模块 `pixiv_client.py`：Ajax 端点、认证（Cookie/PHPSESSID）、
   Session 与连接池、令牌桶限流、响应信封、payload 解析**全部归它**。
2. `fetcher.py` 退化为业务层（过滤/缓存/分页/取消/预算/入库），
   路由、后台、下载、helpers **不再拼 Pixiv URL、不再认识响应字段**。
3. 用**脱敏响应样本 + 离线契约测试**把"Pixiv 改了字段/结构"变成红灯。
4. 不改功能与对外接口行为：保留 Ajax + PHPSESSID、连接池、原图地址校验、凭据分级。

## 非目标
- 不迁移 PixivPy、不引入浏览器自动化、不换认证方式。
- 不把原图**地址安全校验**搬进适配层：`helpers.check_image_url` +
  `config.IMAGE_HOST_ALLOWLIST` + 凭据分级仍留在下游（它们是"要不要发请求"的策略，
  不是"怎么跟 Pixiv 说话"）。
- 不重构业务流水线语义（过滤顺序、缓存键、游标算法、令牌桶速率一律不动）。
- 不新增第三方依赖。

## 设计

### 分层与依赖方向
```
config ──▶ pixiv_client ──▶ fetcher ──▶ background / routes_* / app
             （传输+协议）      （业务）
```
`pixiv_client` 只依赖 `config` 与 `requests`；不碰数据库、不 import `models`、
不认识 `Illust`。`fetcher` 把适配层符号**再导出**，历史调用方与既有测试补丁 seam 不变。

### 适配层公开面
- 认证：`load_cookie` / `build_pixiv_session` / `build_credentialless_session` /
  `get_pooled_session` / `reset_pooled_session` / `set_cookie_cache`
- 限流：`_TokenBucket` + `DETAIL_/FILL_/TOTAL_RATE_PER_MINUTE` + 三个桶实例
- 端点：`endpoint_illust_detail` / `endpoint_search_illustrations` /
  `endpoint_discovery_artworks` / `endpoint_user_profile_all` / `endpoint_follow_latest`
  （**Ajax 路径字符串只在这 5 个函数里出现**）
- 信封与异常分类：`envelope_error`、`handle_list_request_error`、`_warn_403`、`is_auth_error`
- 载荷解析（**字段名只在这里出现**）：`parse_tags`、`extract_original_urls`、
  `item_pixiv_id`、`item_bookmark_count`、`parse_illust_summary`、`parse_illust_detail`
- 请求（一个端点一个函数，返回规范化结果）：
  `fetch_illust_detail`、`fetch_original_urls`、`fetch_search_illusts`、
  `fetch_discovery_artworks`、`fetch_user_profile_ids`、`fetch_following_latest`

### 关键取舍
- **列表条目保持 Pixiv 原始 dict 形态在层间传递**，但任何字段访问都必须经
  `item_pixiv_id` / `item_bookmark_count` / `parse_illust_summary`。
  这样 `_process_items`（业务）不需要为字段改名而改，而字段名仍然只有一处定义。
- **Session 由业务层创建后传入适配层**（`fetch_*(session, …)`），适配层不隐式取全局 session。
  好处：连接池归属清晰，`fetcher.build_pixiv_session` 仍是可被补丁的 seam。
- **规范字段名沿用现状**（`bookmark_count` / `original_urls` / `thumb_url` …），
  即 `parse_illust_detail` 的输出与旧 `_get_illust_detail` 返回值逐键一致 —— 零行为差异。
- 详情哨兵 `DEAD_DETAIL` / `RETRYABLE_GLOBAL_DETAIL` 随详情请求一起进适配层
  （它们是"这次请求的失败形态"，不是业务概念）。

### 契约测试与样本
- `tests/fixtures/pixiv/*.json`：**脱敏**真实响应样本（5 个端点 + 1 个认证错误信封）。
  脱敏规则：`userId`/`userName`/`title`/`tags`/URL/`PHPSESSID` 全部替换为占位值，
  只保留**结构与字段名**（这正是契约测试要盯的东西）。
- `tests/test_pixiv_contract.py`（离线，不读 Cookie、不发请求）断言：
  1. 5 个端点的 URL 形状（路径 + 必要查询参数）；
  2. 每个样本都能被对应 `fetch_*` 解析出规范字段，且**信封遍历路径**与预期一致
     （`body.illust.data` / `body.thumbnails.illust` / `body.illusts` / `page.isLastPage`）；
  3. `parse_illust_detail` 的输出键集合精确等于约定集合（多/少一个键都红）；
  4. 认证类 `error:true` 报文抛 `PixivAuthError`，删除类报文返回死哨兵；
  5. 样本里"客户端要读的字段"必须真的存在 —— 字段改名会让这条断言失败。
- `scripts/pixiv_capture.py`：用真实 Cookie 抓一次响应并**按字段名脱敏**后写入
  fixtures 目录，供 Pixiv 变更后刷新样本（不是运行时依赖，也不进测试链路）。

## 风险控制
- 契约测试只断言"客户端读到的字段"，不断言 Pixiv 的完整 payload（否则 Pixiv 加字段
  就会误报）。
- 再导出的可变状态（`_cookie_value`/`_cookie_mtime`/`_total_limiter`/`_detail_error_samples`）
  **不再**从 `fetcher` 暴露：状态归适配层所有，补丁必须打在 `pixiv_client` 上，
  否则会"打了补丁却没生效"（静默失效）。既有测试引用已同步迁移。
- 样本文件不含任何真实账号信息；`scripts/pixiv_capture.py` 只写字段名与结构。

## 回归验证
全量离线测试（含新增契约测试）通过；`fetcher.py` 中不再出现 `/ajax/` 字面量与
payload 字段名（由契约测试 + 代码审查保证）。
