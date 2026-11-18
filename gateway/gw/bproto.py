"""The messages between the gateway and ESP32 B, and the length-prefix framing around them.

Framing: a 4-byte big-endian length, then that many bytes of compact UTF-8 JSON, at most 1024.
Messages (Interface Contract v1.1): HELLO, HEARTBEAT, CONFIG, CONFIG_DONE, JOB_REQ, JOB_RESULT, ACK.
Field order and compact separators match what ESP32 B itself sends, so message lengths are predictable.
"""
from __future__ import annotations

import json
import struct
from typing import Any, Optional

from .config import U32_MAX

HDR_LEN = 4


# ---- framing ----------------------------------------------------------------------

class FrameError(Exception):
    """A length prefix above the maximum. A TCP stream cannot be re-synchronised after this."""


def frame_encode(body: bytes, max_body: int = 1024) -> bytes:
    if len(body) > max_body:
        raise ValueError(f"message body longer than {max_body} bytes")
    return struct.pack(">I", len(body)) + body


class FrameDecoder:
    """Cuts a TCP byte stream into message bodies. Raises FrameError on an oversize length, and then stays broken."""

    def __init__(self, max_body: int = 1024):
        self.max_body = max_body
        self.buf = bytearray()
        self.failed = False

    def feed(self, data: bytes) -> list[bytes]:
        if self.failed:
            raise FrameError("decoder already failed")
        self.buf += data
        out = []
        while True:
            if len(self.buf) < HDR_LEN:
                break
            (n,) = struct.unpack(">I", self.buf[:HDR_LEN])
            if n > self.max_body:
                self.failed = True
                raise FrameError(f"announced length {n} is over {self.max_body}")
            if len(self.buf) < HDR_LEN + n:
                break
            out.append(bytes(self.buf[HDR_LEN:HDR_LEN + n]))
            del self.buf[:HDR_LEN + n]
        return out


# ---- reading ----------------------------------------------------------------------

def parse(body: bytes) -> Optional[dict]:
    """Returns the message as a dict, or None if it is not a JSON object with a string "type"."""
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        return None
    return obj


def u32(value: Any) -> Optional[int]:
    """The value if it is a JSON integer from 0 to 4294967295, otherwise None. True and 5.0 are not integers here."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= U32_MAX else None


# ---- writing ----------------------------------------------------------------------

def dumps(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("ascii")


def heartbeat(seq: int) -> bytes:
    return dumps({"type": "HEARTBEAT", "seq": seq})


def config(cmd_id: int, n: int, k: int, inspect_time_ms: int) -> bytes:
    return dumps({"type": "CONFIG", "cmd_id": cmd_id, "N": n, "K": k, "inspect_time_ms": inspect_time_ms})


def job_req(job_id: int, part_id: int) -> bytes:
    return dumps({"type": "JOB_REQ", "job_id": job_id, "part_id": part_id})


def ack(ref: str, id_name: str, ident: int, accepted: bool, reason: Optional[str] = None) -> bytes:
    msg: dict = {"type": "ACK", "ref": ref, id_name: ident, "status": "accepted" if accepted else "rejected"}
    if not accepted and reason:
        msg["reason"] = reason
    return dumps(msg)
