# 翻页重做实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将"加载更多"替换为游标驱动的传统翻页制，后端持续抓取 Pixiv 攒够一页，前端缓存已加载页支持前后翻页。

**Architecture:** 后端始终从 Pixiv 第 1 页扫描，用 `yielded` 全局计数器切片出目标页（30s 缓存减轻重复扫描成本）；游标 `base64(JSON).HMAC` 编码搜索状态，含 `created_at` 及 5 分钟+5 秒过期；前端用 `loadedPages[]` 缓存最多 20 页，error_code 驱动行为分支。

**Tech Stack:** Python 3.9+ / Flask / 原生 JS / hmac + base64

---

### 文件地图

| 文件 | 职责 |
|------|------|
| `config.py` | `ITEMS_PER_PAGE`、`CURSOR_SECRET` 生成 |
| `fetcher.py` | 游标编解码/验签/过期、`paginated_search()` 攒页循环 |
| `app.py` | `/search` 游标路由、`/api/settings` 加入 `items_per_page` |
| `templates/index.html` | 翻页栏 + `loadedPages` 缓存 + 搜索重置 |
| `templates/settings.html` | `items_per_page` 输入字段 |
| `.gitignore` | 排除 `instance/.cursor_secret` |

---

### Task 1: config.py — 新增常量和 CURSOR_SECRET

**Files:**
- Modify: `config.py`

- [ ] **Step 1: 在 `config.py` 文件顶部附近（`SECRET_KEY` 生成之后）添加 `CURSOR_SECRET` 生成逻辑**

在 `import json` 行之后、`import os` 行之后确保有 `import secrets`，然后在 `BASE_DIR` 定义之后（约第 5 行）添加：

```python
# 游标签名密钥
_instance_dir = os.path.join(BASE_DIR, 'instance')
_cursor_secret_path = os.path.join(_instance_dir, '.cursor_secret')
if os.path.exists(_cursor_secret_path):
    with open(_cursor_secret_path) as _f:
        CURSOR_SECRET = _f.read().strip()
else:
    CURSOR_SECRET = secrets.token_hex(32)
    os.makedirs(_instance_dir, exist_ok=True)
    with open(_cursor_secret_path, 'w') as _f:
        _f.write(CURSOR_SECRET)
```

- [ ] **Step 2: 在 `config.py` 搜索设置区域（约第 50 行 `MAX_BOOKMARKS_DEFAULT` 之后）添加 `ITEMS_PER_PAGE`**

```python
# 翻页设置
ITEMS_PER_PAGE = 24            # 每页展示作品数 (1-60)
```

- [ ] **Step 3: 在 settings.json 覆盖映射 `_key_map` 中（约第 84 行）添加 `items_per_page`**

在 `_key_map` 字典末尾（`'medium_image_size': 'MEDIUM_IMAGE_SIZE',` 之后）添加：

```python
            'items_per_page': 'ITEMS_PER_PAGE',
```

- [ ] **Step 4: 语法检查**

运行: `python -c "import py_compile; py_compile.compile('config.py', doraise=True); print('OK')"`
预期: `OK`

- [ ] **Step 5: 提交**

```bash
git add config.py
git commit -m "feat: 新增 ITEMS_PER_PAGE 和 CURSOR_SECRET 配置"
```

---

### Task 2: fetcher.py — 游标编解码和验签

**Files:**
- Modify: `fetcher.py`

- [ ] **Step 1: 在 `fetcher.py` 顶部导入区域（`import time` 之后）添加缺少的 import**

```python
import hashlib
import hmac
import json
from base64 import urlsafe_b64encode, urlsafe_b64decode
```

注意：`json` 和 `time` 可能已导入，检查后只添加缺少的。`hmac`、`hashlib`、`base64` 需要新增。

- [ ] **Step 2: 在 `from config import (...)` 中添加 `CURSOR_SECRET, ITEMS_PER_PAGE`**

原行：
```python
from config import (
    COOKIE_PATH, PIXIV_BASE_URL, SEARCH_PAGES, PER_PAGE,
    DETAIL_TIMEOUT, DETAIL_MAX_RETRIES, FETCH_DETAIL_WORKERS,
    PROXY, SSL_VERIFY,
)
```

