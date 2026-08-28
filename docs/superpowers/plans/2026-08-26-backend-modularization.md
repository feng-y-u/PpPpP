# 后端模块化重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `app.py`（2182 行）按域拆分为职责单一的扁平模块（runtime/helpers/middleware/background + 6 个 routes_*.py），`app.py` 保留为组装入口；**零行为变更**，全部 URL/API/CSRF/线程时序/导入约定不变量保持。

**Architecture:** 纯代码搬迁与组织整理。保持项目「无 `__init__.py`、模块直接导入」约定与 `-w 1` 单进程内存状态语义。加载顺序：`helpers`/`runtime`（叶子）→ `middleware` → `background` → `routes_*` → `app.py` 组装。每个 Task 是完整的一次搬迁周期：建模块 → 改 app.py → `py_compile`+pytest 回归 → commit。

**Tech Stack:** Python 3.9+ / Flask 3.1+ / SQLAlchemy 2.0。无新依赖。验证手段：`python -m py_compile` 全部 .py + `powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q`（预期 **208 passed / 4 failed**——4 个失败是 `tests/test_test_setup.py` 沙箱环境问题，与本重构无关的既有状态）+ 最终 gunicorn 启动冒烟。

**设计规格：** `docs/superpowers/specs/2026-08-26-backend-modularization-design.md`（实施前先读一遍）。

**重要纪律（每个 Task 必须遵守）：**
1. **只搬迁，不改逻辑**：函数体逐字搬移，唯一允许的改动是 import 语句、`@app.route` → `@bp.route`、blueprint 注册、线程启动调用位置
2. 搬迁函数时**保留原有注释与文档字符串**
3. 每步完成后：`venv\Scripts\python -m py_compile app.py runtime.py helpers.py middleware.py background.py routes_search.py routes_gallery.py routes_download.py routes_prefetch.py routes_collections.py routes_settings.py`（存在的文件）
4. pytest 回归必须全量跑完（~2-3 分钟），确认无新增失败
5. 404 检查：搬迁后 grep app.py 确认无残留重复定义（`grep -c "def " app.py` 随 Task 递减）

---

### Task 1: runtime.py + helpers.py（状态与工具先行）

**Files:**
- Create: `runtime.py`
- Create: `helpers.py`
- Modify: `app.py`（删除已搬迁定义，改为 import）

- [ ] **Step 1: 创建 `runtime.py`**

从 `app.py` 原样搬移以下**模块级状态定义**（含变量名、默认值、注释）：

```python
# ── 进程内存状态（-w 1 单进程语义，勿多 worker 部署）──
# 所有后台任务与内存状态都依赖单进程常驻，这是单用户自用场景的有意设计。

_scan_cache: dict = {'ts': 0.0, 'data': {}}
_SCAN_CACHE_TTL = 30.0  # 图库目录扫描缓存（秒）：避免每页请求全量重扫磁盘（省 IOPS）

_thumb_sem = threading.Semaphore(6)
_thumb_failed: dict[str, float] = {}
_THUMB_FAIL_COOLDOWN = 30.0

_db_pids_cache: dict = {'ts': 0.0, 'data': set()}
_DB_PIDS_CACHE_TTL = 30.0
```

以及（从 app.py 对应位置原样搬移）：
- `_auto_follow_state`、`_auto_follow_stop`（app.py ~243-249）
- `_prefetch_state`（app.py ~251-257）
- `_queued_downloads: set[int] = set()`、`_download_progress: dict[int, dict] = {}`、`_download_cancellations`（app.py ~664-666，含 `download_executor` 的创建语句与 `DOWNLOAD_MAX_WORKERS` 常量使用）
- `_search_tasks: dict[str, dict] = {}`、`_search_tasks_lock`、`SEARCH_TASK_TTL = 600.0`（app.py ~964-966）
- `_rate_limit_store: dict[str, list[float]] = {}`、`_rate_limit_cleanup_counter = 0`（app.py ~788-789）

