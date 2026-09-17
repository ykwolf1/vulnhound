"""跑批与会话级收集：复用 server.sessions 的会话机制，LLM/沙箱可注入。"""

import asyncio
import json
import time

from eval.scoring import load_groundtruth, score_session

__all__ = ["run_once", "run_batch"]


async def run_once(target: str, creds: dict | None = None, loop_version: str = "v2") -> str:
    """发起一次会话并等它结束，返回 session_id。"""
    import server.sessions as sess

    session_id = await sess.create_session(target, creds, loop_version)
    session = sess.get(session_id)
    assert session is not None
    await session.run()
    return session_id


def load_session(sid: str, sessions_dir=None) -> tuple[dict, list[dict]]:
    """读会话的 report.json 与 proxy.jsonl。"""
    import server.sessions as sess

    base = (sessions_dir or sess.SESSIONS_DIR) / sid
    report = json.loads((base / "report.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (base / "proxy.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return report, records


async def run_batch(
    target: str,
    runs: int,
    concurrency: int = 1,
    creds: dict | None = None,
    gt_name: str = "dvwa",
    loop_version: str = "v2",
) -> dict:
    """N 次会话（K 并发）→ 聚合效果指标。"""
    gt = load_groundtruth(gt_name)
    sem = asyncio.Semaphore(concurrency)

    async def one():
        async with sem:
            t0 = time.monotonic()
            sid = await run_once(target, creds, loop_version)
            elapsed = round(time.monotonic() - t0, 1)
            report, records = load_session(sid)
            score = score_session(report, records, gt)
            return {
                "session_id": sid,
                "elapsed_s": elapsed,
                "steps": report.get("stats", {}).get("steps", 0),
                "flows": report.get("stats", {}).get("flows", 0),
                "stopped_reason": report.get("stopped_reason"),
                **score,
            }

    sessions = await asyncio.gather(*(one() for _ in range(runs)))
    completed = [s for s in sessions if s["stopped_reason"] == "completed"]
    precisions = [s["precision"] for s in sessions if s["precision"] is not None]
    recalls = [s["recall"] for s in sessions if s["recall"] is not None]
    validities = [s["evidence_validity"] for s in sessions if s["evidence_validity"] is not None]
    return {
        "target": target,
        "gt": gt_name,
        "runs": runs,
        "concurrency": concurrency,
        "sessions": sessions,
        "summary": {
            "completed": len(completed),
            "success_rate": round(len(completed) / runs, 3) if runs else None,
            "avg_elapsed_s": round(sum(s["elapsed_s"] for s in sessions) / runs, 1) if runs else None,
            "avg_precision": round(sum(precisions) / len(precisions), 3) if precisions else None,
            "avg_recall": round(sum(recalls) / len(recalls), 3) if recalls else None,
            "avg_evidence_validity": round(sum(validities) / len(validities), 3) if validities else None,
        },
    }