改为：
```python
from config import (
    COOKIE_PATH, PIXIV_BASE_URL, SEARCH_PAGES, PER_PAGE,
    DETAIL_TIMEOUT, DETAIL_MAX_RETRIES, FETCH_DETAIL_WORKERS,
    PROXY, SSL_VERIFY, CURSOR_SECRET, ITEMS_PER_PAGE,
)
```

- [ ] **Step 3: 在 `_is_auth_error()` 函数之后、`_load_cookie()` 之前添加游标函数**

```python
def encode_cursor(data: dict) -> str:
    payload = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
    b64 = urlsafe_b64encode(payload.encode()).decode().rstrip('=')
    sig = hmac.new(CURSOR_SECRET.encode(), b64.encode(), 'sha256').hexdigest()
    return b64 + '.' + sig


def decode_cursor(cursor: str) -> dict | None:
    try:
        b64, sig = cursor.rsplit('.', 1)
    except ValueError:
        return None
    expected = hmac.new(CURSOR_SECRET.encode(), b64.encode(), 'sha256').hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = urlsafe_b64decode(b64 + '===').decode()
    except Exception:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None
```

- [ ] **Step 4: 语法检查**

运行: `python -c "import py_compile; py_compile.compile('fetcher.py', doraise=True); print('OK')"`
预期: `OK`

- [ ] **Step 5: 提交**

```bash
git add fetcher.py
git commit -m "feat: 游标编解码和 HMAC 验签函数"
```

---

### Task 3: fetcher.py — paginated_search() 攒页循环

**Files:**
- Modify: `fetcher.py`

- [ ] **Step 1: 在 `decode_cursor()` 函数之后、`_cache_get()` 之前添加 `paginated_search()`**

```python
_MAX_SCAN_PAGES = 10
_CURSOR_TTL = 305  # 5 分钟 + 5 秒缓冲


def paginated_search(search_fn, query_params: dict, items_per_page: int,
                     cursor_data: dict | None = None) -> tuple[list[dict], str | None, bool]:
    """游标驱动的分页搜索。

    Args:
        search_fn: 搜索函数，签名为 (page: int) -> tuple[list[dict], bool]
        query_params: {type, query, sort, tag_mode, r18_mode, min_bookmarks}
        items_per_page: 每页件数
        cursor_data: 解码后的游标，None 表示新搜索

    Returns:
        (results, next_cursor, has_more)
    """
    yielded_total = cursor_data.get('yielded', 0) if cursor_data else 0
    pixiv_page = 1
    collected: list[dict] = []
    pages_scanned = 0
    pixiv_has_more = True

    while len(collected) - yielded_total < items_per_page and pages_scanned < _MAX_SCAN_PAGES:
        try:
            results, has_more = search_fn(page=pixiv_page)
        except PixivAuthError:
            raise
        except Exception as e:
            logger.error(f'paginated_search: page {pixiv_page} failed: {e}')
            break

        if not results and not has_more:
            pixiv_has_more = False
            break

        collected.extend(results)
        pages_scanned += 1
        pixiv_page += 1

        if not has_more:
            pixiv_has_more = False
            break

    if pages_scanned == _MAX_SCAN_PAGES and len(collected) - yielded_total < items_per_page:
        logger.info(f'paginated_search: 扫描 {_MAX_SCAN_PAGES} 页未攒够 {items_per_page} 件')

    batch = collected[yielded_total : yielded_total + items_per_page]
    new_yielded = yielded_total + len(batch)

    remaining = len(collected) - new_yielded
    has_more = remaining > 0 or (pixiv_has_more and pages_scanned >= _MAX_SCAN_PAGES)

    next_cursor = None
    if has_more and batch:
        next_cursor = encode_cursor({
            **query_params,
            'yielded': new_yielded,
            'created_at': int(time.time()),
        })

    return batch, next_cursor, has_more
```

- [ ] **Step 2: 语法检查**

运行: `python -c "import py_compile; py_compile.compile('fetcher.py', doraise=True); print('OK')"`
预期: `OK`

- [ ] **Step 3: 提交**

```bash
git add fetcher.py
git commit -m "feat: paginated_search 攒页循环 + 游标驱动"
```

---

