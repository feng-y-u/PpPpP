# 预取标签同义词合并（组 = 一行）设计

- 日期：2026-11-05
- 状态：已实现（2026-11-05，见文末「验证结果」）
- 相关代码：`models.py`、`migrations/versions.py`、`background.py`、`routes_prefetch.py`、`templates/settings.html`、`static/page-settings.js`、`static/page-cache.js`、`tests/test_migrations.py`、`tests/test_models.py`、`tests/test_prefetch.py`、`tests/test_prefetch_api.py`、`tests/test_cache_page.py`

## 背景与问题（真实案例）

同一个作品主题在 Pixiv 上常被写成两个标签，例如「吉星排队」与「星引擎party」。用户把两个词都加入预取后：

1. 缓存页出现两个条目，浏览时必须来回切换才能看到全集；
2. 两个标签命中的作品高度重叠，两条 `SearchCache` 各自保存一份 `illust_ids`，用户看到的"条数"接近翻倍；
3. 同一作品在两边各出现一次，无法一眼回答"这个主题到底有多少张"。

这两个标签不是字符串相近，无法自动识别，只能由用户手工声明"它们是一个意思"。

## 目标

把"一个意思的多个标签"合并为**一个缓存分组**：

1. 一个组只占一个缓存条目，浏览/计数/排序看到的是组内并集；
2. 组内 `illust_ids` 按 `pixiv_id` 并集去重，重叠作品只出现一次；
3. 预取仍然分别搜索组名与每个同义词（不同词命中不同作品，必须分开搜），结果写进同一行；
4. 存量已有两个独立标签时，添加同义词即可就地合并、去重并删掉多余行；
5. 不改变 `/search` 的实时搜索语义，不影响图库/下载/容量清理。

## 方案：组 = 一行

一个分组就是一条 `SearchCache` 行：

- `tag`：组名，也是缓存页下拉里显示的名字；
- `alias_tags`（新增）：组名之外的**额外搜索词**，JSON 数组；
- 实际搜索词（sources）= `[tag] + alias_tags`，**组名永远参与搜索**；
- `illust_ids`：所有 sources 结果的并集去重；
- `total`：并集大小；`cached_at`：本轮至少一个 source 成功的时间。

不采用"新建分组表"或"settings.json 存别名"：前者要把缓存页、删除、容量清理、跨标签引用判定全部改成按组聚合；后者让别名这种**数据**混进配置，删组/改名会留下脏映射，且 id 合并的代码一行也省不掉。

## 数据模型与迁移

- `SearchCache` 新增 `alias_tags: Mapped[str] = mapped_column(Text, default='[]')`。
- 属性 `alias_tags_list`：解析失败返回 `[]`（与 `Illust.tags_list` 同款容错）；setter 写 JSON。
- 迁移 **v6** `add_search_cache_alias_tags`：`ALTER TABLE search_cache ADD COLUMN alias_tags TEXT NOT NULL DEFAULT '[]'`；追加进 `MIGRATIONS`。全新库由 `create_all()` 直接建全。
- `init_db()` 在迁移后额外幂等调一次该函数（与 `add_illust_refresh_failed_at` 同款），覆盖"库被外部改动丢列"的场景。
- 老数据 `alias_tags='[]'` → sources 就是 `[tag]`，行为与现在完全一致，无需数据迁移。

## 预取语义

`background._prefetch_one_tag(tag)`：

1. 抢占 `status='fetching'`（沿用现有原子 UPDATE）。
2. 读取 `sources = [row.tag] + row.alias_tags_list`。
3. 逐个 source 跑现有分页搜索（`app.search_by_tag(..., min_bookmarks=1, pages, date_d, r18=all, tag_mode=or)`）。
   - 单个 source 失败（网络/限流/其它）：记录 `(source, 原因)` 后继续下一个；
   - `PixivAuthError`：所有 source 会因同一原因失败，记录后**中止剩余 source**，不空耗限流。
4. 合并：`all_ids` 跨 source 用 `dict.fromkeys` 去重（保持"新抓的在前"），再与行内旧 id 合并去重写回；`total = len(merged)`。
5. `cached_at` 在**至少一个 source 成功**时更新。
6. 状态：
   - 全部 source 成功 → `status='done'`，`error=''`；
   - 部分成功 → `status='done'`，`error='部分同义词失败：a（原因）；b（原因）'`（截断长度）；
   - 全部失败 → `status='error'`，`error` 同上；
   - 外层异常（DB 等）→ 沿用现有 catch → `status='error'`。
7. 竞态兜底：`_prefetch_one_tag` 发现自己要处理的 tag 在库里已不存在、但已成为**别组的同义词**时直接返回，不重建独立行（否则刚合并掉的旧行会被下一轮重新建出来，重复条目复辟）。
8. 合并写回仍持 `_search_cache_guard`（与现有累积合并同一把锁）。

**为什么不会重复花详情请求**：`paginated_search` 对已在库的作品走 `existing_map` 命中，直接发布、不拉详情；第二个同义词命中的重叠作品只多花搜索请求，不会重复请求详情。

## API

- `GET /api/prefetch/tags`：每行增加 `aliases`（数组），其余字段不变。
- `POST /api/prefetch/tags/<path:tag>/aliases`，body `{"alias": "星引擎party"}`：
  - 404：组不存在；
  - 400：alias 为空、或与组名相同；
  - 409：该词已是**任一分组**的同义词；
  - 若该词已是一条**独立标签行**：合并其 `illust_ids`（组在前、去重）、吸收其 `alias_tags`、`cached_at` 取两者较新值、删除该行（存量两个标签就地合并）；
    - 该行 `status='fetching'` 时返回 409，避免丢掉在途结果；
  - 否则直接追加。
