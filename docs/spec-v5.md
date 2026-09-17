# spec-v5：多用户与权限（小团队内部）

> 目标规模：单机部署、团队内部少量用户，SQLite 存储。向后兼容：`AUTH_ENABLED=0`（默认）时行为与 V4 完全一致。

## 认证（server/auth.py，新）

- 注册 `POST /api/auth/register`：用户名 + 密码（密码哈希用标准库 `hashlib.scrypt`，免第三方依赖）；**首个注册用户自动成为 admin**，其余为 user。
- 登录 `POST /api/auth/login`：校验通过签发 HttpOnly cookie token——`uid.exp.hmac`（HMAC-SHA256，密钥 `SECRET_KEY` 环境变量；未设则启动时随机生成并告警，重启后所有 token 失效）。
- 登出 `POST /api/auth/logout`：清 cookie。
- 当前用户 `GET /api/me`：返回 `{id, username, role}`。
- 开关：`AUTH_ENABLED=1` 开启认证；未开启时所有请求视为内置用户 `local`（`id=None`，超管语义），与 V4 行为一致。

## 存储（SQLite，标准库 sqlite3，vulnhound.db）

- `users(id INTEGER PK, username TEXT UNIQUE, password_hash TEXT, role TEXT, created_at TEXT)`
- `session_owners(session_id TEXT PK, owner_id INTEGER, address TEXT, created_at TEXT)`（会话文件目录结构不变，归属单独入库）
- `meta.json` 同步写 `owner` 字段，旧会话（无 owner）视为 `local` 所有。

## 隔离与权限

- 会话端点（`/api/sessions/*`：meta、events、report、export、replay、events.jsonl）校验归属：非本人且非 admin → 404（与不存在同响应，避免探测）。
- `GET /api/sessions/{id}/events`（SSE）同样校验。
- 评测：列表/详情按用户过滤（admin 全量），发起评测归属发起人。
- 静态页、登录/注册/me 端点不做校验。

## Web

- `web/login.html`：登录/注册单页（样式与现有一致）；开启认证时根路径未登录重定向。
- 顶栏：显示当前用户名 + 登出按钮；`AUTH_ENABLED=0` 时不显示。
- 评测页：显示发起人。

## 测试

- auth：scrypt 哈希/校验、注册（首个 admin）、重复用户名、错误密码、token 过期/篡改拒绝。
- 隔离：用户 A 不能读用户 B 的会话（404）、admin 可读、`AUTH_ENABLED=0` 时无 cookie 可访问全部（兼容）。
- API 层用 FakeProxy/FakeSandbox/FakeLLM 全链路（同 test_eval 模式）。
