"""AuditProxy 单测：真 socket，本机假目标 + 代理，httpx 走代理发绝对 URL 请求。"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from proxy import AuditProxy, AuditTarget

RESP_BODY = "hello-from-fake-target"


async def _start_fake_target():
    """本机简单 HTTP 服务器，返回固定响应。返回 (port, server)。"""

    async def handle(reader, writer):
        await reader.readline()  # request line（绝对 URL 形式）
        while await reader.readline() not in (b"\r\n", b"\n", b""):
            pass
        body = RESP_BODY.encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server.sockets[0].getsockname()[1], server



@pytest.fixture
async def env(tmp_path: Path):
    target_port, target_server = await _start_fake_target()
    allowed: AuditTarget = ("127.0.0.1", target_port, "http")
    proxy = AuditProxy(allowed=allowed, record_path=tmp_path / "audit" / "proxy.jsonl")
    proxy_port = await proxy.start()
    yield proxy, proxy_port, target_port, tmp_path / "audit" / "proxy.jsonl"
    await proxy.stop()
    target_server.close()
    await target_server.wait_closed()


def _client(proxy_port: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(proxy=f"http://127.0.0.1:{proxy_port}", timeout=5.0)


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_allowed_request_forwarded_and_recorded(env):
    _proxy, proxy_port, target_port, record_path = env
    async with _client(proxy_port) as client:
        resp = await client.get(f"http://127.0.0.1:{target_port}/index.php")
    assert resp.status_code == 200
    assert resp.text == RESP_BODY
    recs = _records(record_path)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["request_id"] == 1
    assert rec["blocked"] is False
    assert rec["status"] == 200
    assert rec["resp_body"] == RESP_BODY
    assert rec["url"] == f"http://127.0.0.1:{target_port}/index.php"


async def test_block_wrong_host(env):
    _proxy, proxy_port, _target_port, record_path = env
    async with _client(proxy_port) as client:
        resp = await client.get("http://evil.com:80/path")
    assert resp.status_code == 403
    recs = _records(record_path)
    assert len(recs) == 1
    assert recs[0]["blocked"] is True
    assert recs[0]["block_reason"]
    assert recs[0]["status"] is None


async def test_block_wrong_port(env):
    _proxy, proxy_port, _target_port, record_path = env
    async with _client(proxy_port) as client:
        resp = await client.get("http://127.0.0.1:22/path")
    assert resp.status_code == 403
    recs = _records(record_path)
    assert len(recs) == 1
    assert recs[0]["blocked"] is True


async def test_request_id_monotonic(env):
    _proxy, proxy_port, target_port, record_path = env
    async with _client(proxy_port) as client:
        await client.get(f"http://127.0.0.1:{target_port}/a")
        await client.get("http://evil.com:80/b")
        await client.get(f"http://127.0.0.1:{target_port}/c")
    recs = _records(record_path)
    assert [r["request_id"] for r in recs] == [1, 2, 3]


async def test_malformed_request_is_recorded_not_silent(tmp_path):
    """畸形请求行不得逃逸审计：必须有 blocked 记录。"""
    reader = writer = None
    target_port, target_server = await _start_fake_target()
    proxy = AuditProxy(("127.0.0.1", target_port, "http"), tmp_path / "p.jsonl")
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GARBAGE-NO-SPACES\r\n\r\n")
        await writer.drain()
        await asyncio.sleep(0.2)
    finally:
        if writer:
            writer.close()
        await proxy.stop()
        target_server.close()
        await target_server.wait_closed()
    lines = (tmp_path / "p.jsonl").read_text().strip().splitlines()
    assert lines, "畸形请求必须留审计记录"
    entry = json.loads(lines[-1])
    assert entry["blocked"] is True and "malformed" in entry["block_reason"]
