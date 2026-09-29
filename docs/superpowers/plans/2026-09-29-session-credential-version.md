# 会话凭据代次撤销实施计划

> **For agentic workers:** 按任务逐步执行；本次已由用户明确选择直接实施，不另行委派。

**Goal:** 将全站 Flask 登录 session 绑定到 `ACCESS_PASSWORD` 的凭据代次，密码轮换后拒绝旧 session。

**Architecture:** 在 `middleware.py` 提供基于 `SECRET_KEY` 与 `ACCESS_PASSWORD` 的 HMAC 版本派生和统一 session 校验；登录路由写入版本，设置路由调用统一校验。通过 `tests/test_auth.py` 的 Flask client 回归测试验证。保持现有蓝图、CSRF、Cookie 签名与旧 SETTINGS_PASSWORD 流程，不增加 logout。

**Tech Stack:** Python、Flask session、标准库 `hmac` / `hashlib`、pytest。

---

### Task 1: 回归测试（TDD RED）

**Files:**
- Modify: `tests/test_auth.py`

- [x] 增加登录 session 内含 `authed` 和 `credential_version` 的断言，并确认匹配版本可访问。
- [x] 使用 client session_transaction 建立当前 `SECRET_KEY` 签名、但缺少凭据版本或带旧版本的 session；确认受保护 API 返回 401、页面重定向到登录页。
- [x] 验证轮换 `app.ACCESS_PASSWORD` 后既有客户端 Cookie 被拒绝。
- [x] 在 `SETTINGS_PASSWORD` 已设置时验证旧/缺版本 session 不能通过设置门禁；验证 `/api/settings/unlock` 不因旧 `authed` 状态直接成功。
- [x] 旧实现 RED 验证：`test_auth.py` 4 项失败，均是认证结果与预期不符。

### Task 2: 最小实现（GREEN）

**Files:**
- Modify: `middleware.py`
- Modify: `routes_settings.py`

- [x] 在 middleware 中增加 `_credential_version()`，延迟读取 `app.ACCESS_PASSWORD` 与 `app.app.config['SECRET_KEY']`，返回 HMAC-SHA256 十六进制值。
- [x] 在 middleware 中增加 `_session_authed()`，只接受 authed 且凭据代次匹配的 session；空 ACCESS_PASSWORD 保持现有免认证行为。
- [x] 成功登录先 `session.clear()`，写入 `authed=True`、`credential_version`、`permanent=True`。
- [x] `_settings_locked()` 与 `settings_unlock()` 的已登录短路共用统一 helper；SETTINGS_PASSWORD 兼容逻辑保持不变。
- [x] 新增定向认证回归测试通过。


### Task 3: 回归与复核

**Files:**
- Verify: `middleware.py`, `routes_settings.py`, `tests/test_auth.py`
- Update: 本计划与 `progress.md`

- [x] 认证：`test_auth.py` 45 passed, 1 skipped；设置：`test_settings_api.py` 25 passed, 1 skipped。
- [x] 完整离线套件：550 passed, 2 skipped（最终复跑 17.75s）。
- [x] grep 只发现 middleware 中统一读取 `session.get('authed')` 和 login 写入；`routes_settings.py` 无直接旁路。
- [x] `git diff --check` 无报告；审查改动无 logout、无密钥/口令写入 session，且没有触碰无关业务代码。
- [x] 更新 spec/plan 实施与测试结果；最终回复列出代码、测试文件及结果。

**不执行 git commit**，除非用户另外要求。
