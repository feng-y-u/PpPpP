import importlib.util
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_TESTS_SCRIPT = PROJECT_ROOT / "scripts" / "run_tests.ps1"
CONFIG_PATH = PROJECT_ROOT / "config.py"
POWERSHELL = shutil.which("powershell.exe")


def test_conftest_does_not_replace_os_mkdir():
    conftest = (PROJECT_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")

    assert "os.mkdir =" not in conftest


_PRELUDE = f". '{RUN_TESTS_SCRIPT}'"


def _invoke_powershell(statements, env=None):
    """在**健康环境**里执行一段 PS 语句并返回 stdout 行。

    要控制的 `$env:` 一律由语句**在会话内部**赋值，不再经进程 env 传入。原因：Windows
    PowerShell 启动自身就要用 TEMP/TMP，一旦这两个变量指向不存在的目录（本文件早先的
    写法就是 `C:/pixiv-test/environment-temp`），它会直接以非 0 退出 —— 于是"验证 TEMP
    回退"的用例**在断言之前就死在子进程启动上**，长期挂着 4 条红（审计报告里记作
    "预先存在"）。改成会话内赋值后，子进程永远拿到健康 env，被断言的才是
    `Resolve-PixivTestTempRoot` 的真实逻辑。
    """
    if POWERSHELL is None:
        pytest.skip("powershell.exe is required for run_tests.ps1 helper tests")
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            statements,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"powershell 退出码 {result.returncode}（stderr: {result.stderr.strip()[:400]}）"
        )
    return result.stdout.strip().splitlines()


def _force_non_sandbox_branch() -> str:
    """把 `isDshSandbox` 钉成 false：本组用例验证的就是 LOCALAPPDATA/TEMP 回退链。

    正常经 `run_tests.ps1` 运行时子进程 TEMP 已是工作区临时根、该变量本就是 false；
    但直接跑 pytest（TEMP 形如 `...\\Temp\\dsh-<token>`）时它会是 true，回退链就会被
    "沙箱优先"分支短路掉 —— 显式赋值让这组断言与环境无关。
    """
    return "$script:isDshSandbox = $false\n"


def _resolve_temp_root(prelude: str) -> Path:
    """dot-source 脚本 → 会话内改 env → 返回 `Resolve-PixivTestTempRoot` 的结果。"""
    output = _invoke_powershell(f"{_PRELUDE}\n{_force_non_sandbox_branch()}{prelude}\nResolve-PixivTestTempRoot")
    assert output, 'Resolve-PixivTestTempRoot 必须有输出'
    return Path(output[-1])


