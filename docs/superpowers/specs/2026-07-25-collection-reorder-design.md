# 收藏夹自定义排序 — 设计文档

**日期**: 2026-07-25
**状态**: 设计中

---

## 概述

为 CollectionItem 引入自定义排序（分数差值法），取代当前的 `created_at DESC` 固定排序，通过上移/下移按钮实现手动调序。

同时彻底移除旧版 `Illust.is_favorite` 布尔收藏体系。

---

## 约束

- 所有收藏夹为私有（单人自部署，无需权限控制）
- 纯 SQLite，不引入 Redis
- 前端最小化：上移/下移按钮，不做拖拽

---

## 1. 数据模型

### 1.1 CollectionItem — 新增字段

```
position: REAL, nullable=False, default=0.0
```

- 初始间隔 1000：首个项 position = 1000.0，后续依次 +1000
- 查询排序：`ORDER BY position ASC`（替代 `created_at DESC`）
- `UniqueConstraint(collection_id, pixiv_id)` 不变

### 1.2 移除旧版收藏遗留

- 删除 `Illust.is_favorite` 列（Boolean）
- 删除 `Illust.favorited_at` 列（DateTime）
- 删除所有 `_sync_is_favorite()` 函数及其调用
- `init_db()` 中移除对应 `ALTER TABLE ADD COLUMN` 迁移代码

**is_favorite 列清理策略（兼容旧 SQLite < 3.35）：**

SQLite 原生 `ALTER TABLE ... DROP COLUMN` 需要 3.35.0+。为兼容 Termux、旧 NAS 等环境，采用重建表策略：

1. 检查 SQLite 版本，>= 3.35 时直接 DROP COLUMN
2. 低于 3.35 时：创建无 is_favorite/favorited_at 的临时新表 → 复制数据 → 删除旧表 → 重命名新表 → 重建索引

### 1.3 现有数据迁移

启动时为已有 `CollectionItem` 按 `created_at ASC` 顺序分配初始 position：第一条 1000.0，后续递增 1000.0。无 `created_at` 或排序不稳定时回退到 `id ASC`。

**幂等检查（用 PRAGMA user_version，不用 position=0.0 哨兵）**：

position=0.0 不能当哨兵 —— 用户把项移到顶端时 `prev.pos - 1000.0` 可能恰好产生 0.0，重启后会被误判为未迁移数据，导致手动排序被覆盖。

改用 `PRAGMA user_version`：启动时读取，若 `< 1` 则执行 position 分配，完成后 `PRAGMA user_version = 1`。后续启动直接跳过。

---

## 2. API

### 2.1 新增 — 调序

```
POST /api/collections/<collection_id>/items/<pixiv_id>/move
Body: { "direction": "up" | "down" }
需要 CSRF
```

**逻辑**：

1. 查当前项及其排序前后邻居的 position
2. 按 direction 计算新 position：
   - up：移动到上一项之前。新 position = `(prev_of_prev.pos + prev.pos) / 2`
     - 若已是第一项，返回 400
     - 若前面只有一项（即目标已经是第2项，移到第1项），新 position = `prev.pos - 1000.0`（允许负值）
   - down：移动到下一项之后。新 position = `(next.pos + next_of_next.pos) / 2`
     - 若已是最后一项，返回 400
     - 若后面只有一项，新 position = `next.pos + 1000.0`
3. 检查目标间隙是否需要触发重排，按 3.2 的顺序在同一事务内完成 重排→重读→重算→带锁更新
4. `safe_commit`，返回 `{ "position": new_pos, "rebalanced": bool }`

**并发安全**：整个 读邻居 → 算新 position →（可选重排）→ 更新 包在**单个事务**内完成。更新 position 时再加乐观锁 — `UPDATE collection_items SET position=? WHERE pixiv_id=? AND collection_id=? AND position=?`。若影响行数为 0（position 已被其他标签页修改），回滚并返回 409 让前端重试。

**已知残余风险**：乐观锁只保护被移动的那一行；两个标签页同时移动**不同**项仍可能产生重复 position（单人场景概率极低，出现后可通过一次手动重排恢复）。

### 2.2 修改 — 列表返回排序

```
GET /api/collections/<collection_id>/items
```

排序从 `ORDER BY created_at DESC` 改为 `ORDER BY position ASC`。

### 2.2b 修改 — 图库按收藏夹 position 排序（关键路径）

`gallery.html` 浏览收藏夹实际调用的是 `GET /api/gallery?collection_id=N`，当前排序为 `created_at DESC / downloaded_at DESC`（`app.py:978`），与 position 无关。**只改 2.2 不足以让排序生效。**

`/api/gallery` 在 `collection_id` 参数存在时：

```sql
SELECT illusts.id FROM illusts
JOIN collection_items ON collection_items.pixiv_id = illusts.pixiv_id
WHERE collection_items.collection_id = :collection_id AND <其余 wheres>
ORDER BY collection_items.position ASC
LIMIT :lim OFFSET :off
```

- 此时忽略 `sort` 参数（收藏夹视图固定按 position 排序）
- 现有的 `illusts.pixiv_id IN (SELECT pixiv_id FROM collection_items ...)` 过滤改写为上述 JOIN，总数统计同步调整

### 2.3 修改 — 添加时分配 position

