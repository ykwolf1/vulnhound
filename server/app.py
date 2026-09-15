"""薄 FastAPI 后端：创建会话、SSE 事件流、report。"""

import asyncio
import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from server.sessions import create_session, get

app = FastAPI(title="vulnhound")


class CreateBody(BaseModel):
    address: str
    auth: dict | None = None


@app.post("/api/sessions")
async def post_session(body: CreateBody):
    try:
        session_id = await create_session(body.address, body.auth)
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
