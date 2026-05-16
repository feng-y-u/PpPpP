# Pixiv 作品检索与展示系统 - 最终项目计划书 v2

## 1. 项目概述

构建一个轻量级 Web 应用，允许用户按标签或画师 UID 搜索 Pixiv 作品，并支持收藏数下限过滤。系统展示作品缩略图、标题、画师、收藏数、标签，并提供按需下载原图至服务器及打包下载至用户本地的功能。整体设计需适配 **4 核 / 4GB / 40GB SSD / 3Mbps** 的低配服务器，严格控制资源与流量消耗。

---

## 2. 技术选型

| 层级 | 组件 | 作用 |
|------|------|------|
| Pixiv 数据引擎 | pixiv-utils (PixivCrawler) + 自行调用详情 API | 搜索发现（用 pixiv-utils）+ 收藏数过滤与原图 URL 提取（自行调用 `/ajax/illust/{id}`） |
| 后端框架 | Python Flask | 路由、模板渲染、API 接口 |
| 数据库 | SQLite + SQLAlchemy（WAL 模式） | 轻量、单文件、免运维；WAL 模式解决多 worker 写冲突 |
| 前端 | Bootstrap 5 + 原生 JavaScript | 响应式界面、异步交互（不使用 jQuery，减少依赖） |
| 图片下载 | requests 流式下载 + Flask send_file | 后台线程异步下载，打包为 zip 流返回 |
| 限流 | Nginx limit_req_zone | 搜索 10r/m、下载 3r/m，在 Nginx 层统一计数，避免多 worker 各自计数 |
| 生产部署 | Gunicorn + Nginx + systemd | 多进程 WSGI 服务，反向代理与静态文件加速 |
| 环境隔离 | Python 3.9+ venv | 本地与服务器环境一致 |

---

## 3. 认证与安全设计（Cookie 管理）

### 3.1 Cookie 获取与存储

用户手动从浏览器复制 PHPSESSID（登录 Pixiv 后，F12 → Application → Cookies → pixiv.net）。

**存储位置（禁止硬编码）：**

- 本地开发：项目根目录 `cookies.txt`（已加入 `.gitignore`）
- 生产服务器：`/etc/pixiv-viewer/cookies.txt`，权限 `600`，属主 `ubuntu`

**Cookie 热加载机制：**

不只在模块 import 时读取一次。改为在 fetcher.py 中封装 `_load_cookie()` 函数，每次访问时检查文件 mtime，仅在文件变更时重新读取，避免 Cookie 过期后必须重启服务。

```python
import os
import time
from pixiv_utils.pixiv_crawler import user_config

_cookie_mtime = 0

def _load_cookie():
    global _cookie_mtime
    path = '/etc/pixiv-viewer/cookies.txt'
    mtime = os.path.getmtime(path)
    if mtime != _cookie_mtime:
        with open(path, 'r') as f:
            user_config.cookie = f.read().strip()
        _cookie_mtime = mtime
```

### 3.2 安全防御措施

- **日志防泄露**：Flask 日志配置过滤请求头，不记录 Cookie
- **Nginx 限制**：
  - 禁止访问 `/cookies.txt`、`.git`、`/downloads/` 目录列表
  - `client_max_body_size 1m;` 限制上传体积
- **请求频率限制**：由 Nginx `limit_req_zone` 统一实现（搜索 10r/m、下载 3r/m），避免 Flask-Limiter 在多 worker 下各自计数失效
- **输入校验**：严格校验标签/UID 格式，防注入
- **CSRF 保护**：`/download/` POST 接口使用 Flask-WTF CSRF token 或简易 token 校验

---

## 4. 功能模块与数据流

### 4.1 核心数据流

```
用户输入搜索条件（标签/画师 UID + 最低收藏数）
    → Flask 调用 fetcher.py
    → 用 pixiv-utils KeywordCrawler / UserCrawler 搜索（url_only=True，仅收集作品 ID）
    → 自行调用 Pixiv 详情 API /ajax/illust/{id} 获取真实 bookmarkCount 和原图 URL
    → 按最低收藏数过滤
    → 作品信息存入 SQLite（包含缩略图 URL 和原图 URL 列表）
    → 前端从数据库读取缩略图 URL 直接展示（不消耗服务器带宽）
    → 用户点击"下载到服务器"
        → Flask 将下载任务提交到后台线程池
        → 前端轮询任务状态（新接口 GET /download_status/<pixiv_id>）
        → 下载完成后更新数据库 local_paths
    → 用户点击"打包下载"
        → Flask 以流式 zip（ZIP_STORED 不压缩，仅打包）返回已下载图片
```

### 4.2 搜索排序说明

搜索排序使用 `popular_d`（热门降序）。**重要前提：此排序需要 Pixiv Premium 账号**。若用户无 Premium 账号，系统自动回退到 `date_d`（最新降序）并在前端提示。

