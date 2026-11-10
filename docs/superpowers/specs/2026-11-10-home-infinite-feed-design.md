# 首页第二屏起：缓存高收藏随机推荐 设计

- 日期：2026-11-10
- 状态：已实现（2026-11-10，见文末「验证结果」）
- 相关代码：`helpers.py`、`routes_home.py`、`app.py`、`templates/home.html`、`static/page-home.js`、`tests/test_home.py`、`tests/test_app.py`

## 背景与问题

首页（`docs/superpowers/specs/2026-11-10-home-feed-design.md`）只有一屏：最近 48 小时的 24 件作品，按收藏数排序，没有分页也没有滚动加载。用户希望往下滚时继续有内容可看，且内容要"高收藏"，而不是同一天里没人收藏的作品。

## 目标

1. 第一屏 24 张**完全不变**（48h→7d 降级、收藏数降序、60 秒缓存）。
2. 第 25 张起：从**预取缓存**里**随机**抽取`bookmark_count > 500` 的作品，滚动到底自动加载。
3. 随机部分排除已展示过的作品（含第一屏），同一轮浏览不重复。
4. 池子抽空后如实显示"没有更多"，不偷偷降低阈值。
5. 页面明确标注"第 N 张起为缓存随机推荐"，不让用户误以为整页都是 48 小时热门。

## 关键决策

### 随机池：`prefetch_source = 1` 且 `bookmark_count > 500`

- `prefetch_source = 1`：只抽预取/自动关注进来的缓存作品。手搜临时入库的作品不进池 —— 用户要的是"缓存里"，手搜结果属于当次搜索的临时产物。
- `bookmark_count > 500`：严格大于（500 本身不进池），"高收藏"需要一个绝对门槛，否则随机推荐会变成随机抽垃圾。不按"Top N 名"取，避免每次请求都要全表排序。
- 沿用第一屏的三条硬过滤：排除 `R-18` / `R-18G`、排除全局屏蔽标签、`original_urls` 非空（中图地址由原图地址拼出，没有原图只能退回 250px 缩略图）。

### 随机部分不限时间窗

随机池若再加时间窗，等于只能从"最近 48 小时内且收藏数 > 500"的极少数作品里抽，第二屏会立刻抽空。随机推荐的作用是"把缓存里的好作品翻出来"。

### 随机 = 每次请求重新洗牌 + 排除已展示 pid

- `ORDER BY RANDOM() LIMIT n`：SQLite 逐行洗牌，一万行量级是毫秒级；代价是不能用 `offset` 分页（随机序每次不同，offset 会跳号/重复）。
- 跨屏去重靠前端把已展示的 `pixiv_id` 列表带回服务端，服务端用 `json_each` 下推成 `NOT IN`。第一屏的热门大概率也在池子里，**不排除必然重复**。

### 为什么用 POST 而不是 GET

`exclude` 随滚动不断变长（每屏 24 个 id），挂在 query string 上会撞反代默认 8KB 的请求头上限（nginx `large_client_header_buffers`）。所以用 `POST /api/home/more`，排除列表放 JSON body，按仓库约定挂 `@_csrf_required`。

### 不进 60 秒缓存

`/api/home` 的进程内缓存只服务第一屏。随机接口每次都要重新洗牌，缓存会让"随机"在一分钟内定住，还会让排除列表失真。

### 池子空了不降级

与第一屏"48h 不足放宽到 7d"不同，随机池**不做阈值降级**：500 门槛是用户明确要求的，静默降到 100 等于改了口径。返回空 + 前端显示"缓存里暂时没有更多高收藏作品"。

### 分隔条用 `column-span: all`

瀑布流是 `column-count: 3` 的多列布局。分隔条必须是 `column-span: all` 才能横跨三列、把前后两段分开；不支持该属性的旧浏览器里它会退化成一个普通块（出现在某一列里），功能不受影响，属可接受的降级。

已知代价：往多列容器追加节点会触发列高重排，已展示的卡片可能跳一下。这是当前布局的固有问题，本设计不换布局。

## 接口

### `POST /api/home/more`

请求（CSRF：`X-CSRF-Token` 头）：

