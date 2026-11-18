"""The gateway's brain: state machine, inspection queue, IDs, acknowledgements, retries, duplicate suppression, faults.

This file does no input or output of its own. Time is passed in (milliseconds), bytes leave through
an `io` object, and everything the operator should see leaves as dicts through io.emit(). That makes
the whole protocol testable with a fake clock and fake devices (tests/test_engine.py), and the same
code runs in the real gateway (app.py).

The `io` object provides:
    send_a(frame: bytes) -> bool      one ATR record for ESP32 A (SLIP is added by the link); False if it could not be written
    send_b(body: bytes) -> bool       one JSON message for ESP32 B (the length prefix is added by the link)
    close_b() -> None                 drop the TCP connection to ESP32 B
    a_open() -> bool                  is the serial port to ESP32 A open
    emit(msg: dict) -> None           a WebSocket message for the operator
    log(tag: str, text: str) -> None  one line of evidence (tags: A-TX A-RX B-TX B-RX OP-RX OP-TX EVT SYS)
    wall_ms() -> int                  Unix time in ms, for the "ts" field only

Terms used below, all from Interface Contract v1.1:
  * a "leg" is one message to one device that a command needs (batch/start has two: CONFIG to B and CMD to A)
  * a "tx" is a message that waits for an answer and is re-sent with the same bytes if none comes
  * the three events: delivered (bytes written), accepted (the device's ACK), completed (its explicit result)
"""
from __future__ import annotations

import random
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from . import bproto
from . import serialproto as sp
from .config import U32_MAX, Config

DEV_A = "esp32_a"
DEV_B = "esp32_b"

# Which gateway states allow each command (Interface Contract: States and command rules)
ALLOWED_STATES = {
    "batch/start": {"idle"},
    "batch/pause": {"running"},
    "batch/resume": {"paused"},
    "batch/stop": {"running", "paused"},
    "fault/clear": {"fault"},
    "config/inspection": {"idle", "paused"},
    "mode": {"idle"},
}
COMMAND_NAMES = set(ALLOWED_STATES)


@dataclass
class Device:
    name: str
    connected: bool = False
    last_rx: Optional[int] = None       # time of the last valid message of any kind
    next_hb: int = 0
    hb_seq: int = 0


@dataclass(eq=False)
class Leg:
    dev: str
    cmd: "Command"
    delivered: bool = False
    accepted: bool = False
    completed: bool = False
    failed: bool = False


@dataclass(eq=False)
class Command:
    cmd_id: int
    name: str
    internal: bool = False              # made up by the gateway itself: no cmd_event is sent for it
    legs: list = field(default_factory=list)
    emitted: set = field(default_factory=set)
    terminal: bool = False              # rejected or failed: no further stage is reported


@dataclass(eq=False)
class Part:
    part_id: int
    job_id: Optional[int] = None
    state: str = "queued"               # queued -> offered (JOB_REQ sent) -> running (B accepted it)
    rejects: int = 0
    retry_at: int = 0
    emitted: set = field(default_factory=set)


@dataclass(eq=False)
class Job:
    job_id: int
    part: Part
    n: int                              # the parameters this job was offered with
    k: int
    inspect_ms: int
    state: str = "offered"              # offered -> running
    ack_ms: Optional[int] = None        # when ESP32 B accepted it
    tx: Optional["Tx"] = None


@dataclass(eq=False)
class Tx:
    kind: str                           # "A_CMD", "B_CONFIG" or "B_JOB"
    dev: str                            # "a" or "b"
    key: int                            # cmd_id or job_id
    data: bytes                         # the exact bytes, re-sent unchanged
    desc: str                           # for the log
    seq: Optional[int] = None           # ATR seq (ESP32 A only)
    tries: int = 0
    deadline: int = 0
    leg: Optional[Leg] = None
    job: Optional[Job] = None
    expect_done: Optional[int] = None   # A_CMD: the state A must report in CMD_DONE
    extra: Any = None                   # B_CONFIG: the (N, K, inspect_time_ms) that was sent


@dataclass
class Batch:
    target: int = 0
    produced: int = 0                   # distinct parts seen from ESP32 A
    inspected: int = 0                  # verdicts received
    accepted: int = 0                   # PASS
    rejected: int = 0                   # FAIL
    started_ms: int = 0
    seen_upto: int = 0                  # highest part_id seen
    accepted_upto: int = 0              # highest part_id taken into the queue
    held: Optional[int] = None          # part_id that was answered QUEUE_FULL and waits


