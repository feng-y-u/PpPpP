# 图库卡片分区点击 + 详情页跨作品连续翻页 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 图库卡片图片区点击进灯箱、信息区点击进详情页；详情页支持按图库当前视图顺序跨作品连续翻页（←→/滑动/屏幕按钮），无图库上下文时自动降级隐藏。

**Architecture:** 前端上下文传递方案（spec：`docs/superpowers/specs/2026-08-29-gallery-card-split-detail-paging-design.md`）。图库跳详情时把排序/筛选参数放 URL、当前页 id 序列放 sessionStorage（`pv_detail_seq`）；详情页读上下文算出上一作/下一作，翻页用 `location.replace()` 整页跳转，跨页边界时按需拉 `/api/gallery`。后端零改动。

**Tech Stack:** 原生 ES2017 JS（无构建步骤，无前端测试框架）、Bootstrap 5.3、Flask Jinja2 模板。

**验证方式说明:** 本项目前端无测试框架，TDD 不适用。每个任务的验证 = `node --check` 语法校验；最后统一做 pytest 回归 + 浏览器手测清单（Task 4）。

---

### Task 1: 图库卡片分区点击（`static/page-gallery.js`）

**Files:**
- Modify: `static/page-gallery.js`（`renderCard` 的 click 处理 + 新增两个函数）

- [ ] **Step 1: 新增 `buildDetailUrl` / `saveDetailSeq` 两个函数**

插入位置：`loadTags` 函数之后、`renderCard` 函数之前。

```javascript
// ── 详情页跨作品翻页的上下文传递 ──
// 点卡片信息区进详情时：URL 参数定位（排序/筛选/位置），sessionStorage 存
// 当前页 id 序列（与图库 30 分钟前端缓存同一份数据，约几 KB）。详情页据此
// 算上一作/下一作；序列获取失败时详情页自动降级为无翻页。
function buildDetailUrl(r, idx) {
  const pos = (galleryCurrentPage - 1) * PAGE_SIZE + idx;
  const params = new URLSearchParams();
  params.set('ctx', 'gallery');
  params.set('sort', sortOrder);
  if (activeCollectionId) params.set('collection_id', activeCollectionId);
  if (activeTag) params.set('tag', activeTag);
  params.set('pos', pos);
  params.set('page', galleryCurrentPage);
  saveDetailSeq();
  return `/detail/${r.pixiv_id}?${params}`;
}

function saveDetailSeq() {
  try {
    sessionStorage.setItem('pv_detail_seq', JSON.stringify({
      v: 1,
      sort: sortOrder,
      collection_id: activeCollectionId || '',
      tag: activeTag || '',
      total: galleryTotal,
      pages: { [galleryCurrentPage]: currentResults.map(x => x.pixiv_id) },
    }));
  } catch (e) { /* sessionStorage 不可用（隐私模式等）时详情页自动降级 */ }
}
```

- [ ] **Step 2: 改造 `renderCard` 的整卡 click 处理为分区分流**

现有代码（`renderCard` 内）：

```javascript
  col.querySelector('.gallery-card').addEventListener('click', function(e) {
    if (e.target.closest('.delete-btn') || e.target.closest('.card-checkbox') || e.target.closest('.dl-file-btn') || e.target.closest('a') || e.target.closest('.card-fav-btn') || e.target.closest('.card-move-btn')) return;
    const idx = currentResults.findIndex(x => x.pixiv_id === r.pixiv_id);
    lightbox.open(currentResults.map(x => ({
      pixiv_id: x.pixiv_id,
      thumbUrl: proxyThumb(x.thumb_url),
      isFav: !!x.is_favorite,
      collectionView: !!activeCollectionId,
    })), idx >= 0 ? idx : 0);
  });
```

替换为（在原排除清单后加 `.card-body` 分流，其余进灯箱）：