### Task 4: app.py — `/search` 路由改为游标分页

**Files:**
- Modify: `app.py`

- [ ] **Step 1: 在 `app.py` 的 config import 中增加 `ITEMS_PER_PAGE`**

原行：
```python
from config import (
    DOWNLOAD_DIR, DOWNLOAD_MAX_WORKERS, PAGE_DOWNLOAD_INTERVAL,
    MAX_BOOKMARKS_DEFAULT, AUTO_FOLLOW_INTERVAL, AUTO_FOLLOW_DOWNLOAD,
    MEDIUM_IMAGE_SIZE,
    SETTINGS_PASSWORD,
    SSL_VERIFY,
)
```

改为：
```python
from config import (
    DOWNLOAD_DIR, DOWNLOAD_MAX_WORKERS, PAGE_DOWNLOAD_INTERVAL,
    MAX_BOOKMARKS_DEFAULT, AUTO_FOLLOW_INTERVAL, AUTO_FOLLOW_DOWNLOAD,
    MEDIUM_IMAGE_SIZE,
    SETTINGS_PASSWORD,
    SSL_VERIFY, ITEMS_PER_PAGE,
)
```

- [ ] **Step 2: 在 `from fetcher import (...)` 中加入 `encode_cursor, decode_cursor`**

原行：
```python
from fetcher import search_by_tag, search_by_user, fetch_following, browse_discovery, _build_session, _get_illust_detail, PixivAuthError
```

改为：
```python
from fetcher import search_by_tag, search_by_user, fetch_following, browse_discovery, _build_session, _get_illust_detail, PixivAuthError, encode_cursor, decode_cursor, paginated_search
```

- [ ] **Step 3: 重写 `/search` 路由（`app.py:421-477`）**

```python
@app.route('/search')
def search() -> Response:
    search_type = request.args.get('type', 'tag')
    query = request.args.get('query', '').strip()
    min_bookmarks = request.args.get('min_bookmarks', MAX_BOOKMARKS_DEFAULT)
    sort_order = request.args.get('sort', 'date_d')
    cursor_str = request.args.get('cursor', '')

    if search_type == 'user' and not cursor_str and not query:
        return jsonify({'error': '请输入画师ID'}), 400

    try:
        min_bookmarks = int(min_bookmarks)
    except (ValueError, TypeError):
        min_bookmarks = MAX_BOOKMARKS_DEFAULT

    tag_mode = request.args.get('tag_mode', 'or')
    if tag_mode not in ('or', 'and'):
        tag_mode = 'or'

    if sort_order not in ('popular_d', 'date_d'):
        sort_order = 'date_d'

    r18_mode = request.args.get('r18_mode', 'all')
    if r18_mode not in ('all', 'safe'):
        r18_mode = 'all'

    # 解析游标
    cursor_data = None
    if cursor_str:
        cursor_data = decode_cursor(cursor_str)
        if cursor_data is None:
            return jsonify({'error': '游标无效', 'error_code': 'CURSOR_INVALID'}), 400
        if time.time() - cursor_data.get('created_at', 0) > 305:
            return jsonify({'error': '搜索已过期，请重新搜索', 'error_code': 'CURSOR_EXPIRED'}), 400
        # 从游标恢复搜索参数
        search_type = cursor_data.get('type', search_type)
        query = cursor_data.get('query', query)
        sort_order = cursor_data.get('sort', sort_order)
        tag_mode = cursor_data.get('tag_mode', tag_mode)
        r18_mode = cursor_data.get('r18_mode', r18_mode)
        min_bookmarks = cursor_data.get('min_bookmarks', min_bookmarks)

    logger.info(f'搜索：type={search_type}, query={query!r}, min={min_bookmarks}, sort={sort_order}, tag_mode={tag_mode}')

    query_params = {
        'type': search_type,
        'query': query,
        'sort': sort_order,
        'tag_mode': tag_mode,
        'r18_mode': r18_mode,
        'min_bookmarks': min_bookmarks,
    }

    try:
        if search_type == 'tag':
            if len(query) > 200:
                return jsonify({'error': '搜索关键词过长'}), 400
            if not query:
                def _browse_fn(page):
                    return browse_discovery(page, sort_order, min_bookmarks, r18_mode=r18_mode)
                results, next_cursor, has_more = paginated_search(_browse_fn, query_params, ITEMS_PER_PAGE, cursor_data)
            else:
                def _tag_fn(page):
                    return search_by_tag(query, min_bookmarks, page, sort_order, 9999, tag_mode, r18_mode=r18_mode)
                results, next_cursor, has_more = paginated_search(_tag_fn, query_params, ITEMS_PER_PAGE, cursor_data)
        else:
            if not cursor_str and not query.isdigit():
                return jsonify({'error': '画师ID必须为数字'}), 400
            def _user_fn(page):
                return search_by_user(query, min_bookmarks, page, hide_r18=(r18_mode == 'safe'))
            results, next_cursor, has_more = paginated_search(_user_fn, query_params, ITEMS_PER_PAGE, cursor_data)
    except PixivAuthError as e:
        logger.warning(f'搜索认证失败：{e}')
        return jsonify({'error': 'Cookie 已过期，请更新 cookies.txt 后重试'}), 401
    except FileNotFoundError as e:
        logger.error(f'搜索失败 - 文件未找到：{e}')
        return jsonify({'error': f'缺少文件: {e}'}), 500
    except Exception as e:
        logger.error(f'搜索失败：{e}', exc_info=True)
        return jsonify({'error': '搜索服务暂时不可用，请稍后重试'}), 502

    return jsonify({
        'results': results,
        'cursor': next_cursor,
        'has_more': has_more,
    })
```

