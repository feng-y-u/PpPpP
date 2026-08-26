# 前端重塑设计：轻快内容风（搜索 / 图库 / 缓存三页）

- 日期：2026-08-26
- 状态：已批准（brainstorming 流程确认）
- 范围：仅 `搜索`、`图库`、`缓存` 三页；其余五页（详情/下载/设置/登录/解锁）不动

---

## 背景与目标

用户对前端体验不满意：现有视觉风格（紧凑工具向、4px 圆角、玫红强调）与交互（无预览、批量反馈生硬）缺乏「轻快、柔和、亲切」的观感。目标是在当前项目约束（Flask 模板 + Bootstrap 5.3 + 原生 JS、无构建流程、单用户自用）下，将三页重塑为统一的「轻快内容风」设计语言，并升级浏览与批量交互。

## 已确认的决策（brainstorming 问答结论）

| 决策点 | 结论 |
|--------|------|
| 视觉方向 | 轻快内容风：圆润、柔和、亲切 |
| 配色 | 方案 A 玫瑰粉：在现有玫红 #d4436b 基础上柔化 |
| 卡片 | 方案 A 圆润内容风：14px 大圆角、柔和投影、宽松间距 |
| 布局 | 三页线框已确认（见下） |
| 预览 | 居中大图弹窗 lightbox（键盘 ←→ 切换 / Esc 关闭 / 底部操作） |
| 批量 | 仅打磨图库页现有浮条（搜索/缓存页不加选择功能） |
| 动效 | 浮条与弹窗进出动画 + 悬停/选中态微交互 + 列表切换过渡打磨 |
| 主题 | 仅浅色 |
| 实现路线 | A. 就地重塑：无构建、无新依赖 |
| 附加需求 | 图库页默认排序改为「按下载时间」 |

## 视觉系统

### 配色 Token（覆盖 `style.css` `:root`）

| Token | 现值（参考） | 用途 |
|-------|------|------|
| `--accent` | `#e2577e` | 主色（柔化的玫瑰粉，替代 #d4436b） |
| `--accent-hover` | `#cf4668` | 主色 hover |
| `--accent-subtle` | `rgba(226,87,126,.08)` | 标签底/选中底 |
| `--accent-focus` | `rgba(226,87,126,.18)` | 焦点环 |
| `--bg-page` | `#faf6f4` | 暖白页面底（替代 #f5f5f0） |
| `--bg-elevated` | `#ffffff` | 卡片/浮层 |
| `--bg-navbar` | `rgba(250,246,244,.85)` | 导航栏 |
| `--bg-thumb` | `#f2e8ea` | 缩略图占位（粉调灰） |
| `--text-primary` | `#3a3338` | 主文字（暖黑） |
| `--text-secondary` | `#8a6a74` | 次级文字（暖灰紫） |
| `--text-muted` | `#b09aa4` | 弱化文字 |
| `--border-input` | `rgba(180,120,135,.18)` | 输入框描边 |
| `--border-card` | `rgba(226,87,126,.08)` | 卡片分隔 |
| `--success` | `#3b8a5e`（保留） | 成功 |
| `--danger` | `#c44a4a`（保留） | 危险 |

### 圆角 / 阴影 / 间距 Token

- `--radius-sm: 8px`（按钮、小控件）
- `--radius-card: 14px`（卡片，替代 4px）
- `--radius-lg: 18px`（弹窗、hero 容器）
- 阴影：`--shadow-card: 0 2px 10px rgba(200,120,140,.08)`（卡片常驻柔和投影）；
  `--shadow-card-hover: 0 6px 20px rgba(200,120,140,.14)`
- 卡片间距：网格 `gap: 16px`（桌面）/ 8px（≤480px，保持现有媒体查询结构）

### 卡片组件（三页统一，迁移到 style.css 全局）

- 统一类名 `photo-card`（搜索/缓存沿用）+ 图库 `gallery-card` 同步为同一视觉：
  14px 圆角、白底、常驻柔和投影、hover 上浮 2px + 阴影加深
