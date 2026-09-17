"""Task 2 (V2): 双层循环单测——FakeLLM + FakeSandbox，零网络零容器。"""

from agent.fakes import FakeLLM
from agent.llm import LLMResp
from agent.loop_v2 import run_agent_v2
from agent.runner import ExecResult


class FakeSandbox:
    def __init__(self, script: dict[str, str]):
        self.script = script

    async def exec(self, cmd: str, timeout: int = 60) -> ExecResult:
        return ExecResult(stdout=self.script.get(cmd, ""), stderr="", returncode=0)


class RecordingLLM(FakeLLM):
    """记录每次 complete 收到的 messages/tools，便于断言注入时机。"""

    def __init__(self, script: list[LLMResp]):
        super().__init__(script)
        self.calls: list[dict] = []

    async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResp:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        return await super().complete(messages, tools)


def exec_resp(cmd: str, call_id: str = "c1") -> LLMResp:
    return LLMResp(
        content=None,
        tool_calls=[{"id": call_id, "name": "execute_command", "arguments": {"cmd": cmd}}],
    )


def switch_resp(
    next_direction: str, hypothesis: str, call_id: str = "s1", outcome: str | None = None
) -> LLMResp:
    args: dict = {"next_direction": next_direction, "hypothesis": hypothesis}
    if outcome is not None:
        args["current_outcome"] = outcome
    return LLMResp(
        content=None, tool_calls=[{"id": call_id, "name": "switch_direction", "arguments": args}]
    )


def report_resp(payload: dict, call_id: str = "r1") -> LLMResp:
    return LLMResp(
        content=None, tool_calls=[{"id": call_id, "name": "submit_report", "arguments": payload}]
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
    "discarded": [],
}


def user_texts(llm: RecordingLLM, call_idx: int) -> list[str]:
    return [m["content"] for m in llm.calls[call_idx]["messages"] if m["role"] == "user"]


async def test_dual_direction_switch_then_submit():
    """正常路径：探索 → 切换 → 验证 → 提交；切换事件与 direction_id 正确。"""
    llm = RecordingLLM(
        [
            exec_resp("curl http://t/login.php", "c1"),
            switch_resp("xss-stored", "留言板存在存储型 XSS", "s1", outcome="sqli 已验证存在"),
            exec_resp("curl http://t/guestbook.php", "c2"),
            report_resp(VALID_REPORT, "r1"),
        ]
    )
    sandbox = FakeSandbox(
        {"curl http://t/login.php": "resp [request_id=1]", "curl http://t/guestbook.php": "xss [request_id=2]"}
    )
    verdict, events, reason, _m = await run_agent_v2(llm, sandbox, "http://t", None)
    assert reason == "completed"
    assert verdict is not None and verdict.findings[0].evidence == [1]
    dir_events = [e for e in events if e.tag == "direction"]
    assert len(dir_events) == 1
    assert "切换" in dir_events[0].text
    assert "初始" in dir_events[0].text and "xss-stored" in dir_events[0].text
    assert dir_events[0].direction_id == "dir-001"
    # 切换后 exec 事件归属新方向
    exec_events = [e for e in events if e.cmd]
    assert exec_events[0].direction_id is None  # 切换前的自然探索
    assert exec_events[1].direction_id == "dir-001"


async def test_cheat_sheet_injected_after_switch():
    """切换后注入的第一条 user 消息是速查卡。"""
    llm = RecordingLLM(
        [
            exec_resp("curl http://t/", "c1"),
            switch_resp("sqli-blind", "登录框存在盲注", "s1"),
            report_resp(VALID_REPORT, "r1"),
        ]
    )
    await run_agent_v2(llm, FakeSandbox({"curl http://t/": "ok"}), "http://t", None)
    # calls[2] 是切换之后的第一次 complete
    texts = user_texts(llm, 2)
    assert texts and "速查卡" in texts[-1]
    # 切换前不应已注入
    assert not any("速查卡" in t for t in user_texts(llm, 1))