注意：需要在 `app.py` 顶部添加 `import time`（检查是否已存在，可能已有 `import time` 在第 11 行）。

- [ ] **Step 4: 语法检查**

运行: `python -c "import py_compile; py_compile.compile('app.py', doraise=True); print('OK')"`
预期: `OK`

- [ ] **Step 5: 运行单元测试**

运行: `pytest -v -k "not integration" 2>&1 | Select-Object -Last 10`
预期: 仅 `test_to_dict_keys` 一个预存在失败，其余 32 个 PASS

- [ ] **Step 6: 提交**

```bash
git add app.py
git commit -m "feat: /search 路由改为游标分页"
```

---

### Task 5: app.py — `/api/settings` 支持 items_per_page

**Files:**
- Modify: `app.py`

- [ ] **Step 1: 在 `_SETTINGS_DEFAULTS` 字典（约 `app.py:1232`）中添加 `items_per_page`**

在 `'medium_image_size': 600,` 之后添加：

```python
    'items_per_page': 24,
```

- [ ] **Step 2: 在 `api_settings_post()` 的类型转换列表中加入 `items_per_page`**

找到约 `app.py:1297-1299` 的行：
```python
            elif key in ('download_max_workers', 'per_page', 'search_pages',
                         'max_bookmarks_default', 'auto_follow_interval',
                         'fetch_detail_workers', 'medium_image_size'):
```

改为：
```python
            elif key in ('download_max_workers', 'per_page', 'search_pages',
                         'max_bookmarks_default', 'auto_follow_interval',
                         'fetch_detail_workers', 'medium_image_size',
                         'items_per_page'):
```

- [ ] **Step 3: 语法检查**

运行: `python -c "import py_compile; py_compile.compile('app.py', doraise=True); print('OK')"`
预期: `OK`

- [ ] **Step 4: 运行单元测试**

运行: `pytest -v -k "not integration" 2>&1 | Select-Object -Last 5`
预期: 同上

- [ ] **Step 5: 提交**

```bash
git add app.py
git commit -m "feat: /api/settings 支持 items_per_page"
```

---

### Task 6: templates/settings.html — items_per_page 输入字段

**Files:**
- Modify: `templates/settings.html`

- [ ] **Step 1: 在搜索卡片（"搜索" card）中添加 `items_per_page` 字段**

在 `max_bookmarks_default` 的 `</div>` 之后、`</div>`（card-body 结束）之前插入：

```html
      <div class="mb-0">
        <label class="form-label" for="items_per_page">每页展示件数</label>
        <input class="form-control form-control-sm" id="items_per_page" type="number" min="1" max="60" style="max-width:140px;">
        <div class="form-text">搜索结果每页展示数量 (1-60)，修改后搜索自动重置</div>
      </div>
```

