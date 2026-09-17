"""薄 FastAPI 后端：创建会话、SSE 事件流、report。"""

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.exporters import dump_html, dump_messages, dump_sarif, load_jsonl
from server.sessions import create_session, get

app = FastAPI(title="vulnhound")


class CreateBody(BaseModel):
    address: str
    auth: dict | None = None
    loop_version: str = "v2"


@app.post("/api/sessions")
async def post_session(body: CreateBody):
    if body.loop_version not in ("v1", "v2"):
        raise HTTPException(status_code=422, detail="loop_version must be 'v1' or 'v2'")
    try:
        session_id = await create_session(body.address, body.auth, body.loop_version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session = get(session_id)
    assert session is not None
    asyncio.create_task(session.run())
    return {"session_id": session_id}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    session = get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session.meta


@app.get("/api/sessions/{session_id}/events")
async def get_events(session_id: str):
    session = get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return StreamingResponse(
        _sse(session), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


async def _sse(session):
    while True:
        event = await session.queue.get()
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        if event.get("type") == "done":
            break


@app.get("/api/sessions/{session_id}/report")
async def get_report(session_id: str):
    session = get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if not session.report_path.exists():
        raise HTTPException(status_code=409, detail="session still running")
    return json.loads(session.report_path.read_text(encoding="utf-8"))


@app.get("/api/sessions/{session_id}/events.jsonl")
async def get_events_file(session_id: str):
    """V3：完整事件文件（步进审查数据源，区别于 SSE 实时流）。"""
    session = get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    path = session.dir / "events.jsonl"
    if not path.exists():
        raise HTTPException(status_code=404, detail="events not found")
    return Response(path.read_text(encoding="utf-8"), media_type="application/x-ndjson")


class ReplayBody(BaseModel):
    request_id: int
    overrides: dict | None = None


@app.post("/api/sessions/{session_id}/replay")
async def post_replay(session_id: str, body: ReplayBody):
    """V3：从 proxy.jsonl 取原始请求重发（不写留痕），返回新旧响应 diff。"""
    session = get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    records = load_jsonl(session.dir / "proxy.jsonl")
    rec = next((r for r in records if r.get("request_id") == body.request_id), None)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"request_id {body.request_id} not found")
    if rec.get("blocked"):
        raise HTTPException(status_code=422, detail="blocked request is not replayable")

    ov = body.overrides or {}
    method = str(ov.get("method", rec["method"]))
    url = str(ov.get("url", rec["url"]))
    req_body = ov.get("body", rec.get("req_body") or None)
    if req_body is not None and not isinstance(req_body, str):
        req_body = json.dumps(req_body, ensure_ascii=False)

    # 白名单：只允许重放到该会话登记的目标（含 scheme/host/port 完全匹配）
    target = (session.meta["host"], session.meta["port"], session.meta["scheme"])
    parts = urlsplit(url)
    if (parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), parts.scheme) != target:
        raise HTTPException(status_code=422, detail="replay target must match session target")

    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=False, timeout=15) as client:
            resp = await client.request(method, url, content=req_body)
        replayed = {
            "status": resp.status_code,
            "resp_body": resp.text[:65536],
        }
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"replay failed: {type(exc).__name__}") from exc

    original = {"status": rec.get("status"), "resp_body": rec.get("resp_body", "")}
    return {
        "original": original,
        "replayed": replayed,
        "diff": {
            "status_changed": original["status"] != replayed["status"],
            "body_changed": original["resp_body"] != replayed["resp_body"],
        },
    }


@app.get("/api/sessions/{session_id}/export")
async def get_export(session_id: str, format: str):
    """V3：独立单文件导出 messages|sarif|html，不打包。"""
    session = get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    sid = session.session_id
    if format == "messages":
        path = session.dir / "messages.jsonl"
        if not path.exists():
            raise HTTPException(status_code=409, detail="messages not available (session failed or running)")
        return Response(
            dump_messages(load_jsonl(path)),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{sid}.messages.jsonl"'},
        )
    if format not in ("sarif", "html"):
        raise HTTPException(status_code=422, detail="format must be messages|sarif|html")
    if not session.report_path.exists():
        raise HTTPException(status_code=409, detail="session still running")
    report = json.loads(session.report_path.read_text(encoding="utf-8"))
    if format == "sarif":
        return Response(
            dump_sarif(report),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{sid}.sarif"'},
        )
    return Response(
        dump_html(report),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{sid}.html"'},
    )


WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
