# 搜索预取缓存设计

- 日期：2026-08-12
- 状态：待实现
- 相关代码：`app.py`、`models.py`、`fetcher.py`、`config.py`、`templates/index.html`、`templates/settings.html`

## 背景与目标

搜索慢：每次搜索都实时调用 Pixiv 搜索 API 逐页抓取并并行补全详情，耗时数秒到数十秒。

目标：定时预取「常用标签」的搜索结果存入数据库，用户搜索命中预取标签时**零网络请求秒回**。

## 需求摘要（已与用户确认）

1. **预取标签**：手动配置清单（设置页增删），不自动学习
2. **数据范围**：宽松参数预取（收藏数 ≥ 0、R18 不过滤），完整作品数据写入 `Illust` 表；**全局移除 `description` 字段**（不只缓存不要，整个系统都不再需要描述）
3. **命中方式**：搜索命中即秒回缓存 + 标注缓存时间 + 「立即刷新」按钮
4. **调度**：应用内后台 daemon 线程，interval 设置页可配（0 = 禁用）
5. **二次过滤**：命中后在库内按用户的 `min_bookmarks` / R18 / 排序过滤排序，不重新抓 Pixiv
6. **R18 说明**：用户账号在 Pixiv 屏蔽了 R18，API 返回结果天然不含 R-18 作品；保留库内 R-18 过滤逻辑（成本低），但注明当前空转，未来开启 R18 后自动生效
7. **容量上限**：最大缓存条数，超限后从最旧的预取数据开始删除

## 1. 数据模型

### 新增 `SearchCache` 表（`models.py`）

```python
class SearchCache(Base):
    __tablename__ = 'search_cache'
    tag: Mapped[str] = mapped_column(String, primary_key=True)   # 标签名（缓存键），同时代表「配置了预取」
    illust_ids: Mapped[str] = mapped_column(Text, default='[]')  # JSON 数组，该标签结果的 pixiv_id 有序列表（Pixiv 原始顺序）
    cached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 最近一次成功刷新时间
    status: Mapped[str] = mapped_column(String, default='idle')  # idle | fetching | done | error
    error: Mapped[str] = mapped_column(String, default='')
    total: Mapped[int] = mapped_column(Integer, default=0)       # 最近一次预取抓到的条数
```

- 新表由 `create_all()` 自动建，无需 ALTER TABLE
- 标签清单 = 该表中的行：设置页增删标签即插入/删除行

### `Illust` 表变更

- **新增列** `prefetch_source: Mapped[int] = mapped_column(Integer, default=0)`，1 = 由预取写入（用于容量清理时区分）
- **移除** `description` 字段（`models.py:66`）与 `to_dict()` 中的 `'description'` 键（`models.py:124`）

### `init_db()` 迁移

- `ALTER TABLE illusts ADD COLUMN prefetch_source INTEGER DEFAULT 0`（沿用现有迁移模式）
- `ALTER TABLE illusts DROP COLUMN description`（仿现有 `is_favorite` 的 DROP 逻辑；SQLite < 3.35 时走建表重建路径，重建时保留 `prefetch_source` 等新列）

## 2. 后台预取线程

### 状态与配置

- 内存状态 `_prefetch_state = {'running': False, 'last_check': None, 'interval': ..., 'pages': ..., 'max_illusts': ...}`，沿用 `_auto_follow_state` 的模式（`-w 1` 下正常，加入 AGENTS.md 内存状态清单）
- `config.py` 新增常量与 `settings.json` 键映射：
  - `PREFETCH_INTERVAL = 3600`（秒，0 = 禁用）↔ `prefetch_interval`
  - `PREFETCH_PAGES = 3`（每标签预取页数，60 条/页）↔ `prefetch_pages`
  - `PREFETCH_MAX_ILLUSTS = 20000`（预取来源作品上限）↔ `prefetch_max_illusts`
- daemon 线程：启动时若 interval > 0 且清单非空，**首轮立即预取**；之后每 interval 循环

### 预取参数（宽松全覆盖）

`search_by_tag(tag, min_bookmarks=0, sort='date_d', r18_mode='all', tag_mode='or')`

- **排序用 `date_d` 而非 `popular_d`**：`popular_d` 需 Pixiv Premium，非 Premium 静默返回空结果。命中后的人气排序在库内用 `bookmark_count` 降序近似
- `min_bookmarks=0` 保证宽口径，用户搜索任意收藏数门槛都能在库内过滤命中

### 预取流程（串行，尊重限流）

对每个清单内标签依次执行（一次一个，不并发打 Pixiv）：

1. 置 `status='fetching'`
2. 逐页抓取 `pages` 页 → 作品 **upsert** 到 `Illust`（已存在则更新 `bookmark_count`/`upload_date`/`thumb_url`/`tags`/`original_urls`/`page_count`；新插入时 `prefetch_source=1`；**不写 description**）
3. 更新 `SearchCache` 行：`illust_ids`、`cached_at`、`status='done'`、`total`
4. 单标签失败：`status='error'` + 记录 `error`，跳过该标签继续下一个，下轮自动重试
5. 全部标签完成后触发一次容量清理

### 容量清理

每次预取完成后，若 `prefetch_source=1` 的 `Illust` 行数 > `PREFETCH_MAX_ILLUSTS`：