- 图块：`aspect-ratio` 占位保持（无 CLS）；hover 图片微缩放 1.03（0.3s）
- 信息区：标题 0.82rem/600 单行省略；画师 0.72rem 次级色；标签胶囊
  （`--accent-subtle` 底 + `--accent` 字，圆角 999px，替代现有 3px 直角小签）
- 按钮：保持 Bootstrap btn-sm 体系，圆角升级 `--radius-sm`
- 选中态（图库）：卡片描边 `2px --accent` + 轻微上浮动画（0.2s）

### 布局（三页线框，已确认）

1. **搜索页**：居中 hero（搜索类型页签 + 胶囊搜索条 + 筛选 chips 行）→ 工具行
   （全部下载 + 统计）→ 卡片网格 → 分页。搜索中保留现有 spinner + 提示逻辑。
2. **图库页**：标题行（页标题 + 排序/收藏夹下拉 + 管理收藏夹）→ 标签过滤
   （输入框 + 活跃标签胶囊）→ 工具行（全选 + 统计）→ 网格 → 分页（含跳页）。
3. **缓存页**：筛选胶囊行（标签/收藏下限/排序/R18/过滤 + 浏览/刷新）→ 元信息行
   → 网格 → 分页。筛选功能与后端参数一一对应，不改变语义。

## 交互规格

### Lightbox 预览（三页共用，新增）

- 触发：点击卡片（现有跳详情逻辑改为弹预览；lightbox 内「打开详情」按钮负责跳转）
- 结构：遮罩 + 居中大图 + 底部操作条；打开/关闭缩放淡入 0.2s
- 键盘：`←`/`→` 切到本页相邻卡片（环状可选，默认线性 + 边界禁用）、`Esc` 关闭
- 触摸：左右滑动切换（复用 detail 页 swipe 模式），`pointer-events` 不阻塞滚动
- **图源策略**（不新增后端接口）：
  1. 打开瞬间用 `thumb_url` 代理图即时渲染（模糊可接受）
  2. 后台静默 `fetch('/api/detail/<id>')`：若返回 `local_urls`（已下载）则升级为原图
  3. 未下载作品保持缩略图，操作条提供「打开详情」（详情页有完整中图/原图候选链）
- 操作条：下载（调 `triggerDownload`，与卡片一致走轮询）、收藏（仅图库页显示
  ♥ 切换）、打开详情（`location.href='/detail/<id>'`）
- 实现：`static/lightbox.js` 新文件 + `app.js` 暴露少量辅助；不依赖 Bootstrap modal
- 图库分页/搜索翻页后 lightbox 的「下一张」索引随当前网格重建自动失效（关闭即弃）

### 图库批量浮条动效（仅打磨）

- 弹出/收起：`transform: translate(-50%, 12px) + opacity 0` → 原位不透明（0.22s ease）
- 计数变化：数字弹跳（scale 1.15 → 1，0.18s）
- 选中卡片：描边动画同步（见卡片组件）
- 删除确认：沿用 Bootstrap modal，`modal-content` 加 0.18s 缩放淡入

### 列表切换过渡

- 现有 `cardIn`（0.28s + 30ms stagger）保留；翻页/过滤时在 `renderInChunks` 基础上
  微调：stagger 上限 24 张——超过 24 张后 animation-delay 不再累加（同批进入），
  避免长页尾部卡片过慢出现
- `prefers-reduced-motion` 全关（现有实现保持）

### 图库默认排序

- `page-gallery.js`：`sortOrder` 初始值 `'created'` → `'downloaded'`，
  `$('#sortSelect').value` 初始选中「按下载时间」；其余排序逻辑（含 localStorage
  缓存键 `pv_gallery_*`）不变——键含 sortOrder，改默认不影响既有缓存条目

## 技术架构（就地重塑）