```javascript
  col.querySelector('.gallery-card').addEventListener('click', function(e) {
    if (e.target.closest('.delete-btn') || e.target.closest('.card-checkbox') || e.target.closest('.dl-file-btn') || e.target.closest('a') || e.target.closest('.card-fav-btn') || e.target.closest('.card-move-btn')) return;
    const idx = currentResults.findIndex(x => x.pixiv_id === r.pixiv_id);
    if (e.target.closest('.card-body')) {
      location.href = buildDetailUrl(r, idx >= 0 ? idx : 0);
      return;
    }
    lightbox.open(currentResults.map(x => ({
      pixiv_id: x.pixiv_id,
      thumbUrl: proxyThumb(x.thumb_url),
      isFav: !!x.is_favorite,
      collectionView: !!activeCollectionId,
    })), idx >= 0 ? idx : 0);
  });
```

说明：`.gallery-card` 已有内联 `cursor:pointer`，信息区无需重复加 CSS。`.card-checkbox` / `.card-fav-btn` 在图片区内且已被排除清单覆盖，行为不变。

- [ ] **Step 3: 语法校验**

Run: `node --check static/page-gallery.js`
Expected: 无输出（退出码 0）

- [ ] **Step 4: Commit**

```bash
git add static/page-gallery.js
git commit -m "feat: 图库卡片分区点击——图片区进灯箱、信息区进详情页并携带图库上下文"
```

---

### Task 2: 详情页翻页 UI（`templates/detail.html`）

**Files:**
- Modify: `templates/detail.html`（`<style>` 块 + `.info-header` 区域）

- [ ] **Step 1: 加翻页控件样式**

在 `.info-back-btn:hover { ... }` 规则之后插入：

```css
.illust-nav { display: flex; align-items: center; gap: 8px; margin-right: auto; }
.illust-nav-btn {
  height: 48px; padding: 0 14px; border-radius: 10px;
  border: 1px solid var(--border-input);
  background: var(--bg-elevated); color: var(--text-secondary);
  font-size: 0.85rem; cursor: pointer; transition: all 0.15s;
}
.illust-nav-btn:hover { background: var(--accent-subtle); color: var(--accent); }
.illust-nav-btn:disabled { opacity: 0.35; pointer-events: none; }
.illust-nav-pos { font-size: 0.78rem; color: var(--text-muted); white-space: nowrap; }
```

`margin-right: auto` 把控件推到左侧、返回按钮留在右侧（`.info-header` 是 `justify-content: flex-end` 的 flex 容器）。

- [ ] **Step 2: 在 `.info-header` 里预置翻页控件**

现有代码：

```html
    <div class="info-header">
      <button class="info-back-btn" id="backBtn" aria-label="返回">←</button>
    </div>
```

替换为（默认 `display:none`，无图库上下文时永不出现，保持"无 JS 无痕迹"）：

```html
    <div class="info-header">
      <div class="illust-nav" id="illustNav" style="display:none;">
        <button class="illust-nav-btn" id="prevIllustBtn">‹ 上一作</button>
        <span class="illust-nav-pos" id="illustPos"></span>
        <button class="illust-nav-btn" id="nextIllustBtn">下一作 ›</button>
      </div>
      <button class="info-back-btn" id="backBtn" aria-label="返回">←</button>
    </div>
```

- [ ] **Step 3: Commit**

```bash
git add templates/detail.html
git commit -m "feat: 详情页预置跨作品翻页控件（默认隐藏，JS 启用后显示）"
```

---

### Task 3: 详情页翻页逻辑（`static/page-detail.js`）

**Files:**
- Modify: `static/page-detail.js`（新增翻页模块 + 改键盘/触摸 + Init）

- [ ] **Step 1: 插入跨作品翻页模块**

插入位置：触摸滑动 IIFE 之后、`// ── Collection Picker ──` 之前。

