# 累积式预取缓存 + 低收藏优先清理设计

- 日期：2026-08-13
- 状态：待实现
- 相关代码：`app.py`、`config.py`、`templates/settings.html`、`tests/test_prefetch.py`

## 背景与目标

当前预取是**快照覆盖式**：每轮预取把 `SearchCache.illust_ids` 整体替换为本次抓到的 N 页，导致：
- 上一轮缓存的作品从缓存页消失（用户观察：171 → 161）
- 缓存页只能浏览"最近一次快照"，看不到历史累积的作品

用户要求改为**累积式**：每轮预取把新结果**合并**进 `illust_ids`（新旧去重保留），条数只增不减，由容量清理兜底。

同时调整容量清理策略：
- 上限从 `PREFETCH_MAX_ILLUSTS=20000` 改为 **10000**
- 删除策略从"最旧标签尾部优先"改为 **优先删除收藏数低的作品**

## 1. 累积合并（`app.py` `_prefetch_one_tag`）

预取完成后（`app.py:309-317`），把本次 `all_ids` 与现有列表合并去重：

```python
old_ids = json.loads(sc.illust_ids) if sc.illust_ids else []
seen = set(all_ids)
merged = list(all_ids) + [pid for pid in old_ids if pid not in seen]  # 新结果在前，旧作品去重保留在后
row.illust_ids = json.dumps(merged, ensure_ascii=False)
row.total = len(merged)
```

行为：
- 每轮新增作品加入列表，之前抓过的作品保留
- 条数只增不减（直到容量清理触发）
- 缓存页可浏览该标签历次累积的作品

边界：
- 预取失败（status=error）：保持上次成功的 `illust_ids`（现有异常分支不变）

## 2. 容量清理重写（`app.py` `_prefetch_capacity_cleanup`）

从"按最旧标签尾部删"改为"全局按收藏数低优先删"：

```python
def _prefetch_capacity_cleanup() -> None:
    with get_session() as db:
        count = db.query(Illust).filter(Illust.prefetch_source == 1).count()
        if count <= _prefetch_state['max_illusts']:
            return
        need_free = count - _prefetch_state['max_illusts']

        fav_ids = {c.pixiv_id for c in db.query(CollectionItem.pixiv_id).all()}
        candidates = []
        for i in db.query(Illust).filter(Illust.prefetch_source == 1).all():
            if i.download_status == 'done' or i.local_paths_list:
                continue
            if i.pixiv_id in fav_ids:
                continue
            candidates.append(i)

        candidates.sort(key=lambda x: (x.bookmark_count, x.upload_date or datetime.min))
        to_delete = [c.pixiv_id for c in candidates[:need_free]]
        if not to_delete:
            return

        to_delete_set = set(to_delete)
        for sc in db.query(SearchCache).all():
            try:
                ids = json.loads(sc.illust_ids) if sc.illust_ids else []
            except (json.JSONDecodeError, TypeError):
                continue
            new_ids = [p for p in ids if p not in to_delete_set]
            if len(new_ids) != len(ids):
                sc.illust_ids = json.dumps(new_ids, ensure_ascii=False)
        safe_commit(db)

        db.query(Illust).filter(Illust.pixiv_id.in_(to_delete)).delete(synchronize_session=False)
        safe_commit(db)
        logger.info(f'[prefetch] 容量清理: 删除 {len(to_delete)} 条低收藏预取作品')
```

要点：
- 候选筛选：`prefetch_source==1` 且**未下载**（`download_status != 'done'` 且无 `local_paths`）且**未收藏**（不在任何 `CollectionItem`）
- 排序：`bookmark_count` 升序（低收藏优先），并列时 `upload_date` 早的先删
- 删除前从**所有** `SearchCache.illust_ids` 移除该 pid（统一遍历重写，不再按标签轮换）
- `_collect_other_tag_pids` 仍被 `prefetch_tags_delete` 使用，保留

## 3. 默认值与设置页

- `config.py`：`PREFETCH_MAX_ILLUSTS = 20000` → `10000`
- `app.py` `_SETTINGS_DEFAULTS`：`prefetch_max_illusts: 20000` → `10000`
- `templates/settings.html`：`prefetch_max_illusts` 的 `form-text` 说明改为"…超限后自动删除收藏数最低的未下载未收藏作品"

## 4. 测试（`tests/test_prefetch.py`）

更新现有容量清理测试的断言方向（最旧优先 → 低收藏优先），并新增：
- `test_prefetch_one_tag_accumulates`: 第二次预取 mock `search_by_tag` 返回新一批 ID，断言 `illust_ids` = 新 + 旧去重，`total` 增大
- `test_capacity_cleanup_low_bookmark_first`: 构造不同收藏数的预取作品，超限后断言低收藏的被删、高收藏的保留
- 保留：已下载/已收藏不删、引用不误删（更新断言）

## 5. 不做

- 不加每标签独立上限（全局 10000 兜底）
- 不改变搜索行为（仍走实时）

## 6. 验证

- `pytest tests/test_prefetch.py tests/test_search_cache.py tests/test_cache_page.py -v` 全过
- `pytest -q` 全量无回归
