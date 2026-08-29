# 图库卡片分区点击与详情页连续翻页设计

- 日期：2026-08-29
- 状态：已实现
- 相关代码：`templates/gallery.html`、`static/page-gallery.js`、`templates/detail.html`、`static/page-detail.js`（`lightbox.js` 与后端路由不改）

## 背景与目标

现状：图库卡片整卡点击进灯箱沉浸浏览；详情页（`/detail/<pid>`）没有作品间导航，看完一个要返回图库再点下一个。图上 `‹ ›` 按钮与键盘 ←→ 只负责同一作品内的多页翻页。

目标：

1. 卡片分区点击：图片区 → 灯箱沉浸浏览；信息区（标题/画师/标签）→ 详情页
2. 详情页支持跨作品连续翻页，顺序跟随图库当前视图，翻完一页自动接下一页，直到全库最后一个

## 需求决策（已与用户确认）

1. **翻页范围与顺序**：跟随图库当前视图（排序/收藏夹/标签筛选一致），当前页 50 个翻完自动接下一页，翻到全库最后一个为止
2. **键位**：键盘 ←→ 改为上一个/下一个作品；作品内多页翻页保留图上 `‹ ›` 按钮和页码指示器
3. **非图库入口**（搜索页/相关作品/缓存页）：无图库上下文则不显示翻页 UI，页面其余功能不变
4. **实现方案**：前端上下文传递（URL 参数 + sessionStorage，后端零改动）。已否决：后端邻居查询（需抽取复用图库查询逻辑，改动面大）、PJAX 无刷新替换（服务端模板需重写为前端渲染，收益小）

## 1. 图库卡片分区点击（`gallery.html` / `page-gallery.js`）

### 点击分区

`renderCard` 中 `.gallery-card` 的整卡 click 处理改为按点击目标分流：

- 命中 `.card-body` 且不命中操作按钮（`.delete-btn` / `.dl-file-btn` / `a` / `.card-move-btn`）→ 跳详情页
- 其余（图片区 `.card-img-wrap` 及卡片内空白）→ 灯箱（现状）
- 现有排除清单（`.card-checkbox` / `.card-fav-btn` 等）保留，收藏/勾选/移动/删除/下载行为全部不变
- `.card-body` 加 `cursor: pointer` 提示可点（图片区已有手型）

### 跳转 URL 与序列传递

点信息区时构造：

```
/detail/<pid>?ctx=gallery&sort=<sortOrder>&collection_id=<activeCollectionId>&tag=<activeTag>&pos=<全局位置>&page=<galleryCurrentPage>
```

- `pos` 为作品在当前视图全序列中的位置：`(page - 1) * PAGE_SIZE + idx`
- 同时写 sessionStorage 键 `pv_detail_seq`（约几 KB）：

```json
{
  "v": 1,
  "sort": "downloaded",
  "collection_id": "",
  "tag": "",
  "total": 583,
  "pages": { "3": [101, 102, ...] }
}
```

- `pages` 按页号存 id 数组（数据即图库页已有的 `data.data[].pixiv_id`，可复用 30 分钟前端缓存）
- 容量控制：每次写入只保留当前页 ± 1 的页，更早的删掉
- `v` 为结构版本号，旧结构数据直接丢弃

## 2. 详情页跨作品翻页（`detail.html` / `page-detail.js`）

### 启用条件与序列解析（统一入口 `resolveSeq()`）

仅当 URL `ctx=gallery` 时继续：

1. sessionStorage `pv_detail_seq` 命中（`v`、`sort`/`collection_id`/`tag` 与 URL 一致）且 `pages` 含 pid 所在页 → 直接用
2. 否则按 URL 的 `page` 参数请求 `GET /api/gallery?sort=&collection_id=&tag=&limit=50&offset=(page-1)*50`，在结果中定位 pid 得到实际 pos，并把该页写回 sessionStorage（覆盖"新标签页粘贴 URL"场景）
3. 失败（网络错误 / pid 不在结果中 / 图库为空）→ 翻页功能整体禁用，不渲染 UI

### UI

信息面板顶部 `info-header` 行（返回按钮旁）预置翻页控件（模板里 `style="display:none"`，JS 启用时显示，保持无 JS 无痕迹）：

```html
<div class="illust-nav" id="illustNav" style="display:none;">
  <button id="prevIllustBtn">‹ 上一作</button>
  <span id="illustPos">12 / 583</span>
  <button id="nextIllustBtn">下一作 ›</button>
</div>
```

