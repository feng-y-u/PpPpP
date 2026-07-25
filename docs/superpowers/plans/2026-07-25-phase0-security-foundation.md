# Phase 0 安全地基 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复审查 P0-1（测试清空生产库）、P0-2（下载/缩略图漏配代理）、P0-4（公网无认证），为后续批次 A/B/C 打好地基。

**Architecture:** ①conftest 在 import models 之前覆盖 DB 路径；②fetcher 提供唯一 Session 工厂 `build_pixiv_session()`，app.py 三处复用；③`before_request` 全局密码认证（`ACCESS_PASSWORD` 留空免认证）+ Session 加固 + ProxyFix + 安全响应头。

**Tech Stack:** Flask 3.1 / SQLAlchemy 2.0 / requests / pytest / werkzeug ProxyFix。

**Spec:** `docs/superpowers/specs/2026-07-25-security-robustness-fixes-design.md`

---

### Task 1: conftest.py 修复 — 测试与生产库隔离（P0-1）

**Files:**
- Modify: `tests/conftest.py`（整个文件头部）
- Test: `tests/test_app.py`（末尾追加测试类）

**背景：** `conftest.py:10` 顶部 `from models import ...` 触发 `models.py:17` 的 `create_engine(f'sqlite:///{DATABASE_PATH}')`，engine 绑定**生产库** `instance/pixiv.db`；`app` fixture 里的 `config.DATABASE_PATH` 覆盖发生在 engine 创建之后，无效。`clean_db` fixture 实际在生产库上全表 DELETE。

- [ ] **Step 1: 写失败测试**

在 `tests/test_app.py` 末尾追加：

```python
class TestDbIsolation:
    def test_engine_uses_temp_db(self):
        """P0-1 回归测试：测试 engine 必须指向临时库，而非生产 instance/pixiv.db。"""
        import models
        assert 'pixiv_test_' in str(models.engine.url)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_app.py::TestDbIsolation -v`
Expected: FAIL — `assert 'pixiv_test_' in 'sqlite:///E:\\pixiv\\instance\\pixiv.db'` 不成立

- [ ] **Step 3: 修复 conftest.py**

把 `tests/conftest.py` 文件头部（第 1-10 行，到 `from models import ...` 为止）替换为：

```python
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── ⚠ 必须在 import models/app 之前覆盖数据库路径 ──
# models.py 在 import 时即 create_engine(DATABASE_PATH)，事后覆盖无效，
# 会导致测试直连并清空生产数据库（2026-07-25 审查 P0-1）。
import config
_TEST_DB_PATH = os.path.join(tempfile.gettempdir(), f'pixiv_test_{os.getpid()}.db')
config.DATABASE_PATH = _TEST_DB_PATH
config.AUTO_FOLLOW_INTERVAL = 0

import pytest

from models import get_session, safe_commit, Illust, BlockedTag, DownloadLog, Collection, CollectionItem
```

同时把 `app` fixture 替换为（清理 WAL/SHM 残留）：

```python
@pytest.fixture(scope='session')
def app():
    from app import app as flask_app
    flask_app.config.update({'TESTING': True})
    yield flask_app
    for suffix in ('', '-wal', '-shm'):
        p = _TEST_DB_PATH + suffix
        if os.path.exists(p):
            os.unlink(p)
```

