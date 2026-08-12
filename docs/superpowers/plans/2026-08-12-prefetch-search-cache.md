# 搜索预取缓存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a scheduled prefetch engine that caches Pixiv search results for user-configured tags into the existing database, enabling zero-network search hits.

**Architecture:** A new `SearchCache` model stores tag-to-illust_id mappings. A daemon thread (like `_auto_follow_state`) periodically fetches configured tags with loose params (min_bookmarks=1, r18=all, date_d) and upserts results into the global `Illust` table. On `/search` hit, a background task runs an in-DB query (filter/sort/page) instead of calling Pixiv API. Capacity management deletes oldest prefetched data when exceeding a configurable limit.

**Tech Stack:** Python 3.9+ / Flask 3.1+ / SQLAlchemy 2.0 / SQLite (WAL) / 原生 JS / pytest

---

### Task 1: Models — SearchCache + prefetch_source + remove description

**Files:**
- Modify: `models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write failing tests for SearchCache model**

```python
# tests/test_models.py — add at end
from models import SearchCache, safe_commit, get_session

class TestSearchCache:
    def test_create_and_query(self, clean_db):
        sc = SearchCache(
            tag='初音ミク',
            illust_ids='[123, 456, 789]',
            status='done',
            total=3,
        )
        clean_db.add(sc)
        safe_commit(clean_db)
        fetched = clean_db.query(SearchCache).filter(SearchCache.tag == '初音ミク').first()
        assert fetched is not None
        assert fetched.tag == '初音ミク'
        assert fetched.illust_ids == '[123, 456, 789]'
        assert fetched.status == 'done'
        assert fetched.total == 3

    def test_primary_key_is_tag(self, clean_db):
        clean_db.add(SearchCache(tag='tag1'))
        safe_commit(clean_db)
        import pytest
        from sqlalchemy.exc import IntegrityError
        clean_db.add(SearchCache(tag='tag1'))
        with pytest.raises(IntegrityError):
            safe_commit(clean_db)
        clean_db.rollback()
        clean_db.query(SearchCache).filter(SearchCache.tag == 'tag1').delete()
        clean_db.commit()

    def test_defaults(self, clean_db):
        sc = SearchCache(tag='test')
        assert sc.illust_ids == '[]'
        assert sc.status == 'idle'
        assert sc.error == ''
        assert sc.total == 0

    def test_prefetch_source_column(self, clean_db, sample_illust):
        sample_illust.prefetch_source = 1
        safe_commit(clean_db)
        clean_db.refresh(sample_illust)
        assert sample_illust.prefetch_source == 1

    def test_description_removed(self, clean_db, sample_illust):
        assert not hasattr(sample_illust, 'description')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL with "ImportError: cannot import name SearchCache" + "AttributeError: 'Illust' object has no attribute 'description'"

- [ ] **Step 3: Add SearchCache model, add prefetch_source, remove description**

```python
# models.py — add after DownloadLog class (before Collection class)

class SearchCache(Base):
    __tablename__ = 'search_cache'
    tag: Mapped[str] = mapped_column(String, primary_key=True)
    illust_ids: Mapped[str] = mapped_column(Text, default='[]')
    cached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default='idle')
    error: Mapped[str] = mapped_column(String, default='')
    total: Mapped[int] = mapped_column(Integer, default=0)
```

```python
# models.py — remove description field from Illust (line 66)
# Before:
    description: Mapped[str] = mapped_column(Text, default='')
# After: (remove the line)

# Add prefetch_source field:
    prefetch_source: Mapped[int] = mapped_column(Integer, default=0)
```

```python
# models.py — remove 'description' from to_dict (line 124)
# Before:
    'description': self.description,
# After: (remove the line)
```

- [ ] **Step 4: Update init_db() for prefetch_source and description migration**

