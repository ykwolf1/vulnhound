"""V4 单测：打分纯函数、跑批聚合（FakeLLM）、报告聚合与 Web API。零网络零容器。"""

import asyncio
import json

import httpx
import pytest
from httpx import ASGITransport

from eval.report import list_results, load_result, render_markdown, save_result
from eval.scoring import claim_modules, load_groundtruth, module_of_url, score_session
from server.exporters import load_jsonl

GT = [
    {"module": "/vulnerabilities/sqli/", "vuln_type": "SQL Injection", "severity": "high"},
    {"module": "/vulnerabilities/exec/", "vuln_type": "Command Injection", "severity": "high"},
    {"module": "/vulnerabilities/fi/", "vuln_type": "File Inclusion", "severity": "high"},
]


def make_report(findings, evidence_map):
    """findings: list[(title, rationale, evidence)]; evidence_map: rid->url"""
    return {
        "verdict": {"findings": [
            {"title": t, "severity": "high", "rationale": r, "evidence": e} for t, r, e in findings
        ], "discarded": []},
        "stats": {"flows": len(evidence_map), "steps": 3},
    }


def records(evidence_map):
    return [
        {"request_id": rid, "method": "GET", "url": url, "status": 200, "blocked": False}
        for rid, url in evidence_map.items()
    ]


def test_module_of_url():
    assert module_of_url("http://t:8080/vulnerabilities/sqli/?id=1") == "/vulnerabilities/sqli/"
    assert module_of_url("http://t:8080/index.php") is None
    assert module_of_url("http://t:8080/vulnerabilities/fi/?page=x") == "/vulnerabilities/fi/"


def test_claim_modules_by_type_and_path():
    gt = load_groundtruth("dvwa")
    f = {"title": "SQL Injection in sqli module", "rationale": "union select works"}
    assert "/vulnerabilities/sqli/" in claim_modules(f, gt)
    f2 = {"title": "命令注入", "rationale": "Command Injection via ip param at /vulnerabilities/exec/"}
    assert "/vulnerabilities/exec/" in claim_modules(f2, gt)


def test_score_precision_recall_evidence():
    emap = {1: "http://t/vulnerabilities/sqli/?id=1", 2: "http://t/vulnerabilities/exec/"}
    report = make_report(
        [("SQL Injection", "id param union", [1]), ("Command Injection", "ip param", [2, 99])],
        emap,
    )
    s = score_session(report, records(emap), GT)
    assert s["true_positives"] == 2 and s["false_positives"] == 0
    assert s["precision"] == 1.0
    assert s["recall"] == round(2 / 3, 3)  # fi 未发现
    assert s["evidence_total"] == 3 and s["evidence_exists"] == 2 and s["evidence_pointed"] == 2  # rid=99 不存在
    assert s["missed_modules"] == ["/vulnerabilities/fi/"]


def test_score_false_positive_and_wrong_module_evidence():
    emap = {1: "http://t/index.php"}  # 无模块路径
    report = make_report([("Weak Session IDs", "猜的", [1])], emap)
    s = score_session(report, records(emap), GT)
    assert s["false_positives"] == 1 and s["precision"] == 0.0
    # 证据存在但指向非声称模块（未声称时仅要求存在）
    assert s["evidence_exists"] == 1 and s["evidence_pointed"] == 1


def test_score_empty():
    s = score_session(make_report([], {}), records({}), GT)
    assert s["precision"] is None and s["recall"] == 0.0 and s["evidence_validity"] is None


# ---- runner + report（FakeLLM 全链路）----

VERDICT = {
    "findings": [{"title": "SQL Injection in sqli module", "severity": "high",
                  "rationale": "union select", "evidence": [1]}],
    "discarded": [],
}


class FakeProxy:
    def __init__(self, allowed, record_path):
        self.allowed, self.record_path, self.on_record = allowed, record_path, None

    async def start(self):
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"request_id": 1, "blocked": False, "method": "GET",
               "url": self.allowed_url, "status": 200, "req_body": "", "resp_body": "ok"}
        self.record_path.open("a").write(json.dumps(rec) + "\n")
        if self.on_record:
            self.on_record(rec)
        return 18080

    async def stop(self):
        pass