- [ ] **Step 2: 在 JS `FIELD_MAP` 数组（约第 159 行）中添加 `items_per_page`**

```javascript
const FIELD_MAP = [
  'proxy', 'download_max_workers', 'fetch_detail_workers', 'medium_image_size',
  'per_page', 'search_pages', 'items_per_page',
  'max_bookmarks_default', 'auto_follow_interval', 'auto_follow_download',
];
```

- [ ] **Step 3: 提交**

```bash
git add templates/settings.html
git commit -m "feat: 设置页新增每页展示件数字段"
```

---

### Task 7: templates/index.html — 翻页 UI + loadedPages 缓存 + 搜索重置

**Files:**
- Modify: `templates/index.html`

这是最大的一项改动。分 4 个子步骤：

- [ ] **Step 1: 删除旧的 loadMore 相关代码**

删除（定位到对应行）：
- `$('#loadMoreWrap')` 和 `#loadMoreBtn` 的点击事件（`$('#loadMoreBtn').addEventListener('click', loadMore)`）
- `async function loadMore() { ... }` 整个函数
- `function updateLoadMore() { ... }` 整个函数
- `$('#startPage')` 的 `<input>` 元素（行 326）
- `<label>起始页</label>` 标签（行 325-326 附近）
- `loadMoreWrap` 的 div（行 361-363）

**注意：** `$('#startPage')` 在 JS 中作为 filter 的显示/隐藏切换使用（行 455、460、462），这些引用也要移除。

- [ ] **Step 2: 添加翻页栏 HTML**

在 `#masonryGrid` 之后、`#emptyState` 之前插入：

```html
  <!-- Pagination -->
  <div id="paginationBar" style="display:none; text-align:center; padding: 1rem 0;">
    <div style="display:flex; align-items:center; justify-content:center; gap:6px;">
      <button id="prevPageBtn" class="btn btn-sm btn-outline-secondary" disabled>上一页</button>
      <div id="pageNumbers" style="display:flex; gap:4px;"></div>
      <button id="nextPageBtn" class="btn btn-sm btn-outline-primary">下一页</button>
    </div>
    <div id="paginationStatus" style="font-size:0.72rem; color:var(--text-muted); margin-top:4px;"></div>
  </div>
```

- [ ] **Step 3: 替换 JS 搜索逻辑**

删除旧的状态变量和搜索函数（`lastSearchParams`, `allResults`, `currentPage`, `hasMorePages`, `showToolbar`, `doSearch`, `loadMore`, `updateLoadMore`, `showLoading`, `saveSearchState`, `restoreSearchState`, `SEARCH_STATE_KEY`），替换为：

