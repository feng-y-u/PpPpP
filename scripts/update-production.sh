#!/usr/bin/env bash
# Pixiv Viewer 生产环境更新脚本（适配 /home/azure/pixiv-viewer + user-site gunicorn）
# 用法：sudo bash update-production.sh
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/home/azure/pixiv-viewer}"
APP_USER="${APP_USER:-azure}"
SERVICE="${SERVICE:-pixiv-viewer}"
BRANCH="${BRANCH:-main}"
DB_PATH="${DB_PATH:-$APP_DIR/instance/pixiv.db}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/instance/backups}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/csrf-token}"
KEEP_BACKUPS="${KEEP_BACKUPS:-10}"
PYTHON="${PYTHON:-/usr/bin/python3}"
# 现有服务使用 ~/.local 的 gunicorn + 用户级依赖；Ubuntu 24.04 的 PEP 668
# 会拒绝 --user 安装。这里只写用户目录，不改系统 Python 包。
PIP_ARGS=(--user --break-system-packages)

log()  { printf '[update] %s\n' "$*"; }
fail() { printf '[update] ERROR: %s\n' "$*" >&2; exit 1; }

trap 'rc=$?; printf "[update] 更新中断，服务可能已停止；检查 systemctl status %s\n" "$SERVICE" >&2; printf "[update] 数据库备份：%s\n" "${backup:-无}" >&2; exit "$rc"' ERR

[[ "$(id -u)" -eq 0 ]] || fail "请用 root 运行：sudo bash $0"
[[ -d "$APP_DIR/.git" ]] || fail "找不到 git 仓库：$APP_DIR"
[[ -x "$PYTHON" ]] || fail "找不到 Python：$PYTHON"
[[ "$KEEP_BACKUPS" =~ ^[0-9]+$ ]] || fail "KEEP_BACKUPS 必须是整数"

for cmd in git curl systemctl journalctl flock getent; do
    command -v "$cmd" >/dev/null 2>&1 || fail "缺少命令：$cmd"
done

APP_HOME="${APP_HOME:-$(getent passwd "$APP_USER" | cut -d: -f6)}"
[[ -n "$APP_HOME" ]] || fail "找不到用户 $APP_USER 的 HOME"

systemctl cat "$SERVICE" >/dev/null 2>&1 || fail "找不到 systemd 服务：$SERVICE"

if command -v runuser >/dev/null 2>&1; then
    as_app() { runuser -u "$APP_USER" -- env HOME="$APP_HOME" "$@"; }
else
    as_app() { sudo -u "$APP_USER" -- env HOME="$APP_HOME" "$@"; }
fi

# 同一时间只允许一个更新进程。
exec 9>/run/lock/pixiv-viewer-update.lock
flock -n 9 || fail "另一个更新进程正在运行"

# 只检查已跟踪文件；允许把本脚本放在仓库里（untracked 不阻断更新）。
if ! as_app git -C "$APP_DIR" diff --quiet \
   || ! as_app git -C "$APP_DIR" diff --cached --quiet; then
    fail "$APP_DIR 有未提交的已跟踪改动，请先处理：git -C $APP_DIR status"
fi

# 更新前先 fetch + pip dry-run；失败时服务还在跑。
log "预检远端分支"
as_app env GIT_TERMINAL_PROMPT=0 git -C "$APP_DIR" fetch --prune origin "$BRANCH"
if ! as_app git -C "$APP_DIR" merge-base --is-ancestor HEAD "origin/$BRANCH"; then
    fail "本地 HEAD 与 origin/$BRANCH 分叉，请先处理分支关系"
fi

log "预检依赖安装"
as_app env PIP_NO_INPUT=1 "$PYTHON" -m pip install "${PIP_ARGS[@]}" --dry-run \
    -r "$APP_DIR/requirements-lock.txt"

[[ -f "$DB_PATH" ]] || fail "找不到数据库：$DB_PATH"
if [[ ! -d "$BACKUP_DIR" ]]; then
    as_app mkdir -p "$BACKUP_DIR"
fi
as_app test -w "$BACKUP_DIR" || fail "服务用户 $APP_USER 对 $BACKUP_DIR 没有写权限"

if systemctl is-active --quiet "$SERVICE"; then
    log "停止服务：$SERVICE"
    systemctl stop "$SERVICE"
fi

stamp="$(date +%Y%m%dT%H%M%S)"
backup="$BACKUP_DIR/$(basename "$DB_PATH").pre-update-$stamp.bak"
log "备份数据库：$backup"

as_app "$PYTHON" - "$DB_PATH" "$backup" <<'PY'
import sqlite3
import sys

source_path, backup_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(source_path)
dst = sqlite3.connect(backup_path)
try:
    with dst:
        src.backup(dst)
    check = dst.execute('PRAGMA integrity_check').fetchone()[0]
finally:
    src.close()
    dst.close()

print(f'integrity_check = {check}')
raise SystemExit(0 if check == 'ok' else 1)
PY

log "拉取 origin/$BRANCH"
as_app env GIT_TERMINAL_PROMPT=0 git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
next_commit="$(as_app git -C "$APP_DIR" rev-parse HEAD)"
log "代码更新到：$next_commit"

log "安装锁定依赖"
as_app env PIP_NO_INPUT=1 "$PYTHON" -m pip install "${PIP_ARGS[@]}" \
    -r "$APP_DIR/requirements-lock.txt"

log "启动服务：$SERVICE"
if ! systemctl start "$SERVICE"; then
    systemctl status "$SERVICE" --no-pager || true
    journalctl -u "$SERVICE" -n 100 --no-pager || true
    fail "systemctl start 失败；备份：$backup"
fi

healthy=0
for _ in $(seq 1 60); do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
        healthy=1
        break
    fi
    sleep 1
done

if [[ "$healthy" -ne 1 ]]; then
    systemctl status "$SERVICE" --no-pager || true
    journalctl -u "$SERVICE" -n 120 --no-pager || true
    fail "健康检查失败：$HEALTH_URL；备份：$backup"
fi

if [[ "$KEEP_BACKUPS" -gt 0 ]]; then
    ls -1t "$BACKUP_DIR/$(basename "$DB_PATH").pre-update-"*.bak 2>/dev/null \
        | tail -n +"$((KEEP_BACKUPS + 1))" \
        | xargs -r rm -f || true
fi

log "更新完成：$(as_app git -C "$APP_DIR" rev-parse HEAD)"
log "服务状态：$(systemctl is-active "$SERVICE")"
log "数据库备份：$backup"
