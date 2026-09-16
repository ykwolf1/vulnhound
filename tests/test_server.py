"""薄后端单测：会话生命周期、SSE 事件流、report 与失败路径。"""

import json

import httpx
import pytest
from httpx import ASGITransport

from agent.llm import LLMResp
from agent.runner import ExecResult
from server.sessions import SESSIONS_DIR, Session, get

VERDICT_ARGS = {
    "findings": [
        {"title": "f1", "severity": "high", "rationale": "r", "evidence": [1]},
        {"title": "f2", "severity": "low", "rationale": "r", "evidence": [999]},
    ],
    "discarded": [],
}


class FakeProxy:
    """记录两条 JSONL（一条放行一条拦截）并触发 on_record。"""

    def __init__(self, allowed, record_path):
        self.allowed = allowed
        self.record_path = record_path
        self.on_record = None

    async def start(self):
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        for rec in (
            {"request_id": 1, "blocked": False, "url": "http://t/", "method": "GET"},
            {"request_id": 2, "blocked": True, "url": "http://evil/", "method": "GET"},
        ):
            self.record_path.open("a").write(json.dumps(rec) + "\n")
            if self.on_record:
                self.on_record(rec)
        return 18080

    async def stop(self):
        pass


class FakeSandbox:
    def __init__(self, proxy_port):
        self.proxy_port = proxy_port

    async def start(self):
        pass

    async def exec(self, cmd, timeout=60):
        return ExecResult(stdout="ok", stderr="", returncode=0)

    async def stop(self):
        pass


class FakeAgentLLM:
    """先 exec 一条命令，再提交报告。"""

    def __init__(self, api_key, **kwargs):
        self.api_key = api_key
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return LLMResp(tool_calls=[{
                "id": "c1", "name": "execute_command",
                "arguments": {"cmd": "curl http://t/"},
            }])
        return LLMResp(tool_calls=[{
            "id": "c2", "name": "submit_report", "arguments": VERDICT_ARGS,
        }])


@pytest.fixture
def env(tmp_path, monkeypatch):
    import server.sessions as sess
    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sess, "AuditProxy", FakeProxy)
    monkeypatch.setattr(sess, "Sandbox", FakeSandbox)
    monkeypatch.setattr(sess, "DeepSeekLLM", FakeAgentLLM)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    return tmp_path


async def consume_events(client, sid):
    events = []
    async with client.stream("GET", f"/api/sessions/{sid}/events") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def test_session_flow_step_flow_done_report(env):
    from server.app import app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/sessions", json={"address": "http://example.com:8080/"})
        assert r.status_code == 200
        sid = r.json()["session_id"]

        events = await consume_events(c, sid)
        types = [e["type"] for e in events]
        assert "step" in types and "flow" in types
        assert types[-1] == "done" and events[-1]["stopped_reason"] == "completed"

        rep = await c.get(f"/api/sessions/{sid}/report")
        assert rep.status_code == 200
        body = rep.json()
        assert body["verdict"]["findings"][0]["title"] == "f1"
        assert body["stats"]["steps"] >= 1
        assert body["stats"]["flows"] == 2
        assert body["stats"]["requests_blocked"] == 1

        meta = await c.get(f"/api/sessions/{sid}")
        assert meta.json()["status"] == "completed"


async def test_evidence_filtering(env):
    from server.app import app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        sid = (await c.post("/api/sessions", json={"address": "http://example.com"})).json()["session_id"]
        await consume_events(c, sid)
        body = (await c.get(f"/api/sessions/{sid}/report")).json()
        titles = [f["title"] for f in body["verdict"]["findings"]]
        assert titles == ["f1"]  # f2 的 evidence 999 不存在，被过滤
        assert body["notes"]["findings_filtered"] == 1


