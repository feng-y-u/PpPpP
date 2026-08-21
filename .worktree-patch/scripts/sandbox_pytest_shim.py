"""DSH 沙箱专用 pytest 插件（由 scripts/run_tests.ps1 在沙箱环境动态加载）。

背景：DSH 沙箱把 os.mkdir(path, mode) 的 mode 参数映射为目录 ACL
（0o700 → 仅属主 ACL，进程自身反而被拒枚举/删除），导致 pytest 用
mode=0o700 创建的 basetemp / tmp_path 目录在 sessionfinish 清理时报
PermissionError (WinError 5)。Windows 上 mode 本身被忽略（POSIX 位），
因此这里把 mode 剥掉。

该插件只在沙箱环境由 run_tests.ps1 显式加载（-p sandbox_pytest_shim），
conftest.py 保持干净，真实环境不会生效。
"""
import os

import pytest


def _is_dsh_sandbox() -> bool:
    # run_tests.ps1 探测到沙箱后显式置位；不能在插件里用 tempfile.gettempdir()
    # 判断——此时 TEMP 已被 run_tests.ps1 覆盖为工作区目录，不再是 dsh- 前缀。
    return os.environ.get('PIXIV_DSH_SANDBOX') == '1'


def pytest_configure(config):
    if not _is_dsh_sandbox():
        return
    orig_mkdir = os.mkdir

    def _mkdir_sans_mode(path, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            return orig_mkdir(path)
        return orig_mkdir(path, dir_fd=dir_fd)

    os.mkdir = _mkdir_sans_mode