```python
# models.py — in init_db(), after bookmark_updated_at migration (line 227), add:
    if 'prefetch_source' not in columns:
        with engine.connect() as conn:
            conn.execute(text('ALTER TABLE illusts ADD COLUMN prefetch_source INTEGER DEFAULT 0'))
            conn.commit()

    # Drop description column (if it still exists)
    has_desc = any(c == 'description' for c in columns)
    if has_desc:
        import sqlite3 as _sqlite3
        ver = tuple(int(x) for x in _sqlite3.sqlite_version.split('.'))
        if ver >= (3, 35, 0):
            with engine.connect() as conn:
                conn.execute(text('ALTER TABLE illusts DROP COLUMN description'))
                conn.commit()
        else:
            # < 3.35 需重建表（仿现有 is_favorite 的 rebuild 模式）
            with engine.connect() as conn:
                info_rows = conn.exec_driver_sql('PRAGMA table_info(illusts)').fetchall()
                keep = [r[1] for r in info_rows if r[1] != 'description']
                col_list = ', '.join(f'"{c}"' for c in keep)
                conn.execute(text(f'CREATE TABLE _illusts_new AS SELECT {col_list} FROM illusts'))
                conn.execute(text('DROP TABLE illusts'))
                conn.execute(text('ALTER TABLE _illusts_new RENAME TO illusts'))
                # 重建索引
                conn.execute(text('CREATE INDEX IF NOT EXISTS ix_illusts_dl_status_created ON illusts(download_status, created_at)'))
                conn.execute(text('CREATE INDEX IF NOT EXISTS ix_illusts_user_id ON illusts(user_id)'))
                conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_illusts_pixiv_id ON illusts(pixiv_id)'))
                conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_models.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add models.py tests/test_models.py
git commit -m "feat: add SearchCache model, prefetch_source column, remove description"
```

---

### Task 2: Config constants and settings integration

**Files:**
- Modify: `config.py`, `app.py`

- [ ] **Step 1: Add PREFETCH_* constants to config.py**

```python
# config.py — add after FETCH_DETAIL_WORKERS (line 53)
PREFETCH_INTERVAL = 3600        # 预取间隔（秒），0 禁用
PREFETCH_PAGES = 3              # 每标签预取页数
PREFETCH_MAX_ILLUSTS = 20000    # 预取来源作品最大数量
```

- [ ] **Step 2: Add settings.json key mapping in config.py**

```python
# config.py — add to _key_map (line 98-112)
            'prefetch_interval': 'PREFETCH_INTERVAL',
            'prefetch_pages': 'PREFETCH_PAGES',
            'prefetch_max_illusts': 'PREFETCH_MAX_ILLUSTS',
```

- [ ] **Step 3: Add to _SETTINGS_DEFAULTS in app.py**

```python
# app.py — add to _SETTINGS_DEFAULTS (line 1487-1498)
    'prefetch_interval': 3600,
    'prefetch_pages': 3,
    'prefetch_max_illusts': 20000,
```

- [ ] **Step 4: Add to settings.html form**

Add new card block after "搜索" card (around line 97):
```html
  <div class="card shadow-sm mb-3">
    <div class="card-header py-2 fw-semibold small">搜索预取</div>
    <div class="card-body">
      <div class="mb-3">
        <label class="form-label" for="prefetch_interval">预取间隔（秒）</label>
        <input class="form-control form-control-sm" id="prefetch_interval" type="number" min="0" style="max-width:140px;">
        <div class="form-text">定时预取常用标签的间隔（0 = 禁用），修改后需重启生效</div>
      </div>
      <div class="mb-3">
        <label class="form-label" for="prefetch_pages">每标签预取页数</label>
        <input class="form-control form-control-sm" id="prefetch_pages" type="number" min="1" max="20" style="max-width:140px;">
        <div class="form-text">每标签每次预取抓取多少页（60 条/页），修改后需重启生效</div>
      </div>
      <div class="mb-0">
        <label class="form-label" for="prefetch_max_illusts">最大缓存作品数</label>
        <input class="form-control form-control-sm" id="prefetch_max_illusts" type="number" min="1000" step="1000" style="max-width:140px;">
        <div class="form-text">预取来源作品总数上限，超限自动删除最旧未下载未收藏作品，修改后需重启生效</div>
      </div>
    </div>
  </div>
```

- [ ] **Step 5: Update settings.js inline script to load/save new fields**

In `templates/settings.html`, find the `loadSettings` function (around line 170) and add prefetch field IDs:
```javascript
// Add to the list of IDs loaded from settings.json
['prefetch_interval','prefetch_pages','prefetch_max_illusts'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = data[id] ?? '';
});
```

- [ ] **Step 6: Run tests to verify app still works**

Run: `pytest tests/test_app.py -v`
Expected: PASS (no regression)

- [ ] **Step 7: Commit**

```bash
git add config.py app.py templates/settings.html
git commit -m "feat: add prefetch config constants and settings UI"
```

---

### Task 3: Prefetch engine — daemon thread + capacity cleanup