文件头 import：`import threading`、`from concurrent.futures import ThreadPoolExecutor`（如 download_executor 用）、`from config import DOWNLOAD_MAX_WORKERS`（或从 config import 原 app.py 的写法——原样保留 app.py 中该 executor 的构造代码）。

**关键**：从 app.py 删除以上全部已搬移定义（行号以实际为准，用 `git diff` 确认删除集与搬移集一一对应）。

- [ ] **Step 2: 创建 `helpers.py`**

从 app.py 原样搬移以下函数（函数体逐字，仅调整 import）：

| 函数 | app.py 原位置 | 说明 |
|------|--------------|------|
| `_get_download_dir` | ~95 | 路径辅助 |
| `_page_sort_key` | ~99 | 排序键 |
| `_scan_local_downloads` | ~120 | 依赖 `runtime._scan_cache`/`_SCAN_CACHE_TTL`（改 import） |
| `_build_orphan_dicts` | ~148 | 依赖 `_page_sort_key` |
| `_extract_ext` | ~781 | — |
| `_original_to_resized` | ~918 | — |
| `_proxy_thumb` | ~927 | — |
| `_fmt_num` | ~943 | — |
| `_safe_int` | ~830 | — |
| `_fetch_original_urls` | ~933 | 依赖 fetcher |
| `_pid_in_clause` | ~559 | — |
| `query_cached_tag` | ~571 | 依赖 models/fetcher/config，注意其内部对 `_prefetch_state`?（若引用则改 `runtime._prefetch_state`） |
| `_delete_illust_files` | ~1783 | 依赖 models |
| `_next_collection_position` | ~2293 | — |
| `_compute_move_position` | ~2397 | — |

文件头 import：`import os`、`import time`、`from models import get_session, Illust, CollectionItem`、`from config import ...`（按各函数实际依赖，从 app.py 原 import 清单挑选）。从 app.py 删除这些函数定义。

- [ ] **Step 3: app.py 接入**

在 app.py 顶部加：

```python
import helpers
import runtime
```

并把 app.py 中所有对已搬移符号的引用改为限定名（`_scan_cache` → `runtime._scan_cache`，`query_cached_tag` → `helpers.query_cached_tag`，`_proxy_thumb` → `helpers._proxy_thumb`，`_download_progress` → `runtime._download_progress`，等等——**凡引用被搬移符号处全部改为 `模块.符号`**）。为减小 diff 也可以选择 `from runtime import ...`/`from helpers import ...` 显式导入（推荐后者，改动面小且 lint 友好——注意：app.py 内函数定义处用的都是全局引用，`from X import Y` 后直接调用原样可用）。

**推荐做法**：在 app.py 顶部加 `from helpers import ...` 与 `from runtime import ...`（列出全部被搬移符号名），这样 app.py 其余代码零改动。

- [ ] **Step 4: 验证 + 回归 + commit**

```bash
venv\Scripts\python -m py_compile runtime.py helpers.py app.py
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q
```
预期：py_compile 通过；pytest **208 passed / 4 failed（仅 test_test_setup.py）**，无新增失败。

```bash
git add runtime.py helpers.py app.py
git commit -m "refactor: 提取 runtime（进程内存状态）与 helpers（工具/查询）模块"
```

---

### Task 2: middleware.py（认证/CSRF/限流/安全头）

**Files:**
- Create: `middleware.py`
- Modify: `app.py`

- [ ] **Step 1: 创建 `middleware.py`**

从 app.py 原样搬移（函数体逐字）：

| 符号 | app.py 原位置 |
|------|--------------|
| `_rate_limit` | ~791 |
| `_get_csrf_token` | ~818（注意依赖 `CURSOR_SECRET`? 或 session 相关——原样保留其内部逻辑） |
| `_get_json_body` | ~824 |
| `_csrf_required` | ~838 |
| `_AUTH_EXEMPT_PATHS` / `_AUTH_EXEMPT_PREFIXES` | ~851-852 |
| `_is_authed` | ~855 |
| `_require_login` | ~859（`@app.before_request` → **改为 `@bp.before_app_request`**，见下） |
| `_safe_next` | ~871 |
| `_security_headers` | ~903（`@app.after_request` → 改为 `@bp.after_app_request`） |

