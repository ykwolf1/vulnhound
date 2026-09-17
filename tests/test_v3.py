"""V3 单测：导出（messages/sarif/html）与请求重放。零网络零容器。"""

import json

import httpx
import pytest
from httpx import ASGITransport

from server.exporters import dump_html, dump_messages, dump_sarif

REPORT = {
    "verdict": {
        "findings": [
            {"title": "SQL 注入", "severity": "high", "rationale": "union select 回显", "evidence": [3, 7]},
            {"title": "反射 XSS", "severity": "medium", "rationale": "参数回显", "evidence": [9]},
        ],
        "discarded": [{"title": "CSRF", "reason": "表单无 token 但无敏感操作"}],
    },
    "stopped_reason": "completed",
    "target": {"address": "http://example.com:8080/", "host": "example.com", "port": 8080, "scheme": "http"},
    "stats": {"flows": 12, "requests_blocked": 1, "steps": 8},
    "notes": {},
    "finished_at": "2026-09-17T10:00:00+0800",
}

MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "execute_command", "arguments": {"cmd": "curl"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
]


def make_session_dir(tmp_path, monkeypatch):
    import server.sessions as sess
    d = tmp_path / "sessions" / "20260917-abcd"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "id": "20260917-abcd", "address": "http://example.com:8080/", "host": "example.com",
        "port": 8080, "scheme": "http", "creds": None, "status": "completed",
        "loop_version": "v2", "created_at": "t",
    }), encoding="utf-8")
    (d / "events.jsonl").write_text('{"type": "step", "n": 1, "tag": "observe", "text": "exec: x"}\n', encoding="utf-8")
    (d / "proxy.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"request_id": 1, "ts": "t", "method": "GET", "url": "http://example.com:8080/vulnerabilities/",
         "status": 200, "req_body": None, "resp_body": "<html>old</html>", "blocked": False},
        {"request_id": 2, "ts": "t", "method": "GET", "url": "http://evil/", "status": None,
         "req_body": None, "resp_body": "", "blocked": True, "block_reason": "not_allowed"},
    ]) + "\n", encoding="utf-8")
    (d / "report.json").write_text(json.dumps(REPORT, ensure_ascii=False), encoding="utf-8")
    (d / "messages.jsonl").write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in MESSAGES), encoding="utf-8")
    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path / "sessions")
    return d


def client():
    from server.app import app
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ---- exporters ----

def test_dump_messages_roundtrip():
    out = dump_messages(MESSAGES)
    lines = [json.loads(l) for l in out.splitlines()]
    assert lines == MESSAGES
    assert lines[1]["tool_calls"][0]["name"] == "execute_command"


def test_dump_sarif_mapping():
    sarif = json.loads(dump_sarif(REPORT))
    assert sarif["version"] == "2.1.0"
    results = sarif["runs"][0]["results"]
    assert len(results) == 2
    assert results[0]["ruleId"] == "SQL 注入"
    assert results[0]["level"] == "error"
    assert results[1]["level"] == "warning"
    assert results[0]["properties"]["evidence_request_ids"] == [3, 7]
    assert "request_id=3" in results[0]["message"]["text"]
    assert results[0]["locations"][0]["physicalLocation"]["address"]["fullyQualifiedHostName"] == "example.com"


def test_dump_html_contains_sections():
    h = dump_html(REPORT)
    assert "SQL 注入" in h and "反射 XSS" in h
    assert "CSRF" in h and "表单无 token" in h
    assert "example.com" in h and "12" in h
    assert "<script" not in h  # 无外部资源
    assert "union select" in h


def test_dump_html_escapes():
    r = json.loads(json.dumps(REPORT))
    r["verdict"]["findings"][0]["rationale"] = "<img src=x onerror=alert(1)>"
    assert "&lt;img" in dump_html(r)


# ---- endpoints ----

async def test_export_messages(tmp_path, monkeypatch):
    make_session_dir(tmp_path, monkeypatch)
    async with client() as c:
        r = await c.get("/api/sessions/20260917-abcd/export", params={"format": "messages"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-ndjson")
        assert [json.loads(l) for l in r.text.splitlines()] == MESSAGES


async def test_export_sarif_and_html(tmp_path, monkeypatch):
    make_session_dir(tmp_path, monkeypatch)
    async with client() as c:
        r = await c.get("/api/sessions/20260917-abcd/export", params={"format": "sarif"})
        assert r.status_code == 200
        assert json.loads(r.text)["runs"][0]["results"][0]["level"] == "error"
        r = await c.get("/api/sessions/20260917-abcd/export", params={"format": "html"})
        assert r.status_code == 200
        assert "SQL 注入" in r.text
        r = await c.get("/api/sessions/20260917-abcd/export", params={"format": "zip"})
        assert r.status_code == 422


async def test_events_file_endpoint(tmp_path, monkeypatch):
    make_session_dir(tmp_path, monkeypatch)
    async with client() as c:
        r = await c.get("/api/sessions/20260917-abcd/events.jsonl")
        assert r.status_code == 200
        assert json.loads(r.text.splitlines()[0])["n"] == 1


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, status=200, body="new"):
        self.status, self.body = status, body
        self.seen = []

    async def handle_async_request(self, request):
        self.seen.append((request.method, str(request.url), request.content))
        return httpx.Response(self.status, text=self.body)


async def test_replay_diff(tmp_path, monkeypatch):
    make_session_dir(tmp_path, monkeypatch)
    transport = FakeTransport(status=200, body="<html>new</html>")
    c = client()
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_async_client(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}))
    async with c:
        r = await c.post("/api/sessions/20260917-abcd/replay", json={"request_id": 1})
        assert r.status_code == 200
        body = r.json()
        assert body["original"]["resp_body"] == "<html>old</html>"
        assert body["replayed"]["resp_body"] == "<html>new</html>"
        assert body["diff"]["body_changed"] and not body["diff"]["status_changed"]
        assert transport.seen[0][0] == "GET"


async def test_replay_overrides(tmp_path, monkeypatch):
    make_session_dir(tmp_path, monkeypatch)
    transport = FakeTransport()
    c = client()
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_async_client(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}))
    async with c:
        r = await c.post("/api/sessions/20260917-abcd/replay", json={
            "request_id": 1,
            "overrides": {"method": "POST", "url": "http://example.com:8080/login.php", "body": "u=admin"},
        })
        assert r.status_code == 200
        method, url, content = transport.seen[0]
        assert (method, url, content) == ("POST", "http://example.com:8080/login.php", b"u=admin")


async def test_replay_rejects_offtarget_and_blocked(tmp_path, monkeypatch):
    make_session_dir(tmp_path, monkeypatch)
    async with client() as c:
        r = await c.post("/api/sessions/20260917-abcd/replay", json={
            "request_id": 1, "overrides": {"url": "http://evil/x"},
        })
        assert r.status_code == 422
        r = await c.post("/api/sessions/20260917-abcd/replay", json={"request_id": 2})
        assert r.status_code == 422
        r = await c.post("/api/sessions/20260917-abcd/replay", json={"request_id": 99})
        assert r.status_code == 404
