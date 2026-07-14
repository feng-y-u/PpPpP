# Pixiv Viewer — 项目架构说明

## 1. 项目概述

Pixiv Viewer 是一个轻量级 Web 应用，用于搜索、浏览和下载 Pixiv 插画作品。它通过调用 Pixiv 内部 Ajax API（非官方 OAuth）实现作品发现和原图下载，适合部署在低配服务器（4 核 / 4GB / 40GB SSD）上。

技术栈：Python Flask + SQLite + Bootstrap 5

---

## 2. 目录结构

```
E:\pixiv/
├── app.py                  # Flask 主入口，路由 + 下载引擎 + 后台任务
├── fetcher.py              # Pixiv API 封装（搜索、详情）
├── models.py               # SQLAlchemy ORM 模型
├── config.py               # 全局配置常量
├── requirements.txt        # Python 依赖
├── cookies.txt             # Pixiv PHPSESSID（手动放置，已 gitignore）
├── PROJECT_PLAN.md         # 原始设计文档
├── ARCHITECTURE.md         # 本文件
├── scripts/
│   ├── diagnose.py         # 网络诊断工具
│   └── pixiv-cleanup.sh    # 磁盘清理脚本（cron）
├── templates/
│   ├── index.html          # 搜索页（含标签搜索、画师UID、关注流）
│   ├── gallery.html        # 图库页（已下载作品的本地浏览）
│   ├── bulk.html           # 批量下载页（自动翻页 + 下载）
│   ├── downloads.html      # 下载管理页（实时进度、队列、日志）
│   ├── settings.html       # 设置页
│   └── settings_unlock.html# 设置页密码解锁
├── downloads/              # 已下载原图存放目录
│   └── {pixiv_id}/         # 每个作品一个子目录
└── instance/
    ├── pixiv.db            # SQLite 数据库文件
    └── .secret_key         # Flask session 密钥（自动生成）
```

---

## 3. 认证机制（Cookie）

项目支持两种认证方式：Cookie 注入（PHPSESSID）或 OAuth Bearer token（通过 Pixiv 用户名密码自动获取）：

### Cookie 方式

1. 用户手动从浏览器 F12 → Application → Cookies → pixiv.net 复制 `PHPSESSID`
2. 存入 `cookies.txt`（本地开发）或 `/etc/pixiv-viewer/cookies.txt`（生产）
3. `fetcher.py` 中的 `_load_cookie()` 函数每次构建 session 时检查文件 mtime，仅在文件变更时重新加载（热加载，无需重启服务）
4. 支持两种格式：`PHPSESSID=xxxxx` 或纯 `xxxxx`

---

## 4. Pixiv API 调用

### 4.1 调用的 API 端点

| 端点 | 用途 | 实现位置 |
|------|------|----------|
| `GET /ajax/search/illustrations/{keyword}` | 按标签搜索 | `fetcher.py:search_by_tag()` |
| `GET /ajax/user/{uid}/profile/all` | 按画师获取所有作品 ID | `fetcher.py:search_by_user()` |
| `GET /ajax/illust/{id}` | 获取作品详情（真实收藏数、原图 URL） | `fetcher.py:_get_illust_detail()` |
| `GET /ajax/discovery/artworks` | 首页发现流 | `fetcher.py:browse_discovery()` |
| `GET /ajax/follow_latest/illust` | 关注者最新作品 | `fetcher.py:fetch_following()` |

搜索 API 返回的列表不包含 `bookmarkCount`（收藏数恒为 0），因此必须对每个作品单独调用详情 API 获取真实数据。

### 4.2 代理支持

若 `config.py` 中 `PROXY` 不为空，所有请求通过指定代理转发（HTTP/SOCKS5）。

---

## 5. 数据流

### 5.1 搜索流程

