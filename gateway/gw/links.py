"""The two physical links: USB serial to ESP32 A, and the TCP server that ESP32 B connects to.

Neither class knows the protocol. They move bytes and report what happened; the engine decides what it means.
"""
from __future__ import annotations

import asyncio
import socket
from typing import Callable, Optional

from . import serialproto as sp
from .bproto import FrameDecoder, FrameError, frame_encode

try:
    import serial                                # pyserial
except ImportError:                              # reported when a port is actually opened
    serial = None


class SerialLink:
    """ESP32 A over a COM port (or a pyserial URL such as socket://127.0.0.1:7000 for tests).

    The port is polled without blocking. If it cannot be opened, or fails while open, the link stays closed
    and opening is tried again once a second, so unplugging and replugging the board needs no restart.
    """

    def __init__(self, port: Optional[str], baud: int, log: Callable[[str, str], None], reopen_ms: int = 1000) -> None:
        self.port = port
        self.baud = baud
        self.log = log
        self.reopen_ms = reopen_ms
        self.ser = None
        self.decoder = sp.SlipDecoder(sp.ATR_MAX_FRAME_LEN)
        self._next_try = 0
        self._last_error = ""
        self._reported_drops = 0

    @property
    def is_open(self) -> bool:
        return self.ser is not None

    def ensure_open(self, now: int) -> None:
        if self.ser is not None or self.port is None or now < self._next_try:
            return
        self._next_try = now + self.reopen_ms
        if serial is None:
            self._report("pyserial is not installed: python -m pip install pyserial")
            return
        try:
            ser = serial.serial_for_url(self.port, do_not_open=True, baudrate=self.baud, timeout=0, write_timeout=1)
            ser.dtr = False             # on ESP32 boards DTR and RTS drive GPIO0 and EN (reset): keep both inactive
            ser.rts = False             # so that opening the port does not reset the board
            ser.open()
        except Exception as e:          # busy port, wrong name, board unplugged
            self._report(f"cannot open {self.port}: {e}")
            return
        self.ser = ser
        self.decoder = sp.SlipDecoder(sp.ATR_MAX_FRAME_LEN)
        self._reported_drops = 0
        self._last_error = ""
        self.log("SYS", f"serial port {self.port} open at {self.baud} baud 8N1")

    def _report(self, text: str) -> None:
        if text != self._last_error:    # say it once, not every second
            self._last_error = text
            self.log("SYS", text)

    def _fail(self, why: str) -> None:
        self.log("SYS", f"serial port {self.port} closed: {why}")
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def poll(self) -> tuple[list[bytes], int]:
        """Returns (ATR records received since the last call, number of SLIP packets dropped as damaged)."""
        if self.ser is None:
            return [], 0
        try:
            data = self.ser.read(4096)
        except Exception as e:
            self._fail(str(e))
            return [], 0
        if not data:
            return [], 0
        frames = self.decoder.feed(data)
        new_drops = self.decoder.dropped - self._reported_drops
        self._reported_drops = self.decoder.dropped
        return frames, new_drops

    def write(self, record: bytes) -> bool:
        """SLIP-frames one ATR record and writes it. False if the port is not open or the write failed."""
        if self.ser is None:
            return False
        try:
            self.ser.write(sp.slip_encode(record))
            return True
        except Exception as e:
            self._fail(str(e))
            return False

    def close(self) -> None:
        if self.ser is not None:
            self._fail("shutting down")


class BServer:
    """The TCP server ESP32 B connects to. One connection at a time: a new connection replaces the old one.

    Everything runs on the gateway's single asyncio thread, so the engine needs no locks.
    The generation number makes a late event from a replaced connection harmless.
    """

    def __init__(self, host: str, port: int, max_body: int,
                 on_message: Callable[[bytes], None], on_closed: Callable[[], None],
                 log: Callable[[str, str], None]) -> None:
        self.host, self.port, self.max_body = host, port, max_body
        self.on_message, self.on_closed, self.log = on_message, on_closed, log
        self.writer: Optional[asyncio.StreamWriter] = None
        self.gen = 0
        self.server: Optional[asyncio.base_events.Server] = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.host, self.port)
        if self.server.sockets:
            self.port = self.server.sockets[0].getsockname()[1]     # the real port when 0 was asked for
        self.log("SYS", f"listening for ESP32 B on TCP {self.host}:{self.port}")

    async def stop(self) -> None:
        self.close_current()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    def send(self, body: bytes) -> bool:
        if not self.connected():
            return False
        try:
            self.writer.write(frame_encode(body, self.max_body))
            return True
        except Exception as e:
            self.log("SYS", f"write to ESP32 B failed: {e}")
            return False

    def close_current(self) -> None:
        """Engine-initiated close. The engine already knows, so no on_closed callback follows."""
        self.gen += 1
        w, self.writer = self.writer, None
        if w is not None:
            try:
                w.close()
            except Exception:
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # small messages, low latency
            except OSError:
                pass
        if self.writer is not None:
            self.log("SYS", f"new connection from {peer} replaces the existing ESP32 B connection")
            old = self.writer
            self.gen += 1
            self.writer = None
            try:
                old.close()
            except Exception:
                pass
            self.on_closed()                    # the engine treats the old connection as lost
        self.gen += 1
        gen = self.gen
        self.writer = writer
        self.log("SYS", f"TCP connection from {peer}")
        decoder = FrameDecoder(self.max_body)
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                if gen != self.gen:             # this connection was replaced or closed by the engine
                    return
                try:
                    bodies = decoder.feed(data)
                except FrameError as e:
                    self.log("SYS", f"bad framing from ESP32 B, connection dropped: {e}")
                    break
                for body in bodies:
                    if gen != self.gen:
                        return
                    self.on_message(body)
        except (ConnectionError, OSError) as e:
            self.log("SYS", f"TCP error: {e}")
        finally:
            if gen == self.gen:
                self.writer = None
                self.gen += 1
                self.log("SYS", f"TCP connection from {peer} closed")
                try:
                    writer.close()
                except Exception:
                    pass
                self.on_closed()
