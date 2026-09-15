"""Task 4: LLM 循环单测——FakeLLM + FakeSandbox，零网络零容器。"""

from agent.fakes import FakeLLM
from agent.llm import LLMError, LLMResp
from agent.loop import run_agent
from agent.runner import ExecResult


class FakeSandbox:
    """按 cmd 匹配返回 stdout 的假沙箱。"""

    def __init__(self, script: dict[str, str]):
        self.script = script

    async def exec(self, cmd: str, timeout: int = 60) -> ExecResult:
        return ExecResult(stdout=self.script.get(cmd, ""), stderr="", returncode=0)


def exec_resp(cmd: str, call_id: str = "c1") -> LLMResp:
    return LLMResp(
        content=None,
        tool_calls=[{"id": call_id, "name": "execute_command", "arguments": {"cmd": cmd}}],
    )


def report_resp(payload: dict, call_id: str = "r1") -> LLMResp:
    return LLMResp(
        content=None,
        tool_calls=[{"id": call_id, "name": "submit_report", "arguments": payload}],
    )


VALID_REPORT = {
    "findings": [
        {
            "title": "SQL injection in login",
            "severity": "high",
            "rationale": "login 参数可注入，绕过认证",
            "evidence": [1],
        }
    ],
    "discarded": [{"title": "XSS in guestbook", "reason": "仅反射无危害"}],
}

INVALID_REPORT = {
    "findings": [
        {
            "title": "bad",
            "severity": "high",
            "rationale": "evidence 为空",
            "evidence": [],
        }
    ],
    "discarded": [],
}


async def test_normal_completion():
    llm = FakeLLM(
        [
            exec_resp("curl http://target/login.php", "c1"),
            report_resp(VALID_REPORT, "r1"),
        ]
    )
    sandbox = FakeSandbox({"curl http://target/login.php": "resp [request_id=1]"})
    verdict, events, reason = await run_agent(llm, sandbox, "http://target", None)
    assert reason == "completed"
    assert verdict is not None
    assert verdict.findings[0].evidence == [1]
    assert verdict.findings[0].severity == "high"
    assert [e.tag for e in events] == ["act", "conclude"]
    assert events[0].cmd == "curl http://target/login.php"
    assert events[0].result == "resp [request_id=1]"


async def test_step_cap():
    llm = FakeLLM(
        [
            LLMResp(content="thinking...", tool_calls=[]),
            LLMResp(content="still thinking...", tool_calls=[]),
            LLMResp(content="more...", tool_calls=[]),
        ]
    )
    verdict, _events, reason = await run_agent(llm, FakeSandbox({}), "http://t", None, max_steps=3)
    assert reason == "step_cap"
    assert verdict is None


async def test_no_progress_on_repeated_cmd():
    llm = FakeLLM([exec_resp("curl http://t/x", f"c{i}") for i in range(3)])
    sandbox = FakeSandbox({"curl http://t/x": ""})  # stdout 为空：只触发 cmd 相同规则
    verdict, events, reason = await run_agent(llm, sandbox, "http://t", None)
    assert reason == "no_progress"
    assert verdict is None
    assert len(events) == 3


async def test_no_progress_on_similar_stdout():
    llm = FakeLLM(
        [
            exec_resp("curl http://t/a", "c1"),
            exec_resp("curl http://t/b", "c2"),
        ]
    )
    sandbox = FakeSandbox({"curl http://t/a": "AAAA", "curl http://t/b": "AAAA"})
    _verdict, _events, reason = await run_agent(llm, sandbox, "http://t", None)
    assert reason == "no_progress"


async def test_invalid_verdict_twice_llm_error():
    llm = FakeLLM(
        [
            report_resp(INVALID_REPORT, "r1"),
            report_resp(INVALID_REPORT, "r2"),
        ]
    )
    verdict, _events, reason = await run_agent(llm, FakeSandbox({}), "http://t", None)
    assert reason == "llm_error"
    assert verdict is None


async def test_invalid_verdict_then_valid_recovers():
    llm = FakeLLM(
        [
            report_resp(INVALID_REPORT, "r1"),
            report_resp(VALID_REPORT, "r2"),
        ]
    )
    verdict, _events, reason = await run_agent(llm, FakeSandbox({}), "http://t", None)
    assert reason == "completed"
    assert verdict is not None


async def test_llm_error_propagates():
    llm = FakeLLM([LLMError("boom")])
    verdict, _events, reason = await run_agent(llm, FakeSandbox({}), "http://t", None)
    assert reason == "llm_error"
    assert verdict is None
