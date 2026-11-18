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