文件中其余 fixture（`client`、`db`、`clean_db`、`sample_illust`）保持不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_app.py::TestDbIsolation -v`
Expected: PASS

- [ ] **Step 5: 全量回归**

Run: `pytest -v`
Expected: 全绿（所有既有测试通过，因为现在它们真正运行在临时库上）

- [ ] **Step 6: 检查生产库是否已被历史测试污染（手动，向用户报告结果）**

Run:
```powershell
python -c "import sqlite3; conn = sqlite3.connect('instance/pixiv.db'); rows = conn.execute(\"SELECT tag FROM blocked_tags WHERE tag IN ('dupe','delete-me') OR tag LIKE 'csrf-test-%'\").fetchall(); print('垃圾屏蔽标签:', rows if rows else '无'); print('blocked_tags:', conn.execute('SELECT COUNT(*) FROM blocked_tags').fetchone()[0]); print('illusts:', conn.execute('SELECT COUNT(*) FROM illusts').fetchone()[0]); print('collections:', conn.execute('SELECT COUNT(*) FROM collections').fetchone()[0])"
```
Expected: 打印三类计数。若发现垃圾标签，告知用户并提供删除 SQL：`DELETE FROM blocked_tags WHERE tag IN ('dupe','delete-me') OR tag LIKE 'csrf-test-%';`（经用户确认后执行，不自动删）

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py tests/test_app.py
git commit -m "fix(test): P0-1 conftest 在 import models 前覆盖 DB 路径，隔离测试与生产库"
```

---

### Task 2: Session 工厂统一（P0-2）

**Files:**
- Modify: `fetcher.py:186-207`（`_build_session` → `build_pixiv_session` + 别名）
- Modify: `app.py:37`（import）、`app.py:279-284`（`_download_illust`）、`app.py:418-422`（`_fetch_original_urls`）、`app.py:748-755`（`thumb_proxy`）
- Test: `tests/test_app.py`（末尾追加测试类）

**背景：** `_download_illust` 与 `thumb_proxy` 裸建 `requests.Session()`，漏配 `PROXY`。代理用户搜索正常但下载/缩略图全失败。

- [ ] **Step 1: 写失败测试**

在 `tests/test_app.py` 末尾追加：

```python
class TestSessionFactory:
    def _build(self, monkeypatch, proxy=''):
        import fetcher
        monkeypatch.setattr(fetcher, 'PROXY', proxy)
        monkeypatch.setattr(fetcher, '_load_cookie', lambda: None)
        monkeypatch.setattr(fetcher, '_cookie_value', 'test')
        return fetcher.build_pixiv_session()

    def test_proxy_applied(self, monkeypatch):
        s = self._build(monkeypatch, proxy='http://127.0.0.1:7890')
        assert s.proxies == {'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'}

    def test_no_proxy_by_default(self, monkeypatch):
        s = self._build(monkeypatch, proxy='')
        assert s.proxies == {}

    def test_pixiv_headers_present(self, monkeypatch):
        s = self._build(monkeypatch)
        assert s.headers['Referer'].startswith('https://')
        assert 'Mozilla' in s.headers['User-Agent']
        assert 'PHPSESSID=test' in s.headers['Cookie']
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_app.py::TestSessionFactory -v`
Expected: FAIL — `AttributeError: module 'fetcher' has no attribute 'build_pixiv_session'`

- [ ] **Step 3: fetcher.py 改造**

`fetcher.py:186-207` 整段替换为：

```python
def build_pixiv_session() -> requests.Session:
    """构造访问 Pixiv 的 requests.Session（UA/Referer/Cookie/PROXY/SSL_VERIFY/重试 齐全）。

    所有指向 Pixiv 的请求（搜索、详情、下载、缩略图代理）必须经由此工厂，
    禁止裸建 requests.Session()（2026-07-25 审查 P0-2）。
    """
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': f'{PIXIV_BASE_URL}/',
        'Accept-Language': 'ja,zh-CN;q=0.9,zh;q=0.8,en;q=0.7',
    })

    _load_cookie()
    s.headers.update({'Cookie': f'PHPSESSID={_cookie_value}'})
    s.cookies.set('PHPSESSID', _cookie_value, domain=_pixiv_hostname)

    s.verify = SSL_VERIFY

    if PROXY:
        s.proxies = {'https': PROXY, 'http': PROXY}

    adapter = HTTPAdapter()
    retry = Retry(total=1, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503])
    adapter.max_retries = retry
    s.mount('https://', adapter)
    return s


# 向后兼容别名
_build_session = build_pixiv_session
```

- [ ] **Step 4: app.py 三处调用点切换**

① `app.py:37` import 行，把 `_build_session` 替换为 `build_pixiv_session`。