**Files:**
- Modify: `app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Add _prefetch_state and import SearchCache**

```python
# app.py — add near _auto_follow_state (after line 184)
from models import SearchCache

_prefetch_state = {
    'running': False,
    'last_check': None,
    'interval': PREFETCH_INTERVAL,
    'pages': PREFETCH_PAGES,
    'max_illusts': PREFETCH_MAX_ILLUSTS,
}
```

- [ ] **Step 2: Write prefetch worker function**

```python
# app.py — add after _prefetch_state (after line ~194)

def _prefetch_one_tag(tag: str) -> None:
    """预取单个标签：搜索 + upsert Illust + 更新 SearchCache。"""
    with get_session() as db:
        sc = db.query(SearchCache).filter(SearchCache.tag == tag).first()
        if not sc:
            sc = SearchCache(tag=tag, status='fetching')
            db.add(sc)
        else:
            sc.status = 'fetching'
            sc.error = ''
        safe_commit(db)

    try:
        all_ids: list[int] = []
        for page in range(1, _prefetch_state['pages'] + 1):
            results, has_more = search_by_tag(
                tag, min_bookmarks=1, page=page, sort_order='date_d',
                r18_mode='all', tag_mode='or',
            )
            for r in results:
                pid = int(r['pixiv_id'])
                all_ids.append(pid)
                with get_session() as db:
                    existing = db.query(Illust).filter(Illust.pixiv_id == pid).first()
                    if existing:
                        existing.bookmark_count = max(existing.bookmark_count, r.get('bookmark_count', 0))
                        existing.upload_date = r.get('upload_date', existing.upload_date)
                        existing.thumb_url = r.get('thumb_url', existing.thumb_url)
                        existing.page_count = r.get('page_count', existing.page_count)
                        if r.get('tags'):
                            existing.tags_list = r['tags']
                        # 下载/收藏时不覆盖 download_status/local_paths
                        if not existing.download_status:
                            existing.prefetch_source = 1
                    else:
                        existing = Illust(
                            pixiv_id=pid,
                            title=r.get('title', ''),
                            user_id=int(r.get('user_id', 0)),
                            user_name=r.get('user_name', ''),
                            page_count=r.get('page_count', 1),
                            bookmark_count=r.get('bookmark_count', 0),
                            thumb_url=r.get('thumb_url', ''),
                            upload_date=r.get('upload_date'),
                            prefetch_source=1,
                        )
                        existing.tags_list = r.get('tags', [])
                        existing.original_urls_list = r.get('original_urls', [])
                        db.add(existing)
                    safe_commit(db)
            if not has_more:
                break

        with get_session() as db:
            sc = db.query(SearchCache).filter(SearchCache.tag == tag).first()
            if sc:
                sc.illust_ids = json.dumps(all_ids, ensure_ascii=False)
                sc.cached_at = datetime.now(timezone.utc)
                sc.status = 'done'
                sc.total = len(all_ids)
                sc.error = ''
                safe_commit(db)
    except Exception as e:
        logger.error(f'[prefetch] 标签 {tag} 预取失败: {e}')
        with get_session() as db:
            sc = db.query(SearchCache).filter(SearchCache.tag == tag).first()
            if sc:
                sc.status = 'error'
                sc.error = str(e)
                safe_commit(db)
```

- [ ] **Step 3: Write capacity cleanup function**

```python
# app.py — add after _prefetch_one_tag

def _prefetch_capacity_cleanup() -> None:
    """检查预取来源作品数量，超过上限则从最旧标签开始删除。"""
    max_illusts = _prefetch_state['max_illusts']
    with get_session() as db:
        count = db.query(Illust).filter(Illust.prefetch_source == 1).count()
        if count <= max_illusts:
            return

        # 按 cached_at 升序取旧标签
        old_tags = db.query(SearchCache).order_by(SearchCache.cached_at.asc()).all()
        to_delete: list[int] = []
        freed = 0
        need_free = count - max_illusts

        for sc in old_tags:
            if freed >= need_free:
                break
            ids = json.loads(sc.illust_ids) if sc.illust_ids else []
            # 从尾部开始删（最旧的条目）
            while ids and freed < need_free:
                pid = ids.pop()
                # 检查是否被其他 SearchCache 引用
                other_refs = db.query(SearchCache).filter(
                    SearchCache.illust_ids.like(f'%{pid}%'),
                    SearchCache.tag != sc.tag,
                ).count()
                if other_refs > 0:
                    continue
                # 检查是否已下载/收藏
                illust = db.query(Illust).filter(Illust.pixiv_id == pid).first()
                if not illust or illust.download_status == 'done' or illust.local_paths:
                    continue
                coll_ref = db.query(CollectionItem).filter(
                    CollectionItem.pixiv_id == pid
                ).first()
                if coll_ref:
                    continue
                to_delete.append(pid)
                freed += 1
            sc.illust_ids = json.dumps(ids, ensure_ascii=False)
            safe_commit(db)

        if to_delete:
            db.query(Illust).filter(Illust.pixiv_id.in_(to_delete)).delete(synchronize_session=False)
            safe_commit(db)
        logger.info(f'[prefetch] 容量清理: 删除了 {len(to_delete)} 条最旧预取作品')
