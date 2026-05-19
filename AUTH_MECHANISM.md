# Pixiv Viewer 认证机制说明

> 本项目**未实现 OAuth 2.0 或第三方登录**（微信/Google/GitHub 等）。

---

## 实际使用的认证方式：Cookie 注入

### 原理

Pixiv 官方未提供公开的 OAuth API。本项目直接复用浏览器登录后的会话 Cookie（`PHPSESSID`）来调用 Pixiv 内部 Ajax 接口。

### 完整流程

```
用户浏览器 (已登录 Pixiv)
    ↓ F12 → Application → Cookies → pixiv.net → 复制 PHPSESSID 值
    ↓ 粘贴到 cookies.txt
    ↓ 保存文件
    ↓
fetcher.py 启动/运行中
    ↓ _load_cookie() 读取文件内容
    ↓ _build_session() 将 PHPSESSID 注入到每次请求的 Cookie 头
    ↓
Pixiv API 服务器
    ↓ 校验 PHPSESSID 有效性
    ↓ 返回搜索结果/作品详情
```

### 关键代码

**1. Cookie 文件路径切换**（`config.py:7-10`）

```python
if platform.system() == 'Linux' and os.path.exists('/etc/pixiv-viewer/cookies.txt'):
    COOKIE_PATH = '/etc/pixiv-viewer/cookies.txt'  # 生产环境
else:
    COOKIE_PATH = os.path.join(BASE_DIR, 'cookies.txt')  # 本地开发
```

**2. Cookie 热加载**（`fetcher.py:35-48`）

```python
def _load_cookie():
    global _cookie_mtime, _cookie_value
    mtime = os.path.getmtime(COOKIE_PATH)
    if mtime != _cookie_mtime:         # 仅文件变更时重新读取
        with open(COOKIE_PATH) as f:
            raw = f.read().strip()
        if raw.startswith('PHPSESSID='):
            _cookie_value = raw.split('=', 1)[1]
        else:
            _cookie_value = raw        # 兼容纯裸值格式
        _cookie_mtime = mtime
```

**3. Session 构建**（`fetcher.py:51-75`）

```python
def _build_session() -> requests.Session:
    _load_cookie()
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 ...',
        'Referer': 'https://www.pixiv.net/',
    })
    s.headers.update({'Cookie': f'PHPSESSID={_cookie_value}'})
    s.cookies.set('PHPSESSID', _cookie_value, domain='www.pixiv.net')
```

### Cookie 有效期

- Pixiv PHPSESSID 的有效期不定，过期后搜索接口返回空结果
- 无需重启服务：修改 `cookies.txt` 文件后，下次 API 调用自动加载新值（mtime 检测）
- 过期后只需重新从浏览器复制新 Cookie 覆盖文件即可

---

## 项目中"Token"的实际含义

| 文件位置 | 名称 | 实际用途 | 是否与 OAuth 相关 |
|----------|------|----------|-------------------|
| `app.py:254-268` | `_get_csrf_token()` | CSRF 防护：Flask session 中存储的随机 hex 串，POST 请求需在 `X-CSRF-Token` 头中回传，防止跨站请求伪造 | 否 |
| `app.py:42-50` | `SECRET_KEY` | Flask session 签名密钥，用于加密会话 Cookie | 否 |

---

## 如果未来需要接入 OAuth 2.0 的参考方向

当前项目架构如需添加第三方登录，需要做以下工作（目前均不存在）：

1. **数据库新增表**：`users`（用户）、`oauth_accounts`（第三方账号绑定）
2. **新增路由**：`/auth/login`（登录页）、`/auth/{provider}/authorize`（发起授权）、`/auth/{provider}/callback`（回调处理 token）
3. **新增配置**：`config.py` 中增加各平台的 `CLIENT_ID`、`CLIENT_SECRET`、`REDIRECT_URI`
4. **token 管理**：实现 access_token 存储、refresh_token 自动续期
5. **会话集成**：将 OAuth token 关联到 Flask session，实现用户级隔离

但需注意：Pixiv 本身不提供 OAuth API，即使添加了用户登录系统，**调用 Pixiv 数据仍需依赖当前这套 Cookie 注入方案**。OAuth 实现的仅是本项目的访问控制，不改变与 Pixiv 的对接方式。
