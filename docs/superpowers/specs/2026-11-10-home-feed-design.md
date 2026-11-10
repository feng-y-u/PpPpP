# 首页：最近 48 小时热门瀑布流 设计

- 日期：2026-11-10
- 状态：已实现（2026-11-10，见文末「验证结果」）
- 相关代码：`helpers.py`、`routes_home.py`（新增）、`app.py`、`templates/home.html`（新增）、`templates/*.html`（导航）、`static/page-home.js`（新增）、`tests/test_home.py`（新增）、`tests/test_app.py`

## 背景与问题

当前打开站点第一屏就是搜索页。想"随便看看"时必须先想一个标签或画师，否则只能空查询回退到发现页（`browse_discovery`，且 `popular_d` 需要 Premium）。用户希望有一个**打开就有内容**的入口：最近 48 小时内收藏数最高的一批作品，纯图瀑布流。

## 目标

1. `/` 变成首页；搜索页迁到 `/explore`，功能不变。
2. 首页展示：最近 48 小时内、已补全收藏数的作品，按 `bookmark_count DESC` 取前 24 件。
3. 48 小时内不足 24 件时自动放宽到 7 天，并在页面上如实标注实际时间窗。
4. 纯图瀑布流：卡片里只有图；作者与名称在左下角信息牌里，悬停/聚焦浮现；触屏常显。
5. 固定隐藏 R18（无开关）；全局屏蔽标签始终生效。
6. 不发起任何 Pixiv 请求：首页只读本地库。

## 关键决策

### 为什么数据只能来自本地库

| 上游接口 | 不采用的原因 |
|---|---|
| `search/illustrations?order=popular_d` | 需 Premium，且不限定时间窗，非 Premium 静默空结果 |
| `discovery/artworks?order=popular_d` | 同上 |
| `follow_latest` | 只有已关注画师，不是全站热门 |

因此首页是**本地聚合页**：覆盖率来自预取 / 自动关注 / 手搜。这一点必须写进空状态文案，不能让人误以为"Pixiv 上就这些"。

### 只统计 `bookmark_count > 0` 的行

`bookmark_count` 为 0 有两种含义：真的没人收藏，或详情从未补全。两者无法区分，而后者会把新入库的作品全部排到末尾、甚至占满 24 条。所以只认已补全（>0）的行——候选集变小，但排序可信。

### 时间窗按 JST 墙面时间

`illusts.upload_date` 存的是 Pixiv 的 JST 墙面时间（写库时 `+09:00` 偏移被 SQLite 丢掉），所以 48 小时窗口必须用 `datetime.now(utc) + 9h` 的**朴素**时间比较。用 UTC now 直接比会整体偏 9 小时。

### 降级与"仍只看 48 小时"

默认 `window=auto`：48 小时内不足 `limit` 件就退到 168 小时，响应带 `window_hours` 让前端标注。
`window=48`：禁止降级，只返回 48 小时内的结果（页面上"仍只看 48 小时"按钮走它）。

### 布局：多列而不是 flex

搜索页的 `.masonry` 是 `flex-wrap`，一行高度由该行最高的图决定，短图下方会留空洞。首页用 `column-count: 3` 的多列布局：卡片紧贴上一张、列高自动平衡、无空洞。代价是**列优先**阅读顺序（1-8 在第一列），对"按收藏数 Top 24"可接受。

### 不做的事

- 不做分页、不做无限滚动（一屏 24 件）。
- 不加后台补全任务（二期；首页不产生额外上游请求）。
- 不加 R18 开关。
- 不把 `renderCard` 从 `page-index.js` 抽出来共享：首页卡片形态与搜索页差异太大（纯图 + 悬浮信息牌 vs 带徽章/标签/下载按钮），强行共用会让两边都变复杂。首页自带 `page-home.js` 的渲染函数。

## 接口

`GET /api/home?window=auto|48`

```json
{
  "results": [ /* Illust.to_dict() 形状 */ ],
  "window_hours": 48,
  "requested_window": "auto",
  "fell_back": false
}
```

- `results` 最多 24 条，顺序即排行。
- `window_hours` 是**实际**窗口（48 或 168），前端据此显示说明条。
- `fell_back` 只在 auto 模式真的放宽时为 true。
- 进程内缓存 60 秒（按 `window` 两种取值各一份）；屏蔽标签增删时清空，避免"改完一分钟不生效"。

## 查询

```sql
SELECT id FROM illusts
WHERE upload_date IS NOT NULL
  AND upload_date >= :since            -- JST 朴素时间
  AND bookmark_count > 0
  AND NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) WHERE value IN (:r18...))
  AND NOT EXISTS (SELECT 1 FROM json_each(illusts.tags) WHERE value IN (:blocked...))
ORDER BY bookmark_count DESC, upload_date DESC, id ASC
LIMIT 24
```

标签条件单独收集：单条损坏 JSON 会让 `json_each` 抛 `OperationalError`，此时降级只丢标签条件重查（与 `query_cached_tag` 同一套降级口径）。

不加索引：当前 1456 行全表扫描 + 排序是亚毫秒级，8000 行也在几毫秒；有 60 秒缓存兜底。

## 页面结构

- 导航：首页 `/`（激活）、搜索 `/explore`、图库、下载、缓存、设置。
- 头部：标题「最近 48 小时热门」+ 副标题「按收藏数排序 · 悬停图片查看作者与名称」+ 右侧「N 件作品 / 更新于 HH:MM」。
- 时间窗说明条：正常态是粉色底「时间窗：最近 48 小时（发布时间，日本时间）」；降级态是琥珀底并带「仍只看 48 小时」。
- 24 张纯图卡片，3 列；左下角信息牌（名称 + 作者）默认隐藏。
- 空状态：明确说明"首页不主动拉 Pixiv，需要预取或自动关注"，并给两个跳转按钮。
- 页脚一句：收藏数来自最近一次补全；未补全的不参与排序。

## 验证

- 单测覆盖：排序与 0 收藏排除、R18 恒定过滤（带参数也关不掉）、屏蔽标签、降级与严格模式、JST 边界、24 条上限、损坏 JSON 降级、`/` 与 `/explore` 两个页面。
- 手工：浏览器打开 `/` 看布局是否无空洞、悬停信息牌、触屏常显（DevTools 设备模拟）；`/explore` 搜索页回归（预览卡、灯箱、下载按钮）。

## 验证结果

- 2026-11-10 落地，`tests/test_home.py` 9 条全绿（含损坏 tags JSON 降级、60 秒缓存与 `clear_home_cache()`）。
- 全量套件 **451 passed / 2 skipped / 0 failed**，约 19s（含新增 9 条与既有 442 条）。
- 临时实例冒烟：造 5 条 48h 内作品 + 1 条 R-18（收藏数最高）→ `/api/home` 返回 5 条、R-18 不在其中、`window_hours=48`；`/` 与 `/explore` 均 200。
- 变异验证：把 `hide_r18` 固定写作 R18 条件之外（或去掉标签条件）会让 `test_r18_always_hidden_even_with_a_query_param` 变红；把缓存去掉会让 `test_cache_is_used_until_cleared` 变红。