```
用户输入 → /search?type=tag&query=...&min_bookmarks=...
  → fetcher.search_by_tag() / search_by_user()
  → 调用 Pixiv 搜索 API 获取作品列表
  → 检查数据库已有缓存，只对未入库作品调用详情 API
  → 并行获取详情（FETCH_DETAIL_WORKERS=3 线程）
  → 按 min_bookmarks 过滤 + 屏蔽标签过滤
  → 写入 SQLite（Illust 表，unique 约束去重）
  → 返回 JSON 渲染前端卡片

前端搜索状态通过 sessionStorage 持久化，刷新页面可恢复上次结果。
```

### 5.2 下载流程

```
用户点击"下载原图"
  → POST /download/{pixiv_id} (CSRF 保护)
  → 提交到全局 ThreadPoolExecutor(max_workers=2)
  → 立即返回 {status: "accepted"}

后台线程 _download_illust():
  → 状态 'downloading'
  → 创建 {downloads}/{pixiv_id}/ 目录
  → 逐页下载原图（requests 流式，页间隔 3 秒）
  → 更新 _download_progress 字典（前端轮询感知进度）
  → 完成后状态 'done'

下载过程中：
  - 前端每 2 秒轮询 /download_status/{pixiv_id}
  - 按钮显示进度或下载完成状态
```

### 5.3 打包下载流程

```
GET /download_file/{pixiv_id}
  → 检查 local_paths 文件是否存在
  → 单文件: send_file 直接返回
  → 多文件: 内存 ZipFile (ZIP_STORED 不压缩) 流式返回
```

---

## 6. 数据库设计（SQLite + SQLAlchemy）

### 6.1 表结构

#### illusts（作品表）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer (PK) | 自增主键 |
| `pixiv_id` | Integer (Unique, Indexed) | Pixiv 作品 ID |
| `title` | String | 标题 |
| `user_id` | Integer | 画师 ID |
| `user_name` | String | 画师名 |
| `tags` | Text (JSON) | 标签 JSON 数组 |
| `page_count` | Integer | 页数 |
| `bookmark_count` | Integer | 真实收藏数 |
| `upload_date` | DateTime | 上传日期 |
| `thumb_url` | String | 缩略图 URL |
| `description` | Text | 作品描述 |
| `original_urls` | Text (JSON) | 原图 URL 列表 |
| `local_paths` | Text (JSON, nullable) | 本地文件路径列表 |
| `download_status` | String (nullable) | `null` / `downloading` / `done` / `failed` |
| `file_size` | Integer | 已下载文件总字节数 |
| `is_favorite` | Boolean | 是否被收藏 |
| `favorited_at` | DateTime (nullable) | 收藏时间 |
| `created_at` | DateTime | 入库时间 |

#### blocked_tags（屏蔽标签表）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer (PK) | 自增主键 |
| `tag` | String (Unique) | 被屏蔽的标签名 |
| `created_at` | DateTime | 添加时间 |

#### download_logs（下载日志表）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer (PK) | 自增主键 |
| `pixiv_id` | Integer (Indexed) | 作品 ID |
| `action` | String | `start` / `done` / `failed` / `deleted` / `cancelled` |
| `message` | String | 日志详情 |
| `created_at` | DateTime | 记录时间 |

### 6.2 SQLite 配置

- **WAL 模式**：允许并发读 + 单写，解决 Gunicorn 多 worker 写冲突
- **busy_timeout=10000**：写等待 10 秒后报错
- **safe_commit()**：带重试的提交，处理 `database is locked` 错误

---

## 7. 路由一览