def make_fake_llm(target_url):
    class FakeLLM:
        def __init__(self, api_key, **kwargs):
            self.calls = 0

        async def complete(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return type("R", (), {"tool_calls": [{"id": "c1", "name": "execute_command",
                                                      "arguments": {"cmd": f"curl {target_url}/vulnerabilities/sqli/"}}],
                                      "content": None, "reasoning_content": None})()
            return type("R", (), {"tool_calls": [{"id": "c2", "name": "submit_report", "arguments": VERDICT}],
                                  "content": None, "reasoning_content": None})()
    return FakeLLM


@pytest.fixture
def eval_env(tmp_path, monkeypatch):
    import server.sessions as sess
    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sess, "Sandbox", type("S", (), {
        "__init__": lambda self, proxy_port: None,
        "start": lambda self: _noop(),
        "exec": lambda self, cmd, timeout=60: _exec(),
        "stop": lambda self: _noop(),
    }))
    monkeypatch.setattr(sess, "DeepSeekLLM", make_fake_llm("http://example.com"))
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    import eval.runner as runner

    def make_proxy(allowed, record_path):
        p = FakeProxy(allowed, record_path)
        p.record_path = record_path
        p.allowed_url = "http://example.com/vulnerabilities/sqli/"
        return p

    monkeypatch.setattr(sess, "AuditProxy", make_proxy)
    monkeypatch.setattr(runner, "load_groundtruth", lambda name, gt_dir=None: GT)
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results", raising=False)
    return tmp_path


async def _noop():
    return None


async def _exec():
    from agent.runner import ExecResult
    return ExecResult(stdout="ok", stderr="", returncode=0)


async def test_run_batch_and_report(eval_env, tmp_path):
    from eval.runner import run_batch
    result = await run_batch("http://example.com", runs=2, concurrency=2, gt_name="dvwa")
    s = result["summary"]
    assert s["completed"] == 2 and s["success_rate"] == 1.0
    assert s["avg_precision"] == 1.0 and s["avg_recall"] == round(1 / 3, 3)
    assert result["sessions"][0]["evidence_validity"] == 1.0

    eid = save_result(result, results_dir=tmp_path / "results")
    assert (tmp_path / "results" / f"{eid}.json").exists()
    md = (tmp_path / "results" / f"{eid}.md").read_text(encoding="utf-8")
    assert "查准率" in md and "查全率" in md
    assert list_results(results_dir=tmp_path / "results")[0]["eval_id"] == eid
    assert load_result(eid, results_dir=tmp_path / "results")["target"] == "http://example.com"


# ---- Web API ----

async def test_eval_web_api(eval_env, tmp_path, monkeypatch):
    import eval.report as rep
    monkeypatch.setattr(rep, "RESULTS_DIR", tmp_path / "results")
    from server.app import app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/evals")
        assert r.status_code == 200 and r.json() == []
        r = await c.post("/api/evals", json={"target": "http://example.com", "runs": 1, "concurrency": 1})
        assert r.status_code == 200 and r.json()["status"] == "started"
        # 轮询等待后台评测完成
        for _ in range(40):
            lst = (await c.get("/api/evals")).json()
            if lst:
                break
            await asyncio.sleep(0.2)
        assert lst and lst[0]["summary"]["completed"] == 1
        d = await c.get(f"/api/evals/{lst[0]['eval_id']}")
        assert d.status_code == 200 and d.json()["sessions"][0]["precision"] == 1.0
        r = await c.get("/api/evals/nope")
        assert r.status_code == 404




def test_evidence_object_format_with_why():
    """P2：evidence 新格式 {request_id, why}；rationale 命中模块关键词时指向性放宽。"""
    emap = {1: "http://t/vulnerabilities/exec/?ip=1"}
    report = {"verdict": {"findings": [
        {"title": "命令注入", "severity": "high", "rationale": "exec 模块 ip 参数注入",
         "evidence": [{"request_id": 1, "why": "响应含 id 命令输出 uid=33"}]},
    ], "discarded": []}}
    s = score_session(report, records(emap), GT)
    assert s["evidence_exists"] == 1 and s["evidence_pointed"] == 1


def test_evidence_mixed_format():
    report = {"verdict": {"findings": [
        {"title": "SQL Injection sqli", "severity": "high", "rationale": "union",
         "evidence": [1, {"request_id": 2, "why": "x"}]},
    ], "discarded": []}}
    emap = {1: "http://t/vulnerabilities/sqli/", 2: "http://t/vulnerabilities/sqli/?p=2"}
    s = score_session(report, records(emap), GT)
    assert s["evidence_total"] == 2 and s["evidence_pointed"] == 2
