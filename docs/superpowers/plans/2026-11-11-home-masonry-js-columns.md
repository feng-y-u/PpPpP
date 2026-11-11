# 首页瀑布流改为 JS 分列 实施计划

- 日期：2026-11-11
- 对应设计：`docs/superpowers/specs/2026-11-11-home-masonry-js-columns-design.md`
- 状态：已实现（2026-11-11）

## 步骤

1. **CSS（`templates/home.html`）**
   - `.masonry`：`column-count: 3; column-gap: 18px` → `display:flex; align-items:flex-start; gap:18px`。
   - 新增 `.masonry-col { flex: 1 1 0; min-width: 0; }`。
   - `.masonry-item`：保留 `width:100%` / `margin-bottom:18px` / 浮现动画；去掉 `break-inside: avoid`（多列专用）。
   - 删除 `@media (max-width: 992px) { .masonry { column-count: 2 } }`（列数改由 JS 决定），保留 `≤640px` 的间距收缩。
   - `.feed-divider`：删除 `column-span: all` / `-webkit-column-span`。
   - 模板：`<div id="homeGrid" class="masonry">` → `<div id="homeGrid"></div>`。

2. **脚本（`static/page-home.js`）**
   - 新增布局状态：`homeColCount`、`hotNodes`、`moreNodes`、`bandHot`、`bandMore`、`dividerEl`、`resizeTimer`。
   - 新增 `columnCountForViewport()` / `createBand()` / `placeIntoColumns()` / `rebuildBands()`。
   - `insertDivider()` 改为以 `dividerEl` 判重（去掉 `dividerInserted`）。
   - `loadHome()`：重置布局状态 → 读列数 → 建上段 → 卡片经 `placeIntoColumns` 落位并记入 `hotNodes`。
   - `loadMore()`：确保分隔条与下段存在 → 卡片经 `placeIntoColumns` 落位并记入 `moreNodes`。
   - 末尾注册 `resize`：仅在列数变化时（防抖 150ms）`rebuildBands()`。

3. **测试（`tests/test_home.py`）**
   - `test_home_page_card_wiring`：插入断言改为 `placeIntoColumns(bandHot, node)` / `placeIntoColumns(bandMore, node)`（不变式不变）。
   - 新增 `test_home_masonry_uses_js_columns_not_css_multicol`：页面 HTML 不得出现 `column-count` / `column-span`；脚本必须创建 `masonry-col` 并读取 `offsetHeight`。

4. **验证**
   - `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q`。
   - 手工检查：`/` 滚动加载时已显示的卡片不再位移；窗口在 3 列/2 列断点间缩放后布局正确、图片不重新下载。

5. **回写**：在设计文档末尾记录"已实现 + 验证结果"，README/AGENTS 若提到多列布局则同步修正（本次无需改）。
