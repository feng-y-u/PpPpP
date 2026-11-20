# X（Twitter）标签抓图 + 跨平台同作品判定 设计

- 日期：2026-11-18
- 状态：已实现（2026-11-18，见文末「验证结果」）
- 相关代码：`pixivviewer/x_client.py`、`pixivviewer/x_dedupe.py`、`pixivviewer/routes/xfeed.py`、
  `pixivviewer/templates/x.html`、`pixivviewer/static/page-x.js`、`pixivviewer/background.py`、
  `pixivviewer/models.py`、`migrations/versions.py`（v7）

## 背景与目标

用户希望像 Pixiv 预取那样，**给一个标签就后台抓 X 上的图片**，并且因为很多画师同时
在 Pixiv 与 X 发同一作品，需要"同一作品"判定，避免重复收藏/下载。

目标：

1. 新页面 `/x`：添加标签 → 后台抓取 → 浏览/下载；与 Pixiv 图库完全隔离。
2. 三种证据强度的同作品判定（E1 链接 / E2 指纹 / H1 弱信号），**只标记不删除**。
3. 失败可解释：未配置 Cookie、Cookie 失效、限流、queryId 漂移、网络不通各有明确文案。

## 关键约束（决定了整个设计）

- X 没有免费可用的搜索 API。唯一免费路径是**网页端内部 GraphQL + 登录 Cookie**
  （`auth_token` + `ct0`），`query_id` 随发版漂移。
- 网页端限流很紧（约 15 分钟 50 次），必须保守：默认 15 次 / 15 分钟。
- `tweet_id` 是 64 位整数，超过 JS `Number` 的 53 位精度 —— 全链路按**字符串**处理。
- 只取 `pbs.twimg.com` 的 jpg/png/webp；视频/动图不在范围内。
- 服务器在境外，直连即可；`PROXY` 配置仍保留。

## 决策

### 与 Pixiv 数据完全隔离

`illusts` 的 `pixiv_id` 是唯一整型主键，下载引擎的锁/队列/进度也全按 `int` 建键。
把 X 作品塞进同一套结构需要改主键语义与所有路由，回归风险远大于收益。
因此新增 5 张表（`x_tags` / `x_works` / `artist_links` / `work_links` / `image_hashes`），
`/x` 页面、`/api/x/*` 路由、`x_download_executor` 全部独立。删掉这些表/路由就等于
关掉整个功能。

### 同作品判定：三种证据，强度分明

| 层 | 依据 | 强度 | 时机 |
|---|---|---|---|
| E1 | 推文里的 `pixiv.net/artworks/<id>` | 决定性 | 入库时 |
| E2 | dHash 汉明距离 ≤ 阈值（默认 6） | 决定性（阈值内） | 下载时 |
| H1 | 同作者 + ±48h + 张数相同 | 仅疑似 | 入库时 |

`H1` 只写 `suspected`，页面显示徽章，默认**仍然展示**，必须人工「确认/不是」。确认后写
`work_links`，同一对不再提示。任何一层都不自动删文件、不改图库。

Pixiv 的 `upload_date` 是 JST 墙面时间，tweet 时间是 UTC：H1 比较前先 `+9h`，否则整体
偏 9 小时（与首页 48 小时窗口同款坑）。

### queryId / Cookie 放实例目录

- `instance/x_api.json`：`{"query_id": "...", "features": {...}, "bearer": "..."}`，可缺省；
  按 mtime 缓存，改完不用重启进程。
- `instance/x_cookies.txt`：`auth_token=...; ct0=...`，设置页写入（原子写 + 0600），
  GET 只回「是否已配置」，永不回显值。

### E2 的 Pillow 是可选依赖

`requirements-x.txt`（`-r requirements.txt` + Pillow）。未安装时 `x_dedupe.PIL_AVAILABLE`
为 False，设置页显示「Pillow：未安装（哈希去重不可用）」，E1/H1 不受影响。

### 图片代理独立于 `/thumb`

`/ximg/<b64>` 只允许 `pbs.twimg.com`，缓存目录 `instance/x_image_cache/`（复用
`enforce_image_cache_limit(dir)` 的容量淘汰，文件名用 32 位 md5 才会被它识别），
带 30 秒失败冷却与并发槽位。不能复用 `/thumb`：那边入口白名单是 `i.pximg.net`，
把它改成 X 域名等于把 Pixiv 凭据的发送面扩大。

## 数据流

