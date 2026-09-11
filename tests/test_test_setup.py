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


def _invoke_powershell(command, env):
    if POWERSHELL is None:
        pytest.skip("powershell.exe is required for run_tests.ps1 helper tests")
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip().splitlines()


@pytest.mark.parametrize("variable", ["TEMP", "TMP"])
def test_temp_root_uses_explicit_override(variable):
    env = os.environ.copy()
    override = Path(f"C:/pixiv-test/override-{variable.lower()}")
    env.update(
        {
            "PIXIV_TEST_TMP": str(override),
            "LOCALAPPDATA": "C:/pixiv-test/local",
            variable: "C:/pixiv-test/environment-temp",
        }
    )

    output = _invoke_powershell(
        f". '{RUN_TESTS_SCRIPT}'; Resolve-PixivTestTempRoot",
        env,
    )

    assert Path(output[-1]) == override


def test_temp_root_defaults_to_local_app_data():
    env = os.environ.copy()
    env.pop("PIXIV_TEST_TMP", None)
    env["LOCALAPPDATA"] = "C:/pixiv-test/local"

    output = _invoke_powershell(
        f". '{RUN_TESTS_SCRIPT}'; Resolve-PixivTestTempRoot",
        env,
    )

    assert Path(output[-1]) == Path("C:/pixiv-test/local/pixiv-viewer-test-tmp")


def test_temp_root_falls_back_to_temp_environment_variable():
    env = os.environ.copy()
    env.update(
        {
            "PIXIV_TEST_TMP": "",
            "LOCALAPPDATA": "",
            "TEMP": "C:/pixiv-test/temp",
            "TMP": "C:/pixiv-test/tmp",
        }
    )

    output = _invoke_powershell(
        f". '{RUN_TESTS_SCRIPT}'; Resolve-PixivTestTempRoot",
        env,
    )

    assert Path(output[-1]) == Path("C:/pixiv-test/temp/pixiv-viewer-test-tmp")


def test_temp_root_falls_back_to_tmp_when_temp_is_empty():
    env = os.environ.copy()
    env.update(
        {
            "PIXIV_TEST_TMP": "",
            "LOCALAPPDATA": "",
            "TEMP": "",
            "TMP": "C:/pixiv-test/tmp",
        }
    )

    output = _invoke_powershell(
        f". '{RUN_TESTS_SCRIPT}'; Resolve-PixivTestTempRoot",
        env,
    )

    assert Path(output[-1]) == Path("C:/pixiv-test/tmp/pixiv-viewer-test-tmp")


def test_temp_root_falls_back_when_temp_environment_is_empty():
    if POWERSHELL is None:
        pytest.skip("powershell.exe is required for run_tests.ps1 helper tests")
    env = os.environ.copy()
    for variable in ("PIXIV_TEST_TMP", "LOCALAPPDATA", "TEMP", "TMP"):
        env.pop(variable, None)
    expected_parent = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", "[IO.Path]::GetTempPath()"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()

    output = _invoke_powershell(
        f". '{RUN_TESTS_SCRIPT}'; Resolve-PixivTestTempRoot",
        env,
    )

    assert Path(output[-1]) == Path(expected_parent) / "pixiv-viewer-test-tmp"


def test_run_tests_constructs_basetemp_and_preserves_pytest_arguments():
    env = os.environ.copy()
    base_temp = Path("C:/pixiv-test/root/run-unique")
    output = _invoke_powershell(
        f". '{RUN_TESTS_SCRIPT}'; Get-PixivPytestArguments -BaseTemp '{base_temp}' -PytestArgs @('tests/test_app.py','-k','smoke')",
        env,
    )

    assert output == ["-m", "pytest", f"--basetemp={base_temp}", "tests/test_app.py", "-k", "smoke"]


def test_run_tests_generates_distinct_run_directories():
    env = os.environ.copy()
    output = _invoke_powershell(
        f". '{RUN_TESTS_SCRIPT}'; New-PixivTestRunDirectory 'C:/pixiv-test'; New-PixivTestRunDirectory 'C:/pixiv-test'",
        env,
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


def test_instance_dir_defaults_to_repo_instance(tmp_path, monkeypatch):
    """未设置 PIXIV_INSTANCE_DIR（生产默认）时仍是仓库内的 instance/。"""
    module = _load_config_probe(tmp_path, monkeypatch)

    expected = os.path.join(str(tmp_path), 'instance')
    assert _same_path(module._instance_dir, expected)
    assert _same_path(module.DATABASE_PATH, os.path.join(expected, 'pixiv.db'))
    assert _same_path(module._settings_path, os.path.join(expected, 'settings.json'))
    assert _same_path(module._cursor_secret_path, os.path.join(expected, '.cursor_secret'))
    assert os.path.isfile(module._cursor_secret_path)


def test_instance_dir_env_override_rederives_all_paths(tmp_path, monkeypatch):
    """设置 PIXIV_INSTANCE_DIR 后四条派生路径全部跟着走，且不再创建默认实例目录。"""
    custom = tmp_path / 'custom-instance'
    module = _load_config_probe(tmp_path, monkeypatch, instance_dir=custom)

    assert _same_path(module._instance_dir, str(custom))
    assert _same_path(module.DATABASE_PATH, os.path.join(module._instance_dir, 'pixiv.db'))
    assert _same_path(module._settings_path, os.path.join(module._instance_dir, 'settings.json'))
    assert _same_path(module._cursor_secret_path, os.path.join(module._instance_dir, '.cursor_secret'))
    assert os.path.isfile(module._cursor_secret_path)
    # 覆盖生效时不得再创建默认目录：否则实例数据会分裂在两处
    assert not os.path.exists(os.path.join(str(tmp_path), 'instance'))


def test_instance_dir_override_pointing_at_file_fails_loudly(tmp_path, monkeypatch):
    """覆盖值不可用（指向文件）时必须 import 即失败，而不是静默回落到真实 instance/。"""
    blocker = tmp_path / 'not-a-dir'
    blocker.write_text('not a directory', encoding='utf-8')

    with pytest.raises(OSError):
        _load_config_probe(tmp_path, monkeypatch, instance_dir=blocker)
