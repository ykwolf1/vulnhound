"""会话目录管理：sessions/<id>/{meta.json,events.jsonl,proxy.jsonl,report.json}。

目录即状态：创建即建目录（status=running），agent 结束写 report.json。
"""

import asyncio
import logging

logger = logging.getLogger("svh.server")
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from agent.llm import DeepSeekLLM
from agent.loop import run_agent
from agent.loop_v2 import run_agent_v2
from agent.runner import Sandbox
from agent.schema import Verdict
from proxy.audit_proxy import AuditProxy

__all__ = ["SESSIONS_DIR", "Session", "create_session", "get"]

SESSIONS_DIR = Path("sessions")

# session_id -> Session 实例缓存：保证事件队列在请求之间共享（否则 SSE 消费不到 run() 的事件）。
_CACHE: dict = {}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


async def create_session(
    address: str, creds: dict | None, loop_version: str = "v2"
) -> str:
    """解析 address 建会话目录，返回 session_id。"""
    parts = urlsplit(address if "://" in address else f"http://{address}")
    scheme = parts.scheme or "http"
    host = parts.hostname
    if not host:
        raise ValueError(f"invalid address: {address!r}")
    port = parts.port or (443 if scheme == "https" else 80)

    session_id = f"{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
    d = SESSIONS_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": session_id,
        "address": address,
        "host": host,
        "port": port,
        "scheme": scheme,
        "creds": creds,
        "status": "running",
        "loop_version": loop_version,
        "created_at": _now(),
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    (d / "events.jsonl").touch()
    return session_id


class Session:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.dir = SESSIONS_DIR / session_id
        self.meta = json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        self.queue: asyncio.Queue = asyncio.Queue()

    @property
    def report_path(self) -> Path:
        return self.dir / "report.json"

    def _emit(self, event: dict) -> None:
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.queue.put_nowait(event)

    def _finish_meta(self, status: str, end_reason: str | None = None) -> None:
        self.meta["status"] = status
        if end_reason:
            self.meta["end_reason"] = end_reason
        self.meta["finished_at"] = _now()
        (self.dir / "meta.json").write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _write_report(
        self,
        verdict: Verdict | None,
        stopped_reason: str,
        stats: dict,
        notes: dict | None = None,
    ) -> None:
        report = {
            "verdict": verdict.model_dump() if verdict else None,
            "stopped_reason": stopped_reason,
            "target": {
                "address": self.meta["address"],
                "host": self.meta["host"],
                "port": self.meta["port"],
                "scheme": self.meta["scheme"],
            },
            "stats": stats,
            "notes": notes or {},
            "finished_at": _now(),
        }
        self.report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def run(self, time_budget: float = 1200) -> None:
        target = (self.meta["host"], self.meta["port"], self.meta["scheme"])
        record_path = self.dir / "proxy.jsonl"
        proxy = AuditProxy(allowed=target, record_path=record_path)
        proxy.on_record = lambda rec: self._emit({"type": "flow", **rec})
        proxy_port = await proxy.start()
        sandbox = Sandbox(proxy_port=proxy_port)
        try:
            api_key = os.environ.get("LLM_API_KEY")
            if not api_key:
                self._write_report(
                    None,
                    "missing_key",
                    {"flows": 0, "requests_blocked": 0, "steps": 0},
                )
                self._finish_meta("failed", end_reason="missing_key")
                self._emit({"type": "done", "stopped_reason": "missing_key", "status": "failed"})
                return

            await sandbox.start()
            llm = DeepSeekLLM(api_key=api_key)
            agent_fn = run_agent_v2 if self.meta.get("loop_version", "v2") == "v2" else run_agent
            try:
                verdict, events, stopped_reason, messages = await asyncio.wait_for(
                    agent_fn(
                        llm, sandbox, self.meta["address"], self.meta.get("creds"), max_steps=40
                    ),
                    timeout=time_budget,
                )
            except TimeoutError:
                # 时长预算（默认 20 分钟）耗尽：wait_for 取消后拿不到部分结果，
                # 已收集 events 为空——接受，如实标注 time_cap。
                logger.warning("agent time budget (%ss) exceeded", time_budget)
                self._write_report(
                    None, "time_cap",
                    {"flows": 0, "requests_blocked": 0, "steps": 0},
                )
                self._finish_meta("failed", end_reason="time_cap")
                self._emit({"type": "done", "stopped_reason": "time_cap", "status": "failed"})
                return
            except Exception:
                logger.exception("run_agent unexpected failure")
                self._write_report(
                    None, "internal_error",
                    {"flows": 0, "requests_blocked": 0, "steps": 0},
                )
                self._finish_meta("failed", end_reason="internal_error")
                self._emit({"type": "done", "stopped_reason": "internal_error", "status": "failed"})
                return
            # V3：完整对话落盘（训练数据/步进审查的数据源），逐行 JSONL
            with (self.dir / "messages.jsonl").open("w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")

            for ev in events:
                self._emit({"type": "step", **ev.__dict__})

            records = [
                json.loads(line)
                for line in record_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            stats = {
                "flows": len(records),
                "requests_blocked": sum(1 for r in records if r.get("blocked")),
                "steps": len(events),
            }

            notes: dict = {}
            if verdict is not None:
                known = {r["request_id"] for r in records}
                kept = [f for f in verdict.findings if set(f.evidence_ids) <= known]
                filtered = len(verdict.findings) - len(kept)
                if filtered:
                    verdict = Verdict(findings=kept, discarded=verdict.discarded)
                    notes["findings_filtered"] = filtered
                    notes["filter_reason"] = "evidence references request_id absent from proxy.jsonl"

            self._write_report(verdict, stopped_reason, stats, notes)
            status = "completed" if stopped_reason == "completed" else "failed"
            self._finish_meta(status, end_reason=None if status == "completed" else stopped_reason)
            self._emit({"type": "done", "stopped_reason": stopped_reason, "status": status})
        finally:
            await proxy.stop()
            await sandbox.stop()


def get(session_id: str) -> Session | None:
    """取会话（带实例缓存）；目录不存在或损坏 → None。"""
    cached = _CACHE.get(session_id)
    if cached is not None and cached.dir.exists():
        return cached
    _CACHE.pop(session_id, None)
    try:
        session = Session(session_id)
    except (OSError, json.JSONDecodeError, KeyError):
        return None
    _CACHE[session_id] = session
    return session
