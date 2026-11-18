"""The operator side: HTTP commands and the WebSocket event stream on one port (default 8080).

  POST /api/v1/batch/start | batch/pause | batch/resume | batch/stop | config/inspection | mode | fault/clear
  GET  /api/v1/status
  GET  /api/v1/batch/report
  GET  /ws                       (WebSocket, gateway to dashboard only)

The dashboard is a web page, so every response carries CORS headers and OPTIONS requests are answered.
If a UI folder is given, its files are served from the same port (open http://<gateway-ip>:8080/).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Optional

from aiohttp import WSMsgType, web

from .engine import COMMAND_NAMES

if TYPE_CHECKING:
    from .app import Gateway

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
}


def dumps(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=CORS)
    try:
        response = await handler(request)
    except web.HTTPException as e:
        e.headers.update(CORS)
        raise
    response.headers.update(CORS)
    return response


def build_app(gw: "Gateway", ui_dir: Optional[str] = None) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])

    def reply(status: int, body: dict) -> web.Response:
        return web.Response(status=status, text=dumps(body), content_type="application/json")

    async def command(request: web.Request, name: str) -> web.Response:
        raw = await request.read()
        gw.log("OP-RX", f"POST {request.path} {raw.decode('utf-8', 'replace')}")
        try:
            body = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError):
            status, out = 400, {"ok": False, "cmd_id": None,
                                "error": {"code": "MALFORMED", "message": "the request body is not valid JSON"}}
        else:
            status, out = gw.engine.handle_command(name, body, gw.now())
        gw.log("OP-TX", f"{status} {dumps(out)}")
        return reply(status, out)

    async def status(request: web.Request) -> web.Response:
        return reply(200, gw.engine.status(gw.now()))

    async def report(request: web.Request) -> web.Response:
        code, out = gw.engine.get_report()
        return reply(code, out)

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        gw.log("SYS", f"WebSocket client connected from {request.remote}")
        queue.put_nowait(dumps(gw.engine.status(gw.now())))          # contract: a status immediately on connect
        fault = gw.engine.fault_message()
        if fault is not None:                                        # a late-joining dashboard still learns why
            queue.put_nowait(dumps(fault))
        gw.clients.add(queue)

        async def drain_incoming() -> None:                          # v1: the socket is one-way, anything received is ignored
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break

        reader_task = asyncio.ensure_future(drain_incoming())
        try:
            while not reader_task.done():
                try:
                    text = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                await ws.send_str(text)
        except (ConnectionError, RuntimeError):
            pass
        finally:
            gw.clients.discard(queue)
            reader_task.cancel()
            await ws.close()
            gw.log("SYS", "WebSocket client disconnected")
        return ws

    def make_handler(name: str):
        async def handler(request: web.Request) -> web.Response:
            return await command(request, name)
        return handler

    for name in sorted(COMMAND_NAMES):
        app.router.add_post("/api/v1/" + name, make_handler(name))
    app.router.add_get("/api/v1/status", status)
    app.router.add_get("/api/v1/batch/report", report)
    app.router.add_get("/ws", websocket)

    if ui_dir:
        ui_dir = os.path.abspath(ui_dir)

        async def index(request: web.Request) -> web.FileResponse:
            return web.FileResponse(os.path.join(ui_dir, "index.html"))

        app.router.add_get("/", index)
        app.router.add_static("/", ui_dir, follow_symlinks=False)    # added last so it never shadows the API
    return app
