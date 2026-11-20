# X 图片抓取一致性重审（2026-11-20）

任务：X 抓取"应该像系统原生功能一样自然，而不是硬塞进来的独立模块"。
先读原生实现（search/download/gallery/detail 的路由与页面 JS），列出不一致，
再逐项收敛——不是给 X 打补丁，而是让两种来源共用同一套交互与组件。

## 一、审查结论：X 是一套平行实现

| 维度 | 原生（Pixiv） | X（改造前） |
|---|---|---|
| 卡片组件 | `.photo-card` + `.photo-badges` + `.photo-card-info/actions`（搜索/缓存/首页三处共用） | Bootstrap `row/col` + 自有 `.x-card`，视觉与结构双断崖 |
| 网格 | `.masonry-grid/.masonry-item` 瀑布流（缓存页） | Bootstrap 栅格 |
| 详情动线 | 点卡片 → `/detail/<pid>`，下载前后都能看 | 已下载才进 `/x/view`；未下载点卡片跳 x.com 原推 |
| 下载状态词表 | `download_status`：none/downloading/done/failed | `saved_status`：NULL/queued/downloading/done/failed（多一个 queued，语义还是"下载中"） |
| 下载管理 | `/api/downloads` 统一队列视图 | 完全缺席（X 下载只在卡片上转圈） |
| 抓取反馈 | 异步任务 + `/api/search/status` 轮询，结果逐条长出来 | 点"刷新"只有一句 toast，之后全靠手动"刷新列表" |
| 图片懒加载 | `data-src` + IntersectionObserver（app.js 共享） | 直接 `src loading=lazy` |
| 空态 | 区分原因给指引 | 一句"还没有抓取过" |

## 二、实施（复用优先）

1. **`XWork.to_dict` 输出原生别名**（models.py）：`source:'x'`、
   `download_status`（saved_status→原生词表，queued 并入 downloading）、
   `page_count`、`thumb_url`（首图）、`local_urls`（`/api/x/image/...`）、
   `tweet_url`。统一组件从今天起只认一套字段名。
2. **X 页卡片与缓存页同构**（x.html + page-x.js）：`.masonry-grid/.masonry-item/
   .photo-card` 样式逐行取自 cache.html；徽标集合对齐（NP/已下载/疑似重复/
   下载中/下载失败）；操作区 = 下载（主）+ 原推 + 看 Pixiv + dup + 删除；
   `data-src` 懒加载走 app.js 共享 observer。
3. **详情动线统一**：点卡片一律进 `/x/view/<id>`（xfeed.py + x_view.html）。
   未下载作品渲染**代理原图预览**（/ximg）+「下载原图」按钮（POST + 轮询，
   完成后本页自动切成本地文件）；旧版"还没有下载过"的空话页删除。
4. **下载管理并入 X**（download.py + page-downloads.js）：`/api/downloads` 的
   active/queued/completed 三个区都带 X 行（`source:'x'`、缩略图预代理、
   标题取推文首行），前端只按 `source==='x'` 分支：无 取消/清除（引擎无取消
   语义），完成态给「查看本地文件」（/x/view），不再给一个必 404 的
   /download_file 链接。
5. **抓取反馈对齐异步任务模式**（runtime.py + background.py + page-x.js）：
   `runtime._x_fetch_progress`（键集合固定、只改值，无锁读同约定）记录
   status/pages_done/pages_total/found/started_at/finished_at/error；
   `_x_fetch_tag` 每翻一页更新，失败走 `_x_fetch_fail` 收口，启动重置把停在
   fetching 的进度翻成 error；`/api/x/tags` 附带 progress；前端点"立即抓取"
   后轮询同一接口，chip 上实时显示「抓取中 1/3 页 · N 条」，完成自动刷新列表
   并 toast（不再需要手动"刷新列表"）。
6. **空态分原因**：没抓过 → 引导添加；筛选后为空 → 提示切回全部。

## 三、验证

- 离线：**572 passed / 2 skipped / 0 failed**（+6：原生别名形状、标签进度、
  下载页并入 X 行、未下载详情页预览、抓取进度成功/失败/启动重置三态）。
- 真机（Chrome CDP，1440×900 与 390×844）：
  - X 页 24 张标准 photo-card，图片懒加载 23/24、0 破图、0 控制台错误；
  - 未下载作品 `/x/view`：代理原图加载成功、下载按钮在位；
  - 下载管理页 3 行 X 任务（查看链接、无取消按钮）；
  - 点 ↻ → chip 显示"抓取中…" → 轮询到完成自动刷新列表（全链路 <10s）；
  - 移动端 2 列、横向溢出 0。

## 四、有意保留的差异

- X 卡片标题行是作者（推文没有作品名），文本摘录放在第二行——形状仍与
  photo-card 一致，只是语义字段不同。
- X 下载无「取消/清除」按钮：X 引擎没有取消语义，给了按钮就是承诺一个
  不存在的动作（原生侧的幽灵 downloading 自愈也不适用于 X 的执行器结构）。
- X 仍不进 `/gallery`：图库按设计只读 Pixiv 的 illusts 表；X 的浏览面是
  `/x`（本就是与 /cache 平行的来源页）。
