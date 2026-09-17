"""打分纯函数：findings × proxy.jsonl × ground truth → precision/recall/证据有效率。

模块定位：从 evidence request_id 指向的 proxy 记录 URL 提取 DVWA 模块路径
（如 /vulnerabilities/sqli/），与 GT 条目按 module 匹配。发现声称的模块
由其 title/rationale 中出现的 GT 模块路径或 vuln_type 关键词判定。
"""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

__all__ = ["load_groundtruth", "module_of_url", "claim_modules", "score_session"]

_MODULE_RE = re.compile(r"/vulnerabilities/([a-z0-9_]+)/")


def load_groundtruth(name: str, gt_dir: Path | None = None) -> list[dict]:
    path = (gt_dir or Path(__file__).parent / "groundtruth") / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def module_of_url(url: str) -> str | None:
    """URL → /vulnerabilities/<module>/；无模块路径返回 None。"""
    m = _MODULE_RE.search(urlsplit(url).path)
    return m.group(0) if m else None


def claim_modules(finding: dict, gt: list[dict]) -> set[str]:
    """发现声称的 GT 模块集合：title/rationale 命中 GT 的 module 路径或 vuln_type。"""
    text = (finding.get("title", "") + " " + finding.get("rationale", "")).lower()
    claims = set()
    for g in gt:
        mod = g["module"]
        keyword = mod.rstrip("/").rsplit("/", 1)[-1].replace("_", " ")  # sqli_blind → sqli blind
        if mod.lower() in text or g["vuln_type"].lower() in text or keyword in text:
            claims.add(mod)
    return claims


def _evidence_ids(finding: dict) -> list[int]:
    """P2：evidence 兼容旧格式（int 列表）与新格式（{request_id, why} 对象列表）。"""
    out = []
    for e in finding.get("evidence") or []:
        out.append(e if isinstance(e, int) else e.get("request_id"))
    return [x for x in out if x is not None]


def score_session(report: dict, proxy_records: list[dict], gt: list[dict]) -> dict:
    """单会话打分。report 为 report.json 内容，proxy_records 为 proxy.jsonl 记录列表。"""
    verdict = report.get("verdict") or {}
    findings = verdict.get("findings") or []
    by_rid = {r["request_id"]: r for r in proxy_records}

    gt_modules = {g["module"] for g in gt}
    hit_modules: set[str] = set()
    true_positives: list[dict] = []
    false_positives: list[dict] = []

    exists_ok = pointed_ok = total_ev = 0
    for f in findings:
        claims = claim_modules(f, gt) & gt_modules
        claims_text = (f.get("title", "") + " " + f.get("rationale", "")).lower()
        f_evidence = _evidence_ids(f)
        # P2：有效性拆成两个指标——
        #   存在性：引用的 request_id 在留痕中存在；
        #   指向性：存在且 URL 模块 ∈ 声称模块，或 rationale 文本命中该模块关键词（跨模块发现放宽）。
        for rid in f_evidence:
            total_ev += 1
            rec = by_rid.get(rid)
            if rec is None:
                continue
            exists_ok += 1
            mod = module_of_url(rec.get("url", ""))
            if not claims or mod in claims or (mod and mod.rsplit("/", 2)[-2].replace("_", " ") in claims_text):
                pointed_ok += 1
        if claims:
            true_positives.append({"title": f.get("title"), "matched_modules": sorted(claims)})
            hit_modules |= claims
        else:
            false_positives.append({"title": f.get("title"), "rationale": f.get("rationale")})

    missed = sorted(gt_modules - hit_modules)
    n_tp, n_fp = len(true_positives), len(false_positives)
    return {
        "findings_total": len(findings),
        "true_positives": n_tp,
        "false_positives": n_fp,
        "precision": round(n_tp / (n_tp + n_fp), 3) if n_tp + n_fp else None,
        "recall": round(len(hit_modules) / len(gt_modules), 3) if gt_modules else None,
        "hit_modules": sorted(hit_modules),
        "missed_modules": missed,
        "evidence_total": total_ev,
        "evidence_exists": exists_ok,
        "evidence_pointed": pointed_ok,
        "evidence_existence": round(exists_ok / total_ev, 3) if total_ev else None,
        "evidence_validity": round(pointed_ok / total_ev, 3) if total_ev else None,
        "false_positive_list": false_positives,
    }
