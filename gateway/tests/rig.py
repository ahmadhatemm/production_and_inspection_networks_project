"""A fake world for testing the engine without any real I/O: a clock you move yourself, and recorded outputs."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gw import bproto
from gw import serialproto as sp
from gw.config import Config
from gw.engine import Engine


class FakeIO:
    def __init__(self, rig: "Rig") -> None:
        self.rig = rig
        self.a_is_open = True
        self.b_up = True            # False: writes to ESP32 B fail
        self.a_up = True            # False: writes to ESP32 A fail

    def send_a(self, frame: bytes) -> bool:
        if not self.a_up:
            return False
        test_id, seq, payload = sp.atr_parse(frame)     # the engine must only ever send valid frames
        assert test_id == 0 and len(payload) == 16
        self.rig.a_out.append((seq, sp.prod_decode(payload), frame))
        return True

    def send_b(self, body: bytes) -> bool:
        if not self.b_up:
            return False
        assert len(body) <= 1024
        self.rig.b_out.append((json.loads(body), body))
        return True

    def close_b(self) -> None:
        self.rig.b_closed += 1

    def a_open(self) -> bool:
        return self.a_is_open

    def emit(self, msg: dict) -> None:
        self.rig.events.append(msg)

    def log(self, tag: str, text: str) -> None:
        self.rig.logs.append((tag, text))

    def wall_ms(self) -> int:
        return 1_795_000_000_000 + self.rig.now


class Rig:
    def __init__(self, **cfg_overrides) -> None:
        self.cfg = Config(job_id_start=100, **cfg_overrides)
        self.now = 1000
        self.a_out: list = []       # (seq, ProdMsg, bytes) the engine wrote to ESP32 A
        self.b_out: list = []       # (dict, bytes) the engine wrote to ESP32 B
        self.events: list = []      # dicts for the WebSocket
        self.logs: list = []
        self.b_closed = 0
        self.io = FakeIO(self)
        self.eng = Engine(self.cfg, self.io)
        self.a_seq = 0              # ESP32 A's own tx seq
        self._cmd = 1000

    # ---- time -----------------------------------------------------------------
    def run(self, ms: int, step: int = 5, keepalive: bool = False) -> None:
        end = self.now + ms
        while self.now < end:
            self.now += step
            if keepalive and self.now % 1000 < step:
                self.a_hb()
                self.b_hb()
            self.eng.tick(self.now)

    # ---- device -> gateway ----------------------------------------------------------
    def a_send(self, msg: sp.ProdMsg, seq: int = None) -> None:
        if seq is None:
            seq = self.a_seq
            self.a_seq += 1
        self.eng.on_a_frame(sp.atr_build(0, seq, sp.prod_encode(msg)), self.now)

    def a_hb(self) -> None:
        self.a_send(sp.ProdMsg(sp.PM_HEARTBEAT, 0))

    def a_part(self, part_id: int) -> None:
        self.a_send(sp.ProdMsg(sp.PM_PART, part_id))

    def b_send(self, obj: dict) -> None:
        self.eng.on_b_message(json.dumps(obj, separators=(",", ":")).encode(), self.now)

    def hello(self) -> None:
        self.b_send({"type": "HELLO", "device": "esp32_b", "fw": "1.0.0"})

    def b_hb(self) -> None:
        self.b_send({"type": "HEARTBEAT", "seq": 0})

    # ---- gateway -> device, as lists --------------------------------------------------
    def a_msgs(self, t: int) -> list:
        return [m for (_s, m, _r) in self.a_out if m.type == t]

    def b_msgs(self, t: str) -> list:
        return [d for (d, _r) in self.b_out if d.get("type") == t]

    def ev(self, type_: str, **match) -> list:
        return [e for e in self.events if e["type"] == type_ and all(e.get(k) == v for k, v in match.items())]

    def stages(self, cmd_id: int) -> list:
        return [(e["stage"], e.get("reason")) for e in self.events if e["type"] == "cmd_event" and e["cmd_id"] == cmd_id]

    def part_stages(self, part_id: int) -> list:
        return [e["stage"] for e in self.events if e["type"] == "part_event" and e["part_id"] == part_id]

    # ---- operator -----------------------------------------------------------------
    def cmd(self, name: str, cmd_id: int = None, **fields):
        if cmd_id is None:
            self._cmd += 1
            cmd_id = self._cmd
        body = dict(fields)
        body["cmd_id"] = cmd_id
        status, out = self.eng.handle_command(name, body, self.now)
        return status, out

    # ---- convenient set-ups ---------------------------------------------------------
    def connect(self) -> None:
        self.a_hb()
        self.hello()
        self.b_hb()

    def configure(self, n: int = 5, k: int = 2, ms: int = 500) -> None:
        """Both devices connected, N and K set, ESP32 B has confirmed."""
        self.connect()
        status, _ = self.cmd("config/inspection", N=n, K=k)
        assert status == 200
        cfg = self.b_msgs("CONFIG")[-1]
        self.b_send({"type": "ACK", "ref": "CONFIG", "cmd_id": cfg["cmd_id"], "status": "accepted"})
        self.b_send({"type": "CONFIG_DONE", "cmd_id": cfg["cmd_id"], "N": cfg["N"], "K": cfg["K"],
                     "inspect_time_ms": cfg["inspect_time_ms"]})

    def start(self, target: int = 3, cycle: int = 100, inspect: int = 500, cmd_id: int = None):
        status, out = self.cmd("batch/start", cmd_id, target_parts=target, cycle_time_ms=cycle, inspect_time_ms=inspect)
        return status, out

    def answer_start(self) -> None:
        """Play both devices answering a Start: CONFIG to B, CMD to A."""
        cfg = self.b_msgs("CONFIG")[-1]
        self.b_send({"type": "ACK", "ref": "CONFIG", "cmd_id": cfg["cmd_id"], "status": "accepted"})
        self.b_send({"type": "CONFIG_DONE", "cmd_id": cfg["cmd_id"], "N": cfg["N"], "K": cfg["K"],
                     "inspect_time_ms": cfg["inspect_time_ms"]})
        self.answer_a_cmd(self.a_msgs(sp.PM_CMD)[-1])

    def answer_a_cmd(self, c: sp.ProdMsg) -> None:
        self.a_send(sp.ProdMsg(sp.PM_CMD_ACK, c.id, sp.CMDACK_ACCEPTED))
        self.a_send(sp.ProdMsg(sp.PM_CMD_DONE, c.id, sp.EXPECTED_DONE[c.arg]))

    def job_ack(self, job: dict, accept: bool = True, reason: str = None) -> None:
        msg = {"type": "ACK", "ref": "JOB_REQ", "job_id": job["job_id"], "status": "accepted" if accept else "rejected"}
        if reason:
            msg["reason"] = reason
        self.b_send(msg)

    def job_result(self, job: dict, n: int = 5, k: int = 2, verdict: str = None) -> None:
        if verdict is None:
            verdict = "FAIL" if (job["part_id"] + k) % n == 0 else "PASS"
        self.b_send({"type": "JOB_RESULT", "job_id": job["job_id"], "part_id": job["part_id"],
                     "verdict": verdict, "N": n, "K": k})