**关键改动（唯一允许的逻辑差异）**：`_require_login` 与 `_security_headers` 的装饰器从 `@app.before_request`/`@app.after_request` 改为 `@bp.before_app_request`/`@bp.after_app_request`——middleware.py 内建 `bp = Blueprint('middleware', __name__)`（不注册路由，仅承载 app 级钩子），app.py 注册该 blueprint 即等价生效。`_rate_limit_store` 引用改为 `runtime._rate_limit_store`（或 from runtime import）。

文件头 import：`import time`、`import functools`、`from flask import Blueprint, request, session, abort, redirect, url_for`、`from flask import Response`（按实际依赖）。

- [ ] **Step 2: app.py 删除已搬移定义并注册**

app.py 中删除上述全部符号定义；顶部加：

```python
from middleware import bp as middleware_bp
```

并在 `app = Flask(__name__)` 创建后、任何路由注册前加：

```python
app.register_blueprint(middleware_bp)
```

（before_app_request/after_app_request 钩子随 blueprint 注册即全局生效——先注册 middleware_bp，保证钩子先于其他路由注册。）

- [ ] **Step 3: 验证 + 回归 + commit**

```bash
venv\Scripts\python -m py_compile middleware.py app.py
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q
```
预期同 Task 1。额外：登录流程冒烟（POST /login 正常、CSRF 403 行为正常）——用 pytest 已覆盖（test_auth.py / test_app.py 相关用例自动守护）。

```bash
git add middleware.py app.py
git commit -m "refactor: 提取 middleware 模块（认证/CSRF/限流/安全头，app 级钩子随 blueprint 注册）"
```

---

### Task 3: background.py（后台线程与下载引擎）

**Files:**
- Create: `background.py`
- Modify: `app.py`

- [ ] **Step 1: 创建 `background.py`**

从 app.py 原样搬移（函数体逐字）：

| 符号 | app.py 原位置 | 说明 |
|------|--------------|------|
| `_auto_follow_worker` | ~259 | 依赖 `runtime._auto_follow_state`/`_auto_follow_stop` |
| `_prefetch_one_tag` | ~333 | 依赖 `runtime._prefetch_state` |
| `_collect_other_tag_pids` | ~396 | |
| `_remove_pids_from_search_caches` | ~410 | |
| `_prefetch_refresh_bookmarks` | ~425 | |
| `_prefetch_capacity_cleanup` | ~473 | 依赖 `runtime._prefetch_state` |
| `_prefetch_loop` | ~506 | |
| `_start_prefetch_thread` | ~534 | 依赖 `runtime._prefetch_state` |
| `_reset_stuck_downloads` | ~181 | 依赖 `runtime._reset_...`? 实际依赖 `runtime._queued_downloads` 等 |
| `_reset_stuck_prefetch` | ~209 | 依赖 `runtime._prefetch_state` |
| `_download_illust` | ~678 | 依赖 `runtime._queued_downloads`/`_download_progress`/`_download_cancellations`/`download_executor`、helpers |
| `_shutdown_background_threads` | ~668 | |

**新建组装函数**（原 app.py 中 auto_follow 线程启动在 ~329-330、prefetch 启动在 ~556；将这些启动语句收进本函数）：

```python
def start_background_threads() -> None:
    """启动所有后台线程（app.py import 时调用，保持单进程常驻语义）。"""
    runtime._auto_follow_thread = threading.Thread(target=_auto_follow_worker, daemon=True)
    runtime._auto_follow_thread.start()
    _start_prefetch_thread()
```

注：`_auto_follow_stop`/`_shutdown_background_threads` 的既有使用保持不变（依原样）。

