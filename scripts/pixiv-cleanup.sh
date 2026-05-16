#!/bin/bash
# Pixiv Viewer 磁盘清理脚本
# 删除 30 天前下载的、收藏数 < 100 的作品本地文件及数据库记录
# 用法: sudo cp scripts/pixiv-cleanup.sh /etc/cron.weekly/pixiv-cleanup && sudo chmod +x /etc/cron.weekly/pixiv-cleanup

set -e

DB="/home/ubuntu/pixiv-viewer/instance/pixiv.db"
DOWNLOADS="/home/ubuntu/pixiv-viewer/downloads"
LOG_TAG="pixiv-cleanup"

logger -t "$LOG_TAG" "Starting cleanup..."

if [ ! -f "$DB" ]; then
    logger -t "$LOG_TAG" "DB not found at $DB, exiting."
    exit 0
fi

# 查询待清理的作品
records=$(sqlite3 "$DB" \
  "SELECT pixiv_id, local_paths FROM illusts
   WHERE download_status='done'
     AND bookmark_count < 100
     AND julianday('now') - julianday(created_at) > 30;" 2>/dev/null)

if [ -z "$records" ]; then
    logger -t "$LOG_TAG" "No records to clean."
    exit 0
fi

cleaned=0
echo "$records" | while IFS='|' read -r pixiv_id paths_json; do
    # 删除文件
    echo "$paths_json" | python3 -c "
import json, sys, os
try:
    paths = json.loads(sys.stdin.read())
    for p in paths:
        if os.path.exists(p):
            os.remove(p)
    if paths:
        dirname = os.path.dirname(paths[0])
        if os.path.isdir(dirname) and not os.listdir(dirname):
            os.rmdir(dirname)
except Exception as e:
    print(f'File cleanup error: {e}', file=sys.stderr)
" 2>/dev/null

    # 插入删除记录
    sqlite3 "$DB" \
      "INSERT OR IGNORE INTO deleted_records (pixiv_id) VALUES ($pixiv_id);" 2>/dev/null

    # 更新作品状态
    sqlite3 "$DB" \
      "UPDATE illusts SET download_status='cleaned', local_paths=NULL WHERE pixiv_id=$pixiv_id;" 2>/dev/null

    logger -t "$LOG_TAG" "Cleaned illust #$pixiv_id"
done

logger -t "$LOG_TAG" "Cleanup complete."
