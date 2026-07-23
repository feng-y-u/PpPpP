# 翻页修复设计

日期：2026-07-23 | 状态：已批准

---

## 背景

`/search` 搜索结果的翻页存在 4 个 bug，导致分页行为异常。

---

## Bug 1：翻页断裂

**位置**：`templates/index.html` `loadMore()` 函数

**现象**：当前页所有作品都被屏蔽标签/收藏数下限过滤掉时，后端返回 `results=[], has_more=true`。前端判空直接 return，跳过 `has_more` 检查，后续页码无法加载。

**修复**（1 行）：空结果时先读取 `data.has_more` 再退出。

```javascript
// 改前
if (!data.results.length) { hasMorePages = false; updateLoadMore(); return; }

// 改后
if (!data.results.length) { hasMorePages = data.has_more || false; updateLoadMore(); return; }
```

---

## Bug 2：起始页输入框被覆盖

**位置**：`templates/index.html` `loadMore()` 函数

**现象**：`loadMore()` 把 `$('#startPage').value = currentPage + 1`，用户看到的"起始页"变成下一页编号，之后重新搜索会从错误页码开始。

**验证**：`loadMore()` 请求 URL 使用 `page:String(currentPage)`（JS 变量），不从 `#startPage` DOM 读取。删掉写入行不影响功能。

**修复**（1 行删）：删除 `$('#startPage').value = currentPage + 1`。

---

## Bug 3：发现页 has_more 不准

**位置**：`fetcher.py` `browse_discovery()` 函数

**现象**：Pixiv API 的 `total` 包含漫画/小说等非 illust 类型，但代码只展示 illust。`has_more` 按原始 total 计算，可能误判有下一页。

**修复**（2 行）：过滤后 illust 少于 PER_PAGE 时，强制 `has_more = False`。

```python
# has_more 计算之后追加
if len(illusts_data) < PER_PAGE:
    has_more = False
```

---

## Bug 4：屏蔽标签缓存延迟

**位置**：`fetcher.py` 搜索缓存 + `app.py` 屏蔽标签接口

**现象**：搜索结果缓存 30 秒，key 不含屏蔽标签。添加/移除屏蔽标签后 30 秒内重搜同一关键词仍命中旧缓存，返回已屏蔽的内容。

**修复**：
- `fetcher.py`：新增 `clear_search_cache()` 函数，获取锁后 `_SEARCH_CACHE.clear()`
- `app.py`：`add_blocked_tag()` 和 `remove_blocked_tag()` 成功后调用 `clear_search_cache()`

---

## 涉及文件

| 文件 | 改动 |
|------|------|
| `templates/index.html` | Bug 1 + Bug 2，2 处 JS 改动 |
| `fetcher.py` | Bug 3 + Bug 4，`has_more` 保护行 + `clear_search_cache()` 函数 |
| `app.py` | Bug 4，屏蔽标签接口中调用缓存清空 |

---

## 边界情况

- **所有页都被过滤**：Page 1 返回空 + has_more=false，显示空状态 → 正确
- **第 1 页之后的全空页**：loadMore 读到空结果 + has_more=true → 继续加载下一页直到找到内容或 has_more=false → 正确
- **PER_PAGE 刚好满但实际是末页**：Bug 3 保护不会触发（len == PER_PAGE），has_more 由 API total 决定 → 多一次空请求但自然停止
- **30s 内反复改屏蔽标签**：每次变更都清缓存 → 始终最新
