# 在 DSH 沙箱环境中运行 pytest 的包装脚本。
#
# 背景：沙箱为每个进程树分配独立的临时目录访问令牌。pytest 默认在
# %TEMP%\pytest-of-<user> 下创建 basetemp，先前会话遗留的该目录由旧令牌
# 所有，当前进程无法枚举/删除（WinError 5 PermissionError），导致任何用到
# tmp_path 的测试在 setup 阶段全部报错。
#
# 方案：每次运行在 %LOCALAPPDATA%\pixiv-viewer-test-tmp 下生成唯一的
# --basetemp；需要时可用 PIXIV_TEST_TMP 覆盖根目录。
#
# 注意：TEMP 不能指向 basetemp 本身——pytest 在会话结束删除 basetemp 时
# 早于 session 级 fixture 的 teardown，会撞上仍被 SQLite 占用的测试库文件
# （WinError 32）。
#
# 用法：scripts\run_tests.ps1 [pytest 参数…]   （等价 pytest，可传文件/用例名）
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
function Resolve-PixivTestTempRoot {
    $work = $env:PIXIV_TEST_TMP
    if ([string]::IsNullOrWhiteSpace($work)) {
        $parent = $env:LOCALAPPDATA
        if ([string]::IsNullOrWhiteSpace($parent)) {
            $parent = $env:TEMP
        }
        if ([string]::IsNullOrWhiteSpace($parent)) {
            $parent = $env:TMP
        }
        if ([string]::IsNullOrWhiteSpace($parent)) {
            $parent = [IO.Path]::GetTempPath()
        }
        if ([string]::IsNullOrWhiteSpace($parent)) {
            throw 'Unable to determine a temporary directory: PIXIV_TEST_TMP, LOCALAPPDATA, TEMP, TMP, and [IO.Path]::GetTempPath() are empty.'
        }
        $work = Join-Path $parent 'pixiv-viewer-test-tmp'
    }
    return $work
}

function Get-PixivPytestArguments {
    param(
        [Parameter(Mandatory)] [string] $BaseTemp,
        [Parameter(ValueFromRemainingArguments)] [string[]] $PytestArgs
    )
    return @('-m', 'pytest', "--basetemp=$BaseTemp") + $PytestArgs
}

function New-PixivTestRunDirectory {
    param([Parameter(Mandatory)] [string] $Root)
    return Join-Path $Root ('run-{0}-{1}' -f $PID, [Guid]::NewGuid().ToString('N'))
}

if ($MyInvocation.InvocationName -ne '.') {
    $work = Resolve-PixivTestTempRoot
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $run = New-PixivTestRunDirectory -Root $work
    $env:TEMP = $work
    $env:TMP = $work
    $py = Join-Path $root 'venv\Scripts\python.exe'
    & $py (Get-PixivPytestArguments -BaseTemp $run -PytestArgs $args)
    exit $LASTEXITCODE
}