```

- [ ] **Step 4: Write prefetch loop and daemon thread**

```python
# app.py — add after _prefetch_capacity_cleanup

def _prefetch_loop() -> None:
    """后台预取循环：遍历所有 SearchCache 标签，串行预取。"""
    _prefetch_state['running'] = True
    with get_session() as db:
        tags = [sc.tag for sc in db.query(SearchCache.tag).all()]
    if not tags:
        _prefetch_state['running'] = False
        return

    logger.info(f'[prefetch] 开始预取 {len(tags)} 个标签')
    for tag in tags:
        with get_session() as db:
            sc = db.query(SearchCache).filter(SearchCache.tag == tag).first()
            if sc and sc.status == 'fetching':
                continue  # 跳过正在取中的（立即刷新可能正在跑）
        _prefetch_one_tag(tag)

    _prefetch_capacity_cleanup()
    _prefetch_state['last_check'] = datetime.now(timezone.utc).isoformat()
    _prefetch_state['running'] = False
    logger.info('[prefetch] 本轮预取完成')


def _start_prefetch_thread() -> None:
    """启动预取守护线程。首轮立即执行，之后按 interval 循环。"""
    interval = _prefetch_state['interval']
    if interval <= 0:
        return

    def _run() -> None:
        # 首轮延迟 5 秒等 app 完全启动
        time.sleep(5)
        while True:
            _prefetch_loop()
            time.sleep(interval)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    logger.info(f'[prefetch] 后台线程已启动，interval={interval}s')


# 替换 _reset_stuck_downloads() 调用后的启动逻辑
# 在文件末尾的模块级代码中，在 _reset_stuck_downloads() 之后加入：
_start_prefetch_thread()
```

- [ ] **Step 5: Write tests for capacity cleanup**

```python
# tests/test_prefetch.py — new file

from models import SearchCache, Illust, safe_commit, get_session, CollectionItem, Collection
from datetime import datetime, timezone
import json

class TestCapacityCleanup:
    def test_under_limit_does_nothing(self, clean_db):
        # Should not delete anything
        from app import _prefetch_capacity_cleanup
        # Just verify it doesn't crash
        _prefetch_capacity_cleanup()
        assert True

    def test_skip_downloaded_illusts(self, clean_db):
        from app import _prefetch_capacity_cleanup
        # Create an illust that is downloaded
        illust = Illust(pixiv_id=1, title='dl', prefetch_source=1, download_status='done')
        clean_db.add(illust)
        clean_db.add(SearchCache(tag='a', illust_ids='[1]', cached_at=datetime(2020,1,1,tzinfo=timezone.utc)))
        safe_commit(clean_db)
        # With max_illusts=0, we'd try to clean but should skip the downloaded one
        from app import _prefetch_state
        _prefetch_state['max_illusts'] = 0
        _prefetch_capacity_cleanup()
        still = clean_db.query(Illust).filter(Illust.pixiv_id == 1).first()
        assert still is not None  # should not be deleted
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_models.py tests/test_prefetch.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app.py tests/test_prefetch.py
git commit -m "feat: add prefetch engine with daemon thread and capacity cleanup"
```

---

### Task 4: Search hit logic — modify /search + query_cached_tag

**Files:**
- Modify: `app.py`, `fetcher.py`

- [ ] **Step 1: Write query_cached_tag function**

```python
# app.py — add after _prefetch_capacity_cleanup

