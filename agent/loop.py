"""LLM 驱动的 agent 循环：tool calling → 沙箱执行 → 回喂 → 提交报告。"""

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from agent.llm import LLMError
from agent.progress import ProgressTracker
from agent.prompt import EXEC_TOOL, REPORT_TOOL, build_system_prompt
from agent.schema import Verdict, Evidence

__all__ = ["StepEvent", "run_agent"]

Tag = Literal["observe", "act", "hypo", "verify", "conclude", "direction"]
_PROBE_HINTS = ("curl", "nmap", "nikto", "gobuster", "wget", "sqlmap")


@dataclass
class StepEvent:
    n: int
    tag: Tag
    text: str
    cmd: str | None = None
    result: str | None = None
    direction_id: str | None = None  # v2：产生该事件的方向（v1 恒为 None）


def _infer_tag(cmd: str) -> Tag:
    return "act" if any(h in cmd for h in _PROBE_HINTS) else "observe"


def _assistant_msg(resp_tool_calls: list[dict], reasoning_content: str | None = None) -> dict:
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
            }
            for c in resp_tool_calls
        ],
    }
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
    return msg


async def run_agent(
    llm,
    sandbox,
    target_url: str,
    creds: dict | None,
    max_steps: int = 20,
) -> tuple[Verdict | None, list[StepEvent], str]:
    messages: list[dict] = [{"role": "system", "content": build_system_prompt(target_url, creds)}]
    events: list[StepEvent] = []
    n = 0
    progress = ProgressTracker()
    report_retried = False

    while n < max_steps:
        n += 1
        try:
            resp = await llm.complete(messages, tools=[EXEC_TOOL, REPORT_TOOL])
        except LLMError as exc:
            return None, events, f"llm_error:{exc.kind}", messages

        if not resp.tool_calls:
            messages.append({"role": "assistant", "content": resp.content or ""})
            messages.append(
                {
                    "role": "user",
                    "content": "请通过工具调用行动：execute_command 执行命令，或 submit_report 提交报告。",
                }
            )
            continue

        messages.append(_assistant_msg(resp.tool_calls, resp.reasoning_content))

        for call in resp.tool_calls:
            name, args, cid = call["name"], call["arguments"], call["id"]

            if name == "execute_command":
                cmd = str(args.get("cmd", ""))
                result = await sandbox.exec(cmd)
                stdout = result.stdout
                events.append(
                    StepEvent(
                        n=n,
                        tag=_infer_tag(cmd),
                        text=f"exec: {cmd}",
                        cmd=cmd,
                        result=stdout + (f"\n{result.stderr}" if result.stderr else ""),
                    )
                )
                messages.append(
                    {"role": "tool", "tool_call_id": cid, "content": events[-1].result}
                )
                # 无进展检测
                if progress.update(cmd, stdout):
                    return None, events, "no_progress", messages

            elif name == "submit_report":
                try:
                    verdict = Verdict.model_validate(args)
                    if verdict.findings and not all(f.evidence_ids for f in verdict.findings):
                        raise ValueError("每条 finding 的 evidence 不能为空")
                    # P2b：why 必须说明"响应内容证明了什么"，空泛引用打回重报
                    _weak = [f.title for f in verdict.findings
                             if any(isinstance(e, Evidence) and len(e.why.strip()) < 10 for e in f.evidence)]
                    if _weak:
                        raise ValueError(
                            "以下 finding 的 evidence 缺少 why 或 why 过于空泛（需≥10字，说明该请求的响应内容证明了什么）：" + "、".join(_weak)
                        )
                except (ValidationError, ValueError) as exc:
                    if report_retried:
                        return None, events, "llm_error", messages
                    report_retried = True
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": cid,
                            "content": f"报告校验失败：{exc}。请修正后重新调用 submit_report。",
                        }
                    )
                    continue
                events.append(StepEvent(n=n, tag="conclude", text="submit_report", result=None))
                return verdict, events, "completed", messages

            else:
                messages.append({"role": "tool", "tool_call_id": cid, "content": f"未知工具: {name}"})

        # 预算提醒：70% 时注入一次催促
        if n == int(max_steps * 0.7):
            messages.append(
                {
                    "role": "user",
                    "content": f"提醒：步数预算已用 {n}/{max_steps}。请优先完成高价值验证并尽快 submit_report（发现多少报多少）。",
                }
            )

    # 触顶后强制收卷轮：只给 submit_report 工具，迫使模型交出当前已有发现
    events.append(StepEvent(n=n + 1, tag="conclude", text="budget_exhausted_forcing_report", result=None))
    messages.append(
        {
            "role": "user",
            "content": "步数预算已耗尽。请立即调用 submit_report 提交你当前已有的全部发现（可为空列表，但必须提交）。",
        }
    )
    try:
        resp = await llm.complete(messages, tools=[REPORT_TOOL])
    except LLMError as exc:
        return None, events, f"llm_error:{exc.kind}", messages
    if resp.tool_calls and resp.tool_calls[0]["name"] == "submit_report":
        try:
            verdict = Verdict.model_validate(resp.tool_calls[0]["arguments"])
        except ValidationError:
            return None, events, "llm_error", messages
        events.append(StepEvent(n=n + 1, tag="conclude", text="submit_report(forced)", result=None))
        return verdict, events, "step_cap", messages  # 诚实标注：靠强制收卷获得的报告

    return None, events, "step_cap", messages
