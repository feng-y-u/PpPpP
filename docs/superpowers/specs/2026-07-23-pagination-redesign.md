# 翻页重做设计

日期：2026-07-23 | 状态：已批准

---

## 目标

将"加载更多"替换为游标驱动的传统翻页制。后端持续向后抓取 Pixiv，直到攒够一页展示量；前端维护已加载页缓存，支持前后翻页。

---

## 交互模式

```
上一页 ←  [1]  [2]  [3]  → 下一页
```

- "上一页 / 下一页"按钮，首尾页自动禁用
- 仅已加载的页码渲染为可点击元素，当前页高亮
- `loadedPages` 最多缓存 20 页，超出淘汰旧页，被淘汰页不可点
- 点击已缓存页只做视图切换，不发网络请求
- 新搜索：清空 `loadedPages` + `nextCursor`，重置页码，重新请求第 1 页

---

## 配置

| 配置 | 默认值 | 范围 | 说明 |
|------|--------|------|------|
| `ITEMS_PER_PAGE` | 24 | 1-60 | 每页展示件数，可在设置页修改 |
| `CURSOR_SECRET` | 启动生成 | — | 游标签名密钥，存 `instance/.cursor_secret`，不提交版本控制 |

### CURSOR_SECRET 生成

`config.py` 启动时检查 `instance/.cursor_secret`，不存在则 `secrets.token_hex(32)` 生成并写入。`.gitignore` 中排除。

---

## 游标

### 编码

```
cursor = base64(json) + "." + HMAC-SHA256(base64(json), CURSOR_SECRET)
```

### 内容

```json
{
  "type": "tag",
  "query": "猫",
  "sort": "date_d",
  "tag_mode": "or",
  "r18_mode": "all",
  "min_bookmarks": 0,
  "pixiv_page": 4,
  "yielded": 72,
  "created_at": 1753257600
}
```

- `pixiv_page`：下次扫描从 Pixiv 第几页开始
- `yielded`：本次搜索已返回给前端的总作品数
- `created_at`：游标创建时间戳，用于过期检查

### 生命周期

- 有效期 5 分钟 + 5 秒缓冲（`now - created_at > 305`），超时返回 400 `"搜索已过期，请重新搜索"`
- `has_more=false` 时返回 `cursor: null`，前端据此禁用"下一页"
- 验签失败或格式错误返回 400，前端保留当前缓存和游标不变

### 过期后的前端行为

前端根据 `error_code` 而非文案判断行为。收到 `error_code: "CURSOR_EXPIRED"` 时，自动清空 `loadedPages`、`nextCursor`，重置为第 1 页并发起无游标新请求。其他 400 错误（`CURSOR_INVALID` 等）仅 toast 提示，保留缓存和游标不变。

---

## 后端攒页循环

```
pixiv_page = cursor.pixiv_page (无游标时 = 1)
yielded = cursor.yielded (无游标时 = 0)
collected = []
pages_scanned = 0

while len(collected) < items_per_page AND pages_scanned < 10:
    raw_results = fetch_from_pixiv(query, page=pixiv_page)
    if raw_results 无更多数据: break

    filtered = apply_filters(raw_results)

    # 跳过已返回的项（仅在首个扫描页生效）
    if pixiv_page == cursor.pixiv_page AND yielded > 0:
        filtered = filtered[yielded:]

    collected.extend(filtered)
    pages_scanned += 1
    pixiv_page += 1

results = collected[:items_per_page]
has_more = len(collected) > items_per_page OR (raw_results.has_more 当 pages_scanned == 10)
next_cursor = encode_cursor(result) if has_more else None
```

扫描 10 页未攒够时记 INFO 日志，便于排查是否屏蔽标签过多。

注意：Pixiv 数据在两次请求间可能变动，少量重复或跳过可接受。

---

## 错误处理

后端 400 响应统一带 `error_code` 字段，前端按 code 分支，不匹配文案。

| 场景 | HTTP | error_code | 响应 | 前端行为 |
|------|------|------------|------|----------|
| 游标验签失败 | 400 | `CURSOR_INVALID` | `"游标无效"` | toast，保留缓存和游标 |
| 游标格式错误 | 400 | `CURSOR_INVALID` | `"游标格式错误"` | toast，保留缓存和游标 |
| 游标超时 | 400 | `CURSOR_EXPIRED` | `"搜索已过期，请重新搜索"` | toast + 自动重置重新搜索 |
| Pixiv API 异常 | 502 | — | `"搜索服务暂时不可用，请稍后重试"` | toast，保留缓存和游标 |

---

## 前端状态

| 变量 | 说明 |
|------|------|
| `loadedPages[]` | 已缓存页的数据数组，索引即页码-1，最多 20 项 |
| `nextCursor` | 下一页游标，null 表示已到末尾 |
| `currentPage` | 当前展示的页码 |
| `hasMore` | 是否还有下一页 |

### 搜索重置

触发条件：
- 切换搜索条件或表单提交
- 设置页修改 `items_per_page` 保存后，前端自动重置搜索并 toast "每页件数已变更，搜索已重置"
- 收到 `CURSOR_EXPIRED` 错误码

重置操作：清空 `loadedPages`、`nextCursor=null`、`currentPage=1`、发起无游标请求。

---

## 涉及文件

| 文件 | 改动 |
|------|------|
| `config.py` | 新增 `ITEMS_PER_PAGE=24`、`CURSOR_SECRET` 生成逻辑 |
| `fetcher.py` | 游标编解码、验签、过期检查；搜索函数改为游标驱动 + 10 页扫描上限 |
| `app.py` | `/search` 解析游标；`/api/settings` 支持 `items_per_page` |
| `templates/index.html` | 翻页栏 + `loadedPages` + 搜索重置逻辑 |
| `templates/settings.html` | 搜索卡片新增 `items_per_page` 字段 |
| `instance/.cursor_secret` | 首次启动生成 |
| `.gitignore` | 排除 `instance/.cursor_secret` |

---

## 测试要点

- 正常分页：第 1 页 24 件，点"下一页"正常加载并缓存
- 最后一页：不足 24 件，`has_more=false`，"下一页"禁用
- 游标过期：5 分钟后点"下一页"，自动重置重新搜索
- 屏蔽过多：扫描 10 页不足 24 件，返回部分数据且结束，日志有 INFO
- 游标篡改：返回 400，前端缓存不变
- `items_per_page` 极端值 1 和 60 正常