def test_temp_root_fallback_chain_is_ordered():
    """`Resolve-PixivTestTempRoot` 的回退链，逐级钉住。

    顺序：PIXIV_TEST_TMP → `LOCALAPPDATA/pixiv-viewer-test-tmp` → TEMP/… → TMP/… →
    `[IO.Path]::GetTempPath()`（最后一级由下面那条用例单独覆盖，因为它的期望值必须在
    **同一次会话里**取）。

    每一级的期望值不同、prelude 也不同，但"设置哪几个 $env: → 得到哪个根"是同一条
    不变式，故合为一条用例：每个分支的 prelude 与断言逐条保留，并各带自己的失败消息，
    这样"哪一级坏了"仍然一眼可见（原先 5 条用例，其中 2 条是 TEMP/TMP 参数矩阵）。
    """
    # ① PIXIV_TEST_TMP 优先：TEMP 有值也一样
    root = _resolve_temp_root(
        "$env:PIXIV_TEST_TMP = 'C:/pixiv-test/override-temp'\n"
        "$env:LOCALAPPDATA = 'C:/pixiv-test/local'\n"
        "$env:TEMP = 'C:/pixiv-test/environment-temp'"
    )
    assert root == Path("C:/pixiv-test/override-temp"), f'PIXIV_TEST_TMP 应优先于 TEMP：{root}'

    # ② PIXIV_TEST_TMP 优先：TMP 有值也一样
    root = _resolve_temp_root(
        "$env:PIXIV_TEST_TMP = 'C:/pixiv-test/override-tmp'\n"
        "$env:LOCALAPPDATA = 'C:/pixiv-test/local'\n"
        "$env:TMP = 'C:/pixiv-test/environment-temp'"
    )
    assert root == Path("C:/pixiv-test/override-tmp"), f'PIXIV_TEST_TMP 应优先于 TMP：{root}'

    # ③ 没有 PIXIV_TEST_TMP → 在 LOCALAPPDATA 下派生
    root = _resolve_temp_root(
        "$env:PIXIV_TEST_TMP = $null\n"
        "$env:LOCALAPPDATA = 'C:/pixiv-test/local'"
    )
    assert root == Path("C:/pixiv-test/local/pixiv-viewer-test-tmp"), f'应回退到 LOCALAPPDATA：{root}'

    # ④ LOCALAPPDATA 也没有 → TEMP（TMP 有值也不抢）
    root = _resolve_temp_root(
        "$env:PIXIV_TEST_TMP = ''\n"
        "$env:LOCALAPPDATA = ''\n"
        "$env:TEMP = 'C:/pixiv-test/temp'\n"
        "$env:TMP = 'C:/pixiv-test/tmp'"
    )
    assert root == Path("C:/pixiv-test/temp/pixiv-viewer-test-tmp"), f'应回退到 TEMP：{root}'

    # ⑤ TEMP 为空 → 才轮到 TMP
    root = _resolve_temp_root(
        "$env:PIXIV_TEST_TMP = ''\n"
        "$env:LOCALAPPDATA = ''\n"
        "$env:TEMP = ''\n"
        "$env:TMP = 'C:/pixiv-test/tmp'"
    )
    assert root == Path("C:/pixiv-test/tmp/pixiv-viewer-test-tmp"), f'TEMP 为空时应回退到 TMP：{root}'


def test_temp_root_falls_back_when_temp_environment_is_empty():
    """连 TEMP/TMP 都没有时回退到 `[IO.Path]::GetTempPath()`。

    期望值在同一次会话里取（取完再调被测函数）：这样"清空环境变量"这件事不会顺手把
    取期望值的那次调用也弄坏。
    """
    output = _invoke_powershell(
        f"{_PRELUDE}\n"
        f"{_force_non_sandbox_branch()}"
        "Remove-Item Env:PIXIV_TEST_TMP,Env:LOCALAPPDATA,Env:TEMP,Env:TMP "
        "-ErrorAction SilentlyContinue\n"
        "$expected = [IO.Path]::GetTempPath()\n"
        "$actual = Resolve-PixivTestTempRoot\n"
        "Write-Output $expected\n"
        "Write-Output $actual"
    )

    assert len(output) == 2, f'期望值与实际值各一行，实际输出：{output}'
    assert Path(output[-1]) == Path(output[0]) / "pixiv-viewer-test-tmp"


def test_run_tests_constructs_basetemp_and_preserves_pytest_arguments():
    base_temp = Path("C:/pixiv-test/root/run-unique")
    output = _invoke_powershell(
        f"{_PRELUDE}\n"
        f"Get-PixivPytestArguments -BaseTemp '{base_temp}' "
        "-PytestArgs @('tests/test_app.py','-k','smoke')"
    )

    assert output == ["-m", "pytest", f"--basetemp={base_temp}", "tests/test_app.py", "-k", "smoke"]


def test_run_tests_generates_distinct_run_directories():
    output = _invoke_powershell(
        f"{_PRELUDE}\n"
        "New-PixivTestRunDirectory 'C:/pixiv-test'\n"
        "New-PixivTestRunDirectory 'C:/pixiv-test'"
    )

    assert len(output) == 2
    assert output[0] != output[1]


