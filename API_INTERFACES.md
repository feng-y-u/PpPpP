# Pixiv Viewer — 前端调用后端 API 接口清单

## 一、搜索页（index.html）

### 1. 搜索作品

| 项 | 说明 |
|------|------|
| 路径 | `GET /search` |
| 参数 | `type` — 搜索类型，`"tag"` / `"user"` / `"following"` |
| | `query` — 搜索关键词（标签词/画师UID），`"following"` 类型时忽略 |
| | `min_bookmarks` — 最低收藏数，默认 0 |
| | `page` — 页码，从 1 开始 |
| | `sort` — 排序，`"date_d"`（最新）/ `"popular_d"`（综合） |
| | `tag_mode` — 多标签组合，`"or"` / `"and"` |
| | `r18_mode` — R18 过滤，`"all"` / `"safe"` |
| 返回 | `{"results": [...], "has_more": true/false}` |
| - results[] | 每个元素：`{pixiv_id, title, user_id, user_name, tags[], page_count, bookmark_count, upload_date, thumb_url, original_urls[], local_paths[], download_status, created_at}` |
| 调用位置 | `index.html` doSearch()、loadMore() |

### 2. 获取关注流

| 项 | 说明 |
|------|------|
| 路径 | `GET /api/following` |
| 参数 | `page` — 页码，从 1 开始 |
| | `r18_mode` — `"all"` / `"safe"` |
| 返回 | `{"results": [...同上搜索返回格式], "has_more": true/false}` |

### 3. 触发下载

| 项 | 说明 |
|------|------|
| 路径 | `POST /download/{pixiv_id}` |
| 请求头 | `X-CSRF-Token: {token}` |
| 参数 | `pixiv_id` — URL 路径参数，作品 ID |
| 返回 | 成功：`{"status": "accepted", "message": "已加入下载队列"}` |
| | 已存在：`{"status": "done", "message": "已下载"}` 或 `{"status": "downloading", "message": "下载中"}` |
| | 404：`{"error": "作品不存在"}` |
| | 400：`{"error": "无原图链接"}` |
| | 403：`{"error": "CSRF校验失败"}` |

### 4. 查询下载状态

| 项 | 说明 |
|------|------|
| 路径 | `GET /download_status/{pixiv_id}` |
| 参数 | `pixiv_id` — URL 路径参数 |
| 返回 | `{"status": "none"/"downloading"/"done"/"failed", "local_paths": ["..."]}` |
| | 404：`{"error": "作品不存在"}` |

### 5. 打包下载文件

| 项 | 说明 |
|------|------|
| 路径 | `GET /download_file/{pixiv_id}` |
| 参数 | `pixiv_id` — URL 路径参数 |
| 返回 | 单文件：直接返回图片文件 |
| | 多文件：返回 `application/zip`（ZIP_STORED 不压缩） |
| | 404：`{"error": "文件未下载"}` 或 `{"error": "文件已丢失，请重新下载"}` |

### 6. 批量触发下载

| 项 | 说明 |
|------|------|
| 路径 | `POST /api/download/batch` |
| 请求头 | `X-CSRF-Token: {token}`, `Content-Type: application/json` |
| 请求体 | `{"ids": [pixiv_id1, pixiv_id2, ...]}` |
| 返回 | `{"accepted": N, "skipped": M, "message": "已加入 N 个下载任务"}` |

### 7. 获取屏蔽标签列表

| 项 | 说明 |
|------|------|
| 路径 | `GET /api/blocked-tags` |
| 参数 | 无 |
| 返回 | `["tag1", "tag2", ...]`（字符串数组） |

### 8. 添加屏蔽标签

| 项 | 说明 |
|------|------|
| 路径 | `POST /api/blocked-tags` |
| 请求头 | `Content-Type: application/json` |
| 请求体 | `{"tag": "标签名"}` |
| 返回 | 成功：`{"status": "added", "tag": "标签名"}` |
| | 409：`{"error": "标签已存在"}` |
| | 400：`{"error": "标签不能为空"}` |

### 9. 删除屏蔽标签

| 项 | 说明 |
|------|------|
| 路径 | `DELETE /api/blocked-tags/{tag}` |
| 参数 | `tag` — URL 路径参数 |
| 返回 | 成功：`{"status": "deleted", "tag": "标签名"}` |
| | 404：`{"error": "标签不存在"}` |

### 10. 缩略图代理

| 项 | 说明 |
|------|------|
| 路径 | `GET /thumb/{url_b64}` |
| 参数 | `url_b64` — 对 Pixiv CDN 缩略图 URL 做 base64(urlsafe) 编码 |
| 返回 | 图片内容，6 小时缓存 + ETag |
| | 400：请求参数错误 |
| | 403：非 `i.pximg.net` 来源 |
| | 502：上游请求失败 |

---

## 二、图库页（gallery.html）

### 11. 获取已下载作品列表

| 项 | 说明 |
|------|------|
| 路径 | `GET /api/gallery` |
| 参数 | `tag`（可选）— 按标签过滤 |
| 返回 | `[{...标准作品格式..., file_size, file_count, local_urls[]}]` |
| - local_urls[] | `["/api/image/{pixiv_id}/0", "/api/image/{pixiv_id}/1", ...]` 本地图片直链 |

### 12. 获取已下载作品的标签

| 项 | 说明 |
|------|------|
| 路径 | `GET /api/gallery/tags` |
| 参数 | 无 |
| 返回 | `["tag1", "tag2", ...]`（排序后的字符串数组） |

### 13. 删除单个已下载作品