class Engine:
    def __init__(self, cfg: Config, io) -> None:
        self.cfg = cfg
        self.io = io
        self.a = Device(DEV_A)
        self.b = Device(DEV_B)
        self.state = "idle"
        self.mode = "production"
        self.params: dict = {"cycle_time_ms": cfg.default_cycle_time_ms,
                             "inspect_time_ms": cfg.default_inspect_time_ms, "N": None, "K": None}
        self.batch = Batch()
        self.queue: list[Part] = []         # parts accepted and not yet inspected; the one being inspected stays at the head
        self.blocked = False                # production is held back by a full queue
        self.job: Optional[Job] = None      # the job in flight
        self.cancelled: dict[int, int] = {}  # job_id -> time after which ESP32 B can no longer report it
        self.txs: list[Tx] = []
        self.aframer = sp.AFramer()
        self.cmd_history: "OrderedDict[int, tuple]" = OrderedDict()    # cmd_id -> (signature, status, body)
        self.completed_jobs: "OrderedDict[int, bool]" = OrderedDict()
        self.done_cmds: "OrderedDict[int, bool]" = OrderedDict()
        self.report: Optional[dict] = None
        self.fault: Optional[dict] = None
        self.b_hello = False
        self.b_cfg_cmd: Optional[int] = None
        self.b_cfg_confirmed = False
        self.a_rx_seq: Optional[int] = None
        self.next_job_id = cfg.job_id_start if cfg.job_id_start is not None else random.randint(1, 2 ** 31)
        self.next_internal_cmd = cfg.internal_cmd_id_base
        self.stats: dict = defaultdict(int)
        self._last_sig: Optional[tuple] = None

    # ================================================================== helpers

    def _log(self, tag: str, text: str) -> None:
        self.io.log(tag, text)

    def _emit(self, msg: dict) -> None:
        self.io.emit(msg)

    def _remember(self, store: "OrderedDict", key: int, value) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > self.cfg.completed_memory:
            store.popitem(last=False)

    def _internal_id(self) -> int:
        cid = self.next_internal_cmd
        self.next_internal_cmd = self.cfg.internal_cmd_id_base if cid >= U32_MAX else cid + 1
        return cid

    def _set_state(self, new: str) -> None:
        if new != self.state:
            self._log("EVT", f"state {self.state} -> {new}")
            self.state = new

    def _sig(self) -> tuple:
        b = self.batch
        return (self.state, self.mode, b.target, b.produced, b.inspected, b.accepted, b.rejected,
                len(self.queue), self.blocked, self.params["cycle_time_ms"], self.params["inspect_time_ms"],
                self.params["N"], self.params["K"], self.a.connected, self.b.connected)

    def take_dirty(self) -> bool:
        """True if anything the status shows has changed since the last call: send a status message then."""
        sig = self._sig()
        if sig != self._last_sig:
            self._last_sig = sig
            return True
        return False

    # ================================================================== views for the operator

    def status(self, now: int) -> dict:
        def dev(d: Device) -> dict:
            return {"connected": d.connected,
                    "heartbeat_age_ms": None if d.last_rx is None else max(0, now - d.last_rx)}

        b = self.batch
        return {
            "v": 1, "type": "status", "ts": self.io.wall_ms(), "state": self.state, "mode": self.mode,
            "batch": {"target": b.target, "produced": b.produced, "queued": len(self.queue),
                      "inspected": b.inspected, "accepted": b.accepted, "rejected": b.rejected},
            "queue": {"depth": len(self.queue), "capacity": self.cfg.queue_capacity, "blocked": self.blocked},
            "params": dict(self.params),
            "devices": {DEV_A: dev(self.a), DEV_B: dev(self.b)},
        }

    def fault_message(self) -> Optional[dict]:
        """The fault that put the gateway in the fault state, for an operator who connects later."""
        return dict(self.fault) if (self.state == "fault" and self.fault) else None

    def get_report(self) -> tuple[int, dict]:
        if self.report is None:
            return 409, self._err(None, "INVALID_STATE",
                                  "no completed batch: the report is available after a batch completes, until the next Start")
        return 200, dict(self.report)

    @staticmethod
    def _err(cmd_id: Optional[int], code: str, message: str) -> dict:
        return {"ok": False, "cmd_id": cmd_id, "error": {"code": code, "message": message}}

    # ================================================================== operator commands (HTTP)

    def handle_command(self, name: str, body: Any, now: int) -> tuple[int, dict]:
        """Validates and runs one operator command. Returns (HTTP status, JSON body)."""
        if name not in COMMAND_NAMES:
            return 404, self._err(None, "MALFORMED", f"unknown command {name}")
        if not isinstance(body, dict):
            return 400, self._err(None, "MALFORMED", "the request body must be a JSON object")
        cmd_id = bproto.u32(body.get("cmd_id"))
        if cmd_id is None:
            return 400, self._err(None, "MALFORMED", "cmd_id must be an integer from 0 to 4294967295")
        try:
            fields = self._validate(name, body)
        except ValueError as e:
            return 400, self._err(cmd_id, "MALFORMED", str(e))

        signature = (name, tuple(sorted(fields.items())))
        seen = self.cmd_history.get(cmd_id)
        if seen is not None:                    # the same cmd_id again: never execute twice
            old_signature, old_status, old_body = seen
            if old_signature != signature:
                return 400, self._err(cmd_id, "MALFORMED", f"cmd_id {cmd_id} was already used for a different command")
            self.stats["cmd_duplicates"] += 1
            self._log("EVT", f"cmd_id={cmd_id} cmd={name} stage=duplicate (original response repeated, not executed again)")
            repeated = dict(old_body)
            repeated["duplicate"] = True
            return old_status, repeated

        status, response = self._decide(name, cmd_id, fields, now)
        self.cmd_history[cmd_id] = (signature, status, response)
        while len(self.cmd_history) > self.cfg.cmd_history:
            self.cmd_history.popitem(last=False)
        return status, response

    def _validate(self, name: str, body: dict) -> dict:
        """Returns the validated fields, or raises ValueError with the message for the 400 response."""
        c = self.cfg

        def integer(key: str, lo: int, hi: int) -> int:
            v = body.get(key)
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"{key} must be an integer")
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            return v

        if name == "batch/start":
            return {"target_parts": integer("target_parts", c.target_min, c.target_max),
                    "cycle_time_ms": integer("cycle_time_ms", c.time_min_ms, c.time_max_ms),
                    "inspect_time_ms": integer("inspect_time_ms", c.time_min_ms, c.time_max_ms)}
        if name == "config/inspection":
            return {"N": integer("N", c.n_min, U32_MAX), "K": integer("K", c.k_min, U32_MAX)}
        if name == "mode":
            m = body.get("mode")
            if m not in ("production", "benchmark"):
                raise ValueError('mode must be "production" or "benchmark"')
            return {"mode": m}
        return {}

    def _decide(self, name: str, cmd_id: int, f: dict, now: int) -> tuple[int, dict]:
        verb = name.split("/")[1] if name.startswith("batch/") else name
        if self.state not in ALLOWED_STATES[name]:
            return 409, self._err(cmd_id, "INVALID_STATE", f"{verb} is not valid in state {self.state}")

        if name == "batch/start":
            if self.mode != "production":
                return 409, self._err(cmd_id, "INVALID_STATE", "start is not valid in benchmark mode (Milestone 2): set mode to production")
            if self.params["N"] is None or self.params["K"] is None:
                return 409, self._err(cmd_id, "INVALID_STATE",
                                      "the inspection rule is not set: send config/inspection (N, K) before start")
            missing = [d.name for d in (self.a, self.b) if not d.connected]
            if missing:
                return 503, self._err(cmd_id, "DEVICE_NOT_READY", f"not connected: {', '.join(missing)}")
        elif name == "config/inspection" and not self.b.connected:
            return 503, self._err(cmd_id, "DEVICE_NOT_READY", f"not connected: {DEV_B}")

        self._log("EVT", f"cmd_id={cmd_id} cmd={name} stage=accepted (by the gateway)")
        if name == "batch/start":
            self._do_start(cmd_id, f, now)
        elif name == "batch/pause":
            self._set_state("paused")
            cmd = self._new_command(cmd_id, name)
            self._send_a_cmd(cmd, sp.CMD_PAUSE, 0, 0, now)
        elif name == "batch/resume":
            self._set_state("running")
            cmd = self._new_command(cmd_id, name)
            self._send_a_cmd(cmd, sp.CMD_RESUME, 0, 0, now)
            self._check_complete(now)           # the last result may have arrived while paused
        elif name == "batch/stop":
            self._do_stop(cmd_id, now)
        elif name == "config/inspection":
            self.params["N"], self.params["K"] = f["N"], f["K"]
            cmd = self._new_command(cmd_id, name)
            self._send_config(cmd, now)
        elif name == "mode":
            self.mode = f["mode"]
            self._finish_local(self._new_command(cmd_id, name))
        elif name == "fault/clear":
            self.fault = None
            self._set_state("idle")
            self._finish_local(self._new_command(cmd_id, name))
        return 200, {"ok": True, "cmd_id": cmd_id, "stage": "accepted", "state": self.state, "duplicate": False}

    def _do_start(self, cmd_id: int, f: dict, now: int) -> None:
        self.params["cycle_time_ms"] = f["cycle_time_ms"]
        self.params["inspect_time_ms"] = f["inspect_time_ms"]
        self.batch = Batch(target=f["target_parts"], started_ms=now)
        self.report = None
        self.queue.clear()
        self.blocked = False
        self._set_state("running")
        cmd = self._new_command(cmd_id, "batch/start")
        pending: list = []                      # both legs exist before anything is sent, so no stage is reported early
        self._send_config(cmd, now, pending)                                # ESP32 B gets N, K and the inspection time
        self._send_a_cmd(cmd, sp.CMD_START, f["cycle_time_ms"], f["target_parts"], now, pending)   # ESP32 A starts producing
        for tx in pending:
            self._tx_send(tx, now)

    def _do_stop(self, cmd_id: int, now: int) -> None:
        self._drop_work(now)
        self._set_state("idle")
        cmd = self._new_command(cmd_id, "batch/stop")
        self._send_a_cmd(cmd, sp.CMD_STOP, 0, 0, now)

    # ================================================================== commands and their legs

    def _new_command(self, cmd_id: int, name: str, internal: bool = False) -> Command:
        return Command(cmd_id=cmd_id, name=name, internal=internal)

    def _finish_local(self, cmd: Command) -> None:
        """A command with no device leg is complete as soon as the gateway has applied it."""
        if not cmd.internal:
            self._cmd_event(cmd, "completed")

    def _cmd_event(self, cmd: Command, stage: str, reason: Optional[str] = None) -> None:
        msg = {"v": 1, "type": "cmd_event", "ts": self.io.wall_ms(), "cmd_id": cmd.cmd_id, "cmd": cmd.name, "stage": stage}
        if reason is not None:
            msg["reason"] = reason
        self._log("EVT", f"cmd_id={cmd.cmd_id} cmd={cmd.name} stage={stage}" + (f" reason={reason}" if reason else ""))
        self._emit(msg)

    def _progress(self, cmd: Command) -> None:
        """Reports a stage once every leg of the command has reached it, always in the order delivered, accepted, completed."""
        if cmd.internal or cmd.terminal or not cmd.legs:
            return
        for stage in ("delivered", "accepted", "completed"):
            if stage not in cmd.emitted and all(getattr(leg, stage) for leg in cmd.legs):
                cmd.emitted.add(stage)
                self._cmd_event(cmd, stage)

    def _leg_rejected(self, leg: Leg, reason: str) -> None:
        cmd = leg.cmd
        leg.failed = True
        if not cmd.internal and not cmd.terminal:
            cmd.terminal = True
            self._cmd_event(cmd, "rejected", reason)

    def _leg_failed(self, leg: Leg, reason: str) -> None:
        cmd = leg.cmd
        leg.failed = True
        if not cmd.internal and not cmd.terminal:
            cmd.terminal = True
            self._cmd_event(cmd, "failed", reason)

    def _send_a_cmd(self, cmd: Command, arg: int, value_a: int, value_b: int, now: int,
                    pending: Optional[list] = None) -> None:
        m = sp.ProdMsg(sp.PM_CMD, cmd.cmd_id, arg, value_a, value_b)
        frame, seq = self.aframer.build(m)
        leg = Leg(DEV_A, cmd)
        cmd.legs.append(leg)
        tx = Tx(kind="A_CMD", dev="a", key=cmd.cmd_id, data=frame, desc=sp.describe(m), seq=seq, leg=leg,
                expect_done=sp.EXPECTED_DONE[arg])
        self.txs.append(tx)
        if pending is None:
            self._tx_send(tx, now)
        else:
            pending.append(tx)

    def _send_config(self, cmd: Command, now: int, pending: Optional[list] = None) -> None:
        n, k, ms = self.params["N"], self.params["K"], self.params["inspect_time_ms"]
        leg = Leg(DEV_B, cmd)
        cmd.legs.append(leg)
        tx = Tx(kind="B_CONFIG", dev="b", key=cmd.cmd_id, data=bproto.config(cmd.cmd_id, n, k, ms), desc="",
                leg=leg, extra=(n, k, ms))
        self.b_cfg_cmd = cmd.cmd_id
        self.b_cfg_confirmed = False
        self.txs.append(tx)
        if pending is None:
            self._tx_send(tx, now)
        else:
            pending.append(tx)

    def _send_safe_stop(self, now: int) -> None:
        """Tells ESP32 A to stop producing. Used when the gateway enters the fault state."""
        if not self.a.connected:
            return
        cmd = self._new_command(self._internal_id(), "batch/stop", internal=True)
        self._send_a_cmd(cmd, sp.CMD_STOP, 0, 0, now)

    # ================================================================== reliable sending

    def _tx_send(self, tx: Tx, now: int) -> None:
        """Sends the tx's bytes (the same bytes every time) and starts the acknowledgement timer."""
        first = tx.tries == 0
        tx.tries += 1
        tx.deadline = now + self.cfg.ack_timeout_ms
        note = "" if first else f" RETRY {tx.tries - 1} of {self.cfg.max_tries - 1} (same bytes)"
        if tx.dev == "a":
            ok = self.io.send_a(tx.data)
            self._log("A-TX", f"seq={tx.seq} {tx.desc}{note}" + ("" if ok else " NOT WRITTEN (port closed)"))
        else:
            ok = self.io.send_b(tx.data)
            self._log("B-TX", tx.data.decode("ascii") + note + ("" if ok else " NOT WRITTEN (no connection)"))
        if not ok:
            return
        if tx.leg is not None and not tx.leg.delivered:
            tx.leg.delivered = True
            self._progress(tx.leg.cmd)
        if tx.job is not None:
            self._part_event(tx.job.part, "delivered", job_id=tx.job.job_id)

    def _find_tx(self, kind: str, key: int) -> Optional[Tx]:
        for tx in self.txs:
            if tx.kind == kind and tx.key == key:
                return tx
        return None

    def _tx_tick(self, now: int) -> None:
        for tx in list(self.txs):
            if tx not in self.txs or now < tx.deadline:
                continue
            if tx.tries < self.cfg.max_tries:
                self.stats["retries"] += 1
                self._tx_send(tx, now)
            else:
                self._tx_exhausted(tx, now)

    def _tx_exhausted(self, tx: Tx, now: int) -> None:
        """Four tries and no answer: report it as failed, never as success, and go to the fault state."""
        self.txs.remove(tx)
        device = DEV_A if tx.dev == "a" else DEV_B
        self.stats["retries_exhausted"] += 1
        if tx.kind == "B_JOB":
            job = tx.job
            self._log("EVT", f"job_id={job.job_id} part_id={job.part.part_id} stage=failed reason=retries_exhausted")
            self._cancel_job(job, now)
            self._enter_fault("RETRIES_EXHAUSTED", device,
                              f"no answer to JOB_REQ job_id={job.job_id} after {self.cfg.max_tries} tries", now)
            return
        self._leg_failed(tx.leg, "retries_exhausted")
        self._enter_fault("RETRIES_EXHAUSTED", device,
                          f"no answer to {tx.leg.cmd.name} cmd_id={tx.key} after {self.cfg.max_tries} tries", now)

    # ================================================================== faults and the safe state

    def _enter_fault(self, code: str, device: str, message: str, now: int) -> None:
        """running or paused -> fault. In any other state the problem is only logged (contract: Transitions)."""
        if self.state not in ("running", "paused"):
            self._log("EVT", f"fault condition in state {self.state}, no state change: {code} {device}: {message}")
            return
        self._set_state("fault")
        self.stats["faults"] += 1
        self.fault = {"v": 1, "type": "fault", "ts": self.io.wall_ms(), "code": code, "device": device, "message": message}
        self._log("EVT", f"FAULT {code} device={device}: {message}")
        self._emit(dict(self.fault))
        self._drop_work(now)
        self._send_safe_stop(now)

    def _cancel_job(self, job: Job, now: int) -> None:
        """Forget the job in flight. ESP32 B may still be inspecting it, so its result is expected, and thrown away, until `expires`."""
        if job.tx is not None and job.tx in self.txs:
            self.txs.remove(job.tx)
        expires = now + job.inspect_ms + self.cfg.result_age_margin_ms + self.cfg.ack_timeout_ms * self.cfg.max_tries
        self.cancelled[job.job_id] = expires
        if self.job is job:
            self.job = None

    def _drop_work(self, now: int) -> None:
        """Stop, or fault: nothing of this batch is processed any more."""
        if self.job is not None:
            self._cancel_job(self.job, now)
        self.queue.clear()
        self.blocked = False
        self.batch.held = None

    def _cancelled_outstanding(self, now: int) -> bool:
        for jid in [j for j, exp in self.cancelled.items() if now >= exp]:
            del self.cancelled[jid]
        return bool(self.cancelled)

    def _device_lost(self, dev: Device, now: int, why: str) -> None:
        """No heartbeat for 3000 ms, or the connection closed."""
        dev.connected = False
        self.stats["device_lost"] += 1
        self._log("EVT", f"{dev.name} disconnected: {why}")
        kind = "a" if dev is self.a else "b"
        for tx in [t for t in self.txs if t.dev == kind]:
            self.txs.remove(tx)
            if tx.leg is not None:
                self._leg_failed(tx.leg, "device_lost")
        if dev is self.b:
            if self.job is not None:
                self._cancel_job(self.job, now)
            self.cancelled.clear()              # ESP32 B abandons its job when the connection ends
            self.b_hello = False
            self.b_cfg_confirmed = False
            self.io.close_b()
        self._enter_fault("HEARTBEAT_LOST", dev.name, why, now)

    # ================================================================== time-driven work

    def tick(self, now: int) -> None:
        """Call every few milliseconds."""
        c = self.cfg
        # heartbeats to both devices
        if self.io.a_open() and now >= self.a.next_hb:
            m = sp.ProdMsg(sp.PM_HEARTBEAT, self.a.hb_seq)
            frame, seq = self.aframer.build(m)
            self.a.hb_seq += 1
            self.a.next_hb = now + c.heartbeat_ms
            ok = self.io.send_a(frame)
            self._log("A-TX", f"seq={seq} {sp.describe(m)}" + ("" if ok else " NOT WRITTEN"))
        if self.b.connected and now >= self.b.next_hb:
            body = bproto.heartbeat(self.b.hb_seq)
            self.b.hb_seq += 1
            self.b.next_hb = now + c.heartbeat_ms
            ok = self.io.send_b(body)
            self._log("B-TX", body.decode("ascii") + ("" if ok else " NOT WRITTEN"))
        # device silence
        for dev in (self.a, self.b):
            if dev.connected and dev.last_rx is not None and now - dev.last_rx >= c.device_timeout_ms:
                self._device_lost(dev, now, f"no heartbeat for {now - dev.last_rx} ms")
        # acknowledgements and retries
        self._tx_tick(now)
        # the job in flight must report in time
        job = self.job
        if job is not None and job.state == "running" and now - job.ack_ms > job.inspect_ms + c.result_age_margin_ms:
            self._log("EVT", f"job_id={job.job_id} part_id={job.part.part_id} no result within {job.inspect_ms + c.result_age_margin_ms} ms")
            self._cancel_job(job, now)
            self._enter_fault("STALE_RESULT", DEV_B,
                              f"no result for job_id={job.job_id} within {job.inspect_ms + c.result_age_margin_ms} ms", now)
        self._dispatch(now)

    # ================================================================== ESP32 A (USB serial)

    def on_a_frame(self, raw: bytes, now: int) -> None:
        """One ATR record from ESP32 A, SLIP already removed."""
        try:
            test_id, seq, payload = sp.atr_parse(raw)
        except sp.AtrError as e:
            self.stats["a_bad_frames"] += 1
            self._log("A-RX", f"frame discarded ({e}), {len(raw)} bytes")
            return
        if test_id != sp.ATR_TEST_ID_PRODUCTION:
            self.stats["a_other_test_id"] += 1
            self._log("A-RX", f"seq={seq} frame with test_id={test_id} ignored (not production traffic)")
            return
        if len(payload) != sp.ATR_PROD_PAYLOAD_LEN:
            self.stats["a_bad_frames"] += 1
            self._log("A-RX", f"seq={seq} production frame with payload_len={len(payload)} ignored")
            return
        m = sp.prod_decode(payload)
        note = ""
        if self.a_rx_seq is not None and seq != (self.a_rx_seq + 1) & 0xFFFFFFFF:
            if seq == self.a_rx_seq:
                note = " (same seq again: a repeated frame)"
            else:
                self.stats["a_seq_jumps"] += 1
                note = f" (seq jump: expected {(self.a_rx_seq + 1) & 0xFFFFFFFF})"
        self.a_rx_seq = seq
        self._log("A-RX", f"seq={seq} {sp.describe(m)}{note}")
        self._touch(self.a, now)

        if m.type == sp.PM_PART:
            self._on_part(m.id, now)
        elif m.type == sp.PM_CMD_ACK:
            self._on_cmd_ack(m, now)
        elif m.type == sp.PM_CMD_DONE:
            self._on_cmd_done(m, now)
        elif m.type == sp.PM_HEARTBEAT:
            pass
        else:
            self.stats["a_unexpected_type"] += 1
            self._log("A-RX", f"message type 0x{m.type:02x} ignored")
        self._dispatch(now)

    def note_a_slip_drop(self) -> None:
        self.stats["a_slip_drops"] += 1
        self._log("A-RX", "SLIP packet dropped (bad escape or too long)")

    def _touch(self, dev: Device, now: int) -> None:
        dev.last_rx = now
        if not dev.connected:
            dev.connected = True
            self._log("EVT", f"{dev.name} connected")

    def _send_part_ack(self, part_id: int, arg: int) -> None:
        m = sp.ProdMsg(sp.PM_PART_ACK, part_id, arg)
        frame, seq = self.aframer.build(m)
        ok = self.io.send_a(frame)
        self._log("A-TX", f"seq={seq} {sp.describe(m)}" + ("" if ok else " NOT WRITTEN"))

    def _on_part(self, part_id: int, now: int) -> None:
        b = self.batch
        if self.state not in ("running", "paused"):
            self.stats["parts_ignored"] += 1
            self._log("EVT", f"part_id={part_id} ignored: no batch is running (state {self.state}); no answer is sent")
            return
        if part_id < 1 or part_id > b.target:
            self.stats["parts_ignored"] += 1
            self._log("EVT", f"part_id={part_id} ignored: outside this batch (1..{b.target}); no answer is sent")
            return
        if part_id <= b.accepted_upto:          # ESP32 A did not get our answer: repeat it, never queue the part twice
            self.stats["part_duplicates"] += 1
            self._log("EVT", f"part_id={part_id} stage=duplicate (already queued; answer repeated)")
            self._send_part_ack(part_id, sp.ACK_ACCEPTED)
            return
        if part_id > b.seen_upto:               # first time this part is seen
            if part_id != b.seen_upto + 1:
                self._log("EVT", f"warning: part_id jumped from {b.seen_upto} to {part_id}")
            b.seen_upto = part_id
            b.produced += 1
            self._part_event_new(part_id, "produced")
        if len(self.queue) >= self.cfg.queue_capacity:      # queue full: production is blocked (Requirement IV)
            self.stats["queue_full_replies"] += 1
            self.blocked = True
            b.held = part_id
            self._send_part_ack(part_id, sp.ACK_QUEUE_FULL)
            return
        part = Part(part_id=part_id)
        part.emitted.add("produced")
        self.queue.append(part)
        b.accepted_upto = part_id
        if b.held == part_id:
            b.held = None
        self.blocked = False
        self._send_part_ack(part_id, sp.ACK_ACCEPTED)
        self._part_event(part, "queued")

    def _on_cmd_ack(self, m: sp.ProdMsg, now: int) -> None:
        tx = self._find_tx("A_CMD", m.id)
        if tx is None:
            self.stats["stale_acks"] += 1
            self._log("EVT", f"CMD_ACK for cmd_id={m.id}: nothing is waiting for it")
            return
        leg = tx.leg
        if m.arg == sp.CMDACK_ACCEPTED:
            leg.accepted = True
            self._progress(leg.cmd)
        elif m.arg == sp.CMDACK_REJECTED:
            self.txs.remove(tx)
            self._leg_rejected(leg, "device_rejected")
            self._enter_fault("PROTOCOL_ERROR", DEV_A, f"esp32_a rejected {leg.cmd.name} cmd_id={m.id}", now)
        else:
            self.stats["a_bad_args"] += 1
            self._log("EVT", f"CMD_ACK with unknown arg {m.arg} ignored")

    def _on_cmd_done(self, m: sp.ProdMsg, now: int) -> None:
        tx = self._find_tx("A_CMD", m.id)
        if tx is None:
            self.stats["stale_acks"] += 1
            self._log("EVT", f"CMD_DONE for cmd_id={m.id}: nothing is waiting for it")
            return
        leg = tx.leg
        self.txs.remove(tx)
        if m.arg != tx.expect_done:
            self._leg_rejected(leg, "unexpected_state")
            self._enter_fault("PROTOCOL_ERROR", DEV_A,
                              f"esp32_a reported state {sp.DONE_NAMES.get(m.arg, m.arg)} after {leg.cmd.name} cmd_id={m.id}", now)
            return
        leg.accepted = True
        leg.completed = True
        self._progress(leg.cmd)

    # ================================================================== ESP32 B (TCP)

    def on_b_message(self, body: bytes, now: int) -> None:
        """One message body from ESP32 B (the length prefix is already removed)."""
        msg = bproto.parse(body)
        if msg is None:
            self.stats["b_malformed"] += 1
            self._log("B-RX", body.decode("utf-8", "replace") + "  (not a JSON object with a \"type\": ignored)")
            return
        self._log("B-RX", body.decode("utf-8", "replace"))
        kind = msg["type"]
        if kind == "HELLO":
            self._on_b_hello(msg, now)
            return
        if not self.b_hello:
            self.stats["b_before_hello"] += 1
            self._log("EVT", f"{kind} before HELLO ignored")
            return
        self._touch(self.b, now)
        if kind == "HEARTBEAT":
            pass
        elif kind == "ACK":
            self._on_b_ack(msg, now)
        elif kind == "CONFIG_DONE":
            self._on_config_done(msg, now)
        elif kind == "JOB_RESULT":
            self._on_job_result(msg, now)
        else:
            self.stats["b_unexpected_type"] += 1
            self._log("EVT", f"unexpected message type {kind} from esp32_b ignored")
        self._dispatch(now)

    def on_b_closed(self, now: int) -> None:
        """The TCP connection to ESP32 B ended (closed by B, or an error)."""
        self.b_hello = False
        if self.b.connected:
            self._device_lost(self.b, now, "connection closed")

    def _on_b_hello(self, msg: dict, now: int) -> None:
        if msg.get("device") != DEV_B:
            self.stats["b_bad_hello"] += 1
            self._log("EVT", f"HELLO from device {msg.get('device')!r}, expected {DEV_B!r}: connection dropped")
            self.io.close_b()
            return
        if self.b.connected:
            self.stats["b_bad_hello"] += 1
            self._log("EVT", "second HELLO on one connection: connection dropped, esp32_b has to reconnect")
            self.on_b_closed(now)               # closes the socket and treats esp32_b as lost
            return
        self.b_hello = True
        self.b.connected = True
        self.b.last_rx = now
        self.b.next_hb = now
        self.b.hb_seq = 0
        self.b_cfg_confirmed = False
        self._log("EVT", f"{DEV_B} connected (fw {msg.get('fw')})")
        if self.params["N"] is not None and self.params["K"] is not None:
            # a freshly booted ESP32 B has no parameters: send them before any job (contract: CONFIG after every HELLO)
            self._send_config(self._new_command(self._internal_id(), "config/inspection", internal=True), now)

    def _b_ack(self, ref: str, id_name: str, ident: int, accepted: bool, reason: Optional[str] = None) -> None:
        body = bproto.ack(ref, id_name, ident, accepted, reason)
        ok = self.io.send_b(body)
        self._log("B-TX", body.decode("ascii") + ("" if ok else " NOT WRITTEN"))

    def _on_b_ack(self, msg: dict, now: int) -> None:
        ref, status = msg.get("ref"), msg.get("status")
        if ref == "CONFIG":
            cmd_id = bproto.u32(msg.get("cmd_id"))
            tx = self._find_tx("B_CONFIG", cmd_id) if cmd_id is not None else None
        elif ref == "JOB_REQ":
            job_id = bproto.u32(msg.get("job_id"))
            job = self.job
            tx = job.tx if (job is not None and job_id == job.job_id and job.tx in self.txs) else None
        else:
            tx = None
        if tx is None or status not in ("accepted", "rejected"):
            self.stats["stale_acks"] += 1
            self._log("EVT", f"ACK ref={ref} status={status}: nothing is waiting for it, ignored")
            return
        reason = msg.get("reason") if isinstance(msg.get("reason"), str) else "rejected"

        if tx.kind == "B_CONFIG":
            leg = tx.leg
            if status == "accepted":
                leg.accepted = True
                self._progress(leg.cmd)
            else:
                self.txs.remove(tx)
                if self.b_cfg_cmd == tx.key:
                    self.b_cfg_confirmed = False
                self._leg_rejected(leg, reason)
                self._enter_fault("PROTOCOL_ERROR", DEV_B, f"esp32_b rejected {leg.cmd.name} cmd_id={tx.key}: {reason}", now)
            return

        job = tx.job                            # a JOB_REQ
        self.txs.remove(tx)
        job.tx = None
        if status == "accepted":
            job.state = "running"
            job.ack_ms = now
            job.part.state = "running"
            self._part_event(job.part, "accepted", job_id=job.job_id)
        else:
            self._job_rejected(job, reason, now)

    def _job_rejected(self, job: Job, reason: str, now: int) -> None:
        """ESP32 B refused the job. It was never accepted, so the part goes back in the queue and is offered again, a bounded number of times."""
        part = job.part
        self.job = None
        part.state = "queued"
        part.rejects += 1
        part.retry_at = now + self.cfg.job_reject_retry_ms
        if reason in ("abandoned", "id_conflict"):
            part.job_id = None                  # B remembers this job_id: the next offer needs a new one
        self.stats["jobs_rejected"] += 1
        self._part_event(part, "rejected", job_id=job.job_id, reason=reason)
        if part.rejects >= self.cfg.job_reject_limit:
            self._enter_fault("PROTOCOL_ERROR", DEV_B,
                              f"esp32_b rejected part_id={part.part_id} {part.rejects} times (last reason: {reason})", now)

    def _on_config_done(self, msg: dict, now: int) -> None:
        cmd_id = bproto.u32(msg.get("cmd_id"))
        if cmd_id is None:
            self.stats["b_bad_fields"] += 1
            self._log("EVT", "CONFIG_DONE without a valid cmd_id ignored")
            return
        tx = self._find_tx("B_CONFIG", cmd_id)
        if tx is None:
            known = cmd_id in self.done_cmds    # B repeated it because our ACK was lost: answer again
            self._b_ack("CONFIG_DONE", "cmd_id", cmd_id, known, None if known else "stale")
            if not known:
                self.stats["stale_acks"] += 1
                self._log("EVT", f"CONFIG_DONE cmd_id={cmd_id} was not asked for: answered rejected")
            return
        echo = (bproto.u32(msg.get("N")), bproto.u32(msg.get("K")), bproto.u32(msg.get("inspect_time_ms")))
        leg = tx.leg
        self.txs.remove(tx)
        if echo != tx.extra:
            self._b_ack("CONFIG_DONE", "cmd_id", cmd_id, False, "mismatch")
            if self.b_cfg_cmd == cmd_id:
                self.b_cfg_confirmed = False
            self._leg_rejected(leg, "config_mismatch")
            self._enter_fault("PROTOCOL_ERROR", DEV_B,
                              f"esp32_b applied {echo} instead of {tx.extra} for cmd_id={cmd_id}", now)
            return
        self._b_ack("CONFIG_DONE", "cmd_id", cmd_id, True)
        self._remember(self.done_cmds, cmd_id, True)
        if self.b_cfg_cmd == cmd_id:
            self.b_cfg_confirmed = True
        leg.accepted = True
        leg.completed = True
        self._progress(leg.cmd)

    def _on_job_result(self, msg: dict, now: int) -> None:
        job_id = bproto.u32(msg.get("job_id"))
        if job_id is None:
            self.stats["b_bad_fields"] += 1
            self._log("EVT", "JOB_RESULT without a valid job_id ignored")
            return
        part_id = bproto.u32(msg.get("part_id"))
        n, k = bproto.u32(msg.get("N")), bproto.u32(msg.get("K"))
        verdict = msg.get("verdict")
        valid = part_id is not None and n is not None and k is not None and verdict in ("PASS", "FAIL")

        if job_id in self.completed_jobs:       # ESP32 B repeated a result we already took: answer, count nothing
            self.stats["result_duplicates"] += 1
            self._log("EVT", f"job_id={job_id} stage=duplicate (result already counted; ACK repeated)")
            self._b_ack("JOB_RESULT", "job_id", job_id, True)
            return

        job = self.job
        if job is not None and job.job_id == job_id:
            if not valid:
                self._b_ack("JOB_RESULT", "job_id", job_id, False, "malformed")
                self._enter_fault("PROTOCOL_ERROR", DEV_B, f"malformed JOB_RESULT for job_id={job_id}", now)
                return
            if job.state == "offered":          # its ACK never arrived, but the result proves it was accepted
                if job.tx is not None and job.tx in self.txs:
                    self.txs.remove(job.tx)
                job.tx = None
                job.state = "running"
                job.ack_ms = now
                job.part.state = "running"
                self._part_event(job.part, "accepted", job_id=job.job_id)
            limit = job.inspect_ms + self.cfg.result_age_margin_ms
            if part_id != job.part.part_id or n != job.n or k != job.k:
                self._b_ack("JOB_RESULT", "job_id", job_id, False, "mismatch")
                self._enter_fault("PROTOCOL_ERROR", DEV_B,
                                  f"JOB_RESULT job_id={job_id} part_id={part_id} N={n} K={k} does not match the job "
                                  f"(part_id={job.part.part_id} N={job.n} K={job.k})", now)
                return
            if now - job.ack_ms > limit:
                self._b_ack("JOB_RESULT", "job_id", job_id, False, "stale")
                self.stats["stale_results"] += 1
                self._cancel_job(job, now)
                self._enter_fault("STALE_RESULT", DEV_B, f"result for job_id={job_id} arrived after {limit} ms", now)
                return
            self._b_ack("JOB_RESULT", "job_id", job_id, True)
            self._complete_job(job, verdict, now)
            return

        # not the job in flight
        self.stats["stale_results"] += 1
        self._b_ack("JOB_RESULT", "job_id", job_id, False, "stale")
        if job_id in self.cancelled:            # we stopped or gave up on this job ourselves: expected, thrown away
            del self.cancelled[job_id]
            self._log("EVT", f"job_id={job_id} result discarded (the job was cancelled)")
            return
        self._log("EVT", f"job_id={job_id} result discarded (not the job in flight)")
        self._enter_fault("STALE_RESULT", DEV_B, f"result for job_id={job_id}, which is not the job in flight", now)

    def _complete_job(self, job: Job, verdict: str, now: int) -> None:
        b = self.batch
        part = job.part
        self.job = None
        if part in self.queue:
            self.queue.remove(part)
        self._remember(self.completed_jobs, job.job_id, True)
        b.inspected += 1
        if verdict == "PASS":
            b.accepted += 1
        else:
            b.rejected += 1
        self._part_event(part, "completed", job_id=job.job_id, verdict=verdict)
        self._check_complete(now)

    def _check_complete(self, now: int) -> None:
        """Batch completion: every target part produced and inspected (contract: running -> idle)."""
        b = self.batch
        if self.state != "running" or b.target == 0 or b.inspected != b.target or b.produced != b.target:
            return
        self._set_state("idle")
        self.report = {"v": 1, "type": "batch_complete", "ts": self.io.wall_ms(), "target": b.target,
                       "produced": b.produced, "inspected": b.inspected, "accepted": b.accepted,
                       "rejected": b.rejected, "duration_ms": now - b.started_ms}
        self._log("EVT", f"BATCH COMPLETE target={b.target} accepted={b.accepted} rejected={b.rejected} duration_ms={now - b.started_ms}")
        self._emit(dict(self.report))

    # ================================================================== parts and jobs

    def _part_event_new(self, part_id: int, stage: str) -> None:
        """An event for a part that has no Part object yet (it was not taken into the queue)."""
        self._emit_part_event(part_id, None, stage, None, None)

    def _part_event(self, part: Part, stage: str, job_id: Optional[int] = None,
                    verdict: Optional[str] = None, reason: Optional[str] = None) -> None:
        if stage != "rejected":
            if stage in part.emitted:
                return
            part.emitted.add(stage)
        self._emit_part_event(part.part_id, part.job_id if job_id is None else job_id, stage, verdict, reason)

    def _emit_part_event(self, part_id: int, job_id: Optional[int], stage: str,
                         verdict: Optional[str], reason: Optional[str]) -> None:
        msg = {"v": 1, "type": "part_event", "ts": self.io.wall_ms(), "part_id": part_id, "job_id": job_id, "stage": stage}
        if verdict is not None:
            msg["verdict"] = verdict
        if reason is not None:
            msg["reason"] = reason
        self._log("EVT", f"part_id={part_id} job_id={job_id} stage={stage}"
                         + (f" verdict={verdict}" if verdict else "") + (f" reason={reason}" if reason else ""))
        self._emit(msg)

    def _dispatch(self, now: int) -> None:
        """Offers the next queued part to ESP32 B: one job at a time, only while running, only when B has its parameters."""
        if self.state != "running" or self.job is not None:
            return
        if not (self.b.connected and self.b_cfg_confirmed):
            return
        if self._cancelled_outstanding(now):    # B may still be inspecting a job from before a Stop
            return
        part = next((p for p in self.queue if p.state == "queued"), None)    # first in, first inspected
        if part is None or now < part.retry_at:     # a part ESP32 B rejected waits its turn again; it is not overtaken
            return
        if part.job_id is None:
            part.job_id = self.next_job_id
            self.next_job_id = self.next_job_id + 1 if self.next_job_id < U32_MAX else 1
        job = Job(job_id=part.job_id, part=part, n=self.params["N"], k=self.params["K"],
                  inspect_ms=self.params["inspect_time_ms"])
        part.state = "offered"
        tx = Tx(kind="B_JOB", dev="b", key=job.job_id, data=bproto.job_req(job.job_id, part.part_id), desc="", job=job)
        job.tx = tx
        self.job = job
        self.txs.append(tx)
        self._tx_send(tx, now)
