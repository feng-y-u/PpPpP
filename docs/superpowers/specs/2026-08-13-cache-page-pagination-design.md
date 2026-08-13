# 缓存页页码跳转设计

- 日期：2026-08-13
- 状态：待实现
- 相关代码：`app.py`、`templates/cache.html`、`tests/test_search_cache.py`、`tests/test_cache_page.py`

## 背景与目标

缓存页数据总量已知（`SearchCache.total`），但当前分页栏只有「上一页/下一页」，用户不知道总共有几页，也无法直接跳到指定页。

目标：显示总页数（最后一页页码），并支持输入页码直接跳转。

## 需求摘要（已与用户确认）

1. 分页栏显示 `第 X / Y 页`，Y 为总页数
2. 分页栏提供页码输入框 + 跳转按钮，输入页码直接跳转到对应页
3. **总页数按「过滤后总数」计算**（`min_bookmarks` 过滤应用后的结果条数），避免页码虚高跳到空白页

## 1. 后端改动（`app.py`）

### `query_cached_tag` 返回 4 元组

签名与返回值：

```python
def query_cached_tag(tag, min_bookmarks, sort_order, tag_mode, r18_mode,
                     offset=0, limit=24) -> tuple[list[dict], bool, int, int]:
    """返回 (results, has_more, next_offset, filtered_total)"""
```

- 内部已有 `total = len(filtered)`，即过滤后总条数，作为第 4 个返回值
- 提前返回的空结果分支（无缓存行 / 空 illust_ids）改为返回 `([], False, 0, 0)`

### `/api/cache/items` 响应增加 `filtered_total`

```json
{
  "tag": "...",
  "cached_at": "...",
  "status": "done",
  "total": 180,            // 缓存原始总条数（SearchCache.total）
  "filtered_total": 30,    // 过滤后总条数（min_bookmarks/sort 应用后）
  "offset": 0,
  "page_size": 24,
  "results": [...],
  "has_more": true
}
```

调用处改为解包 4 元组：`results, has_more, _next, filtered_total = query_cached_tag(...)`

### 测试更新

`tests/test_search_cache.py` 中 `query_cached_tag` 的 9 处解包点更新为 4 元组，并新增/补充 `filtered_total` 断言：
- `test_min_bookmarks_and_r18`：设 min_bookmarks=10 后 filtered_total == 1
- 基础用例：无过滤时 filtered_total == len(all_ids)（去重后）
- 空结果分支 filtered_total == 0

`tests/test_cache_page.py` 增加：
- 响应含 `filtered_total` 字段：无过滤时 == `total`；设 min_bookmarks 后 == 过滤后条数

## 2. 前端改动（`templates/cache.html`）

### 分页栏 HTML

在 `#paginationBar` 内、`#paginationStatus` 之后加页码跳转控件：

```html
<div id="paginationBar" style="display:none;text-align:center;margin:16px 0;">
  <button class="btn btn-sm btn-soft" id="prevPageBtn" disabled>上一页</button>
  <span id="paginationStatus" style="margin:0 12px;font-size:0.8rem;"></span>
  <button class="btn btn-sm btn-soft" id="nextPageBtn" disabled>下一页</button>
  <span style="margin-left:8px;font-size:0.8rem;">
    跳至 <input id="pageJumpInput" type="number" min="1" style="width:56px;padding:2px 6px;"> 页
    <button class="btn btn-sm btn-soft" id="pageJumpBtn">跳转</button>
  </span>
</div>
```

### JS 逻辑

- 新增状态 `let cacheFilteredTotal = 0;`，每次响应后更新 `cacheFilteredTotal = data.filtered_total || 0`
- `renderCachePagination`：总页数 `totalPages = cacheFilteredTotal > 0 ? Math.ceil(cacheFilteredTotal / cachePageSize) : 1`；状态文本改为 `第 ${pageNum} / ${totalPages} 页`
- 跳转逻辑：

```javascript
$('#pageJumpBtn').addEventListener('click', jumpToPage);
$('#pageJumpInput').addEventListener('keydown', e => { if (e.key === 'Enter') jumpToPage(); });

function jumpToPage() {
  const totalPages = cacheFilteredTotal > 0 ? Math.ceil(cacheFilteredTotal / cachePageSize) : 1;
  const p = parseInt($('#pageJumpInput').value);
  if (!p || p < 1 || p > totalPages) {
    showToast(`页码需在 1-${totalPages} 之间`, true);
    return;
  }
  browseCacheWithOffset((p - 1) * cachePageSize);
}
```

- 切标签/重新浏览时页码输入框不强制清空（跳转即用）

## 3. 不做

- 不做页码按钮列（搜索页那种一串数字）——用户明确要「输入页码跳转」
- 不改变 `/search` 或预取引擎

## 4. 验证

- `pytest tests/test_search_cache.py tests/test_cache_page.py -v` 全过
- `pytest -q` 全量无回归
- cache.html 内联 JS `node --check` 语法校验
- 浏览器手测：切标签、设置 min_bookmarks、输入页码跳转、越界提示
