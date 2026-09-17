"""V3 导出：messages JSONL / SARIF / HTML，纯函数，各自独立单文件。"""

import html
import json
from typing import Any

__all__ = ["dump_messages", "dump_sarif", "dump_html"]

_SEVERITY_LEVEL = {"high": "error", "medium": "warning", "low": "note"}


def dump_messages(messages: list[dict]) -> str:
    """每行一个 OpenAI message 对象。"""
    return "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)


def dump_sarif(report: dict) -> str:
    """report.json → SARIF 2.1.0；每条 finding 一个 result。"""
    verdict = report.get("verdict") or {}
    findings = verdict.get("findings") or []
    results = []
    for f in findings:
        evidence_ids = ", ".join(f"request_id={rid}" for rid in f.get("evidence", []))
        results.append(
            {
                "ruleId": f.get("title", "unknown"),
                "level": _SEVERITY_LEVEL.get(f.get("severity", "low"), "note"),
                "message": {
                    "text": f["rationale"] + (f"（证据：{evidence_ids}）" if evidence_ids else "（无证据引用）")
                },
                "locations": [
                    {
                        "physicalLocation": {
                            "address": {
                                "fullyQualifiedHostName": report.get("target", {}).get("host", "")
                            }
                        }
                    }
                ],
                "properties": {"evidence_request_ids": f.get("evidence", [])},
            }
        )
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "vulnhound", "informationUri": "https://github.com/ykwolf1/vulnhound"}},
                "results": results,
            }
        ],
    }
    return json.dumps(sarif, ensure_ascii=False, indent=2)


def dump_html(report: dict) -> str:
    """自包含文字版报告：无外部资源，可直接分享/归档。"""
    verdict = report.get("verdict") or {}
    findings = verdict.get("findings") or []
    discarded = verdict.get("discarded") or []
    target = report.get("target", {})
    stats = report.get("stats", {})
    esc = html.escape

    def _finding_row(i: int, f: dict) -> str:
        ev = " ".join(f"#{rid}" for rid in f.get("evidence", [])) or "—"
        return (
            f"<tr><td>{i}</td><td>{esc(f.get('title', ''))}</td>"
            f"<td>{esc(f.get('severity', ''))}</td><td>{esc(f.get('rationale', ''))}</td><td>{ev}</td></tr>"
        )

    def _discarded_row(d: dict) -> str:
        return f"<li><b>{esc(d.get('title', ''))}</b>：{esc(d.get('reason', ''))}</li>"

    findings_html = (
        "<table><tr><th>#</th><th>标题</th><th>严重度</th><th>依据</th><th>证据 (request_id)</th></tr>"
        + "".join(_finding_row(i, f) for i, f in enumerate(findings, 1))
        + "</table>"
        if findings
        else "<p>无发现。</p>"
    )
    discarded_html = (
        "<ul>" + "".join(_discarded_row(d) for d in discarded) + "</ul>" if discarded else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>vulnhound 报告 {esc(str(report.get('finished_at', '')))}</title>
<style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;vertical-align:top}}
th{{background:#f5f5f5}}.sev{{font-weight:600}}h2{{border-bottom:2px solid #1a1a1a;padding-bottom:.3rem}}
</style></head><body>
<h1>vulnhound 漏洞扫描报告</h1>
<p><b>目标</b>：{esc(target.get('address', ''))}（{esc(target.get('host', ''))}:{target.get('port', '')}）<br>
<b>结束原因</b>：{esc(report.get('stopped_reason', ''))} &nbsp; <b>时间</b>：{esc(report.get('finished_at', ''))}<br>
<b>统计</b>：流量 {stats.get('flows', 0)} 条 / 拦截 {stats.get('requests_blocked', 0)} 条 / 步数 {stats.get('steps', 0)}</p>
<h2>发现（{len(findings)}）</h2>
{findings_html}
{f"<h2>已排除的疑似</h2><ul>{discarded_html}</ul>" if discarded_html else ""}
<p><small>证据 request_id 对应会话 proxy.jsonl 留痕，可在步进回放中查看原始请求/响应。</small></p>
</body></html>"""


def load_jsonl(path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
