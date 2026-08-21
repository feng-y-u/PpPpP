#!/bin/bash
# Pixiv Viewer 磁盘清理脚本
# 仅清理"已下载原图"：删除 30 天前下载的、收藏数 < 100 的作品本地文件，
# 并把该作品标记为 cleaned。**不参与**预取缓存（SearchCache / prefetch_source）
# 的 10000 条容量控制——那是预取循环内部的事，与本脚本无关。
#
# 用法（手动）:
#   scripts/pixiv-cleanup.sh
# 用环境变量覆盖数据库/下载目录（测试或非常规部署用）:
#   PIXIV_DB=/path/pixiv.db PIXIV_DOWNLOADS=/path/downloads scripts/pixiv-cleanup.sh
# cron 安装:
#   cp scripts/pixiv-cleanup.sh /etc/cron.weekly/pixiv-cleanup && chmod +x /etc/cron.weekly/pixiv-cleanup

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# 数据库与下载目录：环境变量优先，否则基于脚本位置推断
DB="${PIXIV_DB:-$PROJECT_ROOT/instance/pixiv.db}"
DOWNLOADS="${PIXIV_DOWNLOADS:-$PROJECT_ROOT/downloads}"
LOG_TAG="pixiv-cleanup"

# 下载目录的真实路径（规范化，用于删除越界校验）
REAL_DOWNLOADS="$(cd "$DOWNLOADS" 2>/dev/null && pwd -P)" || REAL_DOWNLOADS=""

if [ ! -f "$DB" ]; then
    logger -t "$LOG_TAG" "DB not found at $DB, exiting."
    exit 0
fi
if [ -z "$REAL_DOWNLOADS" ] || [ ! -d "$REAL_DOWNLOADS" ]; then
    logger -t "$LOG_TAG" "Downloads dir not found at $DOWNLOADS, exiting."
    exit 0
fi

logger -t "$LOG_TAG" "Starting cleanup (db=$DB, downloads=$REAL_DOWNLOADS)..."

# 查询待清理的作品
records=$(sqlite3 "$DB" \
  "SELECT pixiv_id, local_paths FROM illusts
   WHERE download_status='done'
     AND bookmark_count < 100
     AND julianday('now') - julianday(created_at) > 30;" 2>/dev/null) || true

if [ -z "$records" ]; then
    logger -t "$LOG_TAG" "No records to clean."
    exit 0
fi

cleaned=0
while IFS='|' read -r pixiv_id paths_json; do
    # pixiv_id 必须为纯数字（防止拼接进 SQL）
    case "$pixiv_id" in
        ''|*[!0-9]*) logger -t "$LOG_TAG" "Skip invalid pixiv_id: $pixiv_id"; continue ;;
    esac

    # 删除文件：仅允许删除下载目录内的文件；路径经 realpath 规范化
    del_ok=$(REAL_DOWNLOADS="$REAL_DOWNLOADS" python3 -c "
import json, os, sys
root = os.environ['REAL_DOWNLOADS']
try:
    paths = json.loads(sys.stdin.read() or '[]')
    if not isinstance(paths, list):
        paths = []
except Exception:
    paths = []
removed = 0
dirs = set()
for p in paths:
    try:
        real = os.path.realpath(p)
        rel = os.path.relpath(real, root)
    except Exception:
        continue
    # 越界保护：必须位于下载目录内
    if rel == '..' or rel.startswith('..' + os.sep) or os.path.isabs(rel):
        continue
    if os.path.isfile(real):
        os.remove(real)
        removed += 1
        dirs.add(os.path.dirname(real))
# 删除因此变空的目录（仍须在下载目录内）
for d in dirs:
    try:
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    except Exception:
        pass
print(removed)
" 2>/dev/null <<< "$paths_json") || del_ok=0

    # 更新作品状态（不再写入不存在的 deleted_records 表）
    sqlite3 "$DB" \
      "UPDATE illusts SET download_status='cleaned', local_paths=NULL WHERE pixiv_id=$pixiv_id;" 2>/dev/null || true

    cleaned=$((cleaned + ${del_ok:-0}))
    logger -t "$LOG_TAG" "Cleaned illust #$pixiv_id (${del_ok:-0} files)"
done <<< "$records"

logger -t "$LOG_TAG" "Cleanup complete ($cleaned files removed)."
