"""LLM 驱动的 agent 循环：tool calling → 沙箱执行 → 回喂 → 提交报告。"""

import difflib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from agent.llm import LLMError
from agent.prompt import EXEC_TOOL, REPORT_TOOL, build_system_prompt
from agent.schema import Verdict

__all__ = ["StepEvent", "run_agent"]

Tag = Literal["observe", "act", "hypo", "verify", "conclude"]
_PROBE_HINTS = ("curl", "nmap", "nikto", "gobuster", "wget", "sqlmap")


@dataclass
class StepEvent:
    n: int
    tag: Tag
    text: str
    cmd: str | None = None
    result: str | None = None


def _infer_tag(cmd: str) -> Tag:
    return "act" if any(h in cmd for h in _PROBE_HINTS) else "observe"


def _assistant_msg(resp_tool_calls: list[dict]) -> dict:
    return {
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
    recent_cmds: list[str] = []
    last_stdout: str | None = None
    report_retried = False

    while n < max_steps:
        n += 1
        try:
            resp = await llm.complete(messages, tools=[EXEC_TOOL, REPORT_TOOL])
        except LLMError:
            return None, events, "llm_error"

        if not resp.tool_calls:
            messages.append({"role": "assistant", "content": resp.content or ""})
            messages.append(
                {
                    "role": "user",
                    "content": "请通过工具调用行动：execute_command 执行命令，或 submit_report 提交报告。",
                }
            )
            continue

        messages.append(_assistant_msg(resp.tool_calls))

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
                recent_cmds.append(cmd)
                if len(recent_cmds) >= 3 and len(set(recent_cmds[-3:])) == 1:
                    return None, events, "no_progress"
                if (
                    last_stdout is not None
                    and stdout.strip()
                    and last_stdout.strip()
                    and difflib.SequenceMatcher(None, last_stdout, stdout).ratio() > 0.9
                ):
                    return None, events, "no_progress"
                last_stdout = stdout

            elif name == "submit_report":
                try:
                    verdict = Verdict.model_validate(args)
                    if verdict.findings and not all(f.evidence for f in verdict.findings):
                        raise ValueError("每条 finding 的 evidence 不能为空")
                except (ValidationError, ValueError) as exc:
                    if report_retried:
                        return None, events, "llm_error"
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
                return verdict, events, "completed"

            else:
                messages.append({"role": "tool", "tool_call_id": cid, "content": f"未知工具: {name}"})

    return None, events, "step_cap"