async def test_missing_api_key(env, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY")
    from server.app import app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        sid = (await c.post("/api/sessions", json={"address": "http://example.com"})).json()["session_id"]
        events = await consume_events(c, sid)
        assert events[-1]["type"] == "done"
        assert events[-1]["stopped_reason"] == "missing_key"
        body = (await c.get(f"/api/sessions/{sid}/report")).json()
        assert body["verdict"] is None
        assert body["stopped_reason"] == "missing_key"
        assert (await c.get(f"/api/sessions/{sid}")).json()["status"] == "failed"


async def test_unknown_session_404(env):
    from server.app import app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for url in ("/api/sessions/nope", "/api/sessions/nope/report", "/api/sessions/nope/events"):
            assert (await c.get(url)).status_code == 404
    assert get("nope") is None
    assert isinstance(SESSIONS_DIR.name, str)  # sanity: fixture patched module constant


async def test_get_corrupt_session_dir_returns_none(env, tmp_path):
    import server.sessions as sess
    d = tmp_path / "sessions" / "broken"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{not json")
    assert sess.get("broken") is None
    with pytest.raises(json.JSONDecodeError):
        Session("broken")


async def test_run_agent_crash_finishes_session(tmp_path, monkeypatch):
    """run_agent 未预期异常：会话必须收尾（failed:internal_error + done + report 可取）。"""

    from server.sessions import Session, create_session

    monkeypatch.setattr("server.sessions.SESSIONS_DIR", tmp_path)
    monkeypatch.setattr("server.sessions._CACHE", {})

    async def boom(*a, **k):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr("server.sessions.run_agent", boom)
    monkeypatch.setattr(
        "server.sessions.AuditProxy",
        lambda allowed, record_path: FakeProxy(allowed, record_path),
    )
    monkeypatch.setattr("server.sessions.Sandbox", lambda proxy_port: FakeSandbox(proxy_port))
    monkeypatch.setenv("LLM_API_KEY", "test")

    sid = await create_session("http://127.0.0.1:8080", None, loop_version="v1")
    session = Session(sid)
    await session.run()

    meta = session.meta
    assert meta["status"] == "failed" and meta.get("end_reason") == "internal_error"
    report = json.loads((tmp_path / sid / "report.json").read_text())
    assert report["stopped_reason"] == "internal_error" and report["verdict"] is None


def _agent_stub(monkeypatch, name="run_agent_v2"):
    """替换 agent 入口为记录调用的 stub，返回 (stub, calls)。"""
    import server.sessions as sess

    calls = []

    async def stub(llm, sandbox, target, creds, max_steps=40):
        calls.append({"target": target, "max_steps": max_steps})
        return None, [], "completed"

    monkeypatch.setattr(sess, name, stub)
    return stub, calls


async def _run_session(tmp_path, monkeypatch, loop_version=None):
    import server.sessions as sess

    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(sess, "_CACHE", {})
    monkeypatch.setattr(
        sess, "AuditProxy", lambda allowed, record_path: FakeProxy(allowed, record_path)
    )
    monkeypatch.setattr(sess, "Sandbox", lambda proxy_port: FakeSandbox(proxy_port))
    monkeypatch.setattr(sess, "DeepSeekLLM", FakeAgentLLM)
    monkeypatch.setenv("LLM_API_KEY", "test")

    body = {"address": "http://127.0.0.1:8080"}
    if loop_version:
        body["loop_version"] = loop_version
    from server.app import app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        sid = (await c.post("/api/sessions", json=body)).json()["session_id"]
        events = await consume_events(c, sid)
        assert events[-1]["type"] == "done"
    return sess.Session(sid)


async def test_loop_version_v2_calls_run_agent_v2(tmp_path, monkeypatch):
    _, calls = _agent_stub(monkeypatch, "run_agent_v2")
    session = await _run_session(tmp_path, monkeypatch, loop_version="v2")
    assert len(calls) == 1
    assert session.meta["loop_version"] == "v2"


async def test_default_loop_version_is_v2(tmp_path, monkeypatch):
    """不传 loop_version：默认走 v2（run_agent_v2 被调，run_agent 不被调）。"""
    _, calls = _agent_stub(monkeypatch, "run_agent_v2")
    _, v1_calls = _agent_stub(monkeypatch, "run_agent")
    session = await _run_session(tmp_path, monkeypatch)
    assert len(calls) == 1 and v1_calls == []
    assert session.meta["loop_version"] == "v2"


async def test_loop_version_v1_calls_run_agent(tmp_path, monkeypatch):
    _, v1_calls = _agent_stub(monkeypatch, "run_agent")
    _, v2_calls = _agent_stub(monkeypatch, "run_agent_v2")
    session = await _run_session(tmp_path, monkeypatch, loop_version="v1")
    assert len(v1_calls) == 1 and v2_calls == []
    assert session.meta["loop_version"] == "v1"


async def test_time_cap_when_agent_exceeds_budget(tmp_path, monkeypatch):
    """time_budget 耗尽：failed:time_cap 收尾，report stopped_reason=time_cap，verdict=None。"""
    import asyncio

    import server.sessions as sess

    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(sess, "_CACHE", {})
    monkeypatch.setattr(
        sess, "AuditProxy", lambda allowed, record_path: FakeProxy(allowed, record_path)
    )
    monkeypatch.setattr(sess, "Sandbox", lambda proxy_port: FakeSandbox(proxy_port))
    monkeypatch.setattr(sess, "DeepSeekLLM", FakeAgentLLM)
    monkeypatch.setenv("LLM_API_KEY", "test")

    async def sleeper(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(sess, "run_agent_v2", sleeper)

    sid = await sess.create_session("http://127.0.0.1:8080", None)
    session = sess.Session(sid)
    await session.run(time_budget=0.05)

    session.meta = json.loads((tmp_path / sid / "meta.json").read_text())
    assert session.meta["status"] == "failed"
    assert session.meta["end_reason"] == "time_cap"
    report = json.loads((tmp_path / sid / "report.json").read_text())
    assert report["stopped_reason"] == "time_cap"
    assert report["verdict"] is None