```javascript
// ── 跨作品翻页（仅图库上下文 ctx=gallery 启用）──
// 顺序跟随图库当前视图（排序/收藏夹/标签筛选）。序列来源：图库跳转时写入的
// sessionStorage（pv_detail_seq）；新标签页直接粘贴 URL 时按 URL 参数现拉
// /api/gallery 定位。两者都失败则不渲染翻页 UI。
const PAGE_SIZE = 50;
const DETAIL_SEQ_KEY = 'pv_detail_seq';
const navCtx = (() => {
  const q = new URLSearchParams(location.search);
  if (q.get('ctx') !== 'gallery') return null;
  return {
    sort: q.get('sort') || 'downloaded',
    collectionId: q.get('collection_id') || '',
    tag: q.get('tag') || '',
    pos: parseInt(q.get('pos'), 10),
    page: parseInt(q.get('page'), 10) || 1,
  };
})();
let seq = null;        // { total, pages: {页码: [pixiv_id, ...]} }
let navReady = false;

function loadStoredSeq() {
  try {
    const s = JSON.parse(sessionStorage.getItem(DETAIL_SEQ_KEY) || 'null');
    if (!s || s.v !== 1) return null;
    if (s.sort !== navCtx.sort || (s.collection_id || '') !== navCtx.collectionId
        || (s.tag || '') !== navCtx.tag) return null;
    return s;
  } catch { return null; }
}

function saveSeq() {
  try { sessionStorage.setItem(DETAIL_SEQ_KEY, JSON.stringify(seq)); } catch {}
}

function galleryParams(pageNo) {
  const p = new URLSearchParams();
  p.set('sort', navCtx.sort);
  if (navCtx.collectionId) p.set('collection_id', navCtx.collectionId);
  if (navCtx.tag) p.set('tag', navCtx.tag);
  p.set('limit', PAGE_SIZE);
  p.set('offset', (pageNo - 1) * PAGE_SIZE);
  return p.toString();
}

async function fetchPage(pageNo) {
  const resp = await fetch('/api/gallery?' + galleryParams(pageNo));
  if (!resp.ok) throw new Error('gallery fetch failed');
  const data = await resp.json();
  seq.total = data.total;
  seq.pages[pageNo] = data.data.map(x => x.pixiv_id);
  // 只保留目标页 ±1，防 sessionStorage 膨胀
  Object.keys(seq.pages).forEach(k => {
    if (Math.abs(parseInt(k, 10) - pageNo) > 1) delete seq.pages[k];
  });
  saveSeq();
}

async function resolveSeq() {
  if (!navCtx || isNaN(navCtx.pos)) return;
  seq = loadStoredSeq() || { total: 0, pages: {} };
  // 当前页必须包含本作品（sessionStorage 可能已过期或被其他筛选覆盖）
  if (!seq.pages[navCtx.page] || !seq.pages[navCtx.page].includes(illust.pixiv_id)) {
    try {
      await fetchPage(navCtx.page);
    } catch {
      seq = null;
      return;
    }
  }
  if (!seq.pages[navCtx.page].includes(illust.pixiv_id)) { seq = null; return; }
  // pos 以图库实际序列为准（图库数据可能在跳转后已变化）
  navCtx.pos = (navCtx.page - 1) * PAGE_SIZE + seq.pages[navCtx.page].indexOf(illust.pixiv_id);
  navReady = true;
}

async function pidAt(pos) {
  const pageNo = Math.floor(pos / PAGE_SIZE) + 1;
  if (!seq.pages[pageNo]) await fetchPage(pageNo);   // 跨页续翻：现拉相邻页
  return seq.pages[pageNo][pos % PAGE_SIZE] || null;
}

async function navTo(delta) {
  if (!navReady) return;
  const target = navCtx.pos + delta;
  if (target < 0 || (seq.total > 0 && target >= seq.total)) return;
  const pid = await pidAt(target).catch(() => null);
  if (!pid) { showToast('加载翻页数据失败', true); return; }
  const params = new URLSearchParams(location.search);
  params.set('pos', target);
  params.set('page', Math.floor(target / PAGE_SIZE) + 1);
  // replace 不压历史栈：连翻多个作品后按返回仍一次回到图库
  location.replace(`/detail/${pid}?${params}`);
}

function renderIllustNav() {
  if (!navReady) return;
  $('#illustNav').style.display = 'flex';
  $('#illustPos').textContent = `${navCtx.pos + 1} / ${seq.total}`;
  $('#prevIllustBtn').disabled = navCtx.pos <= 0;
  $('#nextIllustBtn').disabled = seq.total > 0 && navCtx.pos >= seq.total - 1;
}
```

