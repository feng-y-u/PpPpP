# 前端重塑（轻快内容风）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将搜索/图库/缓存三页重塑为「轻快内容风」设计语言（玫瑰粉配色、14px 圆角卡片、柔和投影），并新增共享 lightbox 预览、图库浮条动效、默认下载时间排序。

**Architecture:** 纯前端就地重塑——重构 `static/style.css` 的 `:root` token 与组件类；新增共享 `static/lightbox.js` 单例组件（原生 JS、无构建）；三个页面 JS（page-index / page-gallery / page-cache）接入 lightbox 并更新类名；模板仅改 hero/筛选行的 HTML 结构与内联样式数值。后端零改动。

**Tech Stack:** Flask 模板 + Bootstrap 5.3 CSS 类 + 原生 JS（无框架无构建）+ CSS 变量。验证手段：`node --check` 语法校验 + `pytest -q` 回归 + 浏览器手测（项目无前端测试设施，遵循既有 spec 的验证惯例）。

**涉及文件总览：**

| 文件 | 职责 |
|------|------|
| `static/style.css` | `:root` token 重构；卡片/胶囊/lightbox/浮条/选中态样式 |
| `static/lightbox.js`（新建） | 共享 lightbox 组件：打开/关闭、键盘/触摸、图源升级、操作条 |
| `static/app.js` | `renderInChunks` stagger 上限微调（一行） |
| `static/page-index.js` | 搜索页接入：页签切换、卡片→lightbox、currentItems |
| `static/page-gallery.js` | 图库页接入：默认排序 downloaded、浮条动效、选中描边、lightbox（含收藏） |
| `static/page-cache.js` | 缓存页接入：lightbox（无收藏）、currentItems |
| `templates/index.html` | hero 页签化 + 胶囊搜索条 + 筛选 chips |
| `templates/gallery.html` | 内联样式数值更新（token 对齐） |
| `templates/cache.html` | 筛选行类名对齐 |

**设计规格：** `docs/superpowers/specs/2026-08-26-frontend-redesign-design.md`（实施前先读）。

---

### Task 1: 设计 Token 重构

**Files:**
- Modify: `static/style.css:2-31`（`:root` 块整体替换）

- [ ] **Step 1: 替换 `:root` 块**

将 `static/style.css` 第 2-31 行整个 `:root { ... }` 块替换为：

```css
:root {
  --accent: #e2577e;
  --accent-hover: #cf4668;
  --accent-subtle: rgba(226, 87, 126, .08);
  --accent-focus: rgba(226, 87, 126, .18);
  --bg-page: #faf6f4;
  --bg-elevated: #ffffff;
  --bg-navbar: rgba(250, 246, 244, .88);
  --bg-input: #ffffff;
  --bg-thumb: #f2e8ea;
  --text-primary: #3a3338;
  --text-secondary: #8a6a74;
  --text-muted: #b09aa4;
  --border-subtle: rgba(180, 120, 135, .10);
  --border-card: rgba(226, 87, 126, .08);
  --border-input: rgba(180, 120, 135, .18);
  --border-active: rgba(226, 87, 126, .25);
  --success: #3b8a5e;
  --success-bg: rgba(59, 138, 94, .12);
  --danger: #c44a4a;
  --danger-border: rgba(196, 74, 74, .15);
  --radius-sm: 8px;
  --radius-card: 14px;
  --radius-lg: 18px;
  --radius-btn: 8px;
  --radius-nav: 10px;
  --radius-badge: 999px;
  --glass-blur: blur(16px) saturate(160%);
  --shadow-card: 0 2px 10px rgba(200, 120, 140, .08);
  --shadow-card-hover: 0 6px 20px rgba(200, 120, 140, .14);
  --font-sans: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', 'Noto Sans SC', 'Segoe UI', system-ui, sans-serif;
}
```

注意：`--radius`（旧）被拆为 `--radius-sm` / `--radius-card` / `--radius-lg`；`--radius-card` 由 4px 变 14px。全局搜索 `var(--radius)` 引用并更新（Step 2 处理其余引用；本任务先替换 :root，旧引用仍可用 `--radius` 名称编译报错？否——CSS 变量名不匹配仅导致该属性无效。因此必须同步处理引用）。