| 路由 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 搜索首页 |
| `/search` | GET | 搜索 API（参数：type, query, min_bookmarks, page, sort, tag_mode, r18_mode） |
| `/gallery` | GET | 本地图库页 |
| `/api/gallery` | GET | 图库数据 API（已下载作品列表，支持 tag 过滤） |
| `/api/gallery/tags` | GET | 已下载作品的所有标签 |
| `/api/gallery/{pixiv_id}` | DELETE | 删除已下载作品 |
| `/api/gallery/batch-delete` | POST | 批量删除已下载作品 |
| `/download/{pixiv_id}` | POST | 触发下载（CSRF 保护） |
| `/download/cancel/{pixiv_id}` | POST | 取消下载 |
| `/download/reset/{pixiv_id}` | POST | 强制重置卡住的下载 |
| `/download_status/{pixiv_id}` | GET | 查询下载进度 |
| `/download_file/{pixiv_id}` | GET | 打包下载已下载文件 |
| `/api/download/batch` | POST | 批量触发下载 |
| `/api/downloads` | GET | 下载管理页数据（活跃/队列/已完成/日志） |
| `/api/following` | GET | 关注流 |
| `/thumb/{base64_url}` | GET | 缩略图代理（绕过 Referer 检查） |
| `/api/image/{pixiv_id}/{index}` | GET | 本地已下载图片直接访问 |
| `/bulk` | GET | 批量下载页面 |
| `/api/bulk/start` | POST | 启动批量下载任务 |
| `/api/bulk/status/{task_id}` | GET | 查询批量任务进度 |
| `/api/bulk/running` | GET | 查询正在运行的批量任务 |
| `/api/bulk/stop/{task_id}` | POST | 停止批量任务 |
| `/downloads` | GET | 下载管理页面 |
| `/api/blocked-tags` | GET/POST | 屏蔽标签列表/添加 |
| `/api/blocked-tags/{tag}` | DELETE | 删除屏蔽标签 |
| `/api/auto-follow/status` | GET | 自动关注状态 |
| `/api/auto-follow/config` | POST | 配置自动关注参数 |
| `/settings` | GET | 设置页面 |
| `/api/settings` | GET/POST | 读取/保存设置 |
| `/api/settings/unlock` | POST | 设置页密码解锁 |
| `/api/collections` | GET/POST | 收藏夹列表/创建 |
| `/api/collections/{id}` | PUT/DELETE | 更新/删除收藏夹 |
| `/api/collections/{id}/items` | GET/POST | 收藏夹内作品列表/添加 |
| `/api/collections/{id}/items/{pid}` | DELETE | 从收藏夹移除作品 |
| `/api/illust/{pid}/collections` | GET | 作品所属收藏夹列表 |
| `/api/favorite/{pid}` | GET/POST | 查询/切换收藏状态 |
| `/detail/{pixiv_id}` | GET | 作品详情页 |
| `/api/detail/{pixiv_id}` | GET | 作品数据 API |
| `/api/download/status/batch` | GET | 批量查询下载状态 |
| `/csrf-token` | GET | 获取 CSRF token |

---

## 8. 后台任务

### 8.1 下载线程池

- `ThreadPoolExecutor(max_workers=2)`（config.py 中 `DOWNLOAD_MAX_WORKERS`）
- 每个下载任务在独立线程中运行
- 使用 `threading.Lock` 防止同一作品重复下载
- 支持取消：`download_cancellations` 集合标记被取消的作品 ID

### 8.2 启动时重置卡死状态

应用启动时，将数据库中所有 `downloading` 状态重置为 `null`，并清理残留文件。

### 8.3 自动关注轮询

- 后台守护线程，每 `AUTO_FOLLOW_INTERVAL` 秒检查一次关注流
- 发现新作品自动入库，可配置自动下载
- 通过 `/api/auto-follow/config` 动态调整参数

### 8.4 批量下载

- 按标签自动翻页搜索并下载
- 每页搜索结果的新作品入库后立即触发下载
- 支持取消和进度跟踪
- 最大 100 页限制

---

## 9. 前端交互

### 9.1 搜索页（index.html）

- 深色主题，毛玻璃导航栏
- 三种搜索模式：标签（支持 AND/OR 多标签）、画师 UID、关注流
- 参数：排序方式、起始页、最低收藏数、R18 过滤
- 响应式瀑布流卡片布局（`col-lg-3 col-md-4 col-sm-6 col-12`）
- 卡片悬停显示收藏数、页数、标题、画师、操作按钮
- 标签点击触发新搜索 + 单标签屏蔽（×）
- 画师名点击切换为画师搜索
- **批量下载**工具栏（一键下载全部可见作品）