async def test_direction_budget_reminder():
    """方向预算尽 → 注入提醒（不强制切，模型可提交）。"""
    llm = RecordingLLM(
        [
            switch_resp("sqli", "假设有注入", "s1"),
            exec_resp("curl http://t/a", "c1"),
            exec_resp("curl http://t/b", "c2"),
            report_resp(VALID_REPORT, "r1"),
        ]
    )
    sandbox = FakeSandbox({"curl http://t/a": "A [request_id=1]", "curl http://t/b": "B [request_id=2]"})
    _verdict, _events, reason, _m = await run_agent_v2(
        llm, sandbox, "http://t", None, max_steps=10, max_steps_per_direction=1
    )
    assert reason == "completed"
    assert "预算" in user_texts(llm, 2)[-1]  # 第二次 exec 后的下次调用已带提醒
    assert any("预算" in t for t in user_texts(llm, 3))


async def test_max_steps_per_direction_zero_boundary():
    """max_steps_per_direction=0：方向内第一步即预算尽。"""
    llm = RecordingLLM(
        [
            switch_resp("sqli", "假设", "s1"),
            exec_resp("curl http://t/a", "c1"),
            report_resp(VALID_REPORT, "r1"),
        ]
    )
    await run_agent_v2(
        llm,
        FakeSandbox({"curl http://t/a": "A [request_id=1]"}),
        "http://t",
        None,
        max_steps_per_direction=0,
    )
    assert "预算" in user_texts(llm, 2)[-1]


async def test_no_progress_soft_then_forced_switch_hint():
    """无进展：首次软提醒，再犯强制 SWITCH 提示。"""
    llm = RecordingLLM(
        [
            switch_resp("sqli", "假设", "s1"),
            exec_resp("curl http://t/x", "c1"),
            exec_resp("curl http://t/x", "c2"),
            exec_resp("curl http://t/x", "c3"),  # 第 3 次相同命令 → 软提醒
            exec_resp("curl http://t/x", "c4"),  # 再犯 → 强制
            report_resp(VALID_REPORT, "r1"),
        ]
    )
    sandbox = FakeSandbox({"curl http://t/x": ""})  # 空 stdout：仅命中重复命令规则
    verdict, _events, reason, _m = await run_agent_v2(llm, sandbox, "http://t", None)
    assert reason == "completed"  # v2 不硬切，模型可自纠后提交
    assert verdict is not None
    assert "连续无新信息" in user_texts(llm, 4)[-1]
    assert "必须立即调用 switch_direction" in user_texts(llm, 5)[-1]


async def test_step_cap_direction_summary_and_forced_report():
    """触顶强制收卷：含方向汇总事件（含 outcome 轨迹），reason=step_cap。"""
    llm = RecordingLLM(
        [
            switch_resp("sqli", "假设注入", "s1"),
            switch_resp("xss", "假设存储型", "s2", outcome="sqli 排除：参数化查询"),
            exec_resp("curl http://t/a", "c1"),
            exec_resp("curl http://t/b", "c2"),
            # 强制收卷轮
            report_resp({"findings": [], "discarded": []}, "r9"),
        ]
    )
    sandbox = FakeSandbox({"curl http://t/a": "A [request_id=1]", "curl http://t/b": "B [request_id=2]"})
    verdict, events, reason, _m = await run_agent_v2(llm, sandbox, "http://t", None, max_steps=4)
    assert reason == "step_cap"
    assert verdict is not None and verdict.findings == []
    summary = [e for e in events if e.tag == "direction" and "方向汇总" in e.text]
    assert len(summary) == 1
    assert "方向汇总: 2 个" in summary[0].text
    assert "concluded" in summary[0].text and "sqli 排除" in summary[0].text


async def test_submit_directly_returns_v1_compat():
    """不经过任何方向直接 submit → v1 兼容路径正常返回。"""
    llm = RecordingLLM([report_resp(VALID_REPORT, "r1")])
    verdict, events, reason, _m = await run_agent_v2(llm, FakeSandbox({}), "http://t", None)
    assert reason == "completed"
    assert verdict is not None
    assert [e.tag for e in events] == ["conclude"]
    assert all(e.direction_id is None for e in events)
