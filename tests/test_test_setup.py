import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_TESTS_SCRIPT = PROJECT_ROOT / "scripts" / "run_tests.ps1"
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
