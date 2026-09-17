"""V2 双层循环：外层方向管理（switch_direction）+ 内层假设驱动 ReAct。

行为契约见 docs/plans/2026-09-16-v2-dual-loop.md Task 2 与 docs/spec-v2.md §3.3。
"""

import asyncio
import logging

from pydantic import ValidationError

logger = logging.getLogger("svh.loop_v2")

from agent.llm import LLMError
from agent.loop import StepEvent, _assistant_msg, _infer_tag
from agent.progress import ProgressTracker
from agent.prompt import (
    CHEAT_SHEET,
    DIRECTION_SECTION,
    EXEC_TOOL,
    REPORT_TOOL,
    SWITCH_DIRECTION_TOOL,
    build_system_prompt,
)
from agent.schema import Direction, Verdict

__all__ = ["run_agent_v2"]


async def run_agent_v2(
    llm,
    sandbox,
    target_url: str,
    creds: dict | None,
    max_steps: int = 100,
    max_steps_per_direction: int = 15,
) -> tuple[Verdict | None, list[StepEvent], str]:
    system = build_system_prompt(target_url, creds) + "\n" + DIRECTION_SECTION
    messages: list[dict] = [{"role": "system", "content": system}]
    events: list[StepEvent] = []
    directions: list[Direction] = []
    current: Direction | None = None
    next_dir_seq = 1
    n = 0
    progress = ProgressTracker()
    no_progress_strikes = 0  # 连续无进展次数：首次提醒，再犯强制
    report_retried = False

    def _new_direction(name: str, hypothesis: str) -> Direction:
        nonlocal next_dir_seq
        d = Direction(id=f"dir-{next_dir_seq:03d}", name=name, hypothesis=hypothesis)
        next_dir_seq += 1
        return d

    while n < max_steps:
        n += 1
        try:
            resp = await llm.complete(messages, tools=[EXEC_TOOL, REPORT_TOOL, SWITCH_DIRECTION_TOOL])
        except LLMError as exc:
            # P3：归因落盘（llm_error:timeout / llm_error:http_429 ...），评测报告可据此分桶
            return None, events, f"llm_error:{exc.kind}", messages

        if not resp.tool_calls:
            messages.append({"role": "assistant", "content": resp.content or ""})
            messages.append(
                {
                    "role": "user",
                    "content": "请通过工具调用行动：execute_command 执行命令，switch_direction 切换方向，或 submit_report 提交报告。",
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
                        direction_id=current.id if current else None,
                    )
                )
                messages.append(
                    {"role": "tool", "tool_call_id": cid, "content": (events[-1].result or "")[:8192]}
                )
                if current is not None:
                    current.steps_used += 1
                # 无进展检测：给模型一次自纠机会，再犯则强制切换提示
                made_progress = not progress.update(cmd, stdout)
                if made_progress:
                    no_progress_strikes = 0
                elif current is not None:
                    no_progress_strikes += 1
                    if no_progress_strikes == 1:
                        messages.append(
                            {
                                "role": "user",
                                "content": "连续无新信息。请立即 switch_direction 切换方向（并用 current_outcome 记录结论），或 submit_report 提交已有发现。",
                            }
                        )
                    else:
                        messages.append(
                            {
                                "role": "user",
                                "content": "再次无进展。必须立即调用 switch_direction：结束当前方向并开启新方向，不要再重复同类命令。",
                            }
                        )
                else:
                    # 无 current：自然探索阶段，不视为方向内停滞
                    pass

            elif name == "switch_direction":
                if current is not None:
                    outcome = args.get("current_outcome")
                    current.outcome = str(outcome) if outcome else None
                    current.status = "concluded" if current.outcome else "abandoned"
                    directions.append(current)
                old_name = current.name if current else None
                current = _new_direction(
                    name=str(args.get("next_direction", "unnamed")),
                    hypothesis=str(args.get("hypothesis", "")),
                )
                # 切换检查点：注入速查卡（防长对话遗忘纪律）+ 方向事件
                messages.append({"role": "tool", "tool_call_id": cid, "content": "方向已切换。"})
                messages.append({"role": "user", "content": CHEAT_SHEET})
                events.append(
                    StepEvent(
                        n=n,
                        tag="direction",
                        text=f"切换: {old_name or '初始'}→{current.name}",
                        direction_id=current.id,
                    )
                )
                progress.reset()
                no_progress_strikes = 0

            elif name == "submit_report":
                try:
                    verdict = Verdict.model_validate(args)
                    if verdict.findings and not all(f.evidence_ids for f in verdict.findings):
                        raise ValueError("每条 finding 的 evidence 不能为空")
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

        # 方向预算：预算尽则每步（且仅一次）注入提醒，由模型选择 switch 或 submit
        if current is not None and current.steps_used >= max_steps_per_direction and not current.budget_reminded:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"提醒：方向「{current.name}」的步数预算（{max_steps_per_direction}）已用尽。"
                        "请 switch_direction 切换方向，或 submit_report 提交报告。"
                    ),
                }
            )
            current.budget_reminded = True

        # 总预算 70% 催促（同 MVP）
        if n == int(max_steps * 0.7):
            messages.append(
                {
                    "role": "user",
                    "content": f"提醒：步数预算已用 {n}/{max_steps}。请优先完成高价值验证并尽快 submit_report（发现多少报多少）。",
                }
            )

    # 触顶：方向汇总事件 + 强制收卷轮（同 MVP：只给 submit_report）
    total_directions = len(directions) + (1 if current else 0)
    summary = "; ".join(
        f"{d.name}({d.status}{': ' + d.outcome if d.outcome else ''})" for d in directions
    ) + (f"; {current.name}(exploring)" if current else "")
    events.append(
        StepEvent(n=n + 1, tag="direction", text=f"方向汇总: {total_directions} 个 — {summary}")
    )
    events.append(StepEvent(n=n + 1, tag="conclude", text="budget_exhausted_forcing_report", result=None))
    messages.append(
        {
            "role": "user",
            "content": "步数预算已耗尽。请立即调用 submit_report 提交你当前已有的全部发现（可为空列表，但必须提交）。",
        }
    )
    resp = None
    for attempt in range(2):
        try:
            resp = await llm.complete(messages, tools=[REPORT_TOOL])
            break
        except LLMError as exc:
            if attempt == 0:
                # P3 兜底：长对话请求偶发失败时不放弃整场——截断历史（system + 首条 + 最近 12 条）再试
                logger.warning("forced-report LLMError (%s): retrying with truncated history", exc.kind)
                system, rest = messages[0], messages[1:]
                messages = [system, *rest] if len(rest) <= 12 else [system, *rest[:1], {"role": "user", "content": f"（中间过程已省略，共 {len(rest) - 1} 条）"}, *rest[-11:]]
                continue
            logger.warning("forced-report LLMError after truncation (%s)", exc.kind)
            return None, events, f"llm_error:{exc.kind}", messages
    if resp.tool_calls and resp.tool_calls[0]["name"] == "submit_report":
        try:
            verdict = Verdict.model_validate(resp.tool_calls[0]["arguments"])
            # 强制收卷同样执行证据契约：无证据的 finding 剔除（保留 discarded 供用户知情）
            if verdict.findings:
                dropped = [f for f in verdict.findings if not f.evidence_ids]
                if dropped:
                    verdict.findings = [f for f in verdict.findings if f.evidence_ids]
                    for f in dropped:
                        verdict.discarded.append(
                            type(f)(title=f.title, reason="强制收卷时无证据引用，已按契约剔除")
                        )
        except ValidationError:
            return None, events, "llm_error", messages
        events.append(StepEvent(n=n + 1, tag="conclude", text="submit_report(forced)", result=None))
        return verdict, events, "step_cap", messages  # 诚实标注：靠强制收卷获得的报告

    return None, events, "step_cap", messages
