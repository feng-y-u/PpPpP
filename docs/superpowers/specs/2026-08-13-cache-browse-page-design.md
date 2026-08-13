# 独立缓存浏览页设计

- 日期：2026-08-13
- 状态：待实现
- 相关代码：`app.py`、`templates/index.html`、`templates/cache.html`（新建）、`templates/*.html`（导航栏）、`tests/test_search_cache.py`

## 背景与目标

上一轮实现「搜索预取缓存」时，`/search` 命中预取标签会直接返回库内缓存结果（零网络、秒回），但缓存只覆盖每标签 3 页（约 180 条）date_d 最新作品，导致命中缓存的搜索**漏掉大量未缓存作品**（收藏数 >5 过滤后可能凑不满一页）。

用户决定：**搜索永远走实时 Pixiv（全量、不漏作品）**，预取结果改为通过**独立的缓存浏览页面**查看。

## 需求摘要（已与用户确认）

1. **去掉 `/search` 缓存命中逻辑**：搜索永远实时拉取，命中预取标签也不再用缓存
2. **移除搜索页缓存徽标 + 「立即刷新」按钮**（该功能搬到缓存页）
3. **新增独立缓存浏览页**：
   - 导航栏新增「缓存」入口（与搜索/图库/批量/下载/设置平级）
   - 页面顶部选择预取标签（下拉），下方展示该标签的缓存作品网格
   - 过滤项：**最低收藏数 + 排序（date_d / popular_d）**（R18 不做，账号已屏蔽 R18，数据天然无 R18）
   - 分页浏览
   - 页面提供「立即刷新」按钮（调用现有 `/api/prefetch/refresh`）
4. **保留**：预取引擎（后台线程）、`SearchCache` 表、容量清理、`/api/prefetch/*` 管理接口、设置页标签清单管理——全部不动

## 1. 移除 `/search` 缓存命中

`app.py` 的 `search()` 路由中删除：

- 缓存命中块（`if search_type == 'tag' and query and not cursor_str:` 查 SearchCache → 提交 `_cache_fn` 返回缓存结果，约 `app.py:882-924`）
- 缓存游标翻页块（`if cursor_data and 'cache_offset' in cursor_data:` 提交 `_cache_page_fn`，约 `app.py:926-955`）

删除后 `/search` 行为与预取功能加入前完全一致：tag/user 搜索全部走 `paginated_search` + Pixiv API。

**保留** `query_cached_tag` 函数（`app.py:443`）——缓存页复用。

## 2. 新增缓存浏览页

### 路由与页面

- `GET /cache` → 渲染 `templates/cache.html`（含 csrf_token，供刷新按钮用）
- `GET /api/cache/items` → 浏览数据 API

### `/api/cache/items` 契约

```
GET /api/cache/items?tag=<标签>&min_bookmarks=<int>&sort=<date_d|popular_d>&offset=<int>
```

响应：

```json
{
  "tag": "ブルーアーカイブ",
  "cached_at": "2026-08-13T03:22:15",
  "status": "done",
  "total": 180,
  "offset": 0,
  "results": [ { ...Illust.to_dict()... } ],
  "has_more": true
}
```

行为：

- `tag` 为空 → 400 `{'error': '缺少标签参数'}`
- `SearchCache` 中无该标签 → 404 `{'error': '标签不存在'}`
- `status != 'done'` → 200 返回空 `results`（`cached_at`/`status` 如实返回，前端提示"预取中/失败"）
- 参数非法按默认值处理（与 `/search` 宽松风格一致）：`min_bookmarks` 非整数 → `0`；`sort` 非 `date_d`/`popular_d` → `date_d`；`offset` 非整数 → `0`；不返回 400（仅 `tag` 缺失/不存在返回 400/404）
- 数据获取复用 `query_cached_tag(tag, min_bookmarks, sort, 'or', 'all', offset, ITEMS_PER_PAGE)`
- `results` 缺失的 Illust 行自动跳过（`query_cached_tag` 已有此行为）

### 前端（`templates/cache.html`）

复用 index.html 的模式（`static/app.js` 的 `$`/`escHtml`/`showToast`/`renderCard` 等）：

- **顶部栏**：标签下拉（数据来自 `GET /api/prefetch/tags`，显示 `tag (状态 · 条数)`）、最低收藏数输入、排序下拉（date_d/popular_d）、「浏览」按钮、「立即刷新」按钮、缓存时间/状态标注
- **结果区**：作品卡片网格。`renderCard` 是各页面内联函数（不在 app.js 全局），缓存页需从 `templates/index.html` 复制其实现（含图片懒加载、跳详情/下载等行为）到 cache.html 的内联 script
- **分页**：复用 index.html 的分页模式（上一页/下一页/页码跳转，offset 递增）
- **刷新**：POST `/api/prefetch/refresh`（带 CSRF），成功后提示"已开始刷新"，可稍后重新浏览
- **空状态**：无缓存数据/无标签时显示提示
- **导航栏**：`templates/cache.html` 的 navbar 加 `<a class="navbar-link" href="/cache">缓存</a>`；**同时**在 index/gallery/bulk/downloads/settings 各模板的 navbar 同样加入（保持全站导航一致）

## 3. 移除搜索页缓存元素

`templates/index.html` 删除：

- `renderCacheBadge(data)` 函数及 `finishSearch` 中的调用
- `refreshCache()` 函数
- `resetPagination` 中的 `renderCacheBadge(null)` 调用

## 4. 测试

- **删除**：`tests/test_search_cache.py` 中 `test_search_route_cache_hit`、`test_search_route_cache_cursor_pagination`（命中逻辑已移除）；`test_search_route_cache_miss_still_async` 保留但语义变为"普通搜索仍异步"
- **保留**：`query_cached_tag` 的单元测试（`test_query_cached_tag_*`）——函数保留
- **新增** `tests/test_cache_page.py`（或并入 test_search_cache.py）：
  - `/api/cache/items` 正常返回（过滤、排序、分页、has_more/offset）
  - `tag` 缺失 → 400；标签不存在 → 404
  - `status != 'done'`（fetching/error）→ 200 空 results + 状态字段如实返回
  - `GET /cache` 页面渲染 200
  - `/search` 不再命中缓存：预取标签搜索走实时（mock `search_by_tag` 断言被调用）

## 5. 文档更新

- `AGENTS.md`：「API 行为」的搜索预取缓存 bullet 更新——搜索不再命中缓存，改为独立缓存页浏览；「架构」表格加 `templates/cache.html`（或注明 9 个模板）

## 边界 / 明确不做

- 不删 `query_cached_tag`、不删预取引擎与管理 API
- 缓存页不做 R18 过滤、不做标签多选、不做收藏夹联动
- 不做"缓存+实时混合"（用户已明确搜索走实时）