② `_download_illust`（app.py:279-284），把：

```python
            session_obj = requests.Session()
            session_obj.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://www.pixiv.net/',
            })
            session_obj.verify = SSL_VERIFY
```

替换为：

```python
            session_obj = build_pixiv_session()
```

③ `_fetch_original_urls`（app.py:420）：`session = _build_session()` → `session = build_pixiv_session()`。

④ `thumb_proxy`（app.py:749-754），把：

```python
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.pixiv.net/',
        })
        s.verify = SSL_VERIFY
        resp = s.get(url, timeout=(10, 30))
```

替换为：

```python
        resp = build_pixiv_session().get(url, timeout=(10, 30))
```

- [ ] **Step 5: 运行测试确认通过 + 全量回归**

Run: `pytest tests/test_app.py::TestSessionFactory -v; if ($?) { pytest -v }`
Expected: PASS + 全绿

- [ ] **Step 6: Commit**

```bash
git add fetcher.py app.py tests/test_app.py
git commit -m "fix(net): P0-2 统一 Session 工厂，下载器与缩略图代理补齐 PROXY 配置"
```

---

### Task 3: 全局认证 — 配置、钩子、登录页（P0-4 核心）

**Files:**
- Modify: `config.py:78-80` 附近（新增 2 个常量）与 `_key_map`（`config.py:92-104`）
- Modify: `app.py`（import、session 配置、before_request 钩子、登录路由）
- Create: `templates/login.html`
- Modify: `tests/conftest.py`（app fixture 加 `SESSION_COOKIE_SECURE: False`）
- Test: `tests/test_auth.py`（新建）

**关键实现细节（勿遗漏）：**
- `ACCESS_PASSWORD` **留空 = 免认证**，本机用户零影响。
- `/csrf-token` 必须在豁免名单里（登录页/前端取 token 用），它只返回调用者自己 session 的 token，无泄露面。
- 测试环境必须设 `SESSION_COOKIE_SECURE=False`，否则 Secure cookie 不会随 http 测试请求回传，全部 CSRF 测试会挂。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_auth.py`：

```python
import pytest


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    import app as app_module
    app_module._rate_limit_store.clear()
    yield


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setattr('app.ACCESS_PASSWORD', 'test-secret')


def _get_token(client):
    return client.get('/csrf-token').get_json()['token']


class TestAuthRequired:
    def test_page_redirects_to_login(self, client, auth_enabled):
        resp = client.get('/')
        assert resp.status_code == 302
        assert resp.headers['Location'].startswith('/login')

    def test_api_returns_401(self, client, auth_enabled):
        resp = client.get('/api/blocked-tags')
        assert resp.status_code == 401
        assert resp.get_json()['error_code'] == 'AUTH_REQUIRED'

    def test_post_returns_401_not_redirect(self, client, auth_enabled):
        resp = client.post('/api/blocked-tags', json={'tag': 'x'})
        assert resp.status_code == 401

    def test_login_page_exempt(self, client, auth_enabled):
        assert client.get('/login').status_code == 200

    def test_static_exempt(self, client, auth_enabled):
        assert client.get('/static/app.js').status_code == 200

    def test_no_password_means_open_access(self, client):
        # ACCESS_PASSWORD 默认空 → 免认证
        assert client.get('/').status_code == 200