| 项 | 说明 |
|------|------|
| 路径 | `DELETE /api/gallery/{pixiv_id}` |
| 请求头 | `X-CSRF-Token: {token}` |
| 参数 | `pixiv_id` — URL 路径参数 |
| 返回 | 成功：`{"status": "deleted", "message": "已删除 N 个文件"}` |
| | 404：`{"error": "作品不存在"}` |

### 14. 批量删除已下载作品

| 项 | 说明 |
|------|------|
| 路径 | `POST /api/gallery/batch-delete` |
| 请求头 | `X-CSRF-Token: {token}`, `Content-Type: application/json` |
| 请求体 | `{"ids": [pixiv_id1, pixiv_id2, ...]}` |
| 返回 | `{"status": "done", "deleted": N, "failed": M, "total_files": N, "message": "已删除 N 个作品 (N 个文件)"}` |

### 15. 获取本地已下载图片

| 项 | 说明 |
|------|------|
| 路径 | `GET /api/image/{pixiv_id}/{index}` |
| 参数 | `pixiv_id` — 作品 ID，`index` — 第几张 (0-based) |
| 返回 | 图片文件 |
| | 404：作品不存在/未下载/index 越界/文件丢失 |

---

## 三、批量下载页（bulk.html）

### 16. 获取 CSRF Token

| 项 | 说明 |
|------|------|
| 路径 | `GET /csrf-token` |
| 参数 | 无 |
| 返回 | `{"token": "16字节hex字符串"}` |

### 17. 启动批量下载任务

| 项 | 说明 |
|------|------|
| 路径 | `POST /api/bulk/start` |
| 请求头 | `X-CSRF-Token: {token}`, `Content-Type: application/json` |
| 请求体 | `{"tag": "标签", "min_bookmarks": 0, "sort_order": "date_d", "max_pages": 10, "r18_mode": "all"}` |
| 返回 | 成功：`{"task_id": "16字节hex"}` |
| | 400：`{"error": "请输入标签"}` |

### 18. 查询批量任务状态

| 项 | 说明 |
|------|------|
| 路径 | `GET /api/bulk/status/{task_id}` |
| 参数 | `task_id` — URL 路径参数 |
| 返回 | `{tag, min_bookmarks, sort, max_pages, current_page, downloaded, failed, status("running"/"stopped"/"done"), r18_mode, log: [["时间ISO", "消息"], ...]}` |
| | 404：`{"error": "任务不存在"}` |

### 19. 停止批量任务

| 项 | 说明 |
|------|------|
| 路径 | `POST /api/bulk/stop/{task_id}` |
| 请求头 | `X-CSRF-Token: {token}` |
| 参数 | `task_id` — URL 路径参数 |
| 返回 | 成功：`{"status": "stopping"}` |
| | 404：`{"error": "任务不存在"}` |

### 20. 获取当前运行的批量任务

| 项 | 说明 |
|------|------|
| 路径 | `GET /api/bulk/running` |
| 参数 | 无 |
| 返回 | 有运行中任务时返回任务完整状态 `{task_id, tag, ...}` |
| | 无运行中任务时返回 `{"task_id": null}` |

---

## 四、下载管理页（downloads.html）

### 21. 获取下载管理概览

| 项 | 说明 |
|------|------|
| 路径 | `GET /api/downloads` |
| 参数 | 无 |
| 返回 | `{active: [...], queued: [...], completed: [...], logs: [...]}` |
| - active[] | 下载中的作品，每个元素额外含 `progress: {current, total}` |
| - queued[] | 队列中的作品 |
| - completed[] | 最近 30 条已完成作品 |
| - logs[] | 最近 50 条下载日志 `{id, pixiv_id, action, message, created_at}` |

### 22. 取消下载

| 项 | 说明 |
|------|------|
| 路径 | `POST /download/cancel/{pixiv_id}` |
| 请求头 | `X-CSRF-Token: {token}` |
| 参数 | `pixiv_id` — URL 路径参数 |
| 返回 | 成功：`{"status": "cancelling", "message": "正在取消..."}` |
| | 404：`{"error": "作品不存在"}` |
| | 400：`{"error": "该作品未在下载中"}` |

### 23. 重置下载

| 项 | 说明 |
|------|------|
| 路径 | `POST /download/reset/{pixiv_id}` |
| 请求头 | `X-CSRF-Token: {token}` |
| 参数 | `pixiv_id` — URL 路径参数 |
| 返回 | 成功：`{"status": "reset", "message": "已重置"}` |
| | 404：`{"error": "作品不存在"}` |
| | 400：`{"error": "该作品未在下载中"}` |

---

## 五、日志页（logs.html）

### 24. 获取下载日志（分页）

| 项 | 说明 |
|------|------|
| 路径 | `GET /api/logs` |
| 参数 | `page` — 页码，从 1 开始 |
| 返回 | `{entries: [{id, pixiv_id, action, message, created_at}, ...], total: N, page: N, per_page: 50}` |

---

## 六、内部管理接口（前端当前未调用）

### 25. 自动关注状态

| 项 | 说明 |
|------|------|
| 路径 | `GET /api/auto-follow/status` |
| 返回 | `{last_check, last_count, interval, auto_download, running}` |

### 26. 配置自动关注

| 项 | 说明 |
|------|------|
| 路径 | `POST /api/auto-follow/config` |
| 请求体 | `{"interval": 秒数, "auto_download": true/false}`（可只传部分） |
| 返回 | 当前完整配置状态 |

---

## 接口统计

| 分类 | 数量 | 说明 |
|------|------|------|
| GET 查询类 | 12 | 搜索、状态查询、数据获取 |
| POST 操作类 | 10 | 启动/停止任务、触发下载、增删配置 |
| DELETE 删除类 | 2 | 删除作品、删除屏蔽标签 |
| 总计 | 24 | 前端实际调用 23 个 + 内部管理 1 个 |