- `pos = 0` 置灰「上一作」；`pos = total - 1` 置灰「下一作」（实现为 disabled 而非移除，视觉上保留位置指示）
- 不与图上作品内页的 `‹ ›` 混淆（后者保持原职责）

### 翻页行为

- **键盘 ←→**：从作品内 `showPage()` 改为跨作品跳转；**触摸滑动跟随键盘**（手势 = 键位同一语义）；作品内多页翻页只用图上 `‹ ›` 按钮
- **跳转**：`location.replace(buildDetailUrl(pid, newPos, pageOf(newPos)))`，同时更新 sessionStorage（跨页时拉取并写入新页）
- **跨页续翻**：next 越界且 `page < total_pages` → `GET /api/gallery` 下一页 offset，取新页 ids 写回后继续；prev 同理向上一页
- **返回**：`location.replace()` 不压浏览器历史栈，连翻 N 个作品后按返回键一次回到图库（不倒着经过翻过的作品）

## 3. 降级与边界

- URL 无 `ctx=gallery`（搜索页、相关作品、缓存页入口）→ 翻页元素保持 `display:none`，完全现状
- 刷新页面：URL 参数 + sessionStorage 恢复；新标签页粘贴 URL → 走 fetch 定位路径；再失败 → 无翻页
- 孤儿作品（本地有文件但 DB 无行）：点信息区跳详情会 404（详情页依赖 DB 记录，既有行为，不做特殊处理）；点图片区进灯箱不受影响
- 翻页序列中的作品中途被删除：拉取后序列里没有的 pid 自然不成为跳转目标（按实际序列渲染）
- 收藏夹视图（`collection_id` 非空）：序列天然限定在收藏夹内（`/api/gallery` 已处理）

## 4. 明确不做（YAGNI）

- 缓存页 `/cache` 卡片行为不变（后续想要可按同模式复制）
- 灯箱内部行为不变
- 不做跳页、缩略图条、位置输入等高级导航
- 搜索页 / 相关作品入口不携带翻页上下文

## 5. 验证

- 后端零改动：`pytest -q` 全量回归（无新用例）
- `node --check` 校验 `page-detail.js` / `page-gallery.js` 语法
- 浏览器手测清单：
  1. 图库：点图片区 → 灯箱；点信息区 → 详情；点删除/下载/移动按钮 → 各自原行为
  2. 详情：←→ / 触摸滑动 / 顶部按钮 → 跨作品；多页作品用图上 `‹ ›` 翻内页
  3. 当前页第 50 个按 → 自动接下一页；全库最后一个无「下一作」
  4. 收藏夹视图 / 标签过滤下序列与图库一致
  5. 搜索页 / 相关作品 / 缓存页进详情：无翻页 UI
  6. 连翻数个作品后按返回 → 一次回图库
  7. 新标签页粘贴带 ctx 的详情 URL → 翻页可用（fetch 定位）

## 6. 实现与验证记录（2026-08-29）

实现提交：`2d7cb1b`（图库分区点击 + 上下文传递）、`ab3949b`（详情页翻页控件）、`56d82b4`（翻页逻辑，含审查加固：`navPending` 防连按、`page` 负数加固、init 链 `.catch`、`#illustPos` 加 `aria-live`、屏幕按钮绑定）。pytest 全量 235 passed 无回归。

浏览器手测（IAB 沙箱；该环境后台标签暂停 requestAnimationFrame，图库分帧渲染不执行，注入页面自身 `renderCard` 产出的同一 DOM 后验证点击逻辑；真实浏览器不受影响）：

- 通过：图片区→灯箱；信息区→详情（契约 URL 正确）；键盘 →/← 跨作品与边界守卫；标签过滤视图 ctx 携带（`tag=金髪`）；无上下文控件隐藏；新标签页 fetch 定位；返回键一次回图库；删除按钮不误跳转。
- 本数据集无法覆盖（代码经两阶段审查）：第 50 个跨界自动接下页（库内仅 1 页）、最后一个作品置灰、多页作品内页翻页（唯一有 DB 记录的作品为单页）、触摸滑动物理操作（与键盘共用 `navTo`）。

**发现的边界（待决策）**：本实例 5 个作品中 4 个是孤儿作品（本地有文件但 `Illust` 表无记录），翻页/信息区点击落到这些作品会 404（§3 既定行为）。若实际数据中孤儿占比同样偏高，可考虑后续小改动：`/api/gallery` 给孤儿项打标，翻页跳过孤儿项。