### 9.2 图库页（gallery.html）

- 显示所有已下载作品
- 标签过滤栏（datalist 自动补全）
- 收藏夹筛选
- 多选批量删除
- 点击进入详情页

### 9.3 批量下载页（bulk.html）

- 配置：标签、最低收藏、排序、最大页数、R18 模式
- 实时进度：当前页、已完成数、失败数、进度条
- 实时日志流
- 页面导航后自动恢复正在运行的任务

### 9.4 详情页（detail.html）

- 左右分栏布局：左侧图片展示（支持多页切换），右侧作品信息
- 键盘 ← → 翻页
- 标签点击跳转到已下载图库（标签过滤）
- 收藏夹管理（多选模态框）
- 画师名点击跳转搜索

### 9.5 设置页（settings.html）

- 网络/下载/搜索/自动关注配置
- 收藏夹管理（创建、重命名、删除）
- 可选密码保护（环境变量 `SETTINGS_PASSWORD`）

### 9.6 下载管理页（downloads.html）

- 三列布局：活跃下载、队列、最近完成
- 每 3 秒自动轮询，实时进度条
- 取消和清除操作

### 9.7 缩略图代理

前端不直接加载 Pixiv CDN 的缩略图，而是通过 `/thumb/{base64_url}` 代理：

```
img.src = /thumb/{base64_encode(thumb_url)}
  → Flask 服务端发起请求（携带正确 Referer）
  → 缓存响应 6 小时 (Cache-Control + ETag)
```

这解决了 Pixiv CDN 的 Referer 检查导致的 403 问题。

### 9.8 CSRF 保护

所有 POST 接口需在请求头中携带 `X-CSRF-Token`。Token 通过 session 存储，页面渲染时嵌入模板。

---

## 10. 配置说明（config.py）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `PIXIV_BASE_URL` | `https://www.pixiv.net` | 可改为镜像站/代理站 |
| `SEARCH_PAGES` | 10 | 每次搜索最多页数 |
| `PER_PAGE` | 60 | 每页作品数 |
| `DETAIL_TIMEOUT` | `(5, 15)` | 详情 API 连接/读取超时 |
| `DETAIL_MAX_RETRIES` | 2 | 详情 API 重试次数 |
| `FETCH_DETAIL_WORKERS` | 3 | 详情 API 并行线程数 |
| `DOWNLOAD_MAX_WORKERS` | 2 | 全局下载并发数 |
| `PAGE_DOWNLOAD_INTERVAL` | 3 | 多页作品页面间间隔（秒） |
| `MAX_BOOKMARKS_DEFAULT` | 0 | 默认最低收藏数 |
| `AUTO_FOLLOW_INTERVAL` | 600 | 自动关注检查间隔（秒） |
| `AUTO_FOLLOW_DOWNLOAD` | False | 是否自动下载新作品 |
| `PROXY` | `''` | HTTP/SOCKS5 代理 |

---

## 11. 注意事项

1. **搜索排序降级**：`popular_d`（综合排序）需要 Pixiv Premium 账号，无 Premium 时自动静默降级为 `date_d`（最新排序）
2. **OAuth 优先**：推荐配置 `PIXIV_USERNAME`/`PIXIV_PASSWORD` 环境变量使用 OAuth Bearer token，避免手动维护 Cookie
3. **Cookie 过期**：Pixiv PHPSESSID 有有效期，过期后 API 返回 401，需重新获取
3. **磁盘清理**：`scripts/pixiv-cleanup.sh` 清理 30 天前且收藏 < 100 的已下载文件，建议通过 cron 每周执行
4. **限流**：生产环境 Nginx 应配置 `limit_req_zone`（搜索 10r/m，下载 3r/m）
5. **图片 Referer**：请求 Pixiv 图片时必须携带 `Referer: https://www.pixiv.net/`，否则返回 403