def query_cached_tag(tag: str, min_bookmarks: int, sort_order: str,
                     tag_mode: str, r18_mode: str, offset: int = 0,
                     limit: int = 24) -> tuple[list[dict], bool, int]:
    """从 SearchCache + Illust 表查询预取结果，支持库内过滤排序分页。

    Returns:
        (results_dicts, has_more, next_offset)
    """
    from models import SearchCache, Illust
    with get_session() as db:
        sc = db.query(SearchCache).filter(SearchCache.tag == tag, SearchCache.status == 'done').first()
        if not sc:
            return [], False, 0

        all_ids = json.loads(sc.illust_ids) if sc.illust_ids else []
        if not all_ids:
            return [], False, 0

        illusts = db.query(Illust).filter(Illust.pixiv_id.in_(all_ids)).all()
        id_map = {i.pixiv_id: i for i in illusts}

    # 按原始顺序过滤
    filtered: list[Illust] = []
    for pid in all_ids:
        illust = id_map.get(pid)
        if not illust:
            continue
        # min_bookmarks 过滤
        if illust.bookmark_count < min_bookmarks:
            continue
        # R18 过滤
        if r18_mode == 'safe' and _is_r18(illust.tags_list):
            continue
        # tag_mode = 'and'：排除不包含 query 中所有标签的作品
        # （单标签时 tag_mode 不影响，但保留兼容）
        # 预取是单标签，tag_mode 仅影响 or/and 语义；and 要求作品包含该标签
        # 由于预取结果本身已按标签搜索，单标签一定命中，故不额外过滤
        filtered.append(illust)

    # 排序
    if sort_order == 'popular_d':
        filtered.sort(key=lambda x: x.bookmark_count, reverse=True)
    else:  # date_d
        filtered.sort(key=lambda x: x.upload_date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    # 分页
    total = len(filtered)
    page = filtered[offset:offset + limit]
    has_more = (offset + limit) < total
    next_offset = offset + limit if has_more else 0

    results = [i.to_dict() for i in page]
    return results, has_more, next_offset


def _is_r18(tags: list[str]) -> bool:
    return 'R-18' in tags or 'R-18G' in tags
```

- [ ] **Step 2: Modify /search to detect cache hit**

```python
# app.py — in search() function (line 580), after parsing query params (line 632), add:

    # 检查是否命中缓存（仅 tag 搜索、首次搜索、单标签精确匹配）
    cache_hit = False
    if search_type == 'tag' and not cursor_str and query:
        with get_session() as db:
            sc = db.query(SearchCache).filter(
                SearchCache.tag == query.strip(),
                SearchCache.status == 'done',
            ).first()
            if sc:
                cache_hit = True
                cached_at = sc.cached_at.isoformat() if sc.cached_at else None
```

- [ ] **Step 3: In cache hit path, submit a cache query task instead of Pixiv search**

```python
# app.py — after the cache_hit check, inside search():

    if cache_hit:
        def _cache_fn():
            # 过滤参数
            mb = min_bookmarks
            so = sort_order
            tm = tag_mode
            rm = r18_mode
            limit = ITEMS_PER_PAGE
            results, has_more, next_offset = query_cached_tag(
                query.strip(), mb, so, tm, rm, offset=0, limit=limit
            )
            # 编码游标
            next_cursor = None
            if has_more:
                next_cursor = encode_cursor({
                    'type': 'tag',
                    'query': query.strip(),
                    'sort': so,
                    'tag_mode': tm,
                    'r18_mode': rm,
                    'min_bookmarks': mb,
                    'cache_offset': next_offset,
                    'created_at': int(time.time()),
                })
            resp = {
                'results': results,
                'cursor': next_cursor,
                'has_more': has_more,
                'fetch_stats': {'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0},
                'cached_at': cached_at,
                'source': 'cache',
            }
            return resp

        task_id = _submit_search_task(_cache_fn)
        return jsonify({'task_id': task_id, 'status': 'running'})
```

- [ ] **Step 4: Handle cache cursor in paginated_search path**

Add a separate route or modify the cursor handling in `/search` to detect cache cursor and call `query_cached_tag` with offset:

```python
# app.py — in search(), after the existing cursor_data handling (line 617), add:

    # 检测缓存游标
    if cursor_data and 'cache_offset' in cursor_data:
        offset = cursor_data.get('cache_offset', 0)
        tag = cursor_data.get('query', '')
        def _cache_page_fn():
            results, has_more, next_offset = query_cached_tag(
                tag, min_bookmarks, sort_order, tag_mode, r18_mode,
                offset=offset, limit=ITEMS_PER_PAGE
            )
            next_cursor = None
            if has_more:
                next_cursor = encode_cursor({
                    **cursor_data,
                    'cache_offset': next_offset,
                    'created_at': int(time.time()),
                })
            resp = {
                'results': results,
                'cursor': next_cursor,
                'has_more': has_more,
                'fetch_stats': {'detail_fetched': 0, 'detail_failed': 0, 'seconds': 0.0},
                'cached_at': cursor_data.get('cached_at'),
                'source': 'cache',
            }
            return resp

        task_id = _submit_search_task(_cache_page_fn)
        return jsonify({'task_id': task_id, 'status': 'running'})
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app.py fetcher.py
git commit -m "feat: cache hit path in /search with query_cached_tag"
```

---

### Task 5: API endpoints for prefetch management

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add import and helper**

```python
# app.py — add at top
import json
from models import SearchCache, CollectionItem
```

- [ ] **Step 2: Add API endpoints**

```python
# app.py — add after the existing /api/auto-follow/config endpoints (around line 1256)

@app.route('/api/prefetch/config', methods=['GET'])
def prefetch_config_get() -> Response:
    return jsonify({
        'interval': _prefetch_state['interval'],
        'pages': _prefetch_state['pages'],
        'max_illusts': _prefetch_state['max_illusts'],
    })

@app.route('/api/prefetch/config', methods=['POST'])
@_csrf_required
def prefetch_config_post() -> Response:
    body = request.get_json(silent=True) or {}
    for key in ('interval', 'pages', 'max_illusts'):
        if key in body:
            try:
                val = max(0, int(body[key]))
                _prefetch_state[key] = val
            except (ValueError, TypeError):
                return jsonify({'error': f'{key} must be integer'}), 400
    return jsonify(_prefetch_state)

@app.route('/api/prefetch/tags', methods=['GET'])
def prefetch_tags_get() -> Response:
    with get_session() as db:
        tags = db.query(SearchCache).order_by(SearchCache.cached_at.desc().nullsfirst()).all()
        return jsonify([{
            'tag': t.tag,
            'cached_at': t.cached_at.isoformat() if t.cached_at else None,
            'status': t.status,
            'total': t.total,
            'error': t.error,
        } for t in tags])

@app.route('/api/prefetch/tags', methods=['POST'])
@_csrf_required
def prefetch_tags_add() -> Response:
    body = request.get_json(silent=True) or {}
    tag = body.get('tag', '').strip()
    if not tag:
        return jsonify({'error': '标签不能为空'}), 400
    with get_session() as db:
        existing = db.query(SearchCache).filter(SearchCache.tag == tag).first()
        if existing:
            return jsonify({'error': '标签已存在'}), 409
        db.add(SearchCache(tag=tag))
        safe_commit(db)
    return jsonify({'tag': tag}), 201

@app.route('/api/prefetch/tags/<path:tag>', methods=['DELETE'])
@_csrf_required
def prefetch_tags_delete(tag: str) -> Response:
    with get_session() as db:
        sc = db.query(SearchCache).filter(SearchCache.tag == tag).first()
        if not sc:
            return jsonify({'error': '标签不存在'}), 404
        illust_ids = json.loads(sc.illust_ids) if sc.illust_ids else []
        # 清理不再被引用的预取作品
        for pid in illust_ids:
            refs = db.query(SearchCache).filter(
                SearchCache.illust_ids.like(f'%{pid}%'),
                SearchCache.tag != tag,
            ).count()
            if refs > 0:
                continue
            illust = db.query(Illust).filter(
                Illust.pixiv_id == pid,
                Illust.prefetch_source == 1,
                Illust.download_status != 'done',
                ~Illust.local_paths.isnot(None),
            ).first()
            if illust:
                coll_ref = db.query(CollectionItem).filter(CollectionItem.pixiv_id == pid).first()
                if not coll_ref:
                    db.delete(illust)
        db.delete(sc)
        safe_commit(db)
    return jsonify({'tag': tag})

@app.route('/api/prefetch/status', methods=['GET'])
def prefetch_status_get() -> Response:
    return jsonify({
        'running': _prefetch_state['running'],
        'last_check': _prefetch_state['last_check'],
        'interval': _prefetch_state['interval'],
    })

@app.route('/api/prefetch/refresh', methods=['POST'])
@_csrf_required
def prefetch_refresh() -> Response:
    body = request.get_json(silent=True) or {}
    tag = body.get('tag', '').strip()
    if not tag:
        return jsonify({'error': '标签不能为空'}), 400
    with get_session() as db:
        sc = db.query(SearchCache).filter(SearchCache.tag == tag).first()
        if not sc:
            return jsonify({'error': '标签不存在'}), 404
        if sc.status == 'fetching':
            return jsonify({'error': '该标签正在刷新中'}), 409
    # 立即在后台线程预取
    threading.Thread(target=lambda: _prefetch_one_tag(tag), daemon=True).start()
    return jsonify({'tag': tag, 'status': 'refreshing'})
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: add prefetch management API endpoints"
```

---

### Task 6: Frontend—search page cache annotation + refresh button

**Files:**
- Modify: `templates/index.html`

- [ ] **Step 1: Show cache annotation in search results**

In `templates/index.html`, in `finishSearch` function (line 669), after `maybeToastFetchStats(data.fetch_stats)` (line 683), add:

```javascript
// 缓存标注
if (data.source === 'cache' && data.cached_at) {
    const cachedTime = new Date(data.cached_at).toLocaleString('zh-CN');
    let badge = document.getElementById('cacheBadge');
    if (!badge) {
        badge = document.createElement('div');
        badge.id = 'cacheBadge';
        badge.style.cssText = 'text-align:center;font-size:0.75rem;color:var(--text-muted);margin-bottom:8px;';
        $('#masonryGrid').before(badge);
    }
    badge.innerHTML = `缓存结果 · 更新于 ${cachedTime}
        <button class="btn btn-sm btn-soft ms-2" id="refreshCacheBtn" style="font-size:0.7rem;padding:1px 8px;">刷新</button>`;
    $('#refreshCacheBtn')?.addEventListener('click', async () => {
        const tag = $('#searchQuery').value.trim();
        if (!tag) return;
        $('#refreshCacheBtn').disabled = true;
        $('#refreshCacheBtn').textContent = '刷新中...';
        try {
            const resp = await fetch('/api/prefetch/refresh', {
                method: 'POST',
                headers: {'Content-Type':'application/json','X-CSRF-Token':csrfToken},
                body: JSON.stringify({tag}),
            });
            if (resp.ok) {
                showToast('已开始刷新缓存，稍后重新搜索即可看到更新', false);
            } else {
                showToast('刷新失败', true);
            }
        } catch { showToast('网络错误', true); }
        finally {
            $('#refreshCacheBtn').disabled = false;
            $('#refreshCacheBtn').textContent = '刷新';
        }
    });
} else {
    const badge = document.getElementById('cacheBadge');
    if (badge) badge.remove();
}
```

- [ ] **Step 2: Verify no syntax errors**

Run: `pytest` (just ensure app loads)
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add templates/index.html
git commit -m "feat: show cache annotation and refresh button in search results"
```

---

### Task 7: Frontend—settings page prefetch tag list management

**Files:**
- Modify: `templates/settings.html`

- [ ] **Step 1: Add tag list management UI within the prefetch card**

```html
<!-- templates/settings.html — inside the "搜索预取" card, after config inputs -->
      <div class="mb-0">
        <label class="form-label">预取标签清单</label>
        <div id="prefetchTagList" class="mb-2" style="display:flex;flex-wrap:wrap;gap:4px;"></div>
        <div class="input-group input-group-sm" style="max-width:300px;">
          <input class="form-control form-control-sm" id="prefetchTagInput" type="text" placeholder="输入标签名">
          <button class="btn btn-primary btn-sm" id="addPrefetchTagBtn">添加</button>
        </div>
        <div class="form-text">添加标签后，定时预取会自动拉取该标签的新作品。点击标签可删除。</div>
      </div>
```

- [ ] **Step 2: Add JS for tag list management in settings.html's inline script**

```javascript
// templates/settings.html — inside the settings <script> (around line 170)
let prefetchTags = [];

async function loadPrefetchTags() {
    try {
        prefetchTags = await fetch('/api/prefetch/tags').then(r => r.json());
        renderPrefetchTags();
    } catch {}
}

function renderPrefetchTags() {
    const list = document.getElementById('prefetchTagList');
    if (!list) return;
    list.innerHTML = prefetchTags.map(t =>
        `<span class="badge" style="background:var(--accent);font-size:0.75rem;cursor:pointer;" data-tag="${escAttr(t.tag)}" title="状态: ${t.status} 条数: ${t.total}">
            ${escHtml(t.tag)}
            <span style="opacity:.6;margin-left:2px;">&times;</span>
        </span>`
    ).join('');
    list.querySelectorAll('[data-tag]').forEach(el => {
        el.addEventListener('click', () => removePrefetchTag(el.dataset.tag));
    });
}

async function addPrefetchTag(tag) {
    const r = await fetch('/api/prefetch/tags', {
        method: 'POST',
        headers: {'Content-Type':'application/json','X-CSRF-Token':csrfToken},
        body: JSON.stringify({tag}),
    });
    if (r.ok) {
        showToast('已添加', false);
        loadPrefetchTags();
    } else {
        const err = await r.json().catch(() => ({}));
        showToast(err.error || '添加失败', true);
    }
}

async function removePrefetchTag(tag) {
    const r = await fetch(`/api/prefetch/tags/${encodeURIComponent(tag)}`, {
        method: 'DELETE',
        headers: {'X-CSRF-Token':csrfToken},
    });
    if (r.ok) {
        showToast('已删除', false);
        loadPrefetchTags();
    } else {
        showToast('删除失败', true);
    }
}

document.getElementById('addPrefetchTagBtn')?.addEventListener('click', () => {
    const input = document.getElementById('prefetchTagInput');
    const tag = input.value.trim();
    if (tag) { addPrefetchTag(tag); input.value = ''; }
});
document.getElementById('prefetchTagInput')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('addPrefetchTagBtn')?.click();
});

// Call loadPrefetchTags() on page load
loadPrefetchTags();
```

- [ ] **Step 3: Add showToast to settings.html if not present**

```javascript
// Check if showToast is defined; if not, add a simple version
if (typeof showToast !== 'function') {
    function showToast(msg, error) {
        const div = document.createElement('div');
        div.textContent = msg;
        div.style.cssText = `position:fixed;bottom:16px;left:50%;transform:translateX(-50%);padding:6px 16px;
            border-radius:6px;background:${error?'#dc3545':'#198754'};color:#fff;font-size:0.85rem;z-index:9999;`;
        document.body.appendChild(div);
        setTimeout(() => div.remove(), 3000);
    }
}
```

- [ ] **Step 4: Commit**

```bash
git add templates/settings.html
git commit -m "feat: prefetch tag list management in settings page"
```

---

### Task 8: Description removal in fetcher and templates

**Files:**
- Modify: `fetcher.py`, `templates/detail.html`, `templates/gallery.html`, `templates/index.html`

- [ ] **Step 1: Remove description from fetcher.py**

```python
# fetcher.py — in _illust_from_item (line 689-711), remove:
    description=detail.get('description', '') if detail else '',
