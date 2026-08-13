# 独立缓存浏览页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the search-cache-hit behavior (search returns to always-live Pixiv) and add a standalone `/cache` browse page for prefetched tag data.

**Architecture:** `/search` drops its two cache-hit blocks and returns to pure live fetching. A new `GET /cache` page renders `templates/cache.html` whose inline JS drives a new `GET /api/cache/items` endpoint that reuses the existing `query_cached_tag()` for in-DB filter/sort/pagination. Prefetch engine, `SearchCache` table, `/api/prefetch/*` endpoints, and settings-page tag management stay untouched.

**Tech Stack:** Python 3.9+ / Flask / SQLAlchemy 2.0 / SQLite (WAL) / 原生 JS / pytest

---

### Task 1: 新增 `/api/cache/items` API

**Files:**
- Modify: `E:\pixiv\app.py`
- Test: `E:\pixiv\tests\test_cache_page.py`（新建）

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cache_page.py
from datetime import datetime, timezone
from models import SearchCache, Illust, safe_commit


class TestCacheItemsApi:
    def _seed(self, db, tag='测试', status='done', ids=None):
        if ids is None:
            ids = [101, 102, 103, 104]
        db.add(SearchCache(tag=tag, illust_ids=str(ids), status=status,
                           cached_at=datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc), total=len(ids)))
        for pid, bm, d in [(101, 100, '2026-08-10'), (102, 10, '2026-08-12'), (103, 500, '2026-08-01'), (104, 1, '2026-08-11')]:
            i = Illust(pixiv_id=pid, title=f't{pid}', bookmark_count=bm,
                       upload_date=datetime.fromisoformat(d + 'T00:00:00'), thumb_url='https://i.pximg.net/x.jpg')
            i.tags_list = ['tag']
            db.add(i)
        safe_commit(db)

    def test_items_basic(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试')
        assert r.status_code == 200
        data = r.get_json()
        assert data['tag'] == '测试'
        assert data['status'] == 'done'
        assert data['total'] == 4
        assert data['page_size'] == 24
        assert len(data['results']) == 4
        assert data['has_more'] is False

    def test_items_min_bookmarks_filter(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试&min_bookmarks=50')
        data = r.get_json()
        ids = {x['pixiv_id'] for x in data['results']}
        assert ids == {101, 103}

    def test_items_popular_sort(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试&sort=popular_d')
        data = r.get_json()
        bookmarks = [x['bookmark_count'] for x in data['results']]
        assert bookmarks == sorted(bookmarks, reverse=True)
        assert bookmarks[0] == 500

    def test_items_pagination(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试&offset=2')
        data = r.get_json()
        assert len(data['results']) == 2
        assert data['offset'] == 2
        assert data['has_more'] is False

    def test_items_missing_tag_param(self, clean_db, client):
        r = client.get('/api/cache/items')
        assert r.status_code == 400

    def test_items_unknown_tag(self, clean_db, client):
        r = client.get('/api/cache/items?tag=不存在')
        assert r.status_code == 404

    def test_items_not_done_status(self, clean_db, client):
        self._seed(clean_db, status='fetching')
        r = client.get('/api/cache/items?tag=测试')
        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'fetching'
        assert data['results'] == []

    def test_items_invalid_params_use_defaults(self, clean_db, client):
        self._seed(clean_db)
        r = client.get('/api/cache/items?tag=测试&min_bookmarks=abc&sort=xxx&offset=yyy')
        assert r.status_code == 200
        data = r.get_json()
        assert len(data['results']) == 4  # 默认 min_bookmarks=0 不过滤，offset=0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cache_page.py -v`
Expected: FAIL（404 Not Found，端点不存在）

- [ ] **Step 3: Implement the endpoint**

在 `app.py` 的 `/api/search/status/<task_id>` 路由之后添加：

```python
@app.route('/api/cache/items')
def cache_items() -> Response:
    """浏览预取缓存：库内过滤/排序/分页，不请求 Pixiv。"""
    tag = request.args.get('tag', '').strip()
    if not tag:
        return jsonify({'error': '缺少标签参数'}), 400

    try:
        min_bookmarks = max(0, int(request.args.get('min_bookmarks', '0') or 0))
    except (ValueError, TypeError):
        min_bookmarks = 0

    sort_order = request.args.get('sort', 'date_d')
    if sort_order not in ('popular_d', 'date_d'):
        sort_order = 'date_d'

    try:
        offset = max(0, int(request.args.get('offset', '0') or 0))
    except (ValueError, TypeError):
        offset = 0

    with get_session() as db:
        sc = db.query(SearchCache).filter(SearchCache.tag == tag).first()
        if not sc:
            return jsonify({'error': '标签不存在'}), 404
        cached_at = sc.cached_at.isoformat() if sc.cached_at else None
        sc_status = sc.status
        sc_total = sc.total

    results, has_more, _next = query_cached_tag(
        tag, min_bookmarks, sort_order, 'or', 'all',
        offset=offset, limit=ITEMS_PER_PAGE,
    )
    return jsonify({
        'tag': tag,
        'cached_at': cached_at,
        'status': sc_status,
        'total': sc_total,
        'offset': offset,
        'page_size': ITEMS_PER_PAGE,
        'results': results,
        'has_more': has_more,
    })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cache_page.py -v`
Expected: PASS（8 个测试）

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_cache_page.py
git commit -m "feat: add /api/cache/items endpoint for cache browsing"
```

---

### Task 2: 移除 `/search` 缓存命中逻辑

**Files:**
- Modify: `E:\pixiv\app.py`
- Modify: `E:\pixiv\tests\test_search_cache.py`

- [ ] **Step 1: Update tests to the new expected behavior**

在 `tests/test_search_cache.py` 中：

1. **删除** `test_search_route_cache_hit`（命中逻辑已移除）
2. **删除** `test_search_route_cache_cursor_pagination`（缓存游标已移除）
3. **改写** `test_search_route_cache_miss_still_async` → 断言改为"预取标签搜索仍走实时（不查缓存）"：mock `app.search_by_tag`，对已 `status='done'` 的预取标签发起 `/search`，断言 `app.search_by_tag` 被调用（证明没命中缓存）。保留对 `task_id` 返回的断言。

```python
    def test_search_route_always_live_even_for_cached_tag(self, clean_db, client, monkeypatch):
        from models import SearchCache
        clean_db.add(SearchCache(tag='缓存标签', status='done', illust_ids='[1,2,3]'))
        safe_commit(clean_db)

        called = []
        def fake_search_by_tag(*args, **kwargs):
            called.append(1)
            return [], False
        monkeypatch.setattr('app.search_by_tag', fake_search_by_tag)

        r = client.get('/search?type=tag&query=缓存标签')
        assert r.status_code == 200
        assert 'task_id' in r.get_json()
        assert called, '预取标签搜索必须走实时 search_by_tag'
```

保留 `query_cached_tag` 的所有单元测试（函数保留）。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_search_cache.py -v`
Expected: FAIL（删除的测试不存在 → 收集错误；或新测试因命中缓存不走 search_by_tag 而失败。若收集报错，先按 Step 3 删除代码块再一起验证）

- [ ] **Step 3: Remove the two cache blocks from `search()`**

在 `app.py` 的 `search()` 路由中删除：

1. **缓存命中块**：从注释 `# 命中预取缓存：返回库内结果，不请求 Pixiv`（约 `app.py:882`）到该 if 块结束（约 `app.py:924`，`return jsonify({'task_id': task_id, 'status': 'running'})` 之后的空行）
2. **缓存游标翻页块**：从注释 `# 缓存游标翻页`（约 `app.py:926`）到约 `app.py:954`（`return jsonify(...)` 之后）

删除后，`search()` 中 cursor 恢复参数之后直接进入 `# 组装后台执行闭包`。确认 `_cache_fn`/`_cache_page_fn` 闭包与 `cached_at` 变量定义一并删除，不留死代码。

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_search_cache.py tests/test_app.py tests/test_prefetch.py tests/test_prefetch_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_search_cache.py
git commit -m "refactor: remove /search cache-hit behavior, search always live"
```

---

### Task 3: 新增 `/cache` 页面路由与 `templates/cache.html`

**Files:**
- Modify: `E:\pixiv\app.py`
- Create: `E:\pixiv\templates\cache.html`

- [ ] **Step 1: Add the page route**

在 `app.py` 的 `/gallery` 路由附近添加：

```python
@app.route('/cache')
def cache_page() -> str:
    """缓存浏览页：查看预取标签的缓存结果。"""
    return render_template('cache.html', csrf_token=_get_csrf_token())
```

检查 `app.py` 顶部是否已导入 `render_template`（其他页面路由在用，应当已导入）；`_get_csrf_token()` 已存在。

- [ ] **Step 2: Create `templates/cache.html`**

以 `templates/index.html` 为蓝本创建（导航栏、full-page 容器、masonryGrid 网格、script 尾部结构一致）。包含：

**结构**（head/nav 与 index.html 相同，nav 加 active 到缓存链接）：

```html
<nav class="navbar">
  <div class="navbar-inner">
    <a class="navbar-brand" href="/">
      <span class="navbar-brand-dot"></span>
      PpPpP
    </a>
    <button class="nav-toggle" aria-label="菜单" onclick="this.nextElementSibling.classList.toggle('open')">☰</button>
    <div class="navbar-links">
      <a class="navbar-link" href="/">搜索</a>
      <a class="navbar-link" href="/gallery">图库</a>
      <a class="navbar-link" href="/bulk">批量</a>
      <a class="navbar-link" href="/downloads">下载</a>
      <a class="navbar-link active" href="/cache">缓存</a>
      <a class="navbar-link" href="/settings">设置</a>
    </div>
  </div>
</nav>
```

**页面主体**（在 full-page 容器内）：

```html
<div class="full-page">
<div class="container mt-3">
  <div class="d-flex align-items-center gap-2 flex-wrap mb-3">
    <select class="form-select form-select-sm" id="cacheTagSelect" style="max-width:220px;"></select>
    <input class="form-control form-control-sm" id="cacheMinBookmarks" type="number" min="0" placeholder="最低收藏数" style="max-width:120px;">
    <select class="form-select form-select-sm" id="cacheSortOrder" style="max-width:140px;">
      <option value="date_d">最新发布</option>
      <option value="popular_d">人气（收藏数）</option>
    </select>
    <button class="btn btn-primary btn-sm" id="cacheBrowseBtn">浏览</button>
    <button class="btn btn-soft btn-sm" id="cacheRefreshBtn" disabled>刷新</button>
  </div>
  <div id="cacheMeta" style="font-size:0.75rem;color:var(--text-muted);margin-bottom:8px;"></div>
  <div id="masonryGrid" class="masonry-grid"></div>
  <div id="emptyState" style="display:none;text-align:center;color:var(--text-muted);padding:40px 0;">暂无缓存数据</div>
  <div id="paginationBar" style="display:none;text-align:center;margin:16px 0;">
    <button class="btn btn-sm btn-soft" id="prevPageBtn" disabled>上一页</button>
    <span id="paginationStatus" style="margin:0 12px;font-size:0.8rem;"></span>
    <button class="btn btn-sm btn-soft" id="nextPageBtn" disabled>下一页</button>
  </div>
</div>
</div>
```

**内联 script**（`<script src="/static/app.js"></script>` 之后）：

- `const csrfToken = {{ csrf_token | tojson }};`
- 从 `templates/index.html` **复制**这些函数（原样，2 空格缩进）：`renderCard`（index.html:835 起，含 tag 点击搜索/屏蔽、卡片点击跳详情、画师点击、下载按钮事件绑定，完整复制到函数结束）、`proxyThumb`、`fmtNum`、`lazyLoad`、`escHtml`/`escAttr`/`$`（若 app.js 已全局提供则跳过——app.js 提供 `$`、`escHtml`、`escAttr`、`showToast`；`proxyThumb`、`fmtNum`、`lazyLoad`、`renderCard` 在 index.html 内联，需复制）
- 复制 `lazyLoad` 及其 IntersectionObserver 初始化和 `window.addEventListener('scroll', lazyLoad)` 调用（在 index.html 找到对应代码复制）
- 新逻辑：

```javascript
let cacheTags = [];
let currentOffset = 0;
let cacheHasMore = false;
let cachePageSize = 24;

async function loadCacheTags() {
  try {
    cacheTags = await fetch('/api/prefetch/tags').then(r => r.json());
    const sel = $('#cacheTagSelect');
    sel.innerHTML = cacheTags.length
      ? cacheTags.map(t => `<option value="${escAttr(t.tag)}">${escHtml(t.tag)}（${escHtml(t.status)} · ${t.total || 0}条）</option>`).join('')
      : '<option value="">暂无预取标签</option>';
    sel.value = cacheTags[0] ? cacheTags[0].tag : '';
    $('#cacheRefreshBtn').disabled = !cacheTags.length;
    updateCacheMeta();
  } catch { $('#cacheTagSelect').innerHTML = '<option value="">加载失败</option>'; }
}

function updateCacheMeta() {
  const tag = $('#cacheTagSelect').value;
  const t = cacheTags.find(x => x.tag === tag);
  const el = $('#cacheMeta');
  if (!t) { el.textContent = ''; return; }
  el.textContent = `状态: ${t.status} · 缓存时间: ${t.cached_at ? new Date(t.cached_at).toLocaleString('zh-CN') : '从未'} · 条数: ${t.total || 0}`;
  if (t.status === 'error') el.textContent += ` · 错误: ${t.error || '未知'}`;
}

async function browseCache() {
  const tag = $('#cacheTagSelect').value;
  if (!tag) return;
  const minBookmarks = parseInt($('#cacheMinBookmarks').value) || 0;
  const sort = $('#cacheSortOrder').value;
  const resp = await fetch(`/api/cache/items?tag=${encodeURIComponent(tag)}&min_bookmarks=${minBookmarks}&sort=${sort}&offset=0`);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    showToast(err.error || '加载失败', true);
    return;
  }
  const data = await resp.json();
  cacheHasMore = data.has_more;
  currentOffset = data.offset;
  renderCacheResults(data);
}

function renderCacheResults(data) {
  cachePageSize = data.page_size || cachePageSize;
  $('#masonryGrid').innerHTML = '';
  if (!data.results.length) {
    $('#emptyState').style.display = 'block';
    $('#paginationBar').style.display = 'none';
    return;
  }
  $('#emptyState').style.display = 'none';
  data.results.forEach(r => renderCard(r));
  lazyLoad();
  updateCacheMeta();
  renderCachePagination();
}

function renderCachePagination() {
  $('#paginationBar').style.display = 'block';
  const pageNum = Math.floor(currentOffset / cachePageSize) + 1;
  $('#prevPageBtn').disabled = currentOffset === 0;
  $('#nextPageBtn').disabled = !cacheHasMore;
  $('#paginationStatus').textContent = `第 ${pageNum} 页`;
}

$('#cacheBrowseBtn').addEventListener('click', browseCache);
$('#cacheTagSelect').addEventListener('change', () => { updateCacheMeta(); browseCache(); });
$('#prevPageBtn').addEventListener('click', async () => {
  if (currentOffset === 0) return;
  await browseCacheWithOffset(Math.max(0, currentOffset - cachePageSize));
});
$('#nextPageBtn').addEventListener('click', async () => {
  if (!cacheHasMore) return;
  await browseCacheWithOffset(currentOffset + cachePageSize);
});
async function browseCacheWithOffset(offset) {
  const tag = $('#cacheTagSelect').value;
  const minBookmarks = parseInt($('#cacheMinBookmarks').value) || 0;
  const sort = $('#cacheSortOrder').value;
  const resp = await fetch(`/api/cache/items?tag=${encodeURIComponent(tag)}&min_bookmarks=${minBookmarks}&sort=${sort}&offset=${offset}`);
  if (!resp.ok) { showToast('加载失败', true); return; }
  const data = await resp.json();
  currentOffset = data.offset;
  cacheHasMore = data.has_more;
  renderCacheResults(data);
}
$('#cacheRefreshBtn').addEventListener('click', async () => {
  const tag = $('#cacheTagSelect').value;
  if (!tag) return;
  const btn = $('#cacheRefreshBtn');
  btn.disabled = true;
  btn.textContent = '刷新中...';
  try {
    const resp = await fetch('/api/prefetch/refresh', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
      body: JSON.stringify({tag}),
    });
    const err = await resp.json().catch(() => ({}));
    if (resp.ok) { showToast('已开始刷新缓存'); loadCacheTags(); }
    else { showToast(err.error || '刷新失败', true); }
  } catch { showToast('网络错误', true); }
  finally {
    btn.disabled = false;
    btn.textContent = '刷新';
  }
});

loadCacheTags();
```

注意：翻页步长以后端响应里的 `page_size` 字段为准（Task 1 已返回），前端不要硬编码 24。前端代码中所有翻页步长引用 `cachePageSize` 变量（见下方代码，`renderCacheResults` 里每次响应后更新 `cachePageSize = data.page_size`），`renderCachePagination` 与 prev/next 处理器均使用它。

- [ ] **Step 3: Verify**

```bash
pytest tests/test_app.py tests/test_cache_page.py -v
# 预期 PASS（含 GET /cache 200 渲染测试——在 test_cache_page.py 中补一条：）
```

补充测试（加入 `tests/test_cache_page.py`）：

```python
    def test_cache_page_renders(self, client):
        r = client.get('/cache')
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert 'cacheTagSelect' in html
        assert '缓存' in html
```

再执行 `node --check` 校验提取的内联 script 语法（若服务器有 node；无则仔细人工检查括号配对）。

- [ ] **Step 4: Commit**

```bash
git add app.py templates/cache.html tests/test_cache_page.py
git commit -m "feat: add /cache browse page with filter and pagination"
```

---

### Task 4: 移除搜索页缓存徽标/刷新按钮

**Files:**
- Modify: `E:\pixiv\templates\index.html`

- [ ] **Step 1: Remove cache UI from index.html**

删除（行号以当前文件为准，用内容定位）：

1. `renderCacheBadge(data)` 函数整体（含其内部 refreshCache 按钮绑定）
2. `refreshCache()` 函数整体
3. `finishSearch` 末尾的 `renderCacheBadge(data);` 调用
4. `resetPagination()` 中的 `renderCacheBadge(null);` 调用

删除后确认 `grep -n "renderCacheBadge\|refreshCache" templates/index.html` 无结果。

- [ ] **Step 2: Verify**

```bash
pytest tests/test_app.py tests/test_search_cache.py -v
# 预期 PASS
# Flask test client GET / 渲染 200 且不含 renderCacheBadge
```

- [ ] **Step 3: Commit**

```bash
git add templates/index.html
git commit -m "refactor: remove cache badge and refresh button from search page"
```

---

### Task 5: 全站导航栏加「缓存」链接

**Files:**
- Modify: `templates/index.html`、`templates/gallery.html`、`templates/bulk.html`、`templates/downloads.html`、`templates/settings.html`、`templates/settings_unlock.html`、`templates/detail.html`

- [ ] **Step 1: Add the nav link to all 7 templates**

每个模板的 `<div class="navbar-links">` 内，在「下载」链接（`/downloads`）之后、「设置」链接（`/settings`）之前插入：

```html
      <a class="navbar-link" href="/cache">缓存</a>
```

7 个模板各自修改（cache.html 在 Task 3 已自带）。`detail.html` 的 navbar 无 active 类差异，同样处理；若 detail.html 的 navbar 结构不同（例如不含某些链接），按它的现有结构把「缓存」插入「下载」与「设置」之间（没有「设置」就追加在末尾）。

- [ ] **Step 2: Verify**

```bash
pytest tests/test_app.py tests/test_cache_page.py -v
# 预期 PASS（含 settings/detail 等页面渲染测试）
```

再用 Flask test client 逐个 GET `/`、`/gallery`、`/bulk`、`/downloads`、`/settings`、`/detail/1`（任意 id，可能 404 但页面模板正常）确认渲染无 Jinja 错误。

- [ ] **Step 3: Commit**

```bash
git add templates/index.html templates/gallery.html templates/bulk.html templates/downloads.html templates/settings.html templates/settings_unlock.html templates/detail.html
git commit -m "feat: add cache link to all navbar templates"
```

---

### Task 6: 更新 AGENTS.md

**Files:**
- Modify: `E:\pixiv\AGENTS.md`

- [ ] **Step 1: Update the prefetch bullet in 「API 行为」**

当前 bullet（搜索预取缓存）描述"`/search` 命中 `SearchCache`（tag 完全匹配且 status=done）时零网络请求"。改写为：

```markdown
- **搜索预取缓存**：手动在设置页配置预取标签，后台线程按 interval 用宽松参数（min_bookmarks=1、date_d、R18 不过滤）定时预取并写入 `Illust` + `SearchCache` 表。**搜索 `/search` 永远走实时 Pixiv，不命中缓存**；预取结果通过独立的 `/cache` 页面浏览（`GET /api/cache/items`，库内按收藏数/排序过滤分页，`query_cached_tag`）。`popular_d` 排序为 `bookmark_count` 降序的库内近似。
```

- [ ] **Step 2: Update the architecture table**

`templates/*.html` 一行从「8 个」改为「9 个」，并把描述中补充「缓存浏览」：

```markdown
| `templates/*.html` | 9 个 Jinja2 模板（搜索、图库、批量、下载管理、详情、设置、设置解锁、登录、缓存浏览） |
```

- [ ] **Step 3: Verify and commit**

```bash
git add AGENTS.md
git commit -m "docs: update AGENTS.md for standalone cache page"
```

---

### 最终验证（全部任务后）

Run: `pytest -v`
Expected: 全量 PASS（集成测试连真实 Pixiv 时网络超时是 stderr 噪音，不影响结果）

确认 `git status` clean，`git push origin main`。