- [ ] **Step 2: 更新 `var(--radius)` 引用点**

在 `static/style.css` 与三个模板的内联 `<style>` 中，将 `var(--radius)` 引用按用途替换：

- `.form-control, .form-select` 的 `border-radius: var(--radius)` → `var(--radius-sm)`
- `.modal-content` 的 `border-radius: var(--radius)` → `var(--radius-lg)`
- `.toast` 相关统一 `var(--radius-sm)`

其余出现处逐一见机：属于按钮/控件用 `--radius-sm`，容器用 `--radius-lg`。模板内联样式同样处理（gallery.html / cache.html / index.html / downloads.html / detail.html / settings.html 若有）。

验证：`grep -rn "var(--radius)" static templates` 无残留（允许注释中提及）。

- [ ] **Step 3: 语法与视觉冒烟**

```bash
node -e "require('fs').readFileSync('static/style.css','utf8')"  # 无异常即可
```

浏览器打开任一页面，确认页面底色变为暖白 `#faf6f4`、主色按钮变为 `#e2577e`、卡片圆角变大。预期其他页面视觉微变（token 全局生效）——这是预期行为。

- [ ] **Step 4: Commit**

```bash
git add static/style.css templates
git commit -m "style: 设计 token 重构 — 玫瑰粉配色 + 圆角/阴影/间距体系"
```

---

### Task 2: 卡片与胶囊组件样式

**Files:**
- Modify: `static/style.css`（photo-card 节、photo-tag 节、gallery-card 内联在模板中）
- Modify: `templates/gallery.html:11-182`（内联卡片样式数值更新）

- [ ] **Step 1: 全局 photo-card 更新（style.css / templates/index.html、cache.html 内联）**

`static/style.css` 中现无 photo-card 定义（在 index.html / cache.html 内联）。将 index.html 与 cache.html 内联 `<style>` 中的 `.photo-card` 块替换为：

```css
.photo-card {
  position: relative;
  border-radius: var(--radius-card);
  overflow: hidden;
  cursor: pointer;
  background: var(--bg-elevated);
  box-shadow: var(--shadow-card);
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.photo-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-card-hover);
}
```

同时 `.photo-card img` 保持原样（宽度/高度/过渡不变），`background: var(--bg-thumb)` 自动使用新占位色。

- [ ] **Step 2: 标签胶囊化（index.html / cache.html 内联 + gallery.html 内联）**

`.photo-tag` 与 `.tag-badge` 统一替换为胶囊：圆角 `999px`、粉底粉字：

```css
.photo-tag, .tag-badge {
  font-size: 0.62rem;
  color: var(--accent) !important;
  background: var(--accent-subtle) !important;
  padding: 2px 9px;
  border-radius: 999px;
  transition: all 0.15s;
}
.photo-tag:hover { color: var(--accent-hover); background: rgba(226,87,126,.16); }
```

`gallery.html` 中原 `.tag-badge` 块（第 60-66 行）整体删除，由本全局规则接管（gallery 模板需要 `!important` 覆盖 Bootstrap badge 底色）。

- [ ] **Step 3: 图库卡片（gallery.html 内联）**

`templates/gallery.html` 内联 `.gallery-card`（第 11-23 行）替换为：

```css
.gallery-card {
  border: none;
  border-radius: var(--radius-card);
  overflow: hidden;
  background: var(--bg-elevated);
  box-shadow: var(--shadow-card);
  transition: transform 0.2s ease, box-shadow 0.2s ease;
  height: 100%;
}
.gallery-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-card-hover);
}
.card-img-wrap img { transition: transform 0.3s ease; }
.card-img-wrap:hover img { transform: scale(1.03); }
/* 选中态：批量勾选时粉描边 + 上浮 */
.gallery-card.card-selected {
  box-shadow: 0 0 0 2px var(--accent), var(--shadow-card-hover);
  transform: translateY(-2px);
}
```

- [ ] **Step 4: 按钮体系圆角**

`static/style.css` `.btn` 块已有 `border-radius: var(--radius-btn)`（8px）——无需改动。核对 `.btn-sm` 等无硬编码圆角。

- [ ] **Step 5: 冒烟验证 + Commit**

