"""V5 认证与授权：scrypt 密码哈希、HMAC token、SQLite 用户存储、归属校验。

AUTH_ENABLED=1 开启；未开启时所有请求视为内置 local 用户（V4 行为）。
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

__all__ = ["AuthStore", "LOCAL_USER", "auth_enabled"]

TOKEN_TTL = 7 * 24 * 3600  # 7 天
DB_PATH = Path(os.environ.get("VULNHOUND_DB", "vulnhound.db"))

LOCAL_USER = {"id": None, "username": "local", "role": "admin"}


def auth_enabled() -> bool:
    return os.environ.get("AUTH_ENABLED") == "1"


def _secret_key() -> bytes:
    key = os.environ.get("SECRET_KEY")
    if key:
        return key.encode()
    # 未设密钥：随机生成（仅进程内有效，重启后 token 全部失效）
    return secrets.token_bytes(32)


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


def _sign(payload: str, key: bytes) -> str:
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


class AuthStore:
    """用户存储 + token 签发/校验。"""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH
        self._key = _secret_key()
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "username TEXT UNIQUE NOT NULL,"
            "password_hash TEXT NOT NULL,"
            "role TEXT NOT NULL DEFAULT 'user',"
            "created_at TEXT NOT NULL)"
        )
        # V5 商业化：api_token_hash（sha256）、quota_daily（每日会话配额，NULL=默认）
        for col, ddl in [("api_token_hash", "TEXT"), ("quota_daily", "INTEGER")]:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(users)").fetchall()]
            if col not in cols:
                self._conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        self._conn.commit()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS session_owners ("
            "session_id TEXT PRIMARY KEY,"
            "owner_id INTEGER,"
            "address TEXT,"
            "created_at TEXT)"
        )
        self._conn.commit()

    # ---- users ----

    def register(self, username: str, password: str) -> dict:
        if not username.strip() or len(password) < 6:
            raise ValueError("用户名不能为空，密码至少 6 位")
        count = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        role = "admin" if count == 0 else "user"  # 首个用户即 admin
        try:
            self._conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username.strip(), _hash_password(password), role, time.strftime("%Y-%m-%dT%H:%M:%S%z")),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名已存在") from exc
        return {"username": username.strip(), "role": role}

    def verify(self, username: str, password: str) -> dict | None:
        row = self._conn.execute(
            "SELECT id, password_hash, role FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None or not _verify_password(password, row[1]):
            return None
        return {"id": row[0], "username": username, "role": row[2]}

    # ---- token ----

    def issue_token(self, user: dict) -> str:
        exp = int(time.time()) + TOKEN_TTL
        payload = f"{user['id']}.{exp}"
        return f"{payload}.{_sign(payload, self._key)}"

    def verify_token(self, token: str) -> dict | None:
        try:
            uid_s, exp_s, sig = token.split(".")
            payload = f"{uid_s}.{exp_s}"
            if not hmac.compare_digest(_sign(payload, self._key), sig):
                return None
            if int(exp_s) < time.time():
                return None
            row = self._conn.execute(
                "SELECT id, username, role FROM users WHERE id = ?", (int(uid_s),)
            ).fetchone()
            if row is None:
                return None
            return {"id": row[0], "username": row[1], "role": row[2]}
        except (ValueError, TypeError):
            return None

    # ---- API Token（V5 商业化：脚本/CI 接入）----

    def issue_api_token(self, user_id: int) -> str:
        """生成随机 token，存 sha256 哈希，返回明文（仅此一次可见）。"""
        token = f"vht_{user_id}_{secrets.token_urlsafe(24)}"
        h = hashlib.sha256(token.encode()).hexdigest()
        self._conn.execute("UPDATE users SET api_token_hash = ? WHERE id = ?", (h, user_id))
        self._conn.commit()
        return token

    def verify_api_token(self, token: str) -> dict | None:
        h = hashlib.sha256(token.encode()).hexdigest()
        row = self._conn.execute(
            "SELECT id, username, role FROM users WHERE api_token_hash = ?", (h,)
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "username": row[1], "role": row[2]}

    # ---- 配额（V5 商业化）----

    def quota_daily(self, user_id: int | None) -> int:
        """用户每日会话配额：个人设置优先，否则环境变量默认。admin 不限。"""
        if user_id is None:
            return 10**9
        row = self._conn.execute(
            "SELECT role, quota_daily FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None or row[0] == "admin":
            return 10**9
        if row[1] is not None:
            return row[1]
        return int(os.environ.get("QUOTA_DAILY", "20"))

    def used_today(self, user_id: int) -> int:
        today = time.strftime("%Y-%m-%d")
        row = self._conn.execute(
            "SELECT COUNT(*) FROM session_owners WHERE owner_id = ? AND created_at LIKE ?",
            (user_id, today + "%"),
        ).fetchone()
        return row[0]

    def set_quota(self, user_id: int, quota: int) -> None:
        self._conn.execute("UPDATE users SET quota_daily = ? WHERE id = ?", (quota, user_id))
        self._conn.commit()

    def all_users(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, username, role, quota_daily, created_at FROM users ORDER BY id"
        ).fetchall()
        return [{"id": r[0], "username": r[1], "role": r[2], "quota_daily": r[3], "created_at": r[4]} for r in rows]

    # ---- session ownership ----

    def record_session(self, session_id: str, owner_id: int | None, address: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO session_owners (session_id, owner_id, address, created_at) VALUES (?, ?, ?, ?)",
            (session_id, owner_id, address, time.strftime("%Y-%m-%dT%H:%M:%S%z")),
        )
        self._conn.commit()

    def can_access(self, session_id: str, user: dict) -> bool:
        """admin 全量；普通用户只读自己；无归属记录的旧会话视为 local 所有。"""
        if user.get("role") == "admin":
            return True
        row = self._conn.execute(
            "SELECT owner_id FROM session_owners WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:  # 旧数据无归属
            return False
        return row[0] == user.get("id")

    def owner_username(self, session_id: str) -> str:
        row = self._conn.execute(
            "SELECT u.username FROM session_owners o JOIN users u ON o.owner_id = u.id WHERE o.session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row else "legacy"

    def owned_sessions(self, user: dict) -> list[str]:
        if user.get("role") == "admin":
            rows = self._conn.execute("SELECT session_id FROM session_owners").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT session_id FROM session_owners WHERE owner_id = ?", (user.get("id"),)
            ).fetchall()
        return [r[0] for r in rows]
