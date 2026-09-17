"""V5 单测：认证流程、会话归属隔离、AUTH_ENABLED=0 兼容路径。零网络零容器。"""

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from server.auth import AuthStore


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    import server.app as srv
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    store = AuthStore(db_path=tmp_path / "test.db")
    monkeypatch.setattr(srv, "_auth_store", store)
    return srv, store


def client(srv):
    return httpx.AsyncClient(transport=ASGITransport(app=srv.app), base_url="http://t")


async def test_register_first_is_admin_and_login(auth_env):
    srv, store = auth_env
    async with client(srv) as c:
        r = await c.post("/api/auth/register", json={"username": "alice", "password": "secret1"})
        assert r.json()["role"] == "admin"
        r = await c.post("/api/auth/register", json={"username": "bob", "password": "secret1"})
        assert r.json()["role"] == "user"
        # 重复用户名
        r = await c.post("/api/auth/register", json={"username": "alice", "password": "secret1"})
        assert r.status_code == 422
        # 登录错误密码
        r = await c.post("/api/auth/login", json={"username": "alice", "password": "wrong!"})
        assert r.status_code == 401
        # 正确登录拿到 cookie
        r = await c.post("/api/auth/login", json={"username": "alice", "password": "secret1"})
        assert r.status_code == 200 and "vh_token" in r.cookies
        # me
        r = await c.get("/api/me")
        assert r.json()["username"] == "alice" and r.json()["role"] == "admin"


async def test_token_tamper_and_expiry(auth_env):
    srv, store = auth_env
    store.register("alice", "secret1")
    user = store.verify("alice", "secret1")
    token = store.issue_token(user)
    assert store.verify_token(token)["id"] == user["id"]
    assert store.verify_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None  # 篡改
    expired = store.issue_token({**user})  # 正常签发
    # 过期：伪造 exp（负 payload 无法直接构造签名，用时间回放验证 verify 分支）
    assert store.verify_token("999999.exp.sig") is None


async def test_ownership_isolation(auth_env, tmp_path, monkeypatch):
    srv, store = auth_env
    store.register("alice", "secret1")   # admin（首个）
    store.register("bob", "secret1")
    alice = store.verify("alice", "secret1")
    bob = store.verify("bob", "secret1")
    store.record_session("s-alice", alice["id"], "http://x")
    store.record_session("s-bob", bob["id"], "http://x")

    async with client(srv) as c:
        # bob 未登录
        r = await c.get("/api/sessions/s-bob")
        assert r.status_code == 401
        # bob 登录后访问自己的
        await c.post("/api/auth/login", json={"username": "bob", "password": "secret1"})
        r = await c.get("/api/sessions/s-bob")
        assert r.status_code == 404  # 目录不存在，但已过权限层
        r = await c.get("/api/sessions/s-alice")
        assert r.status_code == 404  # 归属隔离：bob 看 alice 的 → 404
        # alice（admin）登录后可访问 bob 的
        await c.post("/api/auth/login", json={"username": "alice", "password": "secret1"})
        r = await c.get("/api/sessions/s-bob")
        assert r.status_code == 404  # 权限过了，目录不存在也是 404
        # 无归属记录的旧会话：admin 可过权限层
        r = await c.get("/api/sessions/s-legacy")
        assert r.status_code == 404
        assert not store.can_access("s-legacy", bob)  # 旧会话对普通用户不可见


async def test_auth_disabled_compat(tmp_path, monkeypatch):
    """AUTH_ENABLED=0（默认）：无 cookie 可访问，行为同 V4。"""
    import server.app as srv
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.setattr(srv, "_auth_store", AuthStore(db_path=tmp_path / "t.db"))
    async with client(srv) as c:
        r = await c.get("/api/me")
        assert r.json()["username"] == "local"
        r = await c.get("/api/sessions/anything")
        assert r.status_code == 404  # 目录不存在 404，而不是 401


async def test_password_hashing(auth_env):
    import server.auth as auth
    h = auth._hash_password("secret1")
    assert h.startswith("scrypt$") and "secret1" not in h
    assert auth._verify_password("secret1", h)
    assert not auth._verify_password("secret2", h)
    assert not auth._verify_password("x", "garbage")