```javascript
// ── 翻页状态 ──
let loadedPages = [];
let nextCursor = null;
let currentPage = 1;
let hasMore = false;
let lastSearchQuery = null;

function resetPagination() {
  loadedPages = [];
  nextCursor = null;
  currentPage = 1;
  hasMore = false;
  $('#prevPageBtn').disabled = true;
  $('#nextPageBtn').disabled = true;
  $('#pageNumbers').innerHTML = '';
  $('#paginationBar').style.display = 'none';
}

function renderPaginationBar() {
  const bar = $('#paginationBar');
  const container = $('#pageNumbers');
  bar.style.display = loadedPages.length > 0 ? 'block' : 'none';
  if (!loadedPages.length) return;

  let html = '';
  for (let i = 0; i < loadedPages.length && i < 20; i++) {
    const num = i + 1;
    if (num === currentPage) {
      html += `<span style="padding:2px 10px;border-radius:4px;background:var(--accent);color:#fff;font-size:0.8rem;font-weight:600;">${num}</span>`;
    } else {
      html += `<button class="btn btn-sm btn-soft page-jump-btn" data-page="${num}" style="min-width:32px;font-size:0.75rem;padding:2px 8px;">${num}</button>`;
    }
  }
  container.innerHTML = html;

  container.querySelectorAll('.page-jump-btn').forEach(btn => {
    btn.addEventListener('click', () => jumpToPage(parseInt(btn.dataset.page)));
  });

  $('#prevPageBtn').disabled = currentPage <= 1;
  $('#nextPageBtn').disabled = !hasMore && currentPage >= loadedPages.length;
  $('#paginationStatus').textContent = `第 ${currentPage} 页 · 已缓 ${loadedPages.length} 页`;
}

function renderPage(pageNum) {
  const page = loadedPages[pageNum - 1];
  if (!page) return;
  $('#masonryGrid').innerHTML = '';
  page.forEach(r => renderCard(r));
  lazyLoad();
}

function jumpToPage(pageNum) {
  if (pageNum < 1 || pageNum > loadedPages.length) return;
  currentPage = pageNum;
  renderPage(currentPage);
  renderPaginationBar();
}

$('#prevPageBtn').addEventListener('click', () => jumpToPage(currentPage - 1));
$('#nextPageBtn').addEventListener('click', () => loadNextPage());

async function loadNextPage() {
  if (!nextCursor) return;
  $('#nextPageBtn').disabled = true;
  $('#nextPageBtn').textContent = '加载中...';

  try {
    const params = new URLSearchParams();
    params.set('cursor', nextCursor);
    const resp = await fetch('/search?' + params.toString());
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (err.error_code === 'CURSOR_EXPIRED') {
        showToast('搜索已过期，自动重新搜索');
        doSearch();
        return;
      }
      showToast(err.error || '加载失败', true);
      renderPaginationBar();
      return;
    }
    const data = await resp.json();
    if (!data.results.length) {
      hasMore = false;
      renderPaginationBar();
      return;
    }
    loadedPages.push(data.results);
    nextCursor = data.cursor || null;
    hasMore = data.has_more || false;
    currentPage = loadedPages.length;
    renderPage(currentPage);
    renderPaginationBar();
  } catch { showToast('网络错误', true); }
  finally {
    $('#nextPageBtn').disabled = !hasMore;
    $('#nextPageBtn').textContent = '下一页';
  }
}

async function doSearch() {
  const type = $('#searchType').value;
  const query = $('#searchQuery').value.trim();
  const minBookmarks = parseInt($('#minBookmarks').value) || 0;
  if (type === 'user' && !query) { showToast('请输入画师ID'); return; }

  const sort = $('#sortOrder').value || 'date_d';
  const tagMode = $('#tagMode').value || 'or';
  const r18Mode = $('#r18Mode').value;
  lastSearchQuery = { type, query, minBookmarks, sort, tagMode, r18Mode };
  resetPagination();
  $('#masonryGrid').innerHTML = '';
  $('#emptyState').style.display = 'none';
  $('#authErrorState').style.display = 'none';
  batchInProgress = false;

  showLoading(true);
  try {
    let url;
    if (type === 'following') url = `/api/following?page=1&r18_mode=${r18Mode}`;
    else url = `/search?${new URLSearchParams({type,query,min_bookmarks:minBookmarks,sort,tag_mode:tagMode,r18_mode:r18Mode})}`;

    const resp = await fetch(url);
    if (!resp.ok) {
      if (resp.status === 401) {
        showToast('Cookie 已过期，请更新 cookies.txt', true);
        $('#authErrorState').style.display = 'block';
        return;
      }
      const err = await resp.json().catch(() => ({}));
      if (err.error_code === 'CURSOR_EXPIRED') {
        showToast('搜索已过期，自动重新搜索');
        return doSearch();
      }
      showToast(resp.status === 429 ? '请求过于频繁' : (err.error || '搜索失败'), true);
      return;
    }
    const data = await resp.json();
    if (!data.results.length) {
      $('#emptyState').style.display = 'block';
      return;
    }
    loadedPages = [data.results];
    nextCursor = data.cursor || null;
    hasMore = data.has_more || false;
    currentPage = 1;
    data.results.forEach(r => renderCard(r));
    renderPaginationBar();
    lazyLoad();
  } catch { showToast('网络错误', true); }
  finally { showLoading(false); }
}
```

- [ ] **Step 4: 删除旧的分页残留代码**

