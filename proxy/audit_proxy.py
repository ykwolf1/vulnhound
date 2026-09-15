"""审计代理：三元组（host, port, scheme）白名单 + JSONL 全量记录。

MVP 仅支持 HTTP（绝对 URL 形式的代理请求）；CONNECT 一律 403。
"""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

AuditTarget = tuple[str, int, str]  # (host, port, scheme)

REQ_BODY_LIMIT = 4 * 1024
RESP_BODY_LIMIT = 64 * 1024

_BLOCKED_BODY = b"blocked by proxy\n"
_BLOCKED_RESPONSE = (
    b"HTTP/1.1 403 Forbidden\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Length: " + str(len(_BLOCKED_BODY)).encode() + b"\r\n"
    b"Connection: close\r\n\r\n" + _BLOCKED_BODY
)


@dataclass
class _RequestState:
    request_id: int
    ts: str
    method: str = ""
    url: str = ""
    status: int | None = None
    req_body: str = ""
    resp_body: str = ""
    blocked: bool = False
    block_reason: str | None = None


@dataclass
class AuditProxy:
    """出口审计代理：只放行 allowed 三元组，其余 403，全部记 JSONL。"""

    allowed: AuditTarget
    record_path: Path
    on_record: Callable[[dict], None] | None = field(default=None, repr=False)
    _server: asyncio.AbstractServer | None = field(default=None, repr=False)
    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _next_id: int = field(default=0, repr=False)

    async def start(self) -> int:
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        self.record_path.touch()
        self._next_id = 0
        self._client = httpx.AsyncClient()
        self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- internals ----

    def _record(self, st: _RequestState) -> None:
        record = {
            "request_id": st.request_id,
            "ts": st.ts,
            "method": st.method,
            "url": st.url,
            "status": st.status,
            "req_body": st.req_body[:REQ_BODY_LIMIT],
            "resp_body": st.resp_body[:RESP_BODY_LIMIT],
            "blocked": st.blocked,
            "block_reason": st.block_reason,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self.record_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.on_record is not None:
            self.on_record(record)

    def _new_state(self) -> _RequestState:
        self._next_id += 1
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return _RequestState(request_id=self._next_id, ts=ts)

    async def _reject(self, writer: asyncio.StreamWriter, st: _RequestState) -> None:
        self._record(st)
        writer.write(_BLOCKED_RESPONSE)
        await writer.drain()
        writer.close()

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError) as exc:
            st = self._new_state()
            st.blocked, st.block_reason = True, f"read_error:{type(exc).__name__}"
            self._record(st)
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        st = self._new_state()
        try:
            method, target, _version = lines[0].split(" ", 2)
        except ValueError:
            st.blocked, st.block_reason = True, "malformed_request_line"
            await self._reject(writer, st)
            return
        st.method = method

        if method == "CONNECT":
            st.blocked, st.block_reason = True, "CONNECT not supported (HTTP only)"
            await self._reject(writer, st)
            return

        parts = urlsplit(target)
        if not parts.hostname:
            st.blocked, st.block_reason = True, "non-absolute request target"
            await self._reject(writer, st)
            return
        host, port, scheme = parts.hostname, parts.port or 80, parts.scheme or "http"
        st.url = target

        if (host, port, scheme) != tuple(self.allowed):
            st.blocked = True
            st.block_reason = f"target {host}:{port}:{scheme} not in whitelist"
            await self._reject(writer, st)
            return

        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip().lower() not in ("host", "proxy-connection", "connection"):
                    headers[k.strip()] = v.strip()

        body = b""
        if (cl := headers.get("Content-Length")) is not None:
            body = await reader.readexactly(int(cl))
        st.req_body = body.decode("utf-8", errors="replace")

        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        assert self._client is not None
        try:
            resp = await self._client.request(
                method,
                self._build_url(scheme, host, port, path),
                headers=headers,
                content=body,
            )
        except httpx.HTTPError:
            st.blocked, st.block_reason = True, "upstream request failed"
            await self._reject(writer, st)
            return
        st.status = resp.status_code
        st.resp_body = resp.text

        out_head = (
            f"HTTP/1.1 {resp.status_code} {resp.reason_phrase}\r\n"
            f"Content-Length: {len(resp.content)}\r\n"
            "Connection: close\r\n"
        )
        for k, v in resp.headers.items():
            if k.lower() not in ("content-length", "connection", "transfer-encoding"):
                out_head += f"{k}: {v}\r\n"
        writer.write(out_head.encode("latin-1") + b"\r\n" + resp.content)
        await writer.drain()
        self._record(st)
        writer.close()

    @staticmethod
    def _build_url(scheme: str, host: str, port: int, path: str) -> str:
        default = 443 if scheme == "https" else 80
        authority = host if port == default else f"{host}:{port}"
        return f"{scheme}://{authority}{path}"
