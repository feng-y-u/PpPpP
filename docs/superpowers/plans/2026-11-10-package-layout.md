# 重构：代码收进 pixivviewer/ 包 实施计划

- 日期：2026-11-10
- 对应设计：`docs/superpowers/specs/2026-11-10-package-layout-design.md`
- 状态：已实现（2026-11-10，见文末「实施结果」）

## 步骤

### 1. 建包与搬文件（`git mv`，保留历史）

- `pixivviewer/`：`config.py runtime.py helpers.py backups.py models.py`
  `middleware.py pixiv_client.py fetcher.py background.py`
- `pixivviewer/routes/`：`home.py search.py gallery.py download.py prefetch.py settings.py`
- `pixivviewer/templates/`、`pixivviewer/static/`
- 新增轻量 `pixivviewer/__init__.py`、`pixivviewer/routes/__init__.py`。

### 2. 包内 import 改相对导入

- 包根模块：`from config import` → `from .config import`；`import helpers` → `from . import helpers`（函数内延迟 import 同样处理）。
- 路由：`from config/helpers/...` → `from ..config import`；`from routes_home import` → `from .home import`；`import config as config_module` → `from .. import config as config_module`。
- `import app`（延迟读 seam）**保持原样** —— 它指向根入口模块。
- `backups.py` 的 `from migrations.runner import` 保持绝对导入。

### 3. 根 `app.py` 变薄

- 保留原有装配顺序与注释（配置 → 中间件 → 蓝图 → init_db → 启动线程）。
- 所有 from-import 改成 `from pixivviewer...`，符号集不变（测试 seam 契约）。
- `Flask('pixivviewer')`，模板/静态从包内解析。
- 保留 `threading` / `time` / `fetcher` 等模块属性（`app.threading`、`app.fetcher` 被测试补丁引用）。

### 4. 路径推导上移

- `config.BASE_DIR` 改成包的上一级；`runtime.thumb_redirect_state_path()` 兜底同上。

### 5. tests

- `conftest.py`：防线一改查 `'pixivviewer.models'`；`import config` → `from pixivviewer import config`；`from models import` → `from pixivviewer.models import`；fixture 里 `import models` → `from pixivviewer import models`。
- 19 个测试文件的模块 import 改包路径（保留原有别名，测试正文不动）。
- `test_helpers.py` 的 AST 叶子用例：模块名集合与路径按包调整。
- `test_app.py` 的 CSRF 矩阵 glob：`pixivviewer/routes/*.py`。
- 4 处 `pathlib.Path('static/...')` → `pixivviewer/static/...`。

### 6. scripts / 文档

- `scripts/check_tls.py`、`pixiv_capture.py`、`_inspect_db.py` 的 import 改包路径。
- `AGENTS.md` / `docs/architecture.md` 的架构表与路径引用。

### 7. 验证

- `python -m compileall` 兜底语法。
- 全量 pytest 0 failed。
- 临时实例冒烟：`import app` 能起、`/` 与 `/static/app.js` 200。

## 风险

| 风险 | 处置 |
|---|---|
| conftest 之前 import 到 models（测试库防线） | `__init__.py` 保持轻量；防线一改查包名 |
| 测试 seam 丢失（`app.X` 不存在） | 根 app.py 的 from-import 逐个保留；grep `app\.` 对账 |
| `instance/` 写进包内 | BASE_DIR 上移一级并用测试断言路径 |
| 相对导入层级写错 | compileall + 全量测试覆盖所有模块导入路径 |
| Flask 找不到模板/静态 | `Flask('pixivviewer')` + 冒烟测 `/` 与 `/static/app.js` |

## 实施结果

按计划全部落地。与计划的差异只有一处：`pixivviewer/app.py` 没有创建 —— 装配逻辑留在**根** `app.py`，用 `Flask('pixivviewer')` 显式指定包为根路径（比另建一个 app 模块少一层间接，WSGI 入口也更直观）。

验证：

- 全量 **460 passed / 2 skipped / 0 failed**（19s）。
- `compileall` 通过；临时实例冒烟：`/`、`/static/app.js`、`/api/home`、`/explore` 全 200。
- 细节见设计文档「验证结果」（logger 名、字符串 patch 目标、CSRF glob、config 探针、static 路径共 5 类连带修改）。