文件头 import：`import os`、`import time`、`import threading`、`import logging`、`from models import get_session, Illust, CollectionItem, SearchCache, safe_commit`、`from fetcher import ...`（按原函数体依赖）、`import runtime`、`import helpers`、`from config import ...`。

- [ ] **Step 2: app.py 接入**

删掉 app.py 中搬移的定义与两处线程启动语句（auto_follow 启动、`_start_prefetch_thread()` 调用）；顶部 `import background`（或 from background import 所需）；在**模块级、原线程启动位置**（约原 556 行附近，即原 `_start_prefetch_thread()` 调用处）替换为：

```python
background.start_background_threads()
```

并保留原 `_reset_stuck_downloads()` / `_reset_stuck_prefetch()` 调用（改 `background._reset_stuck_downloads()` 等或 from-import）。

- [ ] **Step 3: 验证 + 回归 + commit**

```bash
venv\Scripts\python -m py_compile background.py app.py
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q
```
预期同上。额外验证：`venv\Scripts\python -c "import app; print('threads started')"` 无异常（import 时线程启动时序正常）。

```bash
git add background.py app.py
git commit -m "refactor: 提取 background 模块（后台线程/下载引擎/启动函数）"
```

---

### Task 4: routes_search.py + routes_gallery.py（第一批 Blueprint）

**Files:**
- Create: `routes_search.py`
- Create: `routes_gallery.py`
- Modify: `app.py`

- [ ] **Step 1: 创建 `routes_search.py`**

```python
from flask import Blueprint, request, jsonify, Response

import helpers
import runtime
from middleware import _csrf_required
from fetcher import (PixivAuthError, fetch_following, )

bp = Blueprint('search', __name__)
```

从 app.py 原样搬移以下路由（`@app.route` → `@bp.route`，函数体逐字，其内部引用的被搬移符号改为从 middleware/runtime/helpers/background import）：

| 路由 | app.py 原位置 |
|------|--------------|
| `/search` | ~1031 |
| `/api/search/status/<task_id>` | ~1122 |
| `/api/cache/items` | ~1147 |
| `/api/cache/tags` | ~1193 |
| `/api/cache/items/<int:pixiv_id>/delete` | ~1212 |
| `/api/following` | ~1230 |

依赖搬运：`_submit_search_task`/`_cleanup_search_tasks`/`_search_tasks`（原 ~964-1028）→ **一并搬到本模块**（`_search_tasks`/`_search_tasks_lock`/`SEARCH_TASK_TTL` 已在 runtime.py——本模块 `from runtime import _search_tasks, _search_tasks_lock, SEARCH_TASK_TTL`；`_cleanup_search_tasks` 与 `_submit_search_task` 函数体搬到本文件顶部）。`query_cached_tag` → `from helpers import query_cached_tag`。

- [ ] **Step 2: 创建 `routes_gallery.py`**

```python
from flask import Blueprint, request, jsonify, abort, render_template, send_file, Response

import helpers
import runtime
from middleware import _csrf_required
from models import get_session, Illust, CollectionItem, BlockedTag, safe_commit
from fetcher import _

bp = Blueprint('gallery', __name__)
```

从 app.py 原样搬移（`@app.route` → `@bp.route`）：