- `DELETE /api/prefetch/tags/<path:tag>/aliases`，body `{"alias": "..."}`：从 `alias_tags` 删除；404 组或别名不存在。
  - **只停止以后搜索，已缓存作品留在组里**：没有逐词来源记录，无法精确拆分；后续由容量清理按常规淘汰。
- `POST /api/prefetch/tags`：新增标签时若该词已是某组同义词 → 409，避免又建出重复行。
- 以上 POST/DELETE 一律 `_csrf_required`；同义词增删与预取写 id 共用 `_search_cache_guard`。

## 前端

- 设置页「预取标签清单」：每个 chip 增加 `+`（title「添加同义词」），点击 `prompt()` 输入后 POST；`stopPropagation` 避免触发"点 chip 删除"。
  - 有别名时 chip 文案追加 ` +N`，title 列出 `同义词：a、b`；
  - 删除组的确认文案说明"同义词会一并删除"。
- 缓存页：查询逻辑零改动；`updateCacheMeta` 增加 `· 同义词：a、b`（数据来自已加载的 `/api/prefetch/tags`），并且 `error` 非空就显示（原来只在 `status='error'` 时显示），让"某个同义词失败"可见。
- `/search` 不变：同义词只作用于预取缓存，实时搜索仍是单标签。

## 取舍与边界

- **组名必须是真实搜索词**：不支持"纯显示名"分组（YAGNI；真需要时应另加只显示不搜索的概念）。
- **删除同义词不拆分作品**：同上，无 provenance；需要精确清理时用户可自行删除作品或等容量清理。
- **不做自动同义词识别**：两个词可能毫无字符串相似度。
- **不改 `/search`、图库、下载、容量清理**：它们只认 `Illust` 行与行上的 `illust_ids`，并集去重后语义自然成立。
- **大小写/空白**：alias 做 strip；重复判定按精确字符串（与 Pixiv 标签搜索口径一致，不做大小写归一）。

## 影响面

| 文件 | 改动 |
|---|---|
| `migrations/versions.py` | 新增 v6 `add_search_cache_alias_tags`，追加进 `MIGRATIONS` |
| `models.py` | `SearchCache.alias_tags` 列 + `alias_tags_list` 属性；`init_db()` 幂等补列 |
| `background.py` | 新增 `_find_alias_group`；`_prefetch_one_tag` 多 source 循环、并集去重、状态聚合、竞态跳过 |
| `routes_prefetch.py` | `GET` 返回 `aliases`；新增同义词 POST/DELETE；新增标签时拦截"已是别组同义词" |
| `templates/settings.html` | 说明文案（同义词入口） |
| `static/page-settings.js` | chip 的 `+` 入口、`+N` 与 title、删除确认文案 |
| `static/page-cache.js` | meta 显示同义词与非空 error |
| `tests/*` | 迁移、属性、预取、API、缓存页用例（见下） |
| `AGENTS.md` | 「预取缓存」段补同义词语义 |

## 验收 / 测试

1. **迁移**：老库补出 `alias_tags` 且默认 `'[]'`；重复执行幂等；全新库建表即有该列。
2. **模型**：`alias_tags_list` 默认 `[]`、JSON 往返、坏 JSON 返回 `[]`。
3. **预取**：
   - 组名 + 一个同义词各被搜索一次；两边重叠的 pid 只出现一次，`total=并集大小`；
   - 一个 source 失败 → `done` + `error` 指出失败词，成功 source 的 id 仍在；
   - 全部失败 → `error`；
   - 认证失效 → 中止剩余 source（不重复失败）；
   - tag 已成为别组同义词时 `_prefetch_one_tag` 不重建行。
4. **API**：
   - 新 alias 追加成功；重复 409；空/同名 400；组不存在 404；
   - alias 是独立行时：id 并集去重、行被删除、`cached_at` 取较新、`prefetch_source` 不变；
   - alias 行 `fetching` 时 409；
   - 删除 alias：列表移除、作品保留；
   - 新标签撞别组同义词 409；
   - CSRF 矩阵覆盖两个新端点。
5. **缓存页**：`/api/cache/items` 的条目按组行返回并集；meta 文案数据（aliases）可由 `/api/prefetch/tags` 取得。

## 验证结果

**全量套件**：441 passed / 2 skipped / 0 failed（约 20s；其中 2 skipped 是 Windows 上
POSIX 权限位相关用例，既有基线）。

**新增/更新的用例**：

- `tests/test_migrations.py::test_alias_tags_migration_adds_column_with_empty_default`
  —— 老库补列、默认 `'[]'`、重放幂等（不会冲掉已有同义词）；表不存在时 no-op 由
  其余最小库迁移用例覆盖。
- `tests/test_models.py::TestSearchCacheAliasTags` —— 默认空、JSON 往返、坏 JSON 退化。
- `tests/test_prefetch.py::TestPrefetchOneTag` —— 双源并集去重、部分失败保持 done、
  认证失效中止剩余、标签已成同义词时不重建行。
- `tests/test_prefetch_api.py::TestPrefetchAliasesAPI` —— 新增同义词、存量行就地合并
  （并集去重/吸收同义词/`cached_at` 取较新/删行）、六种非法输入矩阵、摘同义词不动作品、
  新标签撞同义词 409。
- `tests/test_app.py` 的 CSRF 全量矩阵补两个新端点。

**真实库副本迁移**（未改动生产库）：复制 `instance/pixiv.db` 后跑 `run_migrations`，
`user_version` 5 → 6，`search_cache` 补出 `alias_tags`，存量行取值 `'[]'`，
1456 条 `illusts` 原样保留。

**实测口径**：合并两个已存在标签时，`total` 与 `illust_ids` 长度等于两边并集
（重叠 pid 只计一次）；缓存页因此不会出现重复卡片。