删除：
- `showToolbar()` 函数及其调用
- `saveSearchState()` / `restoreSearchState()` 函数
- `SEARCH_STATE_KEY` / `R18_STATE_KEY` 变量（但保留 `loadR18Mode` 用到的 `R18_STATE_KEY`）
- `$('#startPage')` 在 `updateSearchUI()` 中的引用（行 455-462），改为不引用 `#startPage`
- `lastSearchParams` / `allResults` / `hasMorePages` 旧变量初始化
- `loadMoreWrap` 的 div

简化 `updateSearchUI()` 中不再需要的 filter 显示/隐藏逻辑：
```javascript
function updateSearchUI() {
  const type = $('#searchType').value;
  const isTag = type === 'tag';
  $('#tagMode').style.display = isTag ? '' : 'none';
  $('#searchQuery').placeholder = type === 'following' ? '' : isTag ? '多个标签用逗号分隔' : '输入画师UID...';

  const show = type !== 'following';
  ['#sortOrder','#minBookmarks'].forEach(id => {
    const el = $(id);
    if (el) el.style.display = show ? '' : 'none';
  });
  $$('.filters-row label').forEach(l => l.style.display = show ? '' : 'none');
  if (!show) {
    ['#sortOrder','#minBookmarks','#toggleBlockedBtn'].forEach(id => $(id).style.display = 'none');
  } else {
    ['#sortOrder','#minBookmarks','#toggleBlockedBtn'].forEach(id => $(id).style.display = '');
  }
}
```

- [ ] **Step 5: 删除 `#startPage` 的 `<input>` 元素和 `<label>`**

在 HTML 的 filters-row 区域（约行 325-328）删除：
```html
      <label>起始页</label>
      <input id="startPage" type="number" value="1" min="1">
```

- [ ] **Step 6: 初始化部分修改**

替换文件末尾的初始化代码（约行 730-742）：
```javascript
// ── Init ──
loadBlockedTags();
loadR18Mode();

const urlParams = new URLSearchParams(location.search);
if (urlParams.has('query')) {
  $('#searchQuery').value = urlParams.get('query');
  if (urlParams.has('type')) $('#searchType').value = urlParams.get('type');
  updateSearchUI();
  doSearch();
}
```

删除 `searchBtn` 引用：
```javascript
$('#searchBtn').addEventListener('click', () => doSearch());
$('#searchQuery').addEventListener('keydown', e => { if (e.key==='Enter') doSearch(); });
```

- [ ] **Step 7: 设置页 items_per_page 变更触发搜索重置**

在 `settings.html` 的 JS 中，`saveBtn` 点击处理函数成功回调里，检测 `items_per_page` 变更：

需要 index.html 监听 `storage` 事件或设置页保存后通过 sessionStorage 通信。简单方案：在 settings.html 保存成功后设置 `sessionStorage.setItem('pixiv_settings_changed', '1')`。在 index.html 的页面聚焦或恢复时检查这个标志。

更简单的方案：index.html 不需要知道 settings 变了。设置页的提示 "修改后需重启生效" 对 items_per_page 无效（它是后端实时读取的）。但 spec 要求自动重置搜索。所以采用 sessionStorage 通信。

在 `settings.html` 的 `saveBtn` 成功回调（约第 205-207 行 `if (r.ok)` 块内），在 `loadSettings()` 调用前加：

```javascript
    if (r.ok) {
      sessionStorage.setItem('pixiv_settings_changed', '1');
      showToast('设置已保存（部分设置需重启后生效）');
      loadSettings();
    }
```

在 `index.html` 的窗口 focus 事件中检查：
```javascript
window.addEventListener('focus', () => {
  if (sessionStorage.getItem('pixiv_settings_changed')) {
    sessionStorage.removeItem('pixiv_settings_changed');
    if (loadedPages.length > 0) {
      showToast('每页件数已变更，搜索已重置');
      doSearch();
    }
  }
});
```

- [ ] **Step 8: 提交**

```bash
git add templates/index.html templates/settings.html
git commit -m "feat: 翻页栏 UI + loadedPages 缓存 + 搜索重置"
```

---

### Task 8: `.gitignore` — 排除 CURSOR_SECRET 文件

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: 在 `.gitignore` 的 `instance/` 相关行附近添加**

```
instance/.cursor_secret
```

- [ ] **Step 2: 提交**

```bash
git add .gitignore
git commit -m "chore: gitignore 排除 cursor_secret"
```