```
添加标签 → x_tags(idle) → 后台线程 trigger
  → x_client.search_timeline(tag)  (限流桶 → Cookie → GraphQL)
  → parse: 只留 photo，媒体加 ?name=orig
  → x_works 插入（tweet_id 去重）+ apply_ingest_dedupe(E1/H1)
  → 前端 /api/x/items 分页展示
  → 点下载 → x_download_executor → downloads/x_<tweet_id>/ → E2 比对 → 标记 confirmed
```

## 失败模式

| 场景 | 行为 |
|---|---|
| 未配置 Cookie | 标签 `status=error`：「未配置 X Cookie」 |
| Cookie 失效（401/403） | 本轮该标签失败，状态条提示重新填写 |
| 429 | 该标签退避到下一轮 |
| queryId 漂移（404/422/结构变化） | 「接口版本过期，更新 instance/x_api.json」 |
| 推文已删/受保护/无图 | 跳过，不报错 |
| 网络不通 | `XNetworkError`，状态条提示 |
| 未装 Pillow | E2 自动不可用，页面标注 |

## 测试策略

- 适配层：离线 fixture（`tests/fixtures/x/search_page.json`）钉解析契约、错误分类、
  tweet_id 字符串精度、`?name=orig` 归一化。
- 去重：三类证据各钉一条 + 人工确认/忽略 + 阈值 + Pillow 缺失降级 + 索引幂等。
- 路由：分页/过滤/标签 CRUD/CSRF 矩阵/下载入队/状态形状/Cookie 只写不读。
- 代理：非 `pbs.twimg.com` 403、缓存命中不联网、失败冷却。
- 迁移：v7 建表、幂等、不动 `illusts`、`tweet_id` 必须是 TEXT。

## 验证结果

- 2026-11-18：`scripts/run_tests.ps1 -q` → **538 passed / 2 skipped / 0 failed**。
- 页面冒烟（无头 Chrome，无 Cookie）：`/x` 正常渲染、状态条准确提示未配置 Cookie 与
  query_id、无控制台错误；`/settings` 的 X 卡片字段齐全。
- dHash 实测（Pillow 12.3）：纯色 → `0000000000000000`；噪声图 → 非零；缩放+重压缩后
  距离 5（默认阈值 6 内），说明阈值合适。
- 未做真实 X 抓取：需要用户自己的登录 Cookie 与有效 query_id，先用
  `python scripts/x_probe.py` 在服务器上验证。

## 提交后审查（2026-11-18 第二轮）

两个审查代理对 `4571adf` 全量复查后修复（`549 passed / 0 failed`，+11 条回归测试）：

- **Critical**：标签可永久卡死 `status='fetching'`（标记先于网络 I/O 提交、入库阶段无
  try/except、启动无重置）→ 新增 `_reset_stuck_x()`（app.py 启动调用）+ 入库阶段
  try/except 落 `error`；`queued`/`downloading` 残留同样重置为 `failed`。
- 下载重入：路由对 in-flight 状态 409、提交失败回滚 `queued`；worker 加每作品互斥锁
  （`_x_download_locks`，注销只删自己那份）；前端按钮按 saved_status 全枚举渲染。
- 布尔归一化：`x_enabled` / `x_dedupe_hash` / `auto_follow_download` 走 cookie_secure
  同款归一（`bool("false")` 是 True，直接 bool() 挡不住字符串）。
- 交互层：下载/dup 操作改为单卡原地更新（不再把翻页弹回第一屏）；轮询失败/超时有终态
  提示；状态条 enabled+interval=0 如实显示"不会抓取"；标签 chip 增加 ↻ 手动刷新
  （未启用时后端 409 如实拒绝）；卡片增加 × 删除；新增画师映射管理（GET/DELETE
  `/api/x/artist-links`，H1 的前提数据终于有 UI）。
- 其他：指纹索引加 `_index_lock`（设置页扫描与后台循环不再竞态）；`x_download_executor`
  纳入优雅关停；`/ximg` 缓存命中按扩展名定死 Content-Type（webp 在老 mimetypes 数据库
  下会错）+ 半写 tmp 清理；created_at 解析脱离 locale（固定月份表替代 %a/%b）；
  `tweet_id` 入库加 isdigit 守卫；query_id 优先级提示改为如实（x_api.json 优先）。

## 本地联调（2026-11-20）：SearchTimeline 的第三道墙 —— x-client-transaction-id

本地端到端测试把 Cookie/query_id/代理/管道全部验证通过（对照组 `TweetResultByRestId`
直接 200），唯独搜索一律**空 404**。定位过程：

1. 排除参数：用无头 Chrome（登录态）截获真实请求，拿到与线上完全一致的
   query_id/variables/features（hash `vLHjqbDv7TPFMBkIIO1nOA`，即 twscrape 同款）→
   仍然 404。注意 **features/variables 形状不匹配同样回空 404**，不是 400。
