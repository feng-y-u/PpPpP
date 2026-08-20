from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_conftest_does_not_replace_os_mkdir():
    conftest = (PROJECT_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")

    assert "os.mkdir =" not in conftest


def test_run_tests_uses_overridable_windows_temp_root():
    script = (PROJECT_ROOT / "scripts" / "run_tests.ps1").read_text(encoding="utf-8")

    assert "PIXIV_TEST_TMP" in script
    assert "LOCALAPPDATA" in script
    assert "pixiv-viewer-test-tmp" in script


def test_pytest_configuration_declares_defaults_and_integration_marker():
    pytest_ini = (PROJECT_ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert "testpaths = tests" in pytest_ini
    assert "addopts = -ra" in pytest_ini
    assert "integration" in pytest_ini
