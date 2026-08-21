# 在 DSH 沙箱环境中运行 pytest 的包装脚本。
#
# 背景：沙箱为每个进程树分配独立的临时目录访问令牌。pytest 默认在
# %TEMP%\pytest-of-<user> 下创建 basetemp，先前会话遗留的该目录由旧令牌
# 所有，当前进程无法枚举/删除（WinError 5 PermissionError），导致任何用到
# tmp_path 的测试在 setup 阶段全部报错。
#
# 方案：每次运行生成唯一的 --basetemp（工作区内 .pytest-tmp\run-<pid>-<时间戳>），
# TEMP/TMP 指到 .pytest-tmp 本身（conftest.py 的测试数据库落在其中，由测试
# 自身清理），全程只接触当前令牌创建/可访问的目录，规避遗留目录。
#
# 沙箱的第二个坑：os.mkdir(path, mode=0o700) 生成的目录会带上"仅属主"ACL，
# 进程自身反而无法枚举（WinError 5）。借 pytest 插件 sandbox_pytest_shim
# 剥掉 mode；该插件只在检测到沙箱时加载，conftest.py 保持干净。
#
# 注意：TEMP 不能指向 basetemp 本身——pytest 在会话结束删除 basetemp 时
# 早于 session 级 fixture 的 teardown，会撞上仍被 SQLite 占用的测试库文件
# （WinError 32）。
#
# 用法：scripts\run_tests.ps1 [pytest 参数…]   （等价 pytest，可传文件/用例名）
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$work = Join-Path $root '.pytest-tmp'
New-Item -ItemType Directory -Force -Path $work | Out-Null
$run = Join-Path $work ('run-{0}-{1}' -f $PID, [DateTime]::Now.ToString('yyyyMMddHHmmss'))

# 探测 DSH 沙箱：沙箱把本进程 TEMP 设为 ...\Temp\dsh-<token>（须在覆盖 TEMP 前判断）；
# 命中后置 PIXIV_DSH_SANDBOX 标记并加载沙箱插件（TEMP 随后被覆盖，插件不能靠 TEMP 判断）
$isDshSandbox = $env:TEMP -match '\\dsh-[^\\]+$'
$pluginArgs = @()
if ($isDshSandbox) {
    $env:PIXIV_DSH_SANDBOX = '1'
    if ($env:PYTHONPATH) {
        $env:PYTHONPATH = $root + '\scripts;' + $env:PYTHONPATH
    } else {
        $env:PYTHONPATH = Join-Path $root 'scripts'
    }
    $pluginArgs = @('-p', 'sandbox_pytest_shim')
}

$env:TEMP = $work
$env:TMP = $work
$py = Join-Path $root 'venv\Scripts\python.exe'
& $py -m pytest $pluginArgs "--basetemp=$run" @args
exit $LASTEXITCODE
