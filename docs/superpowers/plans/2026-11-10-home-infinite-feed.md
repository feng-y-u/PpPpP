# 首页第二屏起：缓存高收藏随机推荐 实施计划

- 日期：2026-11-10
- 对应设计：`docs/superpowers/specs/2026-11-10-home-infinite-feed-design.md`
- 状态：已实现（2026-11-10，见文末「实施结果」）

## 步骤

### 1. `helpers.py`

- 加 `HOME_RANDOM_MIN_BOOKMARKS = 500`。
- 抽出 `_home_tag_conditions(blocked) -> (tag_wheres, params)`：R18 固定 + 屏蔽标签，
  `query_home_feed` 与新函数共用。
- 加 `query_home_random(limit, min_bookmarks, exclude) -> (results, pool_total)`：
  `prefetch_source = 1`、`bookmark_count > :min_bookmarks`、原图非空、`NOT IN json_each(:exclude)`、
  `ORDER BY RANDOM() LIMIT`；损坏 tags 时降级重查。
- `query_home_feed` 行为不变，只把标签条件改为调用新助手。

### 2. `routes_home.py`

- 抽出 `_attach_medium(results)`（原图 → master1200 → `/thumb`），两个接口共用。
- `/api/home` 载荷加 `random_min_bookmarks`。
- 新增 `POST /api/home/more`，挂 `@_csrf_required`：
  - `limit` 缺省 `ITEMS_PER_PAGE`，夹到 1–60；
  - `exclude` 从 JSON body 取，只收整数，最多 5000 个；
  - 返回 `{results, min_bookmarks, has_more}`；**不写 `_home_cache`**。

### 3. `app.py`

- `/` 传 `csrf_token=_get_csrf_token()` 与 `items_per_page=ITEMS_PER_PAGE`（与图库同一约定）。

### 4. `templates/home.html`

- `<meta name="csrf-token">`、`<meta name="items-per-page">`。
- 分隔条样式 `.feed-divider`（`column-span: all` + 旧浏览器降级）。
- 页脚补"第 N 张起为缓存随机推荐"。

### 5. `static/page-home.js`

- 读页大小 meta；累计 `shownIds` / `homeItems`。
- 渲染完第一屏后挂哨兵 + `IntersectionObserver`；`maybeLoadMore()` 负责"加载完仍在视口就继续"。
- 第一批随机结果前插 `.feed-divider`；`has_more=false` 或空结果 → 结束提示并断开观察。
- 失败 → toast + 结束提示（不自动重试）。

### 6. 测试

- `tests/test_home.py` 新增 `TestHomeRandom`：池子条件、exclude 去重、R18/屏蔽、原图、has_more、空池、不写缓存、前端接线。
- `tests/test_app.py`：CSRF 矩阵加 `('POST', '/api/home/more')`。

### 7. 回写

- 本文件与设计文档标记"已实现 + 验证结果"。
- `AGENTS.md`：「API 行为」首页那条补随机推荐口径。

## 风险

| 风险 | 处置 |
|---|---|
| 多列布局追加导致已展示卡片重排跳动 | 设计已记录；不换布局 |
| `column-span: all` 在旧浏览器不支持 | 降级为普通块，功能不受影响 |
| exclude 列表无限增长 | POST body + 5000 上限 |
| 500 门槛过高导致池子空 | 如实显示"没有更多"，不静默降级 |
| 每次滚动都洗牌，同一屏刷新会变 | 预期行为（随机推荐） |

## 实施结果

按步骤全部落地，与计划的差异只有一处：

1. **`maybeLoadMore()` 在每批加载完后主动续一轮**：一屏没填满时哨兵仍在视口内，
   `IntersectionObserver` 不会再次触发，只靠观察器会卡在"永远只有一两屏"。

验证：

- `tests/test_home.py` 18 passed；全量 **460 passed / 2 skipped / 0 failed**（18s）。
- 变异验证：阈值 `>`→`>=` 与"丢掉 exclude"各让对应用例变红（详见设计文档「验证结果」）。
- `node --check static/page-home.js` 通过。
- 浏览器手工观感（分隔条横跨列、滚动加载节奏）尚未验证。

