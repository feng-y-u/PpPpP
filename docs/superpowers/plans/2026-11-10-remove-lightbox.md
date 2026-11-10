# 移除共享灯箱 + 三处点击缺陷修复 — 实施计划

- 对应 spec：`docs/superpowers/specs/2026-11-10-remove-lightbox-design.md`
- 状态：**已实现并验证**

## 步骤

- [x] **Step 1 — 首页卡片改跳详情页**（`static/page-home.js`）
  - `renderHomeCard` 去掉灯箱分支与 `homeItems.map(...)`，点击与键盘 Enter/空格都 `location.href = /detail/<pid>`。
- [x] **Step 2 — 搜索页卡片改跳详情页**（`static/page-index.js`）
  - 删除 `lightbox` 判定与 `lightbox.open(...)`；已提交卡与预览卡合流为跳详情页。
  - `renderPage` / `finishSearch` 不再传 `{list, index}`；`renderCard` 只保留 `opts.preview`。
  - 更新 `renderCard` 头部注释（① 由"已提交卡→灯箱"改为"所有卡片→详情页"）。
- [x] **Step 3 — 图库卡片改跳详情页**（`static/page-gallery.js`）
  - 删除 `.card-body` 分流与 `lightbox.open(...)`；整卡（除删除/勾选/下载链接触发）都走 `buildDetailUrl`（保留 `ctx` 上下文）。
- [x] **Step 4 — 移除灯箱资源**
  - 删除 `static/lightbox.js`。
  - `templates/{index,gallery,home}.html` 删 `<script src="/static/lightbox.js">`。
  - `static/style.css` 删 `.lightbox-overlay/.lightbox-main/#lightboxImg/.lightbox-bar/.lightbox-btn/.lightbox-counter/.lightbox-spacer` 及两条媒体查询内的灯箱规则。
- [x] **Step 5 — 修详情页画师名链接**（`templates/detail.html`）
  - `/?type=user&query=` → `/explore?type=user&query=`。
- [x] **Step 6 — 修搜索页标签 ×**
  - `static/app.js` 新增共享 `addBlockedTag(tag)`（自带 CSRF 读取、toast，返回成功与否）。
  - `static/page-settings.js` 删本地同名实现，「添加」按钮改为 `addBlockedTag(t).then(ok => ok && loadBlockedTags())`；`removeBlockedTag` 保持本页。
- [x] **Step 7 — 修图库下载链接死内联**
  - 去掉生成的 `onclick="event.stopPropagation()"`；`.gallery-card` handler 的 `closest('a')` 守卫保留。
- [x] **Step 8 — 「已下载」入口统一为「下载原图」**（同日追加，D8）
  - `app.js::updateDlDone` 卡片分支改为 `<a href="/download_file/<pid>">下载原图</a>`（不再挂监听器）；详情页分支把按钮**替换成**同款 `<a>`。
  - `page-index.js` / `page-cache.js` 的已下载分支同改，并删掉各自的 `.dl-file-btn` 监听器与 `downloadFile()`（`window.open` 新标签页路径整体移除）。
  - `page-gallery.js`、`page-downloads.js` 的已下载链接文案改为「下载原图」。
- [x] **Step 9 — 测试与文档同步**
  - `tests/test_app.py::test_index_page_cards_link_to_detail_without_lightbox`（原灯箱用例改写：模板与脚本均不得出现 lightbox；卡片跳详情；预览卡不渲染下载）。
  - `tests/test_home.py`：首页卡片断言改为"跳详情页 + 无 lightbox"；`/explore` 断言改为**不引入** `lightbox.js`。
  - 更新 `AGENTS.md`（`static/` 清单、前端约定、`/api/image`·`to_dict`·连接复用三处措辞）、`docs/technical-documentation.md`、`docs/maintenance.md` 及 `config.py`/`pixiv_client.py`/`routes_gallery.py` 的注释。

## 验证结果

| 项 | 结果 |
|---|---|
| `node --check`（app / page-index / page-gallery / page-home / page-settings / page-cache / page-detail / page-downloads） | 全部通过（Step 8 后复跑） |
| `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q` | **454 passed / 2 skipped / 0 failed**（19.58s / 20.99s 两次复跑，与基线一致；2 skip 为 Windows 权限位用例） |
| `grep -rn "lightbox\|灯箱" static templates *.py tests` | 无实现残留；仅回归用例的 docstring 叙述历史 |
| `grep -rn "downloadFile\|>下载<" static templates` | 无 `window.open` 新标签页路径残留；已下载态只剩「下载原图」真链接 |
| 行为核对（读代码） | 首页/搜索/图库卡片 → `/detail/<pid>`；图库带 `ctx=gallery` 上下文；搜索预览卡同入口；卡片下载/删除/勾选/标签/画师名的守卫均未变 |