浏览器打开三页：卡片白底圆角 14px 带柔和投影；标签为粉色胶囊；hover 卡片上浮。无布局错乱。

```bash
git add static/style.css templates/index.html templates/cache.html templates/gallery.html
git commit -m "style: 卡片与标签胶囊组件 — 圆润内容风统一视觉"
```

---

### Task 3: renderInChunks stagger 上限

**Files:**
- Modify: `static/app.js:190-216`

- [ ] **Step 1: 修改 animationDelay 累加逻辑**

将 `static/app.js` 的 `renderInChunks` 中这一行：

```js
if (node.style) node.style.animationDelay = `${i * delay}ms`;
```

替换为（超过 24 张后 delay 不再累加，同批进入）：

```js
if (node.style) node.style.animationDelay = `${Math.min(i, 24) * delay}ms`;
```

- [ ] **Step 2: 校验 + Commit**

```bash
node --check static/app.js
git add static/app.js
git commit -m "perf: renderInChunks stagger 上限 24 张，大页尾部不再过慢"
```

---

### Task 4: Lightbox 共享组件

**Files:**
- Create: `static/lightbox.js`
- Modify: `static/style.css`（追加 lightbox 样式节）

- [ ] **Step 1: 创建 `static/lightbox.js`**

完整文件内容：