| 路由 | app.py 原位置 | 内部需要改引用的符号 |
|------|--------------|----------------------|
| `/thumb/<path:url_b64>` | ~1423 | `runtime._thumb_sem`/`_thumb_failed`/`_THUMB_FAIL_COOLDOWN`、`helpers._extract_ext`、`helpers._get_download_dir`? |
| `/api/image/<int:pixiv_id>/<int:index>` | ~1491 | `helpers._get_download_dir`/`_page_sort_key` |
| `/detail/<int:pixiv_id>` | ~1514 | `helpers._fetch_original_urls`/`_proxy_thumb`/`_original_to_resized`、`models.get_favorite_pids` |
| `/api/detail/<int:pixiv_id>` | ~1577 | 同上 |
| `/gallery` | ~1590 | — |
| `/api/gallery` | ~1601 | `helpers._scan_local_downloads`/`_build_orphan_dicts`、`runtime._db_pids_cache`/`_DB_PIDS_CACHE_TTL`、`models.get_favorite_pids` |
| `/api/gallery/tags` | ~1769 | — |
| `/api/gallery/<int:pixiv_id>` (DELETE) | ~1806 | `helpers._delete_illust_files` |
| `/api/gallery/batch-delete` | ~1820 | 同上 |
| `/api/favorite/<int:pixiv_id>` (GET/POST) | ~2509/2523 | `models.get_favorite_pids`、`helpers._next_collection_position` |
| `/api/open-dir` | ~2489 | — |
| `/api/illust/<int:pixiv_id>/collections` | ~2343 | — |

注意 `/detail`、`/gallery` 两个页面路由在本模块（渲染 HTML），模板渲染调用 `render_template` 的 `csrf_token=_get_csrf_token()` 参数 → `from middleware import _get_csrf_token`。

- [ ] **Step 3: app.py 删除搬移的 18 个路由 + 注册**

删除上述全部路由定义与 `_submit_search_task` 等已搬函数；顶部加：

```python
from routes_search import bp as search_bp
from routes_gallery import bp as gallery_bp
```

`app = Flask(__name__)` 创建后（middleware_bp 注册之后）加：

```python
app.register_blueprint(search_bp)
app.register_blueprint(gallery_bp)
```

- [ ] **Step 4: 验证 + 回归 + commit**

```bash
venv\Scripts\python -m py_compile routes_search.py routes_gallery.py app.py
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q
```

预期：**208 passed / 4 failed**。若出现路由 404 类失败（tests 覆盖搜索/缓存/图库/详情/收藏 API），说明迁移遗漏引用，修正后再跑。额外冒烟：`venv\Scripts\python -c "import app; print([str(r) for r in app.app.url_map.iter_rules()][:5])"` 确认路由注册数量与原一致（可用重构前的数量对比，或直接抽查 `/api/gallery`、`/search` 在 url_map 中）。

```bash
git add routes_search.py routes_gallery.py app.py
git commit -m "refactor: 提取 routes_search/routes_gallery Blueprint（搜索/缓存/图库/详情/收藏/图片）"
```

---

### Task 5: 其余 4 个 Blueprint（download/prefetch/collections/settings）

**Files:**
- Create: `routes_download.py`、`routes_prefetch.py`、`routes_collections.py`、`routes_settings.py`
- Modify: `app.py`

- [ ] **Step 1: 创建 `routes_download.py`**

```python
from flask import Blueprint, request, jsonify, render_template, Response

import helpers
import runtime
import background
from middleware import _csrf_required, _get_json_body

bp = Blueprint('download', __name__)
```

从 app.py 原样搬移（`@app.route` → `@bp.route`）：

| 路由 | 原位置 | 引用调整 |
|------|--------|---------|
| `/download/<int:pixiv_id>` | ~1248 | `runtime._queued_downloads`、`background.download_executor`（executor 在 runtime）、`helpers._fetch_original_urls` |
| `/api/download/batch` | ~1274 | 同上 |
| `_cancel_download_internal` | ~1303（**私有函数随本模块**） | |
| `/download/cancel/<int:pixiv_id>` | ~1339 | |
| `/download/reset/<int:pixiv_id>` | ~1345 | |
| `/download_status/<int:pixiv_id>` | ~1351 | |
| `/api/download/status/batch` | ~1363 | |
| `/download_file/<int:pixiv_id>` | ~1379 | `helpers._get_download_dir`/`_page_sort_key` |
| `/downloads` | ~2013 | 页面路由 |
| `/api/downloads` | ~2018 | `runtime._queued_downloads`/`_download_progress` |

- [ ] **Step 2: 创建 `routes_prefetch.py`**