```json
{ "exclude": [123456, 234567], "limit": 24 }
```

- `exclude`：已展示的 `pixiv_id`，最多取前 5000 个，非整数项忽略。
- `limit`：1–60，缺省 `ITEMS_PER_PAGE`。

响应：

```json
{
  "results": [ /* Illust.to_dict() + medium_url */ ],
  "min_bookmarks": 500,
  "has_more": true
}
```

- `results`：随机顺序，最多 `limit` 条。
- `has_more`：排除已展示后池子里是否还有剩余（服务端 COUNT，前端据此决定继续观察还是收尾）。

### `/api/home` 响应新增字段

`random_min_bookmarks: 500` —— 前端用它渲染"第 24 张起为缓存随机推荐（收藏数 > 500）"，避免门槛在前后端各写一份。

## 查询

```sql
SELECT id FROM illusts
WHERE prefetch_source = 1
  AND bookmark_count > :min_bookmarks
  AND COALESCE(original_urls, '') NOT IN ('', '[]')
  AND pixiv_id NOT IN (SELECT value FROM json_each(:exclude_ids))
  AND NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) WHERE value IN (:r18...))
  AND NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) WHERE value IN (:blocked...))
ORDER BY RANDOM()
LIMIT :lim
```

标签条件与第一屏共用同一个构造函数（`_home_tag_conditions`），避免两处规则漂移。损坏 tags JSON 时同样降级为"只丢标签条件"重查，不把请求打成 500。

## 前端行为

- 第一屏渲染完后，在瀑布流下方放一个 1px 高的哨兵；`IntersectionObserver`（`rootMargin: 600px`）触发加载。
- 第一批随机结果插入前，往网格里加一条 `column-span: all` 分隔条。
- 计数（`#homeCount`）随追加累加；页脚口径补一句"第 N 张起为缓存随机推荐"。
- `has_more=false` 或返回空 → 停止观察，显示结束提示。
- 请求失败 → toast + 显示"加载失败，刷新页面重试"，不自动无限重试（避免离线时每帧打请求）。

## 不做的事

- 不换瀑布流布局（不引 JS 砌墙库）。
- 不做"随机推荐"的独立页面/独立分页游标。
- 不给随机作品加收藏数徽章（保持纯图卡片的既有形态）。
- 不统计曝光/点击，不做个性化排序。

## 验证

- 单测：池子条件（prefetch_source、>500 严格、原图地址）、exclude 去重、R18/屏蔽标签、`has_more` 语义、空池不报错、随机接口不写 60 秒缓存、CSRF 矩阵收录。
- 前端接线：`page-home.js` 含哨兵与 `/api/home/more` 调用；`home.html` 有 CSRF 与页大小 meta。
- 全量套件 0 failed。

## 验证结果

- 2026-11-10 落地。`tests/test_home.py` **18 passed**（新增 6 条 `TestHomeRandom`：池子条件、exclude 跨屏去重与 `has_more`、R18/屏蔽/不限时间窗、不写缓存、脏 body 容错、前端接线）。
- 全量套件 **460 passed / 2 skipped / 0 failed**，约 18s（`/api/home/more` 已进 CSRF 矩阵，矩阵完整性用例自动收录）。
- 变异验证（改完立即还原）：
  - `bookmark_count > :min_bookmarks` 改成 `>=` → `test_pool_is_prefetch_with_strict_bookmark_threshold` 变红；
  - 路由把 `exclude` 丢掉传 `[]` → `test_exclude_prevents_duplicates_across_pages` 与 `test_bad_body_or_dirty_exclude_does_not_500` 变红。
- `node --check static/page-home.js` 通过（无构建步骤，语法错误会让整页脚本静默不执行）。
- 一次全量跑里 `test_settings_api.py::test_concurrent_reader_never_sees_a_truncated_cookie` 偶发失败（`os.replace` 的 WinError 5，Windows 下目标文件被并发读打开时的已知竞态）；单独跑与重跑全量均通过，与本次改动无关。
- 未做浏览器手工验证（无头环境）：分隔条的 `column-span: all` 与滚动加载的观感需要在真机确认。