```js
// ── 共享 Lightbox 预览组件 ──
// 用法：lightbox.open(items, index)
//   items: [{ pixiv_id, thumbUrl, isFav, collectionView }]
//   index: 初始显示下标
// 键盘 ←→ 切换 / Esc 关闭；触摸左右滑动；遮罩点击关闭。
// 图源策略：先用缩略图即时渲染，后台静默调 /api/detail/<id>，
// 已下载作品自动升级为原图（未下载保持缩略图，操作条提供「打开详情」）。

const lightbox = (() => {
  let items = [];
  let index = 0;
  let upgraded = new Set();
  let root = null, imgEl = null, prevBtn = null, nextBtn = null,
      closeBtn = null, bar = null, counter = null, favBtn = null,
      detailBtn = null, dlBtn = null;
  let touchX = 0;

  function build() {
    root = document.createElement('div');
    root.className = 'lightbox-overlay';
    root.innerHTML = `
      <div class="lightbox-main">
        <img id="lightboxImg" alt="">
        <div class="lightbox-bar">
          <button type="button" class="lightbox-btn" id="lbPrev">←</button>
          <span class="lightbox-counter" id="lbCounter"></span>
          <button type="button" class="lightbox-btn" id="lbNext">→</button>
          <span class="lightbox-spacer"></span>
          <button type="button" class="lightbox-btn lb-fav" id="lbFav" hidden>♥ 收藏</button>
          <button type="button" class="lightbox-btn" id="lbDl">下载</button>
          <button type="button" class="lightbox-btn" id="lbDetail">打开详情</button>
          <button type="button" class="lightbox-btn" id="lbClose">✕</button>
        </div>
      </div>`;
    document.body.appendChild(root);
    imgEl = root.querySelector('#lightboxImg');
    prevBtn = root.querySelector('#lbPrev');
    nextBtn = root.querySelector('#lbNext');
    counter = root.querySelector('#lbCounter');
    favBtn = root.querySelector('#lbFav');
    dlBtn = root.querySelector('#lbDl');
    detailBtn = root.querySelector('#lbDetail');
    closeBtn = root.querySelector('#lbClose');

    prevBtn.addEventListener('click', () => showAt(index - 1));
    nextBtn.addEventListener('click', () => showAt(index + 1));
    closeBtn.addEventListener('click', close);
    root.addEventListener('click', (e) => { if (e.target === root) close(); });
    detailBtn.addEventListener('click', () => {
      const pid = items[index] && items[index].pixiv_id;
      close();
      if (pid) window.location.href = `/detail/${pid}`;
    });
    dlBtn.addEventListener('click', () => {
      const it = items[index];
      if (!it) return;
      dlBtn.disabled = true;
      dlBtn.textContent = '...';
      fetch(`/download/${it.pixiv_id}`, {
        method: 'POST',
        headers: { 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '', 'Content-Type': 'application/json' },
      }).then(r => r.json()).then(d => {
        dlBtn.disabled = false;
        if (d.status === 'done') { dlBtn.textContent = '已下载'; return; }
        dlBtn.textContent = '下载中...';
        const iv = setInterval(() => {
          fetch(`/download_status/${it.pixiv_id}`).then(r => r.json()).then(s => {
            if (s.status === 'done') { clearInterval(iv); dlBtn.textContent = '已下载'; }
            else if (s.status === 'failed') { clearInterval(iv); dlBtn.textContent = '下载'; dlBtn.disabled = false; }
          }).catch(() => {});
        }, 2000);
        setTimeout(() => clearInterval(iv), 300000);
      }).catch(() => { dlBtn.disabled = false; dlBtn.textContent = '下载'; });
    });
    if (favBtn) favBtn.addEventListener('click', toggleFav);

    document.addEventListener('keydown', onKey);
    imgEl.parentElement.addEventListener('touchstart', (e) => { touchX = e.touches[0].clientX; }, { passive: true });
    imgEl.parentElement.addEventListener('touchend', (e) => {
      const diff = touchX - e.changedTouches[0].clientX;
      if (Math.abs(diff) > 50) showAt(index + (diff > 0 ? 1 : -1));
    }, { passive: true });
  }

  function onKey(e) {
    if (!root || root.style.display === 'none') return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); showAt(index - 1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); showAt(index + 1); }
    else if (e.key === 'Escape') close();
  }

  function showAt(i) {
    if (!items.length) return;
    index = Math.max(0, Math.min(i, items.length - 1));
    const it = items[index];
    imgEl.src = it.thumbUrl || '';
    prevBtn.disabled = index === 0;
    nextBtn.disabled = index >= items.length - 1;
    counter.textContent = `${index + 1} / ${items.length}`;
    const showFav = typeof it.isFav === 'boolean' && !it.collectionView;
    favBtn.hidden = !showFav;
    if (showFav) {
      favBtn.textContent = it.isFav ? '♥ 已收藏' : '♡ 收藏';
      favBtn.classList.toggle('lb-fav-on', !!it.isFav);
    }
    dlBtn.disabled = false;
    dlBtn.textContent = '下载';
    maybeUpgrade();
  }

  // 已下载作品升级为原图（静默，失败忽略）
  function maybeUpgrade() {
    const it = items[index];
    if (!it || upgraded.has(it.pixiv_id)) return;
    upgraded.add(it.pixiv_id);
    fetch(`/api/detail/${it.pixiv_id}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d && d.local_urls && d.local_urls.length && index < items.length && items[index].pixiv_id === it.pixiv_id) {
          imgEl.src = d.local_urls[0];
        }
      }).catch(() => {});
  }

  async function toggleFav() {
    const it = items[index];
    if (!it) return;
    const resp = await fetch(`/api/favorite/${it.pixiv_id}`, {
      method: 'POST',
      headers: {
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '',
        'Content-Type': 'application/json',
      },
    });
    if (!resp.ok) return;
    const d = await resp.json();
    it.isFav = d.is_favorite;
    favBtn.textContent = it.isFav ? '♥ 已收藏' : '♡ 收藏';
    favBtn.classList.toggle('lb-fav-on', !!it.isFav);
    // 同步网格卡片上的 ♥ 状态（页面 JS 提供卡片更新钩子）
    if (window.__lbSyncFav) window.__lbSyncFav(it.pixiv_id, d.is_favorite);
  }

  function open(list, startIndex) {
    if (!root) build();
    items = list || [];
    upgraded.clear();  // 每次打开重新探测，避免陈旧
    showAt(startIndex || 0);
    root.style.display = 'flex';
    requestAnimationFrame(() => root.classList.add('lb-open'));
    document.body.style.overflow = 'hidden';
  }

  function close() {
    if (!root) return;
    root.classList.remove('lb-open');
    setTimeout(() => { root.style.display = 'none'; }, 200);
    document.body.style.overflow = '';
  }

  return { open, close };
})();
```

- [ ] **Step 2: style.css 追加 lightbox 样式**

在 `static/style.css` 末尾追加：

```css
/* ── Lightbox ── */
.lightbox-overlay {
  position: fixed; inset: 0; z-index: 2000;
  display: none; align-items: center; justify-content: center;
  background: rgba(38, 26, 32, .55);
  opacity: 0; transition: opacity 0.2s ease;
}
.lightbox-overlay.lb-open { opacity: 1; }
.lightbox-main {
  position: relative;
  max-width: 92vw; max-height: 88vh;
  display: flex; flex-direction: column; align-items: center;
  transform: scale(.96); transition: transform 0.2s ease;
}
.lightbox-overlay.lb-open .lightbox-main { transform: scale(1); }
#lightboxImg {
  max-width: 92vw; max-height: 78vh;
  border-radius: var(--radius-lg);
  box-shadow: 0 12px 48px rgba(60, 30, 42, .35);
  background: var(--bg-thumb);
  object-fit: contain;
}
.lightbox-bar {
  display: flex; align-items: center; gap: 8px;
  margin-top: 12px; padding: 8px 12px;
  background: rgba(250, 246, 244, .92);
  backdrop-filter: blur(8px);
  border-radius: 999px;
  box-shadow: 0 4px 16px rgba(60, 30, 42, .15);
}
.lightbox-btn {
  border: none; background: var(--accent-subtle);
  color: var(--accent); border-radius: 999px;
  padding: 6px 14px; font-size: 0.78rem; font-weight: 600;
  cursor: pointer; transition: background 0.15s, transform 0.15s;
  min-height: 36px;
}
.lightbox-btn:hover { background: rgba(226, 87, 126, .16); }
.lightbox-btn:disabled { opacity: .35; cursor: default; }
.lightbox-btn.lb-fav-on { background: var(--accent); color: #fff; }
.lightbox-counter { font-size: 0.75rem; color: var(--text-secondary); min-width: 64px; text-align: center; }
.lightbox-spacer { flex: 1; }
@media (max-width: 640px) {
  .lightbox-bar { flex-wrap: wrap; justify-content: center; border-radius: 18px; }
  .lightbox-btn { min-height: 40px; }
}
```

- [ ] **Step 3: 校验**

```bash
node --check static/lightbox.js
```

- [ ] **Step 4: Commit**

```bash
git add static/lightbox.js static/style.css
git commit -m "feat: 共享 lightbox 预览组件（键盘/触摸/原图升级/操作条）"
```

---

### Task 5: 搜索页接入

**Files:**
- Modify: `templates/index.html:10-274`（hero 区域与内联样式）
- Modify: `static/page-index.js`

- [ ] **Step 1: hero 页签化（index.html）**

将 `templates/index.html` 中 `.search-bar` 内的 `<select id="searchType">…</select>`（第 305-309 行）替换为页签组 + 保留隐藏 select 供 JS 读写：

```html
<div class="search-type-tabs" id="searchTypeTabs">
  <button type="button" class="st-tab" data-type="tag">标签</button>
  <button type="button" class="st-tab" data-type="user">画师</button>
  <button type="button" class="st-tab" data-type="following">关注</button>
</div>
<select id="searchType" hidden>
  <option value="tag">标签</option>
  <option value="user">画师</option>
  <option value="following">关注</option>
</select>
```

同时 `.search-bar` 结构改为胶囊造型：删除原 `select#tagMode` 的内联（移到 chips 行），保留 input + 搜索按钮：

```html
<div class="search-bar">
  <input id="searchQuery" class="form-control" placeholder="多个标签用逗号分隔">
  <button id="searchBtn" class="btn-search">搜索</button>
</div>
```

`tagMode` 下拉移入筛选行，`<div class="filters-row">` 变为（顺序：排序 / 组合 / 收藏≥ / R18）：

```html
<div class="filters-row">
  <label>排序</label>
  <select id="sortOrder">
    <option value="date_d">最新</option>
    <option value="popular_d">综合</option>
  </select>
  <label>组合</label>
  <select id="tagMode" title="组合方式">
    <option value="or">OR</option>
    <option value="and">AND</option>
  </select>
  <label>收藏≥</label>
  <input id="minBookmarks" type="number" value="{{ max_bookmarks_default }}" min="0" step="100">
  <label>R18</label>
  <select id="r18Mode">
    <option value="all">显示全部</option>
    <option value="safe">隐藏R18</option>
  </select>
  <span class="r18-notice" title="R18选项是没用的，因为我压根就没有打开账号的显示R18内容设置，嘿嘿">⚠</span>
</div>
```

在 index.html 内联 `<style>` 中，`.search-bar` 与 `.btn-search` 改为胶囊（radius 999px），并新增页签样式：

```css
.search-bar {
  display: flex; background: var(--bg-elevated);
  border: 1px solid var(--border-input); border-radius: 999px;
  padding: 4px; overflow: hidden;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.search-bar:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-focus); }
.search-bar .form-control { border: none; background: transparent !important; border-radius: 999px; }
.search-bar .btn-search {
  background: var(--accent); border: none; color: #fff;
  font-weight: 600; font-size: 0.85rem; padding: 0.55rem 1.5rem;
  border-radius: 999px; cursor: pointer; transition: background 0.2s;
  white-space: nowrap;
}
.search-bar .btn-search:hover { background: var(--accent-hover); }

.search-type-tabs {
  display: inline-flex; gap: 4px; padding: 4px;
  background: var(--bg-elevated); border: 1px solid var(--border-input);
  border-radius: 999px; margin-bottom: 10px;
}
.st-tab {
  border: none; background: transparent; color: var(--text-secondary);
  font-size: 0.8rem; padding: 6px 18px; border-radius: 999px;
  cursor: pointer; transition: all 0.2s;
}
.st-tab:hover { color: var(--text-primary); }
.st-tab.active { background: var(--accent); color: #fff; font-weight: 600; }
```

原 `.search-bar select` 相关规则（第 48-71 行按钮式样）删除；`tagMode` 下拉移到筛选 chips 行（与 sortOrder/minBookmarks/r18Mode 同排，样式用现有 `.filters-row` 胶囊化——见 Step 2）。

- [ ] **Step 2: 筛选 chips 胶囊化（index.html）**

`.filters-row` 下加一条规则使控件胶囊化（内联样式追加）：

```css
.filters-row select, .filters-row input, .filters-row .form-select, .filters-row .form-control {
  border-radius: 999px !important;
}
```

`#tagMode` select 从搜索条移到 `.filters-row` 中（紧随排序 select 之后），并给一行 `<label>组合</label>`。

- [ ] **Step 3: page-index.js 页签联动**

在 `static/page-index.js` 的 `updateSearchUI()` 函数末尾追加页签 active 同步：

```js
  // 页签 active 态同步
  $$('#searchTypeTabs .st-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.type === $('#searchType').value);
  });
```

并在 Init 区（`loadR18Mode();` 之后）追加：

```js
// 搜索类型页签
$('#searchTypeTabs').addEventListener('click', (e) => {
  const tab = e.target.closest('.st-tab');
  if (!tab) return;
  $('#searchType').value = tab.dataset.type;
  updateSearchUI();
});

// 页面加载时按 URL/缓存恢复的类型激活页签（restoreSearchState 也调用 updateSearchUI，自动生效）
```

- [ ] **Step 4: 卡片点击 → lightbox + currentItems**

`static/page-index.js`：

1. 文件顶部状态区新增 `let currentResults = [];`
2. `renderPage()` 中、`renderInChunks` 调用前加 `currentResults = page;`
3. `finishSearch()` 中 `renderInChunks` 调用前加 `currentResults = loadedPages[0];`
4. `renderCard()` 的卡片 click 处理（现为 `window.location.href = /detail/...`）替换为：

```js
  // Card click → lightbox
  item.querySelector('.photo-card').addEventListener('click', (e) => {
    if (e.target.closest('.photo-tag') || e.target.closest('.artist-link') || e.target.closest('.photo-card-actions')) return;
    const idx = currentResults.findIndex(x => x.pixiv_id === r.pixiv_id);
    lightbox.open(currentResults.map(x => ({
      pixiv_id: x.pixiv_id,
      thumbUrl: proxyThumb(x.thumb_url),
    })), idx >= 0 ? idx : 0);
  });
```

- [ ] **Step 5: 校验 + 手测 + Commit**

```bash
node --check static/page-index.js
```

浏览器手测：页签切换搜索类型正常；点卡片弹 lightbox，←→/Esc/遮罩可用；下载按钮可用；「打开详情」跳转正确；标签/画师点击仍为搜索行为。

```bash
git add templates/index.html static/page-index.js
git commit -m "feat: 搜索页接入轻快内容风（页签/胶囊/lightbox）"
```

---

### Task 6: 图库页接入

**Files:**
- Modify: `static/page-gallery.js`
- Modify: `templates/gallery.html:205-242`（标题行/过滤行结构微调可选，类名保持）

- [ ] **Step 1: 默认排序改「按下载时间」**

`static/page-gallery.js` 中 `let sortOrder = 'created';`（第 8 行）替换为：

```js
let sortOrder = 'downloaded';
```

并在 Init（`loadGallery(1)` 前）加一行保证下拉框选中同步：

```js
$('#sortSelect').value = 'downloaded';
```

- [ ] **Step 2: 选中描边**

`page-gallery.js` 的复选框 change 监听（`col.querySelector('.card-checkbox').addEventListener('change', ...)`）中，在 `updateFloatBar()` 前加：

```js
    col.querySelector('.gallery-card').classList.toggle('card-selected', this.checked);
```

- [ ] **Step 3: 浮条进出动画**

`templates/gallery.html` 内联样式 `.batch-float-bar` 块追加过渡（隐藏态由 `hidden` 类控制，增加 transform/opacity 过渡）：

```css
.batch-float-bar {
  transition: transform 0.22s ease, opacity 0.22s ease;
}
.batch-float-bar.hidden {
  transform: translate(-50%, 12px);
  opacity: 0;
  pointer-events: none;
}
```

（≤640px 的媒体查询里 `.batch-float-bar` 有 `transform: none`，需同步补 hidden 态：媒体查询内追加 `.batch-float-bar.hidden { transform: translateY(12px); }`。）

- [ ] **Step 4: 卡片点击 → lightbox + currentItems + __lbSyncFav**

`static/page-gallery.js`：

1. 状态区加 `let currentResults = [];`
2. `renderGalleryData()` 中 `renderInChunks` 调用前加 `currentResults = data.data;`
3. `renderCard()` 卡片 click 处理（现跳 `/detail/`）替换为：

```js
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

4. 文件末尾（Init 区）注册收藏同步钩子：

```js
// lightbox 内收藏后同步卡片 ♥（页面内现有的 fav 按钮更新逻辑复用其展示效果：
// 直接更新对应卡片上的按钮/title，不重载页面）
window.__lbSyncFav = (pid, isFav) => {
  const card = document.querySelector(`.gallery-card[data-pid="${pid}"]`);
  if (!card) return;
  const btn = card.querySelector('.card-fav-btn');
  const titleEl = card.querySelector('.card-title');
  if (btn) {
    btn.classList.toggle('favorited', !!isFav);
    btn.innerHTML = isFav ? '❤' : '♡';
    btn.title = isFav ? '取消收藏' : '收藏';
  }
  if (titleEl && titleEl.firstChild) {
    const prev = titleEl.firstChild.textContent;
    titleEl.firstChild.textContent = isFav ? '❤ ' + prev.replace(/^❤ /, '') : prev.replace(/^❤ /, '');
  }
};
```

注意：`x.is_favorite` 字段在 `to_dict(favorite=...)` 下有值（api_gallery 传入 default_fav_set 判定）。未下载作品的收藏字段也由 `to_dict` 提供。

- [ ] **Step 5: 校验 + 手测 + Commit**

```bash
node --check static/page-gallery.js
```

浏览器手测：进入图库默认按下载时间排序（下拉显示「按下载时间」，列表顺序与下载时间一致）；勾选复选框出现粉描边；浮条弹出/收起带动画；点卡片弹 lightbox，收藏按钮切换后卡片 ♥ 同步；其余按钮行为不回归。

```bash
git add static/page-gallery.js templates/gallery.html
git commit -m "feat: 图库页接入（默认下载排序/选中描边/浮条动效/lightbox）"
```

---

### Task 7: 缓存页接入

**Files:**
- Modify: `static/page-cache.js`
- Modify: `templates/cache.html`（筛选行控件胶囊化样式）

- [ ] **Step 1: 筛选行胶囊化（cache.html）**

`templates/cache.html` 内联样式追加：

```css
#cacheTagSelect, #cacheMinBookmarks, #cacheSortOrder, #cacheR18, #cacheFilterTag, #cacheBrowseBtn, #cacheRefreshBtn {
  border-radius: 999px !important;
}
```

（现有 form-select/form-control 类已有新 token 底色。移动端媒体查询原有 flex 规则保持。）

- [ ] **Step 2: 卡片点击 → lightbox + currentItems**

`static/page-cache.js`：

1. 状态区加 `let currentResults = [];`
2. `renderCacheResults()` 中 `renderInChunks` 调用前加 `currentResults = data.results;`
3. `renderCard()` 卡片 click 处理（现跳 `/detail/`）替换为：

```js
  // Card click → lightbox（下载/删除按钮区域除外）
  item.querySelector('.photo-card').addEventListener('click', (e) => {
    if (e.target.closest('.photo-card-actions')) return;
    const idx = currentResults.findIndex(x => x.pixiv_id === r.pixiv_id);
    lightbox.open(currentResults.map(x => ({
      pixiv_id: x.pixiv_id,
      thumbUrl: proxyThumb(x.thumb_url),
    })), idx >= 0 ? idx : 0);
  });