### 4.3 分页机制

- 每次搜索最多抓取 **10 页**（每页 60 件，共 600 个候选）
- **进度记录**：每个搜索条件单独记录当前已抓取页码，存储在 `settings` 表中。key 为搜索条件的 MD5 hash（避免特殊字符问题）
- 下次相同条件的搜索从上次页码继续，避免重复抓取；达到 10 页后回卷到第 1 页（数据库 unique 约束兜底去重）
- 前端提供**"加载更多"按钮**（非无限滚动），每次加载一页新数据

### 4.4 Pixiv API 兼容处理

- 搜索 API `/ajax/search/illustrations` 不返回 `bookmarkCount`，必须再调用详情 API `/ajax/illust/{id}` 获取真实收藏数
- 原图 URL 兼容三种格式：
  - `body.urls.original`
  - `body.metaPages`（多页漫画，每页一张图）
  - `body.metaSinglePage.originalImageUrl`（单页大图）
- 详情 API 超时设置：`timeout=(5, 15)`（连接 5s，读取 15s），失败重试 2 次
- 请求时必须携带 `Referer: https://www.pixiv.net/` 头，否则图片返回 403

---

## 5. 数据库设计（SQLite + SQLAlchemy）

**SQLite 配置：WAL 模式 + busy_timeout**

```python
from sqlalchemy import event
from sqlalchemy.engine import Engine

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.close()
```

WAL 模式允许多个读操作与一个写操作并发，解决 Gunicorn 多 worker 写冲突问题。

### 表结构：illusts

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer (PK) | 自增主键 |
| pixiv_id | Integer (Unique) | Pixiv 作品 ID |
| title | String | 标题 |
| user_id | Integer | 画师 ID |
| user_name | String | 画师名 |
| tags | Text (JSON) | 标签数组字符串 |
| page_count | Integer | 页数 |
| bookmark_count | Integer | 收藏数 |
| upload_date | DateTime | 上传日期 |
| thumb_url | String | 缩略图 URL（Pixiv 官方） |
| original_urls | Text (JSON) | 原图 URL 列表 |
| local_paths | Text (JSON) | 本地文件路径列表（未下载时为 null） |
| download_status | String | 下载状态：null / 'downloading' / 'done' / 'failed' |
| created_at | DateTime | 首次入库时间 |

### 辅助表：settings

| 字段 | 类型 | 说明 |
|------|------|------|
| key | String (PK) | 搜索条件 MD5 hash（如 `md5("tag:初音ミク:min500")`） |
| current_page | Integer | 当前已抓取到的页码（1-based） |

### 辅助表：deleted_records

| 字段 | 类型 | 说明 |
|------|------|------|
| pixiv_id | Integer (PK) | 已删除作品的 Pixiv ID |

---

## 6. API 路由设计（Flask）

| 路由 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 渲染首页 |
| `/search` | GET | 参数：`type`(tag/user), `query`, `min_bookmarks`, `page`。返回 JSON 作品列表 |
| `/download/<pixiv_id>` | POST | 提交后台下载任务，立即返回 `{status, message}` |
| `/download_status/<pixiv_id>` | GET | 查询下载进度，返回 `{status: downloading/done/failed, progress: 2/10}` |
| `/download_file/<pixiv_id>` | GET | 将已下载原图打包为 zip 流返回（ZIP_STORED 不压缩） |
| `/admin/update-cookie` | POST | 更新 Cookie（选配，用于不重启服务更新 Cookie） |

---

## 7. 下载机制（异步设计）

### 7.1 核心问题

同步下载会在请求内阻塞完成，多页漫画可能耗时 90s+，触发浏览器超时、Nginx 60s 默认超时、Gunicorn 30s 默认 worker 超时。

### 7.2 解决方案

- Flask 应用启动时创建一个全局 `ThreadPoolExecutor(max_workers=2)`（限制并发下载任务数，保护服务器资源）
- `/download/<pixiv_id>` POST 将下载任务 `submit()` 到线程池后立即返回
- 前端改为轮询 `/download_status/<pixiv_id>`（每 2 秒），显示进度条
- 下载线程内部使用 `requests` 流式下载，页面间遵守 `time.sleep(3)` 间隔
- Nginx 配置：`proxy_read_timeout 300s;`
- Gunicorn 配置：`--timeout 300`

### 7.3 下载并行控制

- 多页作品在**单个后台线程内**顺序下载，页面间遵守 3s 间隔
- 全局线程池 `max_workers=2` 限制同时下载的作品数
- 每个下载任务使用 `capacity` 方式控制总流量（单次任务上限 2GB）

---

## 8. 前端界面要求

### 8.1 布局与组件