```
POST /api/collections/<collection_id>/items
POST /api/collections/<collection_id>/items/batch
```

新增项 position = `MAX(position) + 1000.0`（若空收藏夹则从 1000.0 开始）。batch add 时依次递增。

### 2.4 保持不变

- collection CRUD（GET/POST/PUT/DELETE `/api/collections`）
- item remove（DELETE `/api/collections/<id>/items/<pixiv_id>`）
- batch remove（DELETE `/api/collections/<id>/items/batch`）
- `/api/illust/<pixiv_id>/collections`

### 2.5 移除 is_favorite 及消费方替代方案

不能简单删除读写逻辑 —— 以下消费方依赖 `is_favorite`，需同步改为查询"我的收藏"收藏夹 membership：

| 位置 | 现状 | 替代实现 |
|------|------|---------|
| `app.py` 图库 `?favorites=true` 过滤 | `wheres.append('is_favorite = 1')` | `illusts.pixiv_id IN (SELECT pixiv_id FROM collection_items WHERE collection_id = :default_cid)`（`default_cid` 为"我的收藏" id，查询开头解析一次） |
| `app.py` `fav_total` 统计 | `SUM(CASE WHEN is_favorite=1 ...)` | `SUM(CASE WHEN EXISTS (SELECT 1 FROM collection_items ci WHERE ci.collection_id = :default_cid AND ci.pixiv_id = illusts.pixiv_id) THEN 1 ELSE 0 END)` |
| `app.py` `GET /api/favorite/<pid>` | 读 `illust.is_favorite` | 查 CollectionItem 是否存在于默认收藏夹 |
| `app.py` `POST /api/favorite/<pid>` | 切换归属后 `_sync_is_favorite` 并返回 `illust.is_favorite` | 切换归属后直接按 membership 返回布尔值，删除 `_sync_is_favorite` 调用 |
| `models.py` `Illust.to_dict()` | 输出 `is_favorite` 字段 | **保留字段名不变**（前端 gallery.html / detail.html 无需改动），值改为按默认收藏夹 membership 计算；调用方需传入或批量预取 membership 集合避免 N+1 |

同时删除：

- 所有 `_sync_is_favorite()` 函数定义和调用（含删除收藏夹、移除 item、batch 操作后的调用）
- `init_db()` 中 `is_favorite` / `favorited_at` 的 `ALTER TABLE ADD COLUMN` 迁移代码
- `init_db()` 中 is_favorite → 默认收藏夹的旧数据迁移逻辑（已执行过的环境无需再跑；保留 user_version 机制后可按版本跳过）

---

## 3. 重排策略

### 3.1 触发条件

插入新 position 时，检查目标位置前后两项的间隙：若 `后项.position - 前项.position < 1.0`，间距不足以安全插入中值，触发全量重排。

### 3.2 执行

重排与调序在**同一事务**内按固定顺序执行（顺序敏感，不能颠倒）：

1. 检测目标间隙 `后项.position - 前项.position < 1.0`
2. 若间隙不足：全量重排 — 按 `position ASC` 取该收藏夹全部 id，循环赋值 `(index + 1) * 1000.0`
3. **重读后项/前项 position**（重排后已变化）
4. 重新计算新 position 中值
5. 带乐观锁更新被移动项（锁值用重排后的新 position）
6. `safe_commit` 提交

- 在同一个 HTTP 请求中完成（不建后台任务）
- SQLite + 全量更新在几千项规模下事务耗时 < 1s

---

## 4. 前端

### 4.1 按钮

在收藏夹浏览视图中每项增加两个小按钮：▲ 上移 / ▼ 下移。

- 第一项隐藏 ▲，最后一项隐藏 ▼
- 点击后 `POST` 调序接口，成功后局部刷新当前页列表

### 4.2 无需改动

- 收藏夹创建/编辑/删除 UI
- 添加到收藏夹/从收藏夹移除 UI
- 搜索/浏览页面

---

## 5. 涉及文件

| 文件 | 变更 |
|------|------|
| models.py | CollectionItem 加 position 字段；Illust 移除 is_favorite/favorited_at 列定义；to_dict() 的 is_favorite 改为 membership 计算；init_db() 更新迁移逻辑（user_version） |
| app.py | 新增 move API；/api/gallery 支持 collection_id 时按 position 排序；?favorites 过滤与 fav_total 改用 membership；/api/favorite GET/POST 改用 membership；删除 _sync_is_favorite |
| templates/gallery.html | 收藏夹视图每项加上移/下移按钮（前端实际浏览路径） |

---

## 6. 测试要点

- CollectionItem position 自动分配正确性
- move up / move down 正确性（含边界：第一项/最后一项/仅两项）
- 乐观锁并发冲突返回 409（模拟 position 已被其他请求修改）
- 重排触发正确性（极端密集插入后验证全量重排）
- **重启后手动排序不丢失**（覆盖 user_version 幂等逻辑，回归 position=0.0 哨兵缺陷）
- **`/api/gallery?collection_id=N` 按 position 排序返回**（关键路径）
- `?favorites=true` 过滤和 `fav_total` 统计改用 membership 后结果与旧行为一致
- `to_dict()['is_favorite']` 仍返回正确布尔值（前端契约不变）
- 旧 is_favorite / favorited_at 列已删除
- 现有 collection CRUD 不受影响