- [ ] **Step 2: 键盘 ←→ 改为跨作品**

现有代码：

```javascript
document.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') { e.preventDefault(); showPage(currentPage - 1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); showPage(currentPage + 1); }
});
```

替换为（无图库上下文时 `navTo` 直接 no-op——作品内多页翻页只走图上 `‹ ›` 按钮）：

```javascript
// ←→：跨作品翻页（图库上下文下）；无上下文时不动作，作品内翻页用图上 ‹ ›
document.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') { e.preventDefault(); navTo(-1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); navTo(1); }
});
```

- [ ] **Step 3: 触摸滑动跟随键盘改为跨作品**

现有代码（触摸滑动 IIFE 内）：

```javascript
    if (Math.abs(diff) > 50) {
      if (diff > 0) showPage(currentPage + 1);
      else showPage(currentPage - 1);
    }
```

替换为：

```javascript
    if (Math.abs(diff) > 50) {
      if (diff > 0) navTo(1);
      else navTo(-1);
    }
```

- [ ] **Step 4: Init 末尾启用翻页**

现有代码：

```javascript
// ── Init ──
showPage(0);
```

在其后追加一行：

```javascript
resolveSeq().then(renderIllustNav);
```

- [ ] **Step 5: 语法校验**

Run: `node --check static/page-detail.js`
Expected: 无输出（退出码 0）

- [ ] **Step 6: Commit**

```bash
git add static/page-detail.js
git commit -m "feat: 详情页跨作品连续翻页——←→/滑动/屏幕按钮，sessionStorage 序列 + 跨页自动续拉"
```

---

### Task 4: 回归与手工验证

**Files:** 无新改动（验证 + 文档状态更新）

- [ ] **Step 1: pytest 全量回归**

Run: `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q`
Expected: 全部 passed（改动前基线 235 passed；末尾的"Detail API 连接失败"日志是离线环境已知的 daemon 线程收尾噪声）

- [ ] **Step 2: 浏览器手测清单**

`flask run --debug` 起服务，逐项过（来自 spec 第 5 节）：

1. 图库：点卡片图片区 → 灯箱；点信息区（标题/画师/标签）→ 详情页；点删除/下载/上移/下移按钮 → 各自原行为
2. 详情页（从图库进入）：键盘 ←→ / 触摸滑动 / 顶部「上一作/下一作」→ 跨作品跳转；多页作品用图上 `‹ ›` 翻内页
3. 当前页第 50 个作品按 → → 自动拉下一页并跳转；全库最后一个作品「下一作」按钮置灰
4. 切换排序/收藏夹视图/标签过滤后进入详情 → 位置指示与序列跟图库一致
5. 从搜索页 / 详情页相关作品 / 缓存页进入详情 → 无翻页控件
6. 连翻 3 个作品后按浏览器返回 → 一次回到图库（不倒序经过翻过的作品）
7. 新标签页粘贴带 `ctx=gallery` 参数的详情 URL → 翻页可用（fetch 定位路径）

- [ ] **Step 3: 更新 spec 状态并提交**

`docs/superpowers/specs/2026-08-29-gallery-card-split-detail-paging-design.md` 中 `- 状态：待实现` 改为 `- 状态：已实现`。

```bash
git add docs/superpowers/specs/2026-08-29-gallery-card-split-detail-paging-design.md
git commit -m "docs: 图库分区点击/详情页连续翻页 spec 标记已实现"
```