- 按 `SearchCache.cached_at` 从最旧的标签开始，从其 `illust_ids` 尾部（最早预取、最过时）批量移除条目，同步删除对应 `Illust` 行，直到 ≤ 上限
- **删除某行须同时满足**（防误删，优先级最高）：
  1. `prefetch_source = 1`（只动预取数据）
  2. 未下载（`download_status != 'done'` 且无本地文件）
  3. 未被任何 `CollectionItem` 引用（未收藏）
  4. 已从所有 `SearchCache.illust_ids` 中移除
- 已下载/已收藏的作品即使来自预取也永不删除

## 3. 搜索命中流程（前端几乎零改动）

### 命中判断（`/search`，`app.py:580`）

- **首次搜索**（`cursor_str` 为空）：满足以下条件才命中缓存，否则走现有异步 Pixiv 流程（不变）：
  1. `search_type == 'tag'`
  2. `query.strip()` 与某个 `SearchCache.tag` **精确匹配**（单标签；多标签逗号组合搜索不命中，走实时）
  3. 该行 `status == 'done'`
- **翻页**（携带 `cursor_str`）：仅当游标解码后为缓存游标（含 `cache_offset`）时继续走缓存分页；否则走现有 Pixiv 翻页流程

### 缓存任务（保持异步协议，秒级完成）

命中时 `/search` **仍返回 `task_id`**，后台任务不调 Pixiv，执行 `query_cached_tag(...)`：

1. 取 `illust_ids` → `WHERE pixiv_id IN (...)` 批量读 `Illust`（缺失的行跳过）
2. 内存过滤：`bookmark_count >= min_bookmarks`；`r18_mode='safe'` 剔除 tags 含 `"R-18"` 的作品；`tag_mode` 匹配（单标签命中下仅 `or` 语义生效）
3. 排序：`date_d` → `upload_date` 降序；`popular_d` → `bookmark_count` 降序（**近似**，注明与 Pixiv 不完全一致）
4. 分页切片 → 返回 `{results, cursor, has_more, cached_at, source: 'cache'}`

### 缓存游标

复用现有 cursor 编码机制（含 `created_at` 时间戳，24h 过期），额外携带 `cache_offset` 字段 + 搜索参数（沿用现有参数恢复/校验逻辑）。前端轮询/翻页/去重/过期恢复逻辑**完全不用改**。

### 页面标注 + 立即刷新

- 结果带 `cached_at`，搜索页结果栏显示「缓存于 <时间>」标签 + 「立即刷新」按钮
- `POST /api/prefetch/refresh {tag}` → 后台 daemon 线程立即预取该标签；若 `status='fetching'`（定时/手动正在跑）则跳过，避免并发打 Pixiv

## 4. 接口与设置页

| 接口 | 说明 |
|------|------|
| `GET /api/prefetch/config` | 读 interval / pages / max_illusts（内存值） |
| `POST /api/prefetch/config` | 写配置：改内存立即生效 + 持久化 settings.json |
| `GET /api/prefetch/tags` | 标签清单及各标签状态 |
| `POST /api/prefetch/tags` `{tag}` | 添加标签（插入 SearchCache 行） |
| `DELETE /api/prefetch/tags/<tag>` | 删除标签（删行 + 清理不再被引用且可安全删除的预取作品） |
| `GET /api/prefetch/status` | 各标签 `cached_at`/`status`/`total` + `last_check` |
| `POST /api/prefetch/refresh` `{tag}` | 立即预取单个标签 |

- 所有写接口走 CSRF（`@_csrf_required`）
- 设置页新增「搜索预取」区块：标签清单增删（显示每标签缓存时间/状态/条数）、interval、预取页数、最大条数配置

## 5. description 移除范围

- `models.py`：字段、`to_dict()` 键
- `init_db()`：DROP COLUMN 迁移
- `fetcher.py`：搜索结果/详情中保存 description 的代码
- 模板：`detail.html`、`gallery.html`、`index.html` 等所有展示描述的位置（实现时逐一排查）
- 相关测试同步更新

## 6. 错误处理

- 预取失败：`status='error'`，下轮重试；Pixiv 请求遵循 `fetcher` 现有限速/退避
- 「立即刷新」与定时预取互斥（`status='fetching'` 跳过）
- 命中缓存但个别 `Illust` 行已被容量清理删除：跳过缺失 id，不影响返回

## 7. 测试策略

- `SearchCache` 模型：增删、`illust_ids` 序列化
- 库内过滤/排序/分页纯函数单元测试（min_bookmarks、R-18、tag_mode、date/popular 排序、翻页切片）
- 容量清理：超限删最旧、下载/收藏永不删、跨标签引用不误删
- `/search` 命中与未命中路径（mock `fetcher`）、缓存任务返回结构（含 `cached_at`/`source`）
- description 迁移（DROP 后查询/写入正常）
- 设置页接口（CSRF、配置持久化）

## 8. 文档更新

- `AGENTS.md`：内存状态清单追加 `_prefetch_state`；config 新增 3 个常量与 settings.json 映射；数据库章节补充 `SearchCache` 表与 `prefetch_source` 列

## 边界 / 明确不做

- 多标签组合搜索（逗号分隔）不命中缓存，走实时
- `popular_d` / `date_d` 为库内近似排序，不保证与 Pixiv 完全一致
- 不更换数据库（SQLite 足够；量级估算：50 标签 × 300 条 ≈ 1.5 万行 ≈ 10–50 MB）
- 不自动学习常用标签（手动配置）
