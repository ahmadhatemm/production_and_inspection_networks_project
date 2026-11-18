"""Wires the engine to the real world: serial port, TCP server, HTTP/WebSocket server, clock, log files.

One asyncio thread runs everything. A 5 ms ticker polls the serial port and calls engine.tick(); TCP and HTTP
events arrive as callbacks on the same thread, so the engine is never entered twice at once.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from aiohttp import web

from . import serialproto as sp
from .config import Config
from .engine import Engine
from .evidence import Evidence
from .links import BServer, SerialLink
from .server import build_app, dumps


class _IO:
    """What the engine uses to reach the outside world (see the docstring of engine.py)."""

    def __init__(self, gw: "Gateway") -> None:
        self.gw = gw

    def send_a(self, frame: bytes) -> bool:
        return self.gw.serial.write(frame)

    def send_b(self, body: bytes) -> bool:
        return self.gw.bserver.send(body)

    def close_b(self) -> None:
        self.gw.bserver.close_current()

    def a_open(self) -> bool:
        return self.gw.serial.is_open

    def emit(self, msg: dict) -> None:
        self.gw.broadcast(msg)

    def log(self, tag: str, text: str) -> None:
        self.gw.evidence.log(tag, text)

    def wall_ms(self) -> int:
        return int(time.time() * 1000)


class Gateway:
    def __init__(self, cfg: Config, serial_port: Optional[str], ui_dir: Optional[str] = None,
                 log_dir: Optional[str] = None, verbose: bool = False, console: bool = True) -> None:
        self.cfg = cfg
        self.ui_dir = ui_dir
        self.t0 = time.monotonic()
        self.evidence = Evidence(log_dir, verbose=verbose, console=console)
        self.engine = Engine(cfg, _IO(self))
        self.serial = SerialLink(serial_port, cfg.serial_baud, self.log)
        self.bserver = BServer(cfg.b_host, cfg.b_port, cfg.tcp_max_body,
                               on_message=self._on_b_message, on_closed=self._on_b_closed, log=self.log)
        self.clients: set = set()                  # one queue per connected WebSocket
        self._runner: Optional[web.AppRunner] = None
        self._ticker: Optional[asyncio.Task] = None
        self.http_port = cfg.http_port             # the real port once started (useful when 0 was asked for)

    # ---- small helpers -----------------------------------------------------------

    def now(self) -> int:
        """Milliseconds on a monotonic clock, from the start of the gateway."""
        return int((time.monotonic() - self.t0) * 1000)

    def log(self, tag: str, text: str) -> None:
        self.evidence.log(tag, text)

    def broadcast(self, msg: dict) -> None:
        text = dumps(msg)
        for q in list(self.clients):
            try:
                q.put_nowait(text)
            except asyncio.QueueFull:              # a dashboard that stopped reading must not slow the gateway
                self.clients.discard(q)
                self.log("SYS", "WebSocket client dropped: it was not reading")

    # ---- callbacks from the TCP server -------------------------------------------

    def _on_b_message(self, body: bytes) -> None:
        self.engine.on_b_message(body, self.now())

    def _on_b_closed(self) -> None:
        self.engine.on_b_closed(self.now())

    # ---- running -----------------------------------------------------------------

    async def start(self) -> None:
        self.log("SYS", f"gateway starting (serial={self.serial.port or 'none'}, http={self.cfg.http_port}, "
                        f"esp32_b tcp={self.cfg.b_port}); evidence log: {self.evidence.path or 'not written'}")
        await self.bserver.start()
        app = build_app(self, self.ui_dir)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.cfg.http_host, self.cfg.http_port)
        await site.start()
        sockets = getattr(site._server, "sockets", None) or []
        if sockets:
            self.http_port = sockets[0].getsockname()[1]
        self.log("SYS", f"operator API on http://{self.cfg.http_host}:{self.http_port}  (WebSocket ws://...:{self.http_port}/ws)"
                        + (f"; dashboard files from {self.ui_dir}" if self.ui_dir else ""))
        self._ticker = asyncio.ensure_future(self._tick_loop())

    async def stop(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            try:
                await self._ticker
            except (asyncio.CancelledError, Exception):
                pass
        await self.bserver.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self.serial.close()
        self.log("SYS", "gateway stopped")
        self.evidence.close()

    async def run_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    async def _tick_loop(self) -> None:
        c = self.cfg
        next_status = 0
        while True:
            try:
                now = self.now()
                self.serial.ensure_open(now)
                frames, drops = self.serial.poll()
                for _ in range(drops):
                    self.engine.note_a_slip_drop()
                for raw in frames:
                    self.engine.on_a_frame(raw, now)
                self.engine.tick(now)
                dirty = self.engine.take_dirty()
                if dirty or now >= next_status:    # on every change, and at least every status_period_ms
                    self.broadcast(self.engine.status(now))
                    next_status = now + c.status_period_ms
            except Exception as e:                 # a bug must show up in the evidence, not silently stop the gateway
                self.log("SYS", f"INTERNAL ERROR in the gateway loop: {type(e).__name__}: {e}")
                import traceback
                self.log("SYS", traceback.format_exc().replace("\n", " | "))
            await asyncio.sleep(c.poll_ms / 1000)
