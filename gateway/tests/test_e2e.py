"""End-to-end tests: the real gateway (serial link, TCP server, HTTP, WebSocket) against the real ESP32 A and ESP32 B
firmware logic compiled for the PC (sim_a.c and sim_b.c, the same station.c files that run on the boards).

ESP32 A's serial port is a TCP socket here (pyserial's socket:// URL), ESP32 B connects over loopback TCP.
Needs gcc and the sibling folders ESP32_A and ESP32_B. Without them these tests are skipped.
Run:  python -m unittest tests.test_e2e -v
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

from gw.app import Gateway
from gw.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))              # the folder that holds gateway, ESP32_A and ESP32_B
A_DIR = os.path.join(ROOT, "ESP32_A")
B_DIR = os.path.join(ROOT, "ESP32_B")
BUILD = os.path.join(tempfile.gettempdir(), "gw_e2e_build")
CFLAGS = ["-std=gnu11", "-Wall", "-Wextra", "-O1", "-g"]


def build_sims():
    """Compiles sim_a and sim_b once per run. Returns (sim_a, sim_b) paths, or raises SkipTest."""
    if shutil.which("gcc") is None:
        raise unittest.SkipTest("gcc not found")
    if not (os.path.isdir(A_DIR) and os.path.isdir(B_DIR)):
        raise unittest.SkipTest("ESP32_A and ESP32_B folders not next to the gateway folder")
    os.makedirs(BUILD, exist_ok=True)
    sim_a, sim_b = os.path.join(BUILD, "sim_a"), os.path.join(BUILD, "sim_b")
    a_src = [os.path.join(A_DIR, "test", "sim_a.c")] + [os.path.join(A_DIR, "main", f) for f in
                                                          ("station.c", "atr.c", "prodmsg.c", "crc16.c", "slip.c")]
    b_src = [os.path.join(B_DIR, "test", "sim_b.c")] + [os.path.join(B_DIR, "main", f) for f in
                                                          ("link.c", "station.c", "jsonmini.c", "tcpframe.c")]
    for out, src in ((sim_a, a_src), (sim_b, b_src)):
        newest = max(os.path.getmtime(f) for f in src)
        if not os.path.exists(out) or os.path.getmtime(out) < newest:
            r = subprocess.run(["gcc", *CFLAGS, "-o", out, *src], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("cannot build the simulators:\n" + r.stderr)
    return sim_a, sim_b


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(port: int, seconds: float = 3.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.005)
    raise RuntimeError(f"nothing is listening on port {port}")


class Proc:
    def __init__(self, args):
        self.p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def kill(self):
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait()


class E2E(unittest.IsolatedAsyncioTestCase):
    sim_a = sim_b = None

    @classmethod
    def setUpClass(cls):
        cls.sim_a, cls.sim_b = build_sims()

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.procs = []
        self.a_port = free_port()
        self.b_port = free_port()
        self.gw = None
        self.ws = None
        self.session = aiohttp.ClientSession()
        self.ws_msgs = []
        self.ws_task = None
        self.cmd_n = 0

    async def asyncTearDown(self):
        if self.ws_task:
            self.ws_task.cancel()
        if getattr(self, "ws", None) is not None:
            await self.ws.close()
        await self.session.close()
        if self.gw:
            await self.gw.stop()
        for p in self.procs:
            p.kill()

    # ---- helpers ----------------------------------------------------------------
    def start_a(self, drop_rx=0, drop_tx=0):
        p = Proc([self.sim_a, str(self.a_port), str(drop_rx), str(drop_tx)])
        self.procs.append(p)
        wait_listening(self.a_port)
        return p

    def start_b(self):
        p = Proc([self.sim_b, "127.0.0.1", str(self.b_port)])
        self.procs.append(p)
        return p

    async def start_gateway(self, with_a=True, **cfg):
        c = Config(http_host="127.0.0.1", http_port=0, b_host="127.0.0.1", b_port=self.b_port, **cfg)
        self.gw = Gateway(c, f"socket://127.0.0.1:{self.a_port}" if with_a else None, console=False)
        await self.gw.start()
        self.base = f"http://127.0.0.1:{self.gw.http_port}"
        await self.open_ws()

    async def open_ws(self):
        ws = await self.session.ws_connect(f"ws://127.0.0.1:{self.gw.http_port}/ws")
        self.ws = ws

        async def collect():
            async for m in ws:
                if m.type == aiohttp.WSMsgType.TEXT:
                    self.ws_msgs.append(json.loads(m.data))
        self.ws_task = asyncio.ensure_future(collect())

    async def post(self, path, **body):
        self.cmd_n += 1
        body.setdefault("cmd_id", self.cmd_n)
        async with self.session.post(f"{self.base}/api/v1/{path}", json=body) as r:
            return r.status, await r.json()

    async def get(self, path):
        async with self.session.get(f"{self.base}/api/v1/{path}") as r:
            return r.status, await r.json()

    async def wait_for(self, pred, timeout=15.0, what="condition"):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return
            await asyncio.sleep(0.02)
        self.fail(f"timed out waiting for {what}; state={self.gw.engine.state}; "
                  f"last events={self.ws_msgs[-6:]}")

    def events(self, type_, **match):
        return [m for m in self.ws_msgs if m["type"] == type_ and all(m.get(k) == v for k, v in match.items())]

    async def devices_up(self):
        await self.wait_for(lambda: self.gw.engine.a.connected and self.gw.engine.b.connected, 10, "both devices connected")

    async def configure(self, n=5, k=2):
        st, _ = await self.post("config/inspection", N=n, K=k)
        self.assertEqual(st, 200)
        cmd_id = self.cmd_n
        await self.wait_for(lambda: any(e["cmd_id"] == cmd_id and e["stage"] == "completed"
                                        for e in self.events("cmd_event")), 5, "config completed")

    def check_batch(self, target, n, k):
        """Everything that must be true after a finished batch, whatever happened on the way."""
        done = self.events("batch_complete")
        self.assertEqual(len(done), 1)
        d = done[0]
        want_fail = [p for p in range(1, target + 1) if (p + k) % n == 0]            # the rule, computed here, not in the gateway
        self.assertEqual((d["target"], d["produced"], d["inspected"]), (target, target, target))
        self.assertEqual(d["rejected"], len(want_fail))
        self.assertEqual(d["accepted"], target - len(want_fail))
        completed = {e["part_id"]: e for e in self.events("part_event", stage="completed")}
        self.assertEqual(sorted(completed), list(range(1, target + 1)))             # each part exactly once
        self.assertEqual(len(self.events("part_event", stage="completed")), target)
        for p, e in completed.items():
            self.assertEqual(e["verdict"], "FAIL" if p in want_fail else "PASS", p)
        job_ids = [e["job_id"] for e in completed.values()]
        self.assertEqual(len(set(job_ids)), target)                                 # a job_id is never reused
        for p in range(1, target + 1):
            stages = [e["stage"] for e in self.ws_msgs if e["type"] == "part_event" and e["part_id"] == p
                      and e["stage"] != "rejected"]
            self.assertEqual(stages, ["produced", "queued", "delivered", "accepted", "completed"], f"part {p}: {stages}")
        self.assertEqual(self.gw.engine.state, "idle")

    # ---- tests --------------------------------------------------------------------
    async def test_full_batch(self):
        self.start_a()
        await self.start_gateway()
        self.start_b()
        await self.devices_up()
        await self.configure(5, 2)
        st, out = await self.post("batch/start", target_parts=12, cycle_time_ms=30, inspect_time_ms=60)
        self.assertEqual((st, out["state"]), (200, "running"))
        await self.wait_for(lambda: self.events("batch_complete"), 20, "batch_complete")
        self.check_batch(12, 5, 2)
        st, rep = await self.get("batch/report")
        self.assertEqual(st, 200)
        self.assertEqual((rep["target"], rep["accepted"], rep["rejected"]), (12, 10, 2))   # parts 3 and 8 fail for N=5, K=2
        st, s = await self.get("status")
        self.assertEqual((s["state"], s["batch"]["inspected"], s["queue"]["depth"]), ("idle", 12, 0))
        start_stages = [e["stage"] for e in self.events("cmd_event") if e["cmd"] == "batch/start"]
        self.assertEqual(start_stages, ["delivered", "accepted", "completed"])
        self.assertEqual(self.events("fault"), [])

    async def test_other_rule_values(self):
        self.start_a()
        await self.start_gateway()
        self.start_b()
        await self.devices_up()
        await self.configure(3, 0)
        await self.post("batch/start", target_parts=9, cycle_time_ms=20, inspect_time_ms=20)
        await self.wait_for(lambda: self.events("batch_complete"), 20, "batch_complete")
        self.check_batch(9, 3, 0)

    async def test_queue_fills_and_nothing_is_lost(self):
        self.start_a()
        await self.start_gateway()
        self.start_b()
        await self.devices_up()
        await self.configure(5, 2)
        await self.post("batch/start", target_parts=15, cycle_time_ms=10, inspect_time_ms=250)
        await self.wait_for(lambda: self.events("batch_complete"), 40, "batch_complete")
        self.check_batch(15, 5, 2)
        statuses = self.events("status")
        self.assertTrue(any(s["queue"]["blocked"] for s in statuses), "production was never blocked")
        self.assertTrue(all(s["queue"]["depth"] <= 5 for s in statuses))
        self.assertTrue(any(s["queue"]["depth"] == 5 for s in statuses))

    async def test_lossy_serial_link(self):
        self.start_a(drop_rx=3, drop_tx=4)               # every 3rd frame to A and every 4th frame from A is lost
        await self.start_gateway()
        self.start_b()
        await self.devices_up()
        await self.configure(5, 2)
        await self.post("batch/start", target_parts=8, cycle_time_ms=30, inspect_time_ms=60)
        await self.wait_for(lambda: self.events("batch_complete"), 60, "batch_complete")
        self.check_batch(8, 5, 2)
        self.assertGreater(self.gw.engine.stats["retries"] + self.gw.engine.stats["part_duplicates"], 0)
        self.assertEqual(self.events("fault"), [])

    async def test_pause_resume_stop(self):
        self.start_a()
        await self.start_gateway()
        self.start_b()
        await self.devices_up()
        await self.configure(5, 2)
        await self.post("batch/start", target_parts=40, cycle_time_ms=100, inspect_time_ms=50)
        await self.wait_for(lambda: self.gw.engine.batch.inspected >= 3, 15, "3 parts inspected")
        st, _ = await self.post("batch/pause")
        self.assertEqual(st, 200)
        pause_id = self.cmd_n
        await self.wait_for(lambda: any(e["cmd_id"] == pause_id and e["stage"] == "completed" for e in self.events("cmd_event")),
                            5, "pause completed")
        await asyncio.sleep(0.5)                          # A may still have a part in flight; let it settle
        produced = self.gw.engine.batch.produced
        await asyncio.sleep(1.0)
        self.assertEqual(self.gw.engine.batch.produced, produced)       # nothing new while paused
        self.assertEqual(self.gw.engine.state, "paused")
        st, _ = await self.post("batch/resume")
        self.assertEqual(st, 200)
        await self.wait_for(lambda: self.gw.engine.batch.produced > produced, 5, "production resumed")
        st, _ = await self.post("batch/stop")
        self.assertEqual(st, 200)
        stop_id = self.cmd_n
        await self.wait_for(lambda: any(e["cmd_id"] == stop_id and e["stage"] == "completed" for e in self.events("cmd_event")),
                            5, "stop completed")
        self.assertEqual(self.gw.engine.state, "idle")
        stopped_at = self.gw.engine.batch.produced
        await asyncio.sleep(0.6)
        self.assertEqual(self.gw.engine.batch.produced, stopped_at)
        self.assertEqual(self.events("batch_complete"), [])
        self.assertEqual(self.events("fault"), [])
        # a new batch can start straight away (the old job on B is waited for)
        st, _ = await self.post("batch/start", target_parts=4, cycle_time_ms=30, inspect_time_ms=30)
        self.assertEqual(st, 200)
        await self.wait_for(lambda: self.events("batch_complete"), 20, "second batch complete")
        self.assertEqual(self.events("batch_complete")[0]["inspected"], 4)

    async def test_esp32_b_lost_during_batch(self):
        self.start_a()
        await self.start_gateway()
        b = self.start_b()
        await self.devices_up()
        await self.configure(5, 2)
        await self.post("batch/start", target_parts=100, cycle_time_ms=100, inspect_time_ms=50)
        await self.wait_for(lambda: self.gw.engine.batch.inspected >= 2, 15, "2 parts inspected")
        b.kill()
        await self.wait_for(lambda: self.events("fault"), 8, "fault event")
        f = self.events("fault")[0]
        self.assertEqual((f["code"], f["device"]), ("HEARTBEAT_LOST", "esp32_b"))
        self.assertEqual(self.gw.engine.state, "fault")
        # A was told to stop: it goes idle and stops making parts
        await asyncio.sleep(1.0)
        produced = self.gw.engine.batch.produced
        await asyncio.sleep(0.8)
        self.assertEqual(self.gw.engine.batch.produced, produced)
        # a dashboard that connects now still learns about the fault
        ws2 = await self.session.ws_connect(f"ws://127.0.0.1:{self.gw.http_port}/ws")
        first = json.loads((await ws2.receive(timeout=3)).data)
        second = json.loads((await ws2.receive(timeout=3)).data)
        await ws2.close()
        self.assertEqual((first["type"], first["state"]), ("status", "fault"))
        self.assertEqual((second["type"], second["code"]), ("fault", "HEARTBEAT_LOST"))
        # recover: clear, bring B back, run a batch
        st, _ = await self.post("batch/start", target_parts=3, cycle_time_ms=30, inspect_time_ms=30)
        self.assertEqual(st, 409)
        st, _ = await self.post("fault/clear")
        self.assertEqual(st, 200)
        self.start_b()
        await self.devices_up()
        await self.wait_for(lambda: self.gw.engine.b_cfg_confirmed, 5, "CONFIG after the new HELLO")
        self.ws_msgs.clear()
        await self.post("batch/start", target_parts=3, cycle_time_ms=30, inspect_time_ms=30)
        await self.wait_for(lambda: self.events("batch_complete"), 20, "batch after recovery")
        self.assertEqual(self.events("batch_complete")[0]["inspected"], 3)

    async def test_esp32_a_lost_during_batch(self):
        a = self.start_a()
        await self.start_gateway()
        self.start_b()
        await self.devices_up()
        await self.configure(5, 2)
        await self.post("batch/start", target_parts=100, cycle_time_ms=100, inspect_time_ms=50)
        await self.wait_for(lambda: self.gw.engine.batch.inspected >= 2, 15, "2 parts inspected")
        a.kill()
        await self.wait_for(lambda: self.events("fault"), 8, "fault event")
        f = self.events("fault")[0]
        self.assertEqual((f["code"], f["device"]), ("HEARTBEAT_LOST", "esp32_a"))

    async def test_serial_port_reopens(self):
        a = self.start_a()
        await self.start_gateway()
        await self.wait_for(lambda: self.gw.engine.a.connected, 5, "A connected")
        a.kill()                                          # the cable is pulled
        await self.wait_for(lambda: not self.gw.engine.a.connected, 6, "A shown as disconnected")
        self.assertEqual(self.gw.engine.state, "idle")    # idle: no fault, only connected=false
        self.start_a()                                    # plugged in again
        await self.wait_for(lambda: self.gw.engine.a.connected, 8, "A connected again")

    async def test_stale_result_after_a_restart_of_the_gateway(self):
        """A new gateway (job ids start at a random number) works with a B that remembers the old gateway's jobs."""
        self.start_a()
        await self.start_gateway()
        self.start_b()
        await self.devices_up()
        await self.configure(5, 2)
        await self.post("batch/start", target_parts=6, cycle_time_ms=20, inspect_time_ms=20)
        await self.wait_for(lambda: self.events("batch_complete"), 20, "first batch")
        await self.gw.stop()
        self.ws_task.cancel()
        self.gw = None
        self.ws_msgs.clear()
        await self.start_gateway(job_id_start=None)
        await self.devices_up()                           # B reconnects (it retries) and A answers again
        await self.configure(5, 2)
        await self.post("batch/start", target_parts=6, cycle_time_ms=20, inspect_time_ms=20)
        await self.wait_for(lambda: self.events("batch_complete"), 30, "second batch")
        self.check_batch(6, 5, 2)

    async def test_http_details(self):
        await self.start_gateway(with_a=False)
        # CORS preflight and headers
        async with self.session.options(f"{self.base}/api/v1/batch/start",
                                        headers={"Origin": "http://x", "Access-Control-Request-Method": "POST"}) as r:
            self.assertEqual(r.status, 204)
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
            self.assertIn("POST", r.headers["Access-Control-Allow-Methods"])
        st, s = await self.get("status")
        self.assertEqual(st, 200)
        self.assertEqual((s["state"], s["devices"]["esp32_a"]["connected"]), ("idle", False))
        async with self.session.get(f"{self.base}/api/v1/status") as r:
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
        # bad JSON, wrong type, empty body
        for raw in (b"{nope", b"[]", b"", b"NaN", b'{"cmd_id": NaN}'):
            async with self.session.post(f"{self.base}/api/v1/batch/pause", data=raw) as r:
                body = await r.json()
                self.assertEqual((r.status, body["error"]["code"]), (400, "MALFORMED"), raw)
        # state error, with the contract's exact body
        st, out = await self.post("batch/resume", cmd_id=17)
        self.assertEqual((st, out), (409, {"ok": False, "cmd_id": 17, "error": {
            "code": "INVALID_STATE", "message": "resume is not valid in state idle"}}))
        # duplicate over HTTP
        st, out = await self.post("mode", cmd_id=30, mode="benchmark")
        st2, out2 = await self.post("mode", cmd_id=30, mode="benchmark")
        self.assertEqual((out["duplicate"], out2["duplicate"]), (False, True))
        # report before any batch
        st, out = await self.get("batch/report")
        self.assertEqual((st, out["error"]["code"]), (409, "INVALID_STATE"))
        # missing device
        await self.post("mode", mode="production")
        st, out = await self.post("config/inspection", N=5, K=2)
        self.assertEqual((st, out["error"]["code"]), (503, "DEVICE_NOT_READY"))
        # unknown path
        async with self.session.post(f"{self.base}/api/v1/nope", json={}) as r:
            self.assertEqual(r.status, 404)

    async def test_websocket_status_stream(self):
        await self.start_gateway(with_a=False)
        await self.wait_for(lambda: len(self.events("status")) >= 1, 2, "status on connect")
        n = len(self.events("status"))
        await asyncio.sleep(2.3)
        self.assertGreaterEqual(len(self.events("status")) - n, 2)       # roughly one per second
        before = len(self.events("status"))
        await self.post("mode", mode="benchmark")
        await self.wait_for(lambda: len(self.events("status")) > before, 1, "status on change")
        self.assertEqual(self.events("status")[-1]["mode"], "benchmark")

    async def test_ui_folder_is_served(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write("<html>hello</html>")
        with open(os.path.join(d, "app.js"), "w") as f:
            f.write("//js")
        c = Config(http_host="127.0.0.1", http_port=0, b_host="127.0.0.1", b_port=self.b_port)
        self.gw = Gateway(c, None, ui_dir=d, console=False)
        await self.gw.start()
        base = f"http://127.0.0.1:{self.gw.http_port}"
        async with self.session.get(base + "/") as r:
            self.assertEqual((r.status, await r.text()), (200, "<html>hello</html>"))
        async with self.session.get(base + "/app.js") as r:
            self.assertEqual(r.status, 200)
        async with self.session.get(base + "/api/v1/status") as r:           # the API is not shadowed
            self.assertEqual((await r.json())["type"], "status")


class CommandLine(unittest.IsolatedAsyncioTestCase):
    """gateway.py started the way the README says, with the evidence log it writes."""

    async def test_cli_batch_and_evidence_log(self):
        sim_a, sim_b = build_sims()
        a_port, b_port, http_port = free_port(), free_port(), free_port()
        logdir = tempfile.mkdtemp()
        procs = []
        try:
            procs.append(Proc([sim_a, str(a_port)]))
            wait_listening(a_port)
            gw = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(HERE), "gateway.py"),
                                   "--serial", f"socket://127.0.0.1:{a_port}", "--http-host", "127.0.0.1",
                                   "--http-port", str(http_port), "--b-host", "127.0.0.1", "--b-port", str(b_port),
                                   "--log-dir", logdir, "--set", "queue_capacity=3"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(gw)
            wait_listening(http_port, 10)
            procs.append(Proc([sim_b, "127.0.0.1", str(b_port)]))
            base = f"http://127.0.0.1:{http_port}/api/v1"
            async with aiohttp.ClientSession() as sess:
                async def get_status():
                    async with sess.get(base + "/status") as r:
                        return await r.json()
                end = time.monotonic() + 10
                while time.monotonic() < end:
                    s = await get_status()
                    if s["devices"]["esp32_a"]["connected"] and s["devices"]["esp32_b"]["connected"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    self.fail("devices never connected")
                self.assertEqual(s["queue"]["capacity"], 3)                  # --set was applied
                async with sess.post(base + "/config/inspection", json={"cmd_id": 1, "N": 4, "K": 1}) as r:
                    self.assertEqual(r.status, 200)
                await asyncio.sleep(0.3)
                async with sess.post(base + "/batch/start", json={"cmd_id": 2, "target_parts": 6,
                                                                  "cycle_time_ms": 20, "inspect_time_ms": 30}) as r:
                    self.assertEqual(r.status, 200)
                end = time.monotonic() + 20
                while time.monotonic() < end:
                    async with sess.get(base + "/batch/report") as r:
                        if r.status == 200:
                            rep = await r.json()
                            break
                    await asyncio.sleep(0.1)
                else:
                    self.fail("batch never completed")
            fails = [p for p in range(1, 7) if (p + 1) % 4 == 0]
            self.assertEqual((rep["target"], rep["rejected"], rep["accepted"]), (6, len(fails), 6 - len(fails)))
        finally:
            for p in procs:
                if isinstance(p, Proc):
                    p.kill()
                elif p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(5)
                    except subprocess.TimeoutExpired:
                        p.kill()
        logs = [f for f in os.listdir(logdir) if f.endswith(".log")]
        self.assertEqual(len(logs), 1)
        with open(os.path.join(logdir, logs[0]), encoding="utf-8") as f:
            text = f.read()
        for needle in ("A-TX", "A-RX", "B-TX", "B-RX", "OP-RX", "OP-TX", "EVT", "SYS",
                       "CMD cmd_id=2 cmd=start cycle_time_ms=20 target_parts=6", "CMD_DONE cmd_id=2",
                       '"type":"CONFIG"', '"type":"JOB_REQ"', '"type":"JOB_RESULT"', "stage=completed", "BATCH COMPLETE"):
            self.assertIn(needle, text, needle)
        self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} \+ *\d+ ms ")     # timestamped lines


if __name__ == "__main__":
    unittest.main()