- **顶部导航栏**（深色主题）：项目名称、简短说明
- **搜索栏**：下拉选择（标签/画师 UID）、文本输入框、收藏数下限、搜索按钮。小屏幕垂直堆叠
- **结果展示**：卡片网格，响应式列 `col-lg-3 col-md-4 col-sm-6 col-12`
  - 悬停放大效果 + 阴影
  - 图片上方叠加收藏数徽章、页数徽章
  - 底部标签（小徽章），点击可触发新搜索
  - `<img loading="lazy">` 原生懒加载 + 纯色占位背景
- **"加载更多"按钮**（非无限滚动）
- **空状态提示**：无结果时友好提示

### 8.2 异步交互

- 搜索 → `fetch('/search?...')` → 动态渲染卡片
- 下载按钮状态三态：
  - **就绪**（未下载）："下载到服务器" — 蓝色可点击
  - **下载中**：按钮显示进度 `下载中 2/10...` — 灰色禁用
  - **已下载**："打包下载" — 绿色可点击
- 下载流程：
  1. 点击"下载到服务器" → POST `/download/<pixiv_id>`
  2. 按钮变为"下载中..."并禁用
  3. 前端每 2 秒轮询 `/download_status/<pixiv_id>` 更新进度文字
  4. 完成后按钮变为"打包下载"，颜色变绿
- 下载失败显示 Bootstrap Toast + "重试"按钮

### 8.3 错误与加载状态

- 网络错误 / 5xx → Bootstrap 5 Toast 提示
- 搜索期间 → 旋转加载图标覆盖结果区
- 无 Premium 账号时热门排序降级 → Toast 提示"热门排序需要 Premium，已切换为最新排序"

---

## 9. 资源与流量控制策略

### 9.1 带宽控制

- 缩略图直接走 Pixiv CDN，零服务器带宽消耗
- 原图仅用户主动触发"下载到服务器"时才下载
- 下载任务限制全局并发数（线程池 max_workers=2）

### 9.2 磁盘管理

- 每周 cron 清理脚本（`/etc/cron.weekly/pixiv-cleanup`）：

```bash
#!/bin/bash
# 清理 30 天前下载的、收藏数 < 100 的作品文件
DB="/home/ubuntu/pixiv-viewer/instance/pixiv.db"
DOWNLOADS="/home/ubuntu/pixiv-viewer/downloads"

sqlite3 "$DB" \
  "SELECT local_paths FROM illusts
   WHERE download_status='done'
     AND bookmark_count < 100
     AND julianday('now') - julianday(created_at) > 30;" \
| while read -r paths_json; do
    echo "$paths_json" | python3 -c "
import json, sys, os
try:
    paths = json.loads(sys.stdin.read())
    for p in paths:
        os.remove(p) if os.path.exists(p) else None
    # 删除作品目录
    dirname = os.path.dirname(paths[0]) if paths else None
    os.rmdir(dirname) if dirname and os.path.isdir(dirname) else None
except: pass
" 2>/dev/null
done

# 更新数据库状态
sqlite3 "$DB" \
  "UPDATE illusts SET download_status='failed', local_paths=NULL
   WHERE download_status='done'
     AND bookmark_count < 100
     AND julianday('now') - julianday(created_at) > 30;"
```

### 9.3 限流

在 Nginx 配置中实现（零额外依赖，全局统一计数）：

```nginx
limit_req_zone $binary_remote_addr zone=search:10m rate=10r/m;
limit_req_zone $binary_remote_addr zone=download:10m rate=3r/m;

server {
    location /search {
        limit_req zone=search burst=3 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }
    location ~ ^/download/ {
        limit_req zone=download burst=1 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }
}
```

---

## 10. 项目目录结构

```
pixiv-viewer/
├── app.py                # Flask 主入口
├── models.py             # 数据库模型
├── fetcher.py            # 搜索与解析（pixiv-utils + 自行调用详情 API）
├── config.py             # 配置常量（路径、超时等）
├── requirements.txt
├── .gitignore            # 忽略 venv, cookies.txt, downloads, instance, __pycache__
├── templates/
│   └── index.html        # 前端页面（含所有 HTML/CSS/JS）
├── static/               # （可选）额外 CSS/JS
├── downloads/            # 存放已下载原图（服务器自动创建）
└── instance/             # SQLite 数据库文件存放处
```

---

## 11. 本地开发步骤

### 11.1 环境初始化

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask sqlalchemy pixiv-utils gunicorn requests flask-wtf
pip freeze > requirements.txt
mkdir -p downloads instance
```

### 11.2 配置 Cookie

在项目根目录创建 `cookies.txt`，写入 `PHPSESSID=xxxxxx`，并确认 `.gitignore` 包含此文件。

### 11.3 运行测试

```bash
flask run --debug
```

访问 `http://127.0.0.1:5000` 测试搜索、下载、打包全流程。

---