```python
from flask import Blueprint, request, jsonify, Response

import background
import runtime
from config import SETTINGS_KEYS
from middleware import _csrf_required, _get_json_body

bp = Blueprint('prefetch', __name__)
```

搬移：`_PREFETCH_SETTINGS_KEYS`（~1874）与路由 `/api/prefetch/config`(GET/POST)（~1881/1890）、`/api/prefetch/tags`(GET/POST)（~1922/1935）、`/api/prefetch/tags/<path:tag>`(DELETE)（~1949）、`/api/prefetch/status`（~1986）、`/api/prefetch/refresh`（~1995）。引用调整：`runtime._prefetch_state`、`background._prefetch_one_tag`/`_start_prefetch_thread` 等。

- [ ] **Step 3: 创建 `routes_collections.py`**

```python
from flask import Blueprint, request, jsonify, Response

import helpers
from models import get_session, Collection, CollectionItem, safe_commit
from middleware import _csrf_required, _get_json_body

bp = Blueprint('collections', __name__)
```

搬移全部收藏夹路由（`/api/collections` 及 items/batch/move 系列，~2212-2485）。注：`/api/illust/<pid>/collections` 已在 Task 4 搬到 routes_gallery（保持不动，勿重复）。引用调整：`helpers._next_collection_position`/`_compute_move_position`。

- [ ] **Step 4: 创建 `routes_settings.py`**

```python
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, Response

import runtime
from config import SETTINGS_KEYS
from middleware import (_csrf_required, _get_json_body, _rate_limit, _safe_next, _is_authed)

bp = Blueprint('settings', __name__)
```

搬移：`_SETTINGS_PATH`/`_SETTINGS_DEFAULTS`/`_load_settings`/`_settings_locked`（~2090-2118）与路由 `/login`(GET/POST)（~882/889）、`/settings`（~2120）、`/api/settings/unlock`（~2127）、`/api/settings`(GET/POST)（~2140/2152）、`/api/blocked-tags`（~2053-2085 全部）、`/api/auto-follow/status`（~1853）、`/api/auto-follow/config`（~1857）。引用调整：`runtime._auto_follow_state`/`_auto_follow_stop`、`background._auto_follow_worker`（如需要重启）。**注意**：`_rate_limit` 装饰器用在 `/login` 上（从 middleware import）。

- [ ] **Step 5: app.py 删除已搬移的一切并注册**

app.py 中删除本 Task 涉及的全部路由与私有函数（`_cancel_download_internal` 等）；顶部加 4 个 import，注册处加：

```python
app.register_blueprint(download_bp)
app.register_blueprint(prefetch_bp)
app.register_blueprint(collections_bp)
app.register_blueprint(settings_bp)
```

- [ ] **Step 6: 验证 + 回归 + commit**

```bash
venv\Scripts\python -m py_compile routes_download.py routes_prefetch.py routes_collections.py routes_settings.py app.py
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q
python -c "import app; rules=[str(r) for r in app.app.url_map.iter_rules()]; print(len(rules))"
```
预期：208 passed / 4 failed；路由总数与重构前一致（重构前可先记录 baseline，或抽查关键路由存在）。额外冒烟：`python -c "import app"` 无异常 + gunicorn 启动验证见 Task 6。

```bash
git add routes_download.py routes_prefetch.py routes_collections.py routes_settings.py app.py
git commit -m "refactor: 提取 routes_download/prefetch/collections/settings Blueprint"
```

---

### Task 6: 收尾（app.py 瘦身核对 + 文档 + 总验证）

**Files:**
- Modify: `app.py`
- Create: `docs/architecture.md`

- [ ] **Step 1: app.py 瘦身核对**

`app.py` 现在应只含：import 区（含全部 blueprint/模块导入）、`app = Flask(__name__)` 与配置（`app.config[...]` 等原样保留）、middleware 注册（register_blueprint(middleware_bp) + 其余 6 个 bp）、`background.start_background_threads()` 调用、页面路由 `/`、`/cache`、`/favicon.ico`（如涉及）与残留的极小辅助。

