# 缓存页页码跳转 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add total-page display and page-number jump input to the `/cache` browse page, with accurate pagination based on post-filter result count.

**Architecture:** `query_cached_tag` returns a 4th value (filtered_total). `/api/cache/items` exposes it as `filtered_total`. `templates/cache.html` displays `第 X / Y 页` and a page-jump input that computes offset = (page-1)*page_size.

**Tech Stack:** Python 3.9+ / Flask / 原生 JS / pytest

---

### Task 1: 后端 — query_cached_tag 返回 filtered_total

**Files:**
- Modify: `E:\pixiv\app.py`
- Modify: `E:\pixiv\tests\test_search_cache.py`
- Modify: `E:\pixiv\tests\test_cache_page.py`

- [ ] **Step 1: Update tests to expect 4-tuple**

In `E:\pixiv\tests\test_search_cache.py`, all `query_cached_tag` calls unpack 3 values. Update to 4-tuple. Lines to touch (by content):
- `results, has_more, next_offset = app.query_cached_tag(...)` → `results, has_more, next_offset, filtered_total = app.query_cached_tag(...)` and add `assert filtered_total == <expected>`
- `results, _, _ = app.query_cached_tag(...)` → `results, _, _, _ = app.query_cached_tag(...)`
- `results, has_more, next_offset = app.query_cached_tag(...)` for empty-result branches → add 4th unpack and `assert filtered_total == 0`

Add explicit filtered_total assertions:
- `test_min_bookmarks_and_r18`: after filtering (min_bookmarks=10, safe), `filtered_total == 1`
- `test_missing_or_not_done`: both empty branches `filtered_total == 0`
- `test_empty_ids`: `filtered_total == 0`
- `test_missing_illust_skipped`: `filtered_total == 1`
- `test_basic` (first test, offset/limit): `filtered_total == 4` (or whatever the seed total is — read the test)

Also in `E:\pixiv\tests\test_cache_page.py`, add a test:

```python
    def test_items_filtered_total(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试')
        d = r.get_json()
        assert d['total'] == 4
        assert d['filtered_total'] == 4

        r2 = client.get('/api/cache/items?tag=测试&min_bookmarks=50')
        d2 = r2.get_json()
        assert d2['filtered_total'] == 2  # 101(100) 和 103(500)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_search_cache.py tests/test_cache_page.py -v`
Expected: FAIL（TypeError: too many values to unpack）

- [ ] **Step 3: Implement in app.py**

1. Change `query_cached_tag` return type annotation to `tuple[list[dict], bool, int, int]`, docstring to `(results, has_more, next_offset, filtered_total)`.
2. All early returns `[], False, 0` → `[], False, 0, 0`.
3. Final return `[i.to_dict() for i in page], has_more, next_offset` → `[i.to_dict() for i in page], has_more, next_offset, total`.
4. In `/api/cache/items`, unpack 4-tuple and add `'filtered_total': filtered_total` to the response (keep existing `total` field).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_search_cache.py tests/test_cache_page.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_search_cache.py tests/test_cache_page.py
git commit -m "feat: expose filtered_total in cache items API"
```

---

### Task 2: 前端 — cache.html 总页数 + 页码跳转

**Files:**
- Modify: `E:\pixiv\templates\cache.html`

- [ ] **Step 1: Update pagination bar HTML**

In `#paginationBar`, after `#paginationStatus`, add the jump controls:

```html
  <span style="margin-left:8px;font-size:0.8rem;">
    跳至 <input id="pageJumpInput" type="number" min="1" style="width:56px;padding:2px 6px;"> 页
    <button class="btn btn-sm btn-soft" id="pageJumpBtn">跳转</button>
  </span>
```

- [ ] **Step 2: Update JS**

1. Add state `let cacheFilteredTotal = 0;` near `cachePageSize`.
2. In `renderCacheResults`, set `cacheFilteredTotal = data.filtered_total || 0;`.
3. Rewrite `renderCachePagination`:

```javascript
function renderCachePagination() {
  $('#paginationBar').style.display = 'block';
  const pageNum = Math.floor(currentOffset / cachePageSize) + 1;
  const totalPages = cacheFilteredTotal > 0 ? Math.ceil(cacheFilteredTotal / cachePageSize) : 1;
  $('#prevPageBtn').disabled = currentOffset === 0;
  $('#nextPageBtn').disabled = !cacheHasMore;
  $('#paginationStatus').textContent = `第 ${pageNum} / ${totalPages} 页`;
}
```

4. Add jump handlers + function:

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

- [ ] **Step 3: Verify**

```bash
pytest tests/test_app.py tests/test_cache_page.py -v
# PASS；提取 cache.html 内联 script 跑 node --check 或人工查括号配对
```

- [ ] **Step 4: Commit**

```bash
git add templates/cache.html
git commit -m "feat: show total pages and page jump input on cache page"
```

---

### Task 3: 全量验证

- [ ] **Step 1: Full suite**

Run: `pytest -q`
Expected: 全量 PASS（整合测试的 Pixiv 网络超时是 stderr 噪音）

- [ ] **Step 2: Commit docs and push**

```bash
git add docs/superpowers/specs/2026-08-13-cache-page-pagination-design.md
git commit -m "docs: cache page pagination design"
git push origin main
```
