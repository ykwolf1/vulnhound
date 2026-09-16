"""真实端到端测试（默认跳过，`pytest -m e2e` 运行）。

前置条件（缺一请在跑时自查）：
- DVWA 靶场可访问（默认 http://127.0.0.1:8080，可用 DVWA_ADDR 覆盖）
- docker 守护进程 + 已构建镜像 vh-agent
- 环境变量 LLM_API_KEY（缺失则 skip，不 fail）
"""

import asyncio
import json
import os
import time

import httpx
import pytest
from httpx import ASGITransport

from server.app import app
from server.sessions import SESSIONS_DIR, create_session, get

pytestmark = pytest.mark.e2e

POLL_INTERVAL = 5
MAX_WAIT = 480  # 最长 8 分钟


async def test_dvwa_end_to_end():
    if not os.environ.get("LLM_API_KEY"):
        pytest.skip("LLM_API_KEY not set; real e2e needs a live LLM key")
    dvwa_addr = os.environ.get("DVWA_ADDR", "http://127.0.0.1:8080")

    session_id = await create_session(dvwa_addr, None)
    session = get(session_id)
    assert session is not None
    run_task = asyncio.create_task(session.run())

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        report = None
        deadline = time.monotonic() + MAX_WAIT
        while time.monotonic() < deadline:
            resp = await client.get(f"/api/sessions/{session_id}/report")
            if resp.status_code == 200:
                report = resp.json()
                break
            assert resp.status_code == 409, f"unexpected report status: {resp.status_code}"
            await asyncio.sleep(POLL_INTERVAL)
        assert report is not None, f"report not ready within {MAX_WAIT}s"

    await run_task

    verdict = report["verdict"]
    assert verdict is not None, f"no verdict: stopped_reason={report['stopped_reason']}"
    assert verdict["findings"], "expected at least one finding"

    proxy_path = SESSIONS_DIR / session_id / "proxy.jsonl"
    records = [
        json.loads(line)
        for line in proxy_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    proxy_ids = {r["request_id"] for r in records}
    assert any(
        set(f["evidence"]) & proxy_ids for f in verdict["findings"]
    ), "no finding evidence references a request_id present in proxy.jsonl"

    assert report["stats"]["flows"] > 10, f"too few flows: {report['stats']}"