核对：`grep -n "@app.route" app.py` 应只剩 `/`、`/cache`、`/favicon.ico` 等 2-3 条；`Get-Content app.py | Measure-Object -Line` 应 ≤ 250 行。若某私有函数仍留在 app.py 且仅被单一路由使用，评估搬去对应模块（能搬则搬）。

随后清理 Task 1 遗留的死 import（均仅 import、无 app.py 内引用）：`_pid_in_clause`、`_scan_cache`、`_SCAN_CACHE_TTL`（from-import 块）、`urlsafe_b64encode`、`ThreadPoolExecutor`、`DOWNLOAD_MAX_WORKERS`、`AUTO_FOLLOW_INTERVAL`、`AUTO_FOLLOW_DOWNLOAD`、`PREFETCH_INTERVAL`、`PREFETCH_PAGES`、`PREFETCH_MAX_ILLUSTS`、`MEDIUM_IMAGE_SIZE`。

- [ ] **Step 2: 创建 `docs/architecture.md`**

记录模块地图（简要，参照本 plan 的"目标文件结构"表 + 迁移映射），含：每个模块职责一句话、加载顺序、`-w 1` 语义说明、重构日期与 commit 起点。

- [ ] **Step 3: 全量验证**

```bash
venv\Scripts\python -m py_compile app.py runtime.py helpers.py middleware.py background.py routes_search.py routes_gallery.py routes_download.py routes_prefetch.py routes_collections.py routes_settings.py
powershell -ExecutionPolicy Bypass -File scripts\run_tests.ps1 -q
```
预期 208 passed / 4 failed（仅 test_test_setup.py 环境失败）。

**gunicorn 启动冒烟**（前台短暂运行，验证 import 时序与线程启动）：

```bash
venv\Scripts\gunicorn.exe -w 1 --timeout 300 -b 127.0.0.1:8000 app:app
```
（运行 5 秒后用 Ctrl+C 或后台 job 结束后查看输出：无 import 错误、无线程报错。）

- [ ] **Step 4: 浏览器冒烟清单（交人工/或agent逐条）**

1. `/` 搜索页：搜索 + 轮询 + 分页
2. `/gallery` 图库：列表 + 排序 + 删除 + 收藏 + lightbox
3. `/downloads` 下载管理
4. `/cache` 缓存浏览：浏览/翻页/删除
5. `/settings` 设置页：读取/保存 + `/login` 登录流程（若启用密码）
6. 收藏夹全流程：创建/添加/排序/移除
7. `/thumb/<...>` 与 `/api/image/...` 图片加载

- [ ] **Step 5: 最终 commit**

```bash
git add docs/architecture.md app.py
git commit -m "refactor: 收尾 — app.py 组装瘦身核对 + architecture 模块地图文档"
```

---

## Self-Review 备注

- **Spec 覆盖**：11 模块全部有对应 Task（r1/helpers=Task1，middleware=Task2，background=Task3，routes_search/gallery=Task4，routes_download/prefetch/collections/settings=Task5，收尾=Task6）；约束（import 不变/URL 不变/时序不变）在每个 Task 的"引用调整"列落实；验证方案（每步 pytest + 最终 gunicorn 冒烟）贯穿
- **不做项**：无 create_app、无 `__init__.py` 包结构、无 DB/API 改动、无新依赖——计划无对应任务 ✓
- **关键风险**：Task 4/5 搬迁后若 tests 出现 404/500=引用遗漏；计划在 Step 4/6 给出明确处置（修正引用后重跑）。`_rate_limit` 装饰器与 `_csrf_required` 跨模块使用均通过 middleware import 解决。`/api/illust/<pid>/collections` 不重复搬迁（Task 4 已定）
- **类型一致性**：跨模块引用统一用 `模块.符号` 或 from-import 显式导入；blueprint 变量名统一 `bp`；注册顺序统一（middleware 最先）