```

- [ ] **Step 3: 校验 + 手测 + Commit**

```bash
node --check static/page-cache.js
```

浏览器手测：筛选控件为胶囊造型；浏览/翻页正常；点卡片弹 lightbox（无收藏按钮）；下载/删除按钮行为不回归（删除仍为 confirm）。

```bash
git add static/page-cache.js templates/cache.html
git commit -m "feat: 缓存页接入（筛选胶囊/lightbox）"
```

---

### Task 8: 总验证

**Files:**
- 无代码改动（如发现问题则修复并提交）

- [ ] **Step 1: 全 JS 语法校验**

```bash
node --check static/app.js && node --check static/lightbox.js && node --check static/page-index.js && node --check static/page-gallery.js && node --check static/page-cache.js
```

预期：全部无输出。

- [ ] **Step 2: 后端回归**

```bash
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q
```

预期：206 passed / 4 failed（`tests/test_test_setup.py` 环境相关失败，与本改动无关的既有状态）。

- [ ] **Step 3: 浏览器手测清单（逐项确认）**

1. 三页配色一致（暖白底、玫瑰粉主色、粉胶囊标签）
2. 三页卡片：14px 圆角、常驻柔和投影、hover 上浮 + 图片微缩放
3. lightbox：打开缩放淡入、←→/Esc/遮罩关闭、触摸滑动、页码计数、已下载作品原图升级、未下载保持缩略图可打开详情
4. 搜索页：类型页签切换、胶囊搜索条、筛选 chips、搜索流程与轮询提示不回归
5. 图库页：默认「按下载时间」、勾选粉描边、浮条弹出动画与计数、批量删除确认、lightbox 收藏同步卡片 ♥
6. 缓存页：筛选胶囊、浏览/翻页、lightbox 无收藏按钮、删除确认
7. 骨架屏/图片淡入/cardIn 过渡、prefers-reduced-motion 下无动画
8. 移动端 ≤640px：布局不崩、按钮 ≥40px、lightbox 操作条换行

- [ ] **Step 4: 最终提交（如有修复）**

```bash
git add -A
git commit -m "fix: 前端重塑验证修正"
```

---

## Self-Review 备注

- **Spec 覆盖**：配色/卡片/布局（T1-T2）✓；lightbox（T4 + T5/6/7 接入）✓；浮条动效与选中态（T6）✓；默认排序（T6）✓；列表过渡（T3 保持既有 cardIn）✓；验证（T8）✓。
- **排除项**：深色模式/其余五页/顶部进度条/FLIP/新后端接口——均无对应任务，符合 spec「不做」节。
- **类型一致性**：lightbox.open 接收 items 形如 `{pixiv_id, thumbUrl, isFav?, collectionView?}`；三页调用处字段名统一；`__lbSyncFav(pid, isFav)` 仅图库页注册。`currentResults` 命名三页一致。