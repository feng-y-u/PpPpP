# 重构：代码收进 pixivviewer/ 包（根目录去平铺） 设计

- 日期：2026-11-10
- 状态：已实现（2026-11-10，见文末「验证结果」）
- 相关代码：根目录 16 个 `*.py`、`templates/`、`static/`、`tests/`、`scripts/`、`migrations/`、`app.py`、`AGENTS.md`

## 背景与问题

根目录平铺 16 个 Python 模块 + 2 个资源目录。单人项目、模块少时这样最省事，但打开仓库第一屏全是 `.py`：路由、ORM、Pixiv 适配、后台线程、下载引擎平级排在一起，读代码时没有层次。用户明确希望"不要这么平铺"。

## 目标

1. 业务模块、模板、静态资源收进 `pixivviewer/` 包；根目录只保留**入口** `app.py` 与工程文件。
2. 部署命令不变：`gunicorn app:app` / `flask run` 照旧。
3. `app` 命名空间的测试补丁 seam 原样保留（`app.ACCESS_PASSWORD`、`app.get_session`、`app.time` 等）。
4. conftest 的三道防线语义不变（只是模块名变成 `pixivviewer.models`）。
5. `helpers` 仍是叶子：模块级不 import `models`。
6. 全量测试 0 failed。

## 目标结构

```text
app.py                     # 薄入口：建 Flask、注册蓝图、启动线程 + 测试补丁 seam
pixivviewer/
  __init__.py              # 有意保持轻量：不建 app、不读配置
  config.py runtime.py helpers.py backups.py models.py
  middleware.py pixiv_client.py fetcher.py background.py
  routes/
    __init__.py
    home.py search.py gallery.py download.py prefetch.py settings.py
  templates/               # 9 个 Jinja2 模板
  static/                  # app.js / page-*.js / style.css / vendor
tests/ scripts/ migrations/ docs/  + requirements*.txt / AGENTS.md / .gitignore
```

## 关键决策

### 为什么 `pixivviewer/__init__.py` 必须轻量

`tests/conftest.py` 在 import `config` **之前**要覆盖 `DATABASE_PATH`（engine 在 `models` import 时创建）。如果包 `__init__` 里有任何会 import `config`/`models` 的语句，`import pixivviewer.config` 会先执行 `__init__`，engine 就在测试库路径生效之前绑死了 —— 这正是 2026-09-30 清空生产库事故的物理原因。所以 `__init__.py` 只放 docstring 与版本号；装配逻辑留在根 `app.py`。

### 为什么装配留在根 `app.py` 而不是 `pixivviewer/app.py`

- 根 `app.py` 就是 WSGI 入口（`gunicorn app:app`），顺理成章。
- 测试补丁 seam 在根 `app` 模块上，路由里的延迟 `import app` 不需要改。
- `Flask('pixivviewer')` 让 Flask 以包目录为根，模板/静态自动从 `pixivviewer/templates`、`pixivviewer/static` 解析，不用手写相对路径。

### 包内 import 用相对导入

包内模块一律 `from .config import ...`，路由用 `from ..helpers import ...`。这样模块在包内怎么搬都不会断。唯一的绝对导入是 `from migrations.runner import ...`（`migrations/` 仍是根目录包，与 `pixivviewer` 平级，靠仓库根在 `sys.path` 上）。

### 路径推导必须上移一级

`config.BASE_DIR = dirname(dirname(__file__))`：`instance/`、`.env`、`cookies.txt`、`downloads/` 都从 BASE_DIR 派生，留在包内会把运行数据写进 `pixivviewer/`。`runtime.thumb_redirect_state_path()` 的兜底同理。

### 不做的

- 不引入 `pyproject.toml` / `pip install -e .`（本项目不是被 import 的库，部署就是 `gunicorn app:app`）。
- 不改任何行为、接口、SQL、前端逻辑：这次只搬位置。
- 不把 `tests/`、`scripts/`、`migrations/` 收进包：它们不是运行时模块，平铺在根目录反而好找。

## 验证

- 全量 460 条测试 0 failed（conftest / seam / AST 叶子用例都要过）。
- `python -c "import app"` 能在临时实例目录下起应用（不碰生产库）。
- 手工：`gunicorn app:app` 的命令不变；首页 `/static/*.js` 能取到。

## 验证结果

- 2026-11-10 落地。全量 **460 passed / 2 skipped / 0 failed**（19s），与重构前完全一致。
- `python -m compileall pixivviewer app.py` 通过。
- 临时实例冒烟（`PIXIV_INSTANCE_DIR` 指到工作区临时目录、后台线程 interval 置 0）：
  `GET /` 200 且含 `homeGrid`、`GET /static/app.js` 200（`text/javascript`）、
  `GET /api/home` 200、`GET /explore` 200 —— `Flask('pixivviewer')` 的模板/静态解析成立。
- 重构中修正的连带项：
  - 日志 logger 名随模块变成 `pixivviewer.pixiv_client` / `pixivviewer.background`，3 处 caplog 断言同步；
  - 5 处字符串 patch 目标（`'fetcher.x'` / `'pixiv_client.x'` / `'routes_gallery.x'`）改成包路径；
  - CSRF 矩阵的静态对账 glob 从根目录 `routes_*.py` 改为 `pixivviewer/routes/*.py`（否则会静默扫到空集）；
  - `test_test_setup.py` 的 config 探针改放 `<target>/pixivviewer/`，因为 `BASE_DIR` 现在是包的上一级；
  - 4 处 `pathlib.Path('static/...')` 改为 `pixivviewer/static/...`。
- 未做：在真实部署机上重启验证（需要用户执行；`gunicorn app:app` 命令本身未变）。
