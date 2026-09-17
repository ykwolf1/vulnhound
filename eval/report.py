"""报告聚合：JSON + Markdown。"""

import json
import secrets
import time
from pathlib import Path

__all__ = ["save_result", "render_markdown", "list_results", "load_result"]

RESULTS_DIR = Path(__file__).parent / "results"


def save_result(result: dict, results_dir: Path | None = None, owner: str | None = None) -> str:
    d = results_dir or RESULTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    if owner:
        result["owner"] = owner
    eid = time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2) + "-" + result.get("gt", "x")  # token_hex 防同秒覆盖
    (d / f"{eid}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / f"{eid}.md").write_text(render_markdown(result), encoding="utf-8")
    return eid


def render_markdown(result: dict) -> str:
    s = result["summary"]
    lines = [
        f"# 评测报告 {result.get('gt', '')} · {result['target']}",
        "",
        f"- 跑批：{result['runs']} 次，并发 {result['concurrency']}",
        f"- 完成率：{s['completed']}/{result['runs']}（{s['success_rate']}），平均耗时 {s['avg_elapsed_s']}s",
        f"- **查准率 avg precision：{s['avg_precision']}**",
        f"- **查全率 avg recall：{s['avg_recall']}**",
        f"- **证据有效率 avg：{s['avg_evidence_validity']}**",
        "",
        "| 会话 | 耗时s | 步数 | 流量 | findings | P | R | 证据有效率 | 结束原因 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for x in result["sessions"]:
        lines.append(
            f"| {x['session_id']} | {x['elapsed_s']} | {x['steps']} | {x['flows']} | {x['findings_total']} "
            f"| {x['precision']} | {x['recall']} | {x['evidence_validity']} | {x['stopped_reason']} |"
        )
    fps = [(x["session_id"], x["false_positive_list"]) for x in result["sessions"] if x["false_positive_list"]]
    if fps:
        lines += ["", "## 误报明细"]
        for sid, items in fps:
            for f in items:
                lines.append(f"- `{sid}` {f['title']} —— {f.get('rationale', '')}")
    missed = sorted({m for x in result["sessions"] for m in x["missed_modules"]})
    if missed:
        lines += ["", "## 常见漏报模块"] + [f"- {m}" for m in missed]
    return "\n".join(lines) + "\n"


def list_results(results_dir: Path | None = None, user: dict | None = None) -> list[dict]:
    """user 传入时按 V5 归属过滤：admin 全量；普通用户只看自己的（无归属的旧结果仅 admin 可见）。"""
    d = results_dir or RESULTS_DIR
    if not d.exists():
        return []
    is_admin = user is None or user.get("role") == "admin"
    out = []
    for p in sorted(d.glob("*.json"), reverse=True):
        data = json.loads(p.read_text(encoding="utf-8"))
        if not is_admin and data.get("owner") != user.get("username"):
            continue
        out.append({"eval_id": p.stem, "target": data["target"], "runs": data["runs"],
                    "owner": data.get("owner", "legacy"), "summary": data["summary"]})
    return out


def load_result(eval_id: str, results_dir: Path | None = None, user: dict | None = None) -> dict | None:
    p = (results_dir or RESULTS_DIR) / f"{eval_id}.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    if user is not None and user.get("role") != "admin" and data.get("owner") != user.get("username"):
        return None
    return data
