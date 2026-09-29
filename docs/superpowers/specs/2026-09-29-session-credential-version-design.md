# 会话凭据代次撤销设计

## 状态
已实现并验证：认证测试 45 passed / 1 skipped，设置 API 测试 25 passed / 1 skipped，全量离线测试 550 passed / 2 skipped。

## 背景与问题
网站启用 `ACCESS_PASSWORD` 时，Flask session 只以 `session['authed']` 表示已授权。签名 Cookie 在密码轮换后仍可能保持有效，因此旧授权不能随门禁密码变化而撤销。

## 目标
使当前登录会话绑定到当前 `ACCESS_PASSWORD` 代次；密码变更后，旧或无代次信息的 session 一律不能通过全站门禁或设置页快捷授权判断。

## 设计
- 在 `middleware.py` 以 HMAC-SHA256 派生凭据版本：key 为 `app.app.config['SECRET_KEY']`，message 为 `app.ACCESS_PASSWORD` 的 UTF-8 编码。使用 hmac digest 的十六进制表示作为 session 中的版本值。
- 在 `middleware._is_authed` 内统一判断：若全局口令为空，保持现有开放行为；否则必须同时满足 `session['authed']` 为真且 `session['credential_version']` 等于当前派生版本。缺少版本默认拒绝。
- 登录成功时清理已有 session，再写入 `authed=True`、当前 `credential_version` 和 `permanent=True`。
- `routes_settings._settings_locked` 与 `/api/settings/unlock` 的全局登录短路共用 middleware 的统一认证 helper；设置密码自身的兼容逻辑不变。
- `SECRET_KEY` 不变时，轮换 `ACCESS_PASSWORD` 会改变版本并拒绝旧 session；轮换/删除 `SECRET_KEY` 仍由 Flask 签名校验使旧 Cookie 全局失效。

## 非目标
- 不增加 logout。
- 不引入服务端 session store、用户账号、凭据管理接口或认证系统重构。
- 不改变 Pixiv `PHPSESSID`/Cookie 处理、CSRF 语义和 SETTINGS_PASSWORD 兼容门禁。

## 回归验证
在 `tests/test_auth.py` 覆盖：登录成功与正常访问、匹配版本可访问、修改 ACCESS_PASSWORD 后旧 Cookie 被拒绝、缺少版本的旧 session 被拒绝、设置页与 `/api/settings/unlock` 不接受旧 session。使用当前应用密钥对测试 session 签名，以隔离验证代次判断本身。

## 风险控制
凭据版本必须是 keyed HMAC，不能将口令、SECRET_KEY 或普通口令摘要放入 session。所有 `session['authed']` 读取点都必须使用统一校验，避免旁路。

## 验收
目标测试与相关认证测试通过；`middleware.py` 和 `routes_settings.py` 不再存在绕过统一判定的 `session.get('authed')` 读取；无 logout 与无关功能改动。