def test_pytest_configuration_declares_defaults_and_integration_marker():
    pytest_ini = (PROJECT_ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert "testpaths = tests" in pytest_ini
    assert "addopts = -ra" in pytest_ini
    assert "integration" in pytest_ini


def test_live_pixiv_fixture_is_opt_in_and_skips_without_cookie():
    conftest = (PROJECT_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")

    assert "def live_pixiv_required" in conftest
    assert "pytest.skip" in conftest
    assert "config.COOKIE_PATH" in conftest


def test_dependency_files_separate_runtime_development_and_locking():
    runtime = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    development = (PROJECT_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    locked = (PROJECT_ROOT / "requirements-lock.txt").read_text(encoding="utf-8-sig")

    assert "pytest" not in runtime.lower()
    assert "Flask>=3.1,<3.2" in runtime
    assert "SQLAlchemy>=2.0,<2.1" in runtime
    assert "-r requirements.txt" in development
    assert "pytest>=8,<10" in development
    assert "Flask==" in locked
    assert "SQLAlchemy==" in locked
    assert "requests==" in locked


# ── 实例目录隔离（PIXIV_INSTANCE_DIR 是唯一入口）──


def _same_path(left, right):
    return (os.path.normcase(os.path.realpath(str(left)))
            == os.path.normcase(os.path.realpath(str(right))))


def _load_config_probe(target_dir, monkeypatch, instance_dir=None):
    """把 config.py 复制到 target_dir 后独立 exec，返回该模块。

    BASE_DIR 随 __file__ 落在 target_dir，于是「默认实例目录」与「PIXIV_INSTANCE_DIR
    覆盖」两条分支都能在临时目录里验证 —— 直接 exec 仓库里的 config.py 验证默认分支
    必然要读写真实 instance/。
    """
    probe = target_dir / "config_probe.py"
    shutil.copy(CONFIG_PATH, probe)
    if instance_dir is None:
        monkeypatch.delenv("PIXIV_INSTANCE_DIR", raising=False)
    else:
        monkeypatch.setenv("PIXIV_INSTANCE_DIR", str(instance_dir))
    spec = importlib.util.spec_from_file_location("config_probe", probe)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_conftest_redirects_instance_dir_before_importing_config():
    conftest = (PROJECT_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")

    # 事后覆盖无效：config.py 在 import 时就算好了实例目录/密钥/DB 路径，
    # app.py 还会往里写 .secret_key；且必须强制覆盖，不能 setdefault。
    assert conftest.index("os.environ['PIXIV_INSTANCE_DIR']") < conftest.index("\nimport config")
    assert "setdefault('PIXIV_INSTANCE_DIR'" not in conftest


def test_instance_dir_isolated_in_tests():
    """测试进程派生出的每一个实例路径都必须落在临时目录。"""
    import config
    import routes_gallery
    import routes_settings
    import runtime

    temp_root = os.path.normcase(os.path.realpath(tempfile.gettempdir())) + os.sep
    derived = {
        'config._instance_dir': config._instance_dir,
        'config._cursor_secret_path': config._cursor_secret_path,
        'config.DATABASE_PATH': config.DATABASE_PATH,
        'config._settings_path': config._settings_path,
        'routes_settings._SETTINGS_PATH': routes_settings._SETTINGS_PATH,
        'routes_gallery.CACHE_DIR': routes_gallery.CACHE_DIR,
        'runtime.thumb_redirect_state_path()': runtime.thumb_redirect_state_path(),
    }
    for name, path in derived.items():
        assert os.path.normcase(os.path.realpath(path)).startswith(temp_root), f'{name} 未隔离：{path}'

    assert _same_path(config._instance_dir, os.environ['PIXIV_INSTANCE_DIR'])
    assert os.path.isfile(config._cursor_secret_path)
    # 同一来源：settings.json / image_cache 都必须在实例目录下
    for path in (config._settings_path, routes_gallery.CACHE_DIR):
        assert _same_path(os.path.dirname(path), config._instance_dir)


def test_instance_dir_derives_all_paths_from_one_source(tmp_path, monkeypatch):
    """`_instance_dir` 是**单点派生**：四条路径必须同源。

    两个输入：未设置 PIXIV_INSTANCE_DIR（生产默认 = 仓库内 `instance/`）与设置覆盖值。
    断言形状完全同构（实例目录 / DB / settings / cursor secret / 秘密文件确实生成），
    只有输入与期望值不同，故合为一条用例。

    两段各用独立临时目录：默认分支会就地创建 `instance/`，共用目录会让覆盖分支的
    "不得再创建默认目录" 断言假失败。
    """
    # ① 未设置 PIXIV_INSTANCE_DIR（生产默认）时仍是仓库内的 instance/
    default_base = tmp_path / 'default'
    default_base.mkdir()
    module = _load_config_probe(default_base, monkeypatch)

    expected = os.path.join(str(default_base), 'instance')
    assert _same_path(module._instance_dir, expected)
    assert _same_path(module.DATABASE_PATH, os.path.join(expected, 'pixiv.db'))
    assert _same_path(module._settings_path, os.path.join(expected, 'settings.json'))
    assert _same_path(module._cursor_secret_path, os.path.join(expected, '.cursor_secret'))
    assert os.path.isfile(module._cursor_secret_path)

    # ② 设置 PIXIV_INSTANCE_DIR 后四条派生路径全部跟着走，且不再创建默认实例目录
    override_base = tmp_path / 'override'
    override_base.mkdir()
    custom = override_base / 'custom-instance'
    module = _load_config_probe(override_base, monkeypatch, instance_dir=custom)

    assert _same_path(module._instance_dir, str(custom))
    assert _same_path(module.DATABASE_PATH, os.path.join(module._instance_dir, 'pixiv.db'))
    assert _same_path(module._settings_path, os.path.join(module._instance_dir, 'settings.json'))
    assert _same_path(module._cursor_secret_path, os.path.join(module._instance_dir, '.cursor_secret'))
    assert os.path.isfile(module._cursor_secret_path)
    # 覆盖生效时不得再创建默认目录：否则实例数据会分裂在两处
    assert not os.path.exists(os.path.join(str(override_base), 'instance'))


def test_instance_dir_override_pointing_at_file_fails_loudly(tmp_path, monkeypatch):
    """覆盖值不可用（指向文件）时必须 import 即失败，而不是静默回落到真实 instance/。"""
    blocker = tmp_path / 'not-a-dir'
    blocker.write_text('not a directory', encoding='utf-8')

    with pytest.raises(OSError):
        _load_config_probe(tmp_path, monkeypatch, instance_dir=blocker)


class TestCorruptSettingsOnLoad:
    """settings.json 损坏时的启动行为（审计 S16）。

    回退默认值的行为不变，但必须留下副本：否则设置页下次保存会用默认值**整体覆盖**
    那个文件，用户那份（可能只是少个括号）的配置就永久没了。
    """

    @staticmethod
    def _write_corrupt(instance_dir):
        instance_dir.mkdir(parents=True, exist_ok=True)
        path = instance_dir / 'settings.json'
        path.write_text('{"per_page": 24, ', encoding='utf-8')   # 截断的 JSON
        return path

    def test_corrupt_settings_backed_up_on_load(self, tmp_path, monkeypatch):
        instance = tmp_path / 'inst'
        corrupted = self._write_corrupt(instance)

        module = _load_config_probe(tmp_path, monkeypatch, instance_dir=instance)

        backup = instance / 'settings.json.corrupt.bak'
        assert backup.is_file(), '损坏文件必须留一份副本'
        assert backup.read_bytes() == corrupted.read_bytes(), '副本要与现场逐字节一致'
        # 回退行为不变：拿不到覆盖值就用默认值
        assert module.SETTINGS_KEYS['per_page'][1] == 60
        assert module.PER_PAGE == 60

    def test_valid_settings_creates_no_backup(self, tmp_path, monkeypatch):
        """没坏就不该产生 .corrupt.bak —— 否则这个文件会变成噪声。"""
        instance = tmp_path / 'inst'
        instance.mkdir(parents=True, exist_ok=True)
        (instance / 'settings.json').write_text('{"per_page": 33}', encoding='utf-8')

        module = _load_config_probe(tmp_path, monkeypatch, instance_dir=instance)

        assert module.PER_PAGE == 33
        assert not (instance / 'settings.json.corrupt.bak').exists()

    def test_existing_backup_is_not_overwritten(self, tmp_path, monkeypatch):
        """第二次仍损坏时不得覆盖第一份副本：首次现场才有诊断价值。"""
        instance = tmp_path / 'inst'
        corrupted = self._write_corrupt(instance)
        backup = instance / 'settings.json.corrupt.bak'
        backup.write_text('first crime scene', encoding='utf-8')

        _load_config_probe(tmp_path, monkeypatch, instance_dir=instance)

        assert backup.read_text(encoding='utf-8') == 'first crime scene'
        assert corrupted.read_text(encoding='utf-8') == '{"per_page": 24, ', '原始损坏文件也不该被改动'