## 12. 生产环境部署（Ubuntu 22.04）

### 12.1 系统准备

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv nginx git sqlite3
```

### 12.2 上传代码

将项目（排除 venv、downloads、__pycache__）上传至 `/home/ubuntu/pixiv-viewer`。

### 12.3 虚拟环境与依赖

```bash
cd /home/ubuntu/pixiv-viewer
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 12.4 Cookie 安全放置

```bash
sudo mkdir -p /etc/pixiv-viewer
sudo vim /etc/pixiv-viewer/cookies.txt   # 粘贴 Cookie
sudo chmod 600 /etc/pixiv-viewer/cookies.txt
sudo chown ubuntu:ubuntu /etc/pixiv-viewer/cookies.txt
```

### 12.5 测试 Gunicorn

```bash
gunicorn -w 2 --timeout 300 -b 127.0.0.1:8000 app:app
```

### 12.6 配置 Nginx

`/etc/nginx/sites-available/pixiv-viewer`：

```nginx
limit_req_zone $binary_remote_addr zone=search:10m rate=10r/m;
limit_req_zone $binary_remote_addr zone=download:10m rate=3r/m;

server {
    listen 80;
    server_name _;

    client_max_body_size 1m;
    proxy_read_timeout 300s;

    # 禁止访问敏感文件
    location ~ /(cookies\.txt|\.git) { deny all; }

    # 下载目录，禁止目录列表
    location /downloads/ {
        alias /home/ubuntu/pixiv-viewer/downloads/;
        autoindex off;
    }

    # 搜索接口限流
    location /search {
        limit_req zone=search burst=3 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # 下载接口限流
    location ~ ^/download {
        limit_req zone=download burst=1 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

启用配置：

```bash
sudo ln -s /etc/nginx/sites-available/pixiv-viewer /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 12.7 systemd 服务

`/etc/systemd/system/pixiv-viewer.service`：

```ini
[Unit]
Description=Pixiv Viewer Flask App
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/pixiv-viewer
Environment="PATH=/home/ubuntu/pixiv-viewer/venv/bin"
ExecStart=/home/ubuntu/pixiv-viewer/venv/bin/gunicorn -w 2 --timeout 300 -b 127.0.0.1:8000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pixiv-viewer
```

### 12.8 防火墙

```bash
sudo ufw allow 80/tcp
sudo ufw enable   # 若未启用
```

### 12.9 磁盘清理 cron

```bash
sudo cp /home/ubuntu/pixiv-viewer/scripts/pixiv-cleanup.sh /etc/cron.weekly/pixiv-cleanup
sudo chmod +x /etc/cron.weekly/pixiv-cleanup
```

---

## 13. 验证清单

- [ ] Cookie 能正常认证，搜索返回正确结果
- [ ] 按标签/画师 UID 搜索 + 收藏数过滤生效
- [ ] 无 Premium 时热门排序降级为日期排序并提示用户
- [ ] 搜索结果首次抓取后存入数据库，再次搜索直接走缓存
- [ ] 缩略图正常显示，且不消耗服务器带宽
- [ ] "下载到服务器" → 后台异步下载 → 按钮显示进度 → 完成后状态切换
- [ ] 下载过程中刷新页面，状态仍然正确（数据库持久化）
- [ ] 打包下载 zip（ZIP_STORED）文件完整，命名正确
- [ ] 卡片按钮三态切换正确（就绪 → 下载中 → 已下载）
- [ ] "加载更多"功能正常，不重复抓取同一页
- [ ] 网络错误时显示 Toast 提示，可重试
- [ ] Nginx 禁止访问 `/cookies.txt`、禁止目录列表
- [ ] 日志中无 Cookie 明文
- [ ] Nginx 搜索及下载接口频率限制生效（超出后返回 503）
- [ ] SQLite WAL 模式已启用
- [ ] 连续运行 24 小时无内存泄漏或磁盘写满
- [ ] 下载超时不会导致 Gunicorn worker 被 kill（300s timeout 已配置）

---

## v2 修订记录

| 版本 | 日期 | 修改内容 |
|------|------|----------|
| v2 | 2026-05-16 | 1. 新增 Premium 账号前置条件 + 排序降级策略 2. SQLite 改为 WAL 模式解决多 worker 写冲突 3. 下载改为异步（后台线程池 + 前端轮询）+ `/download_status` 路由 4. 修复 `/download_file` 路由缺参数，改为 `/download_file/<pixiv_id>` 5. 限流从 Flask-Limiter 改为 Nginx limit_req_zone 6. Cookie 热加载替代一次性 import 读取 7. zip 打包策略改为 ZIP_STORED 8. 补充 Nginx/Gunicorn 超时配置 9. settings 表 key 改为 MD5 hash 10. 补充 cron 清理脚本完整逻辑 11. 新增 CSRF 保护要求 |