class TestLogin:
    def test_wrong_password_403(self, client, auth_enabled):
        token = _get_token(client)
        resp = client.post('/login', json={'password': 'wrong'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 403

    def test_login_success_then_access(self, client, auth_enabled):
        token = _get_token(client)
        resp = client.post('/login', json={'password': 'test-secret'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert resp.get_json()['ok'] is True
        assert client.get('/').status_code == 200

    def test_open_redirect_blocked(self, client, auth_enabled):
        token = _get_token(client)
        resp = client.post('/login', json={'password': 'test-secret', 'next': '//evil.com'},
                           headers={'X-CSRF-Token': token})
        assert resp.get_json()['next'] == '/'

    def test_rate_limit_after_5_attempts(self, client, auth_enabled):
        for _ in range(5):
            token = _get_token(client)
            client.post('/login', json={'password': 'wrong'},
                        headers={'X-CSRF-Token': token})
        token = _get_token(client)
        resp = client.post('/login', json={'password': 'wrong'},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 429

    def test_login_requires_csrf(self, client, auth_enabled):
        resp = client.post('/login', json={'password': 'test-secret'})
        assert resp.status_code == 403
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL — 404/405 on `/login`，redirect 测试失败（当前无认证钩子）

- [ ] **Step 3: config.py 新增常量**

`config.py:80`（`SETTINGS_PASSWORD = ...` 之后）追加：

```python
# 全局访问密码（留空 = 免认证，本机使用无需设置；公网部署必须设置）
ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', '')

# Session Cookie 仅 HTTPS 传输（公网反代 HTTPS 时应为 True；本地 HTTP 调试可设 false）
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', 'true').lower() != 'false'
```

`config.py` 的 `_key_map`（第 92-104 行）中追加两行：

```python
            'access_password': 'ACCESS_PASSWORD',
            'cookie_secure': 'COOKIE_SECURE',
```

- [ ] **Step 4: app.py — import 与 session 配置**

`app.py:3` 区域 import 追加 `import hmac`；`app.py:22-25` 的 flask import 行追加 `redirect, url_for`；`app.py:28-34` 的 config import 追加 `ACCESS_PASSWORD, COOKIE_SECURE`。

`app.py:61`（`MAX_CONTENT_LENGTH` 之后）追加：

```python
# ── Session 安全加固（公网部署基线）──
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)
```

- [ ] **Step 5: app.py — 认证钩子与登录路由**

在 `_csrf_required` 装饰器定义（app.py:392-400）之后插入：

```python
# ── 全局认证 ──
_AUTH_EXEMPT_PATHS = {'/login', '/favicon.ico', '/csrf-token'}
_AUTH_EXEMPT_PREFIXES = ('/static',)


def _is_authed() -> bool:
    return not ACCESS_PASSWORD or bool(session.get('authed'))


@app.before_request
def _require_login():
    if _is_authed():
        return None
    path = request.path
    if path in _AUTH_EXEMPT_PATHS or any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return None
    if path.startswith('/api/') or path == '/search' or request.method != 'GET':
        return jsonify({'error': '未登录', 'error_code': 'AUTH_REQUIRED'}), 401
    return redirect(url_for('login_page', next=path))


def _safe_next(url: str) -> str:
    """防开放重定向：只允许站内相对路径。"""
    if not url or not url.startswith('/') or url.startswith('//'):
        return '/'
    return url


@app.route('/login', methods=['GET'])
def login_page():
    if _is_authed():
        return redirect(_safe_next(request.args.get('next', '')))
    return render_template('login.html', csrf_token=_get_csrf_token())


@app.route('/login', methods=['POST'])
@_rate_limit(max_attempts=5, window=60)
@_csrf_required
def login_submit():
    body = request.get_json(silent=True) or {}
    password = str(body.get('password', ''))
    if ACCESS_PASSWORD and hmac.compare_digest(password.encode(), ACCESS_PASSWORD.encode()):
        session['authed'] = True
        session.permanent = True
        return jsonify({'ok': True, 'next': _safe_next(str(body.get('next', '')))})
    time.sleep(1)  # 失败延迟，减缓爆破
    return jsonify({'error': '密码错误'}), 403
```

- [ ] **Step 6: 新建 templates/login.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 - PpPpP</title>
<link href="/static/vendor/bootstrap-5.3.3/bootstrap.min.css" rel="stylesheet">
<link href="/static/style.css" rel="stylesheet">
<style>
.unlock-wrap { min-height: calc(100vh - 52px); display: flex; align-items: center; justify-content: center; padding: 2rem; }
.unlock-card { width: 100%; max-width: 360px; text-align: center; }
.unlock-icon { width: 48px; height: 48px; border-radius: 50%; background: var(--accent-subtle); color: var(--accent); display: flex; align-items: center; justify-content: center; margin: 0 auto 1rem; }
.unlock-card h5 { margin-bottom: 0.3rem; }
.unlock-card .text-muted { margin-bottom: 1.5rem; font-size: 0.82rem; }
.unlock-card .form-control { text-align: center; font-size: 1rem; padding: 0.6rem 0.75rem; letter-spacing: 2px; }
.unlock-card .btn { width: 100%; margin-top: 0.75rem; }
.unlock-error { color: var(--danger); font-size: 0.78rem; margin-top: 0.5rem; display: none; }
</style>
</head>
<body>
<div class="full-page">
<div class="unlock-wrap">
  <div class="unlock-card">
    <div class="unlock-icon">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
    </div>
    <h5>需要登录</h5>
    <p class="text-muted">请输入访问密码</p>
    <input id="passwordInput" class="form-control" type="password" placeholder="请输入密码" autofocus>
    <button id="unlockBtn" class="btn btn-primary">登录</button>
    <div id="unlockError" class="unlock-error">密码错误，请重试</div>
  </div>
</div>
<script src="/static/app.js"></script>
<script>
const csrfToken = {{ csrf_token | tojson }};
const nextUrl = {{ (request.args.get('next', '') or '/') | tojson }};

$('#passwordInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('#unlockBtn').click();
});

$('#unlockBtn').addEventListener('click', async () => {
  const pw = $('#passwordInput').value;
  if (!pw) return;
  $('#unlockBtn').disabled = true;
  $('#unlockBtn').textContent = '登录中...';
  $('#unlockError').style.display = 'none';
  try {
    const r = await fetch('/login', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw, next: nextUrl }),
    });
    if (r.ok) {
      const d = await r.json();
      window.location.href = d.next || '/';
    } else if (r.status === 429) {
      $('#unlockError').textContent = '尝试过于频繁，请稍后再试';
      $('#unlockError').style.display = 'block';
    } else {
      $('#unlockError').style.display = 'block';
    }
  } catch {
    $('#unlockError').textContent = '网络错误';
    $('#unlockError').style.display = 'block';
  } finally {
    $('#unlockBtn').disabled = false;
    $('#unlockBtn').textContent = '登录';
  }
});
</script>
</div>
</body>
</html>
```

- [ ] **Step 7: conftest.py 测试环境关闭 Secure Cookie**

`tests/conftest.py` 的 `app` fixture 中：

```python
    flask_app.config.update({'TESTING': True, 'SESSION_COOKIE_SECURE': False})
```

- [ ] **Step 8: 运行测试确认通过 + 全量回归**

Run: `pytest tests/test_auth.py -v; if ($?) { pytest -v }`
Expected: 11 个新测试 PASS + 全绿

- [ ] **Step 9: Commit**

```bash
git add config.py app.py templates/login.html tests/conftest.py tests/test_auth.py
git commit -m "feat(auth): P0-4 全局访问密码认证 + Session 加固（ACCESS_PASSWORD 留空免认证）"
```

---

### Task 4: settings 页兼容 + CSRF 常量时间比较

**Files:**
- Modify: `app.py:392-400`（`_csrf_required`）、`app.py:1309-1338`（settings 三处门禁）、`app.py:1316-1324`（`settings_unlock`）
- Test: `tests/test_auth.py`（末尾追加）

**背景：** 全局密码启用后，设置页复用全局会话，`settings_unlock` 降级直通；旧 `SETTINGS_PASSWORD` 保留兼容。

- [ ] **Step 1: 写失败测试**

`tests/test_auth.py` 末尾追加：

```python
class TestSettingsCompat:
    def test_authed_session_skips_settings_lock(self, client, auth_enabled, monkeypatch):
        monkeypatch.setattr('app.SETTINGS_PASSWORD', 'settings-pw')
        token = _get_token(client)
        client.post('/login', json={'password': 'test-secret'},
                    headers={'X-CSRF-Token': token})
        # 已全局登录 → 设置页直通，不再要求 settings 密码
        assert client.get('/settings').status_code == 200
        assert client.get('/api/settings').status_code == 200

    def test_unlock_passthrough_when_authed(self, client, auth_enabled):
        token = _get_token(client)
        client.post('/login', json={'password': 'test-secret'},
                    headers={'X-CSRF-Token': token})
        token = _get_token(client)
        resp = client.post('/api/settings/unlock', json={},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_auth.py::TestSettingsCompat -v`
Expected: FAIL — `test_authed_session_skips_settings_lock` 返回 403/解锁页

- [ ] **Step 3: 实现**

① `_csrf_required`（app.py:392-400）的比较改常量时间：

```python
def _csrf_required(f: Callable) -> Callable:
    """装饰器：POST 接口要求携带有效的 X-CSRF-Token 请求头。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-CSRF-Token', '')
        expected = session.get('_csrf_token', '')
        if not token or not expected or not hmac.compare_digest(token, expected):
            return jsonify({'error': 'CSRF校验失败'}), 403
        return f(*args, **kwargs)
    return decorated
```

② 在 `_SETTINGS_PATH` 定义之前插入辅助函数：

```python
def _settings_locked() -> bool:
    """设置页门禁：已全局登录则直通；否则按旧 SETTINGS_PASSWORD 流程。"""
    if session.get('authed'):
        return False
    return bool(SETTINGS_PASSWORD) and not session.get('settings_unlocked')
```

③ `settings_page`、`api_settings_get`、`api_settings_post` 中的门禁判断 `if SETTINGS_PASSWORD and not session.get('settings_unlocked'):` 三处统一替换为 `if _settings_locked():`。

④ `settings_unlock` 替换为以下版本（**保留**原有的 `@_rate_limit(max_attempts=5, window=60)` 与 `@_csrf_required` 两个装饰器，以下仅展示签名与函数体）：

```python
def settings_unlock() -> Response:
    if session.get('authed') or not SETTINGS_PASSWORD:
        return jsonify({'ok': True})
    body = request.get_json(silent=True) or {}
    if hmac.compare_digest(str(body.get('password', '')).encode(), SETTINGS_PASSWORD.encode()):
        session['settings_unlocked'] = True
        return jsonify({'ok': True})
    return jsonify({'error': '密码错误'}), 403
```

- [ ] **Step 4: 运行测试确认通过 + 全量回归**

Run: `pytest tests/test_auth.py -v; if ($?) { pytest -v }`
Expected: PASS + 全绿（既有 CSRF 测试不受 compare_digest 影响）

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_auth.py
git commit -m "refactor(auth): settings 页复用全局会话，CSRF/密码比较改 hmac.compare_digest"
```

---

### Task 5: ProxyFix + open-dir 本机限制

**Files:**
- Modify: `app.py:50` 附近（ProxyFix 挂载）、`app.py:1528-1544`（`api_open_dir` 加 IP 限制）
- Test: `tests/test_auth.py`（末尾追加）

**背景：** ProxyFix 让反代后的 `remote_addr` 为真实 IP（限流器恢复有效）；`/api/open-dir` 仅本机可调用。

- [ ] **Step 1: 写失败测试**

`tests/test_auth.py` 末尾追加：

```python
class TestOpenDir:
    def test_non_localhost_forbidden(self, client):
        token = _get_token(client)
        resp = client.post('/api/open-dir', json={'path': '/tmp'},
                           headers={'X-CSRF-Token': token,
                                    'X-Forwarded-For': '8.8.8.8'})
        assert resp.status_code == 403

    def test_localhost_allowed(self, client, monkeypatch, tmp_path):
        import app as app_module
        monkeypatch.setattr(app_module.platform, 'system', lambda: 'Windows')
        called = []
        monkeypatch.setattr(app_module.os, 'startfile',
                            lambda p: called.append(p), raising=False)
        token = _get_token(client)
        resp = client.post('/api/open-dir', json={'path': str(tmp_path)},
                           headers={'X-CSRF-Token': token})
        assert resp.status_code == 200
        assert called == [str(tmp_path)]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_auth.py::TestOpenDir -v`
Expected: FAIL — `test_non_localhost_forbidden` 返回 404/200 而非 403

- [ ] **Step 3: 实现**

① `app.py:50`（`app = Flask(__name__)` 之后）插入：

```python
# 反代后还原真实客户端 IP（限流/open-dir 本机判断依赖）；x_proto 供 HTTPS 判定
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
```

② `api_open_dir` 函数体开头插入：

```python
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'error': '该功能仅本机可用'}), 403
```

> **注意（部署前提，写进 commit message 与 AGENTS.md）**：`ProxyFix(x_for=1)` 无条件信任 `X-Forwarded-For`。公网部署必须确保流量只经反代进入（防火墙/安全组拦住应用端口的直连），否则攻击者可伪造该头绕过限流与 open-dir 本机判断。

- [ ] **Step 4: 运行测试确认通过 + 全量回归**

Run: `pytest tests/test_auth.py -v; if ($?) { pytest -v }`
Expected: PASS + 全绿（无 X-Forwarded-For 的既有测试 remote_addr 仍是 127.0.0.1，不受影响）

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_auth.py
git commit -m "feat(sec): ProxyFix 还原真实 IP，/api/open-dir 限制仅本机可用"
```

---

### Task 6: 安全响应头 + Phase 0 收尾验证

**Files:**
- Modify: `app.py`（`after_request` 钩子）
- Test: `tests/test_auth.py`（末尾追加）

- [ ] **Step 1: 写失败测试**

`tests/test_auth.py` 末尾追加：

```python
class TestSecurityHeaders:
    def test_headers_present(self, client):
        resp = client.get('/')
        assert resp.headers['X-Content-Type-Options'] == 'nosniff'
        assert resp.headers['X-Frame-Options'] == 'DENY'
        assert resp.headers['Referrer-Policy'] == 'no-referrer'
        assert "script-src 'self' 'unsafe-inline'" in resp.headers['Content-Security-Policy']

    def test_csp_allows_self_and_data_images(self, client):
        resp = client.get('/')
        csp = resp.headers['Content-Security-Policy']
        assert "img-src 'self' data:" in csp
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_auth.py::TestSecurityHeaders -v`
Expected: FAIL — `KeyError` 或无响应头

- [ ] **Step 3: 实现**

在 `_require_login` 钩子之后插入：

```python
@app.after_request
def _security_headers(resp: Response) -> Response:
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    # 宽松版 CSP：内联 script 抽离到 static/（批次 C）后收紧为 script-src 'self'
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:"
    )
    return resp
```

- [ ] **Step 4: 运行测试确认通过 + 全量回归**

Run: `pytest tests/test_auth.py -v; if ($?) { pytest -v }`
Expected: PASS + 全绿

- [ ] **Step 5: 手动冒烟（真实环境）**

启动 `flask run`，完成以下走查并逐项确认：
1. 未设置 `ACCESS_PASSWORD` 时：五页面（搜索/图库/批量/下载/设置）正常访问 — 免认证不破坏本机使用
2. 设置 `ACCESS_PASSWORD=test` 重启后：访问 `/` 跳转 `/login`；错误密码 403；正确密码登录后五页面正常；图片缩略图正常显示
3. 设置页：已登录状态进入不再要求 settings 密码
4. `curl -H "X-Forwarded-For: 8.8.8.8" -X POST .../api/open-dir` 返回 403

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_auth.py
git commit -m "feat(sec): 安全响应头（nosniff/DENY/no-referrer/宽松版 CSP）"
```

---

## Phase 0 完成标准（DoD）

- [ ] `pytest -v` 全绿（含新增 ~20 个测试）
- [ ] 生产库污染检查结果已报告用户
- [ ] 手动冒烟 4 项全部通过
- [ ] 6 个 commit 均可独立回退