# Change to:
    # description field removed globally

# fetcher.py — in _illust_from_detail (line 714-729), remove:
    description=detail.get('description', ''),
# Change to:
    # description field removed globally
```

- [ ] **Step 2: Remove description from templates**

Search for `description` in all template files and remove description display sections:

```bash
# grep for description usage in templates
grep -n "description" templates/*.html
```

Expected changes:
- `templates/detail.html`: remove any `.description` or `{{ illust.description }}` display
- `templates/gallery.html`: remove any description display  
- `templates/index.html`: remove any description display in search result cards

- [ ] **Step 3: Run tests**

Run: `pytest -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add fetcher.py templates/detail.html templates/gallery.html templates/index.html
git commit -m "refactor: remove description field from fetcher and templates"
```

---

### Task 9: Final integration test and documentation

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Run full test suite**

Run: `pytest -v`
Expected: all tests pass

- [ ] **Step 2: Update AGENTS.md**

Add `_prefetch_state` to the in-memory state list in the "进程与状态" section:
```markdown
- **Gunicorn 必须用 `-w 1`**：以下状态在进程内存中 — `_auto_follow_state`、`download_locks`、`download_cancellations`、`_queued_downloads`、`_download_progress`、`_bulk_tasks`、`_search_tasks`、`_rate_limit_store`、`_prefetch_state`。多 worker 不共享。详见 `app.py:170-181` 注释。
```

Add config constants to the "配置与重启" section:
```markdown
- **预取配置**：新增 `PREFETCH_INTERVAL`、`PREFETCH_PAGES`、`PREFETCH_MAX_ILLUSTS`（`config.py`），`settings.json` 键 `prefetch_interval`、`prefetch_pages`、`prefetch_max_illusts`。运行时通过 `/api/prefetch/config` 改内存立即生效，持久化需从设置页保存。
```

- [ ] **Step 3: Final commit**

```bash
git add AGENTS.md
git commit -m "docs: update AGENTS.md with prefetch state and config"
```