2. 排除 TLS 指纹：`curl_cffi` 伪装 Chrome 照样 404（`TweetResultByRestId` 能通）。
3. 定位真凶：真实浏览器的每个 GraphQL 请求都带 `x-client-transaction-id`
   （前端 JS 逐请求算出的防爬令牌，2026 起搜索接口强制；twscrape 主线已内置
   `xclid.py`，见 twitterapi.io 的分析）。缺失 → 空 404。

修复：可选依赖 `XClientTransaction`（PyPI，社区逆向算法）。

- `x_client._build_transaction()`：登录态（**干净浏览器头**，带 API 头会被 HTML 页
  401）抓 `x.com/home` → 从 webpack 清单取 `ondemand.s.<hash>a.js` → 构建
  ClientTransaction，带锁缓存；
- `search_timeline` 每请求生成令牌并发送；**404 时强制换新素材重试一次**（素材随
  前端发版轮换），仍 404 才报 query_id 过期；未装包时 404 报错直接指向安装命令。
- `/api/x/status` 新增 `transaction_available`，设置页/X 页如实展示。
- 实测：`#原神` 搜索 200，返回 13 条推文、游标正常、解析器原样吃下。

附带结论（写在这里防后人重蹈）：`instance/x_api.json` 现在承载三元组
`query_id + features + variables`，三者必须**同源同版**（都从真实浏览器抓），
混用社区仓库里的不同版本必 404。

## 三轮试用反馈（2026-11-20）：手办/截图、原图大小、下载按钮、卡片噪音

1. **"一堆手办、游戏截图"**：画师发画必带绘画标签（#イラスト/#絵/#創作/
   #二次創作 与 #fanart/#illustration/#art，参考 2026 年标签指南），手办照与
   截图不会带。默认 `x_query_filter` 改为要求命中任一绘画标签（X 支持括号
   OR 分组）+ 原有 filter:images/排除转发回复。真机验证：过滤后最新 40 条
   抽样 14/14 全是绘画作品（fan art/illustration/手绘过程），零手办截图。
2. **"原图比 Pixiv 小得多"**：实测同一媒体 URL 的 `name=orig` 与 `name=large`
   **字节级相同**（383334=383334），medium 明显更小——即 X 上传即重编码
   （JPEG 质量 ~70%），`orig` 已是 X 手里最大的一份，非我方下载路径问题。
   顺势优化：网格缩略图改用 `medium`（1200px、约 orig 40% 体积），下载与
   预览仍用 orig（`XWork.thumb_url` property）。
3. **"没有下载到本地按钮"**：新增 `/x/file/<id>`（对齐原生 `/download_file`
   单图直发/多图 ZIP_STORED，路径复核 downloads/x_<tweet_id> 归属）；
   挂到 /x/view 已下载态与下载管理页 X 完成行。
4. **"只需要图片和作者"**：卡片与看图页移除推文文本/日期/来源标签行，
   只留作者 + 图 + 操作；文本仍入库（E1 链接解析在入库时做，展示层不再消费），
   下载管理行标题改用作者名。

## 二轮试用反馈（2026-11-20）

试用反馈两条，均已实现（562 passed / 0 failed）：

1. **「下载后图片去哪了，图库没有」**：设计即如此（`/gallery` 只读 `illusts`
   表），但当时 X 页只有小缩略图，缺本地看图入口 —— 这是实际缺口。
   补：`/x/view/<id>` 看图页（本地图 + 元信息 + 原推/Pixiv 链接 + 删除）、
   `/api/x/image/<id>/<idx>` 图片服务（路径复核 `downloads/x_<tweet_id>`
   归属，防任意文件读取；Content-Type 按扩展名定死 + 7 天缓存）、
   X 页「只看已下载」筛选 + 已下载卡片点击进看图页。
   （`saved=undone` 过滤踩了一次 SQL 三值逻辑：`NULL != 'done'` 不为真，
   未下载（NULL）必须显式 `or_(is_(None), ...)` 并入。）
2. **「搜到很多无关的东西，只要画师作品」**：裸标签搜索的噪音主要来自
   转发/回复/无图推文。补：`x_client._apply_query_filter` 给裸标签自动追加
   `filter:images -filter:replies -filter:retweets`（设置键 `x_query_filter`
   可改可清空；查询里手写了 `filter:` 则完全不追加，让高级搜索式优先）。
   注：解析层本来就只存 photo 媒体，纯视频推文从未入库——该过滤主要省
   网络翻页与列表噪音。「真正只看特定画师」属 UserMedia 维度，未做（候选
   后续功能）。