| 文件 | 改动 |
|------|------|
| `static/style.css` | `:root` token 重构（配色彩值替换 + 圆角/阴影/间距新增）；卡片组件、标签胶囊、选中态、lightbox、浮条动效样式；保持现有布局媒体查询与动画节 |
| `static/lightbox.js` | 新增：共享 lightbox 组件（单例、键盘/触摸、图源升级逻辑、操作条） |
| `static/app.js` | 现有工具保留；补充 lightbox 初始化钩子与 `renderInChunks` 的 stagger 上限微调 |
| `static/page-index.js` | 卡片类名/标签胶囊更新；点击卡片 → 打开 lightbox（含下载/详情操作）；搜索工具行样式对齐 |
| `static/page-gallery.js` | 同上 + 默认 `sortOrder='downloaded'`；浮条动效接入；选中描边 |
| `static/page-cache.js` | 同上（无收藏按钮）；筛选行类名对齐 |
| `templates/index.html` | hero 结构与类名更新（页签化搜索类型、胶囊搜索条、筛选 chips）；卡片区域无结构变化 |
| `templates/gallery.html`、`templates/cache.html` | 标题行/筛选行类名对齐新 token；删除内联样式中的旧卡片 CSS（迁移至全局） |

不涉及：`app.py`、`fetcher.py`、`models.py`、`config.py`、数据库、下载/搜索引擎、详情/下载/设置/登录/解锁页面。

### 兼容与退化

- 无构建、无新依赖；原生 JS + CSS 变量 + Bootstrap 5.3
- `prefers-reduced-motion: reduce` 关闭全部动画（现有实现保留）
- 触控目标 ≥44px 约束保持（卡片与按钮尺寸不变）
- 旧浏览器：lightbox 与动画全部原生特性，无降级路径问题

## 迭代修订（2026-08-26 用户试用反馈后追加）

以下修订基于用户试用反馈，覆盖原设计的对应章节：

| 修订 | 内容 |
|------|------|
| Lightbox 范围 | **仅图库页**使用 lightbox；搜索页/缓存页点击卡片恢复直接跳转详情页（移除其 lightbox 接线与脚本引用，`lightbox.js` 保留由图库页单独加载） |
| 多图预览 | lightbox 支持**分页预览**：同一作品多张图以"图级"导航展示（←/→/按钮/触摸滑动按图前进，到达作品边界自动跨到下一作品）；计数器为 `第 K / 总张数` |
| 清晰图源 | 图源优先级：`/api/detail` 的 `local_urls`（已下载原图）> `medium_urls`（新增：`_original_to_resized` 中图代理）> 缩略图兜底；探测失败的作品下次切换到该作品时重试 |
| 后端变更 | `detail_api` 新增 `medium_urls` 响应字段（唯一后端改动），并有响应形状测试守护 |
| 页数徽章 | 图库卡片左下角新增 `.page-badge` 显示 `N 张`（`file_count \|\| page_count \|\| 1`，file_count 为磁盘真实值） |
| 数量一致性 | 图库排序/标签/收藏夹视图切换时 `invalidateGalleryCache()` 立即失效前端缓存，避免 30 分钟 TTL 快照导致两种排序显示不同总数（根因确认：后端两种排序 COUNT 相同，差异纯属前端缓存快照） |

## 验证

- `node --check` 通过全部改动的 JS（app/lightbox/page-*）
- `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q` 全量回归
  （无后端改动，预期 206 passed / 4 env 相关失败维持现状）
- 浏览器手测清单：
  1. 三页视觉一致性（卡片/标签/按钮/token）
  2. lightbox：打开/关闭、←→ 切换、Esc、点击遮罩、触摸滑动、已下载作品原图升级
  3. 图库默认排序为「按下载时间」；切换排序下拉正常
  4. 图库浮条：弹出动画、计数弹跳、选中描边、批量删除确认
  5. 搜索/缓存翻页与过渡动效、骨架屏、图片淡入不回归
  6. 移动端宽度（≤640px）布局不崩、触控目标达标

## 不做（明确排除）

- 深色模式；其余五页重设计；顶部加载进度条；FLIP 列表重排动画；
  新增后端接口；搜索/缓存页批量选择功能；卡片「单卡大图首卡」板式
  （线框中的 hero card 仅为示意，本次不做，保持统一网格）