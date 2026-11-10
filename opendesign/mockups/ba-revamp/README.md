# ba-revamp · Blue Archive 视觉改版预览稿

四个页面 + 一张交互状态表的静态视觉稿。**只覆盖视觉层**：类名与真实模板一致，
确认后再拆进 `static/style.css` 与各模板的内联 `<style>` 块。

## 文件

| 文件 | 内容 |
|---|---|
| `preview.css` | 设计令牌 + 组件样式 + 动效层（约 2200 行，按节分组） |
| `home.html` | 首页瀑布流（悬停信息牌） |
| `explore.html` | 搜索页（控制台面板 / 筛选胶囊 / 卡片） |
| `gallery.html` | 图库页（四列网格 / 批量条 / 分页） |
| `detail.html` | 详情页（观片台 + 信息栏） |
| `states.html` | 交互状态表：同一张卡的默认 / 悬停 / 悬停+信息牌 |
| `manifest.json` | 设计令牌、动效参数、截图清单 |

## 本地复现作品图

`art/` 与所有截图都不入库（含第三方作品画面，且体量大）。需要看效果时，
从仓库根 `downloads/` 裁样本图放到 `art/`，文件名与 `*.html` 里的引用一致：

```
p1 p2 p3        # 竖构图整图
w1a w1b w2a w2b # 横构图切两块
sq1 sq2 sq3 sq4 # 方形切片
```

## 渲染截图（Windows，需已装 Chrome）

```powershell
$chrome="C:\Program Files\Google\Chrome\Application\chrome.exe"
& $chrome --headless=new --disable-gpu --hide-scrollbars `
  --force-prefers-reduced-motion --force-device-scale-factor=2 `
  --window-size=1440,1600 --screenshot=home.png home.html
```

`--force-prefers-reduced-motion` 是必须的：入场动画带 `backwards` 填充，
不加这个开关会截到动画第一帧（整页透明）。

## 动效约束

- 只动 `transform` / `opacity` / `box-shadow`，不改布局属性。
- 时长 180–720 ms，缓动只有两条：`cubic-bezier(.22,.61,.36,1)` 与 `(.4,0,.2,1)`。
- `prefers-reduced-motion` 下关闭极光、扫描线、括号脉冲与漂浮。
- 真实站的卡片入场延迟由 `app.js` 的 `renderInChunks` 内联 `animation-delay` 提供，
  `preview.css` 里的 nth-child 错峰只是无 JS 时的兜底。
