# 验证 scripts/pixiv-cleanup.sh 的可移植清理行为。
#
# 需要：bash、sqlite3、python3（脚本内优先用项目 venv 的 python）。
# 在 Linux 服务器上运行：
#   powershell -ExecutionPolicy Bypass -File tests/test_cleanup_script.ps1
#
# 覆盖点：
#   1. 30 天前、收藏 < 100、状态 done → 文件被删、空目录被删、状态变 cleaned、
#      local_paths 置空
#   2. 越界保护：local_paths 指向 DOWNLOADS 之外的文件 → 不被删除
#   3. 30 天内作品 → 不受影响
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$bash = Get-Command bash -ErrorAction SilentlyContinue
$sqlite3 = Get-Command sqlite3 -ErrorAction SilentlyContinue
if (-not $bash) { Write-Error 'bash 不可用，请在有 bash 的 Linux 环境运行'; exit 1 }

$python = Join-Path $root 'venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python3' }

$tmp = Join-Path ([IO.Path]::GetTempPath()) ('pixiv-cleanup-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$db = Join-Path $tmp 'pixiv.db'
$dl = Join-Path $tmp 'downloads'
New-Item -ItemType Directory -Force -Path $dl | Out-Null

try {
    $env:PIXIV_TEST_DB = $db
    $env:PIXIV_TEST_DL = $dl
    # 建库 + 构造测试数据 + 备份断言辅助
    $setup = @'
import json, os, sqlite3, tempfile
db = os.environ['PIXIV_TEST_DB']; dl = os.environ['PIXIV_TEST_DL']
# 30 天前 + 收藏 10 + done（应清理）
old_dir = os.path.join(dl, '111111'); os.makedirs(old_dir, exist_ok=True)
old_file = os.path.join(old_dir, '111111_p0.jpg'); open(old_file, 'w').write('x')
# 越界文件（在 DOWNLOADS 外，不应被删）
outside_dir = os.path.join(tempfile.gettempdir(), 'pixiv-cleanup-outside-' + os.environ.get('PIXIV_TEST_UID','x'))
os.makedirs(outside_dir, exist_ok=True)
outside_file = os.path.join(outside_dir, '333333_p0.jpg'); open(outside_file, 'w').write('x')
# 30 天内 + 收藏 10 + done（不应清理）
new_dir = os.path.join(dl, '222222'); os.makedirs(new_dir, exist_ok=True)
new_file = os.path.join(new_dir, '222222_p0.jpg'); open(new_file, 'w').write('x')
con = sqlite3.connect(db)
con.execute('CREATE TABLE illusts (pixiv_id INTEGER, title TEXT, download_status TEXT, bookmark_count INTEGER, created_at TEXT, local_paths TEXT)')
con.execute('INSERT INTO illusts VALUES (?,?,?,?,?,?)',
    (111111, 'old', 'done', 10, '2020-01-01 00:00:00', json.dumps([old_file])))
con.execute('INSERT INTO illusts VALUES (?,?,?,?,?,?)',
    (333333, 'outside', 'done', 10, '2020-01-01 00:00:00', json.dumps([outside_file])))
con.execute('INSERT INTO illusts VALUES (?,?,?,?,?,?)',
    (222222, 'new', 'done', 10, '2026-08-20 00:00:00', json.dumps([new_file])))
con.commit(); con.close()
# 输出期望恒定的路径供后续断言
print(outside_file)
'@
    $outsidePath = (& $python -c $setup 2>&1 | Select-Object -Last 1).Trim()

    # 运行清理脚本（环境变量覆盖 DB / DOWNLOADS）
    $env:PIXIV_DB = $db
    $env:PIXIV_DOWNLOADS = $dl
    $env:REMOVE_PIXIV_TEST = '1'  # 供脚本内部清理 outside 目录？不需要
    & $bash (Join-Path $root 'scripts\pixiv-cleanup.sh') | Out-Null

    $verify = @'
import json, os, sqlite3
db = os.environ['PIXIV_TEST_DB']; dl = os.environ['PIXIV_TEST_DL']
con = sqlite3.connect(db)
rows = {r[0]: r for r in con.execute('SELECT pixiv_id, download_status, local_paths, bookmark_count FROM illusts').fetchall()}
con.close()
fails = []
# 1) 过期作品已清理
row = rows[111111]
if row[1] != 'cleaned': fails.append('111111 状态应 cleaned，实际 ' + str(row[1]))
if row[2] is not None: fails.append('111111 local_paths 应 NULL')
if os.path.exists(os.path.join(dl, '111111', '111111_p0.jpg')): fails.append('111111 文件应被删除')
if os.path.isdir(os.path.join(dl, '111111')): fails.append('111111 空目录应被删除')
# 2) 越界文件未被删除
if not os.path.exists(os.environ['PIXIV_TEST_OUTSIDE']): fails.append('越界文件不应被删除')
# 3) 新作品不受影响
row = rows[222222]
if row[1] != 'done': fails.append('222222 状态不应变化')
if not os.path.exists(os.path.join(dl, '222222', '222222_p0.jpg')): fails.append('222222 文件应保留')
if fails:
    print('FAIL: ' + '; '.join(fails)); exit(1)
print('PASS')
'@
    $env:PIXIV_TEST_OUTSIDE = $outsidePath
    $result = & $python -c $verify 2>&1 | Select-Object -Last 1
    Write-Output ("cleanup script test: " + $result.Trim())
    if ($result.Trim() -ne 'PASS') { exit 1 }
}
finally {
    Remove-Item Env:PIXIV_TEST_DB, Env:PIXIV_TEST_DL, Env:PIXIV_TEST_OUTSIDE, Env:PIXIV_DB, Env:PIXIV_DOWNLOADS -ErrorAction SilentlyContinue
}
