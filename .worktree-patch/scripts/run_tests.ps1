# 在 DSH 沙箱环境中运行 pytest 的包装脚本。
#
# 背景一（跨会话遗留目录）：沙箱为每个进程树分配独立的临时目录访问令牌。
# pytest 默认在 %TEMP%\pytest-of-<user> 下创建 basetemp，先前会话遗留的该
# 目录由旧令牌所有，当前进程无法枚举/删除（WinError 5），导致用 tmp_path
# 的测试在 setup 阶段报错。
#
# 背景二（0o700 ACL）：沙箱把 os.mkdir(path, mode) 的 mode 映射为目录 ACL，
# mode=0o700 生成的目录进程自身反而无法枚举（WinError 5）。借 pytest 插件
# scripts/sandbox_pytest_shim 剥掉 mode；该插件只在沙箱时加载，conftest.py
# 保持干净。
#
# 背景三（临时根可写性）：沙箱只允许写工作区（如 E:\pixiv），LOCALAPPDATA
# 只读。因此沙箱下临时根回退到工作区内 .pytest-tmp；真实环境仍用
# LOCALAPPDATA\pixiv-viewer-test-tmp。任何时候可用 PIXIV_TEST_TMP 显式覆盖。
#
# 注意：TEMP 不能指向 basetemp 本身——pytest 会话结束删除 basetemp 时早于
# session 级 fixture 的 teardown，会撞上仍被 SQLite 占用的测试库（WinError 32）。
#
# 用法：scripts\run_tests.ps1 [pytest 参数…]   （等价 pytest，可传文件/用例名）
$ErrorActionPreference = 'Stop'
$script:root = Split-Path -Parent $PSScriptRoot

# 沙箱探测：进程 TEMP 形如 ...\Temp\dsh-<token>，须在覆盖 TEMP 前判断。
# 用 -like 通配避免正则边缘情况（真实环境 TEMP 不含 "dsh-"）。
$script:isDshSandbox = $false
if ($env:TEMP -like '*dsh-*') {
    $script:isDshSandbox = $true
}

# 计算 pytest 临时根目录（供测试/本脚本复用）：PIXIV_TEST_TMP 优先，
# 沙箱下用工作区内 .pytest-tmp（LOCALAPPDATA 只读），否则 LOCALAPPDATA。
function Resolve-PixivTestTempRoot {
    if ($env:PIXIV_TEST_TMP) {
        return $env:PIXIV_TEST_TMP
    }
    if ($script:isDshSandbox) {
        return Join-Path $script:root '.pytest-tmp'
    }
    $parent = $env:LOCALAPPDATA
    if (-not $parent) { $parent = $env:TEMP }
    if (-not $parent) { $parent = $env:TMP }
    if (-not $parent) { $parent = [IO.Path]::GetTempPath() }
    if (-not $parent) {
        throw 'Unable to determine a temporary directory: PIXIV_TEST_TMP, LOCALAPPDATA, TEMP, TMP, and [IO.Path]::GetTempPath() are empty.'
    }
    return Join-Path $parent 'pixiv-viewer-test-tmp'
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
    $unique = '{0}-{1}' -f $PID, [Guid]::NewGuid().ToString('N')
    return Join-Path $Root ('run-' + $unique)
}

if ($MyInvocation.InvocationName -ne '.') {
    $pluginArgs = @()
    if ($script:isDshSandbox) {
        # 置标记通知沙箱插件（TEMP 随后被覆盖，插件不能靠 TEMP 判断）
        $env:PIXIV_DSH_SANDBOX = '1'
        if ($env:PYTHONPATH) {
            $env:PYTHONPATH = $script:root + '\scripts;' + $env:PYTHONPATH
        } else {
            $env:PYTHONPATH = Join-Path $script:root 'scripts'
        }
        $pluginArgs = @('-p', 'sandbox_pytest_shim')
    }

    $work = Resolve-PixivTestTempRoot
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $run = New-PixivTestRunDirectory -Root $work
    $env:TEMP = $work
    $env:TMP = $work
    $py = Join-Path $script:root 'venv\Scripts\python.exe'
    & $py (Get-PixivPytestArguments -BaseTemp $run -PytestArgs $args) $pluginArgs
    exit $LASTEXITCODE
}
