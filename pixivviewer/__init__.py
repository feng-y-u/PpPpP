"""Pixiv Viewer 的应用包。

⚠ `__init__.py` 必须保持轻量：这里**不能** import config / models，也不能创建
Flask app。`tests/conftest.py` 要在 import config **之前**覆盖 `DATABASE_PATH`
（engine 在 models import 时创建），包一旦在 `import pixivviewer.config` 时先执行
会碰 config 的初始化，engine 就会绑到真实库 —— 2026-09-30 清空生产库事故的物理
原因就是这个顺序。装配逻辑放在仓库根的 `app.py`。
"""
