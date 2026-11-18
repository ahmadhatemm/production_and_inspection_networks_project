#!/usr/bin/env python3
"""Quick check for ESP32 B. Plays the gateway: listens on TCP port 5000 and talks to the board.

    python quick_check_b.py            (or: python quick_check_b.py 5000)

1. Set GATEWAY_IP in main/config.h to this PC's address, build, flash.
2. Allow Python through the Windows firewall (incoming, private network).
3. Press the board's EN (reset) button, THEN run this script. It must be a fresh boot, because step 2
   expects "not_configured" and the board keeps its settings until it reboots.
Every step prints PASS or FAIL. Takes about 10 seconds.
"""
import json
import socket
import struct
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
fails = 0


def check(name, ok):
    global fails
    fails += 0 if ok else 1
    print(("PASS  " if ok else "FAIL  ") + name)


class Gateway:
    def __init__(self, conn):
        self.conn, self.buf, self.beats, self.last_hb, self.raw = conn, b"", 0, 0.0, ""

    def send(self, msg):
        body = json.dumps(msg, separators=(",", ":")).encode()
        self.conn.sendall(struct.pack(">I", len(body)) + body)

    def get(self, secs):
        """Next message that is not a heartbeat, or None after `secs`. Sends a heartbeat every second."""
        end = time.time() + secs
        while time.time() < end:
            if time.time() - self.last_hb >= 1.0:
                self.send({"type": "HEARTBEAT", "seq": int(self.last_hb)})
                self.last_hb = time.time()
            if len(self.buf) >= 4 and len(self.buf) >= 4 + struct.unpack(">I", self.buf[:4])[0]:
                n = struct.unpack(">I", self.buf[:4])[0]
                body, self.buf = self.buf[4:4 + n], self.buf[4 + n:]
                self.raw, msg = body.decode(), json.loads(body)
                if msg["type"] == "HEARTBEAT":
                    self.beats += 1
                    continue
                return msg
            self.conn.settimeout(0.05)
            try:
                data = self.conn.recv(4096)
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError("board closed the connection")
            self.buf += data
        return None


def ack(ref, key, ident, status="accepted"):
    return {"type": "ACK", "ref": ref, key: ident, "status": status}


srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", PORT))
srv.listen(1)
print(f"Listening on port {PORT}. Reset the board now...")
conn, addr = srv.accept()
conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
print("Board connected from", addr[0])
gw = Gateway(conn)

m = gw.get(5)
check("HELLO from esp32_b", bool(m) and m["type"] == "HELLO" and m["device"] == "esp32_b")
gw.get(2.5)
check("heartbeats arrive (about 1 per second)", gw.beats >= 2)

gw.send({"type": "JOB_REQ", "job_id": 1, "part_id": 1})
m = gw.get(1)
check("job before CONFIG is rejected: not_configured",
      m == {"type": "ACK", "ref": "JOB_REQ", "job_id": 1, "status": "rejected", "reason": "not_configured"})

gw.send({"type": "CONFIG", "cmd_id": 1, "N": 0, "K": 2, "inspect_time_ms": 500})
m = gw.get(1)
check("CONFIG with N=0 is rejected: out_of_range",
      m == {"type": "ACK", "ref": "CONFIG", "cmd_id": 1, "status": "rejected", "reason": "out_of_range"})

gw.send({"type": "CONFIG", "cmd_id": 2, "N": 5, "K": 2, "inspect_time_ms": 500})
m1, m2 = gw.get(1), gw.get(1)
check("CONFIG accepted (ACK)", m1 == ack("CONFIG", "cmd_id", 2))
check("CONFIG_DONE with N=5 K=2 inspect_time_ms=500",
      m2 == {"type": "CONFIG_DONE", "cmd_id": 2, "N": 5, "K": 2, "inspect_time_ms": 500})
gw.send(ack("CONFIG_DONE", "cmd_id", 2))

for job, part, want in [(28, 8, "FAIL"), (29, 9, "PASS")]:       # (8+2)%5==0 -> FAIL, (9+2)%5!=0 -> PASS
    t0 = time.time()
    gw.send({"type": "JOB_REQ", "job_id": job, "part_id": part})
    a, r = gw.get(1), gw.get(2)
    waited = time.time() - t0
    check(f"job {job} part {part}: ACK accepted", a == ack("JOB_REQ", "job_id", job))
    check(f"job {job} part {part}: {want} after ~500 ms (took {waited:.2f} s)",
          r == {"type": "JOB_RESULT", "job_id": job, "part_id": part, "verdict": want, "N": 5, "K": 2}
          and 0.45 <= waited <= 1.2)
    gw.send(ack("JOB_RESULT", "job_id", job))

gw.send({"type": "JOB_REQ", "job_id": 40, "part_id": 4})
gw.get(1)
gw.send({"type": "JOB_REQ", "job_id": 41, "part_id": 5})
m = gw.get(1)
check("second job while busy is rejected: busy",
      m == {"type": "ACK", "ref": "JOB_REQ", "job_id": 41, "status": "rejected", "reason": "busy"})
gw.get(2)                                                         # result of job 40: left unacknowledged
first = gw.raw
t0 = time.time()
m = gw.get(1)
check("unacknowledged result is re-sent after ~500 ms, same bytes",
      m is not None and gw.raw == first and 0.4 <= time.time() - t0 <= 0.9)
gw.send(ack("JOB_RESULT", "job_id", 40))

print("\nALL PASSED" if fails == 0 else f"\n{fails} CHECK(S) FAILED")
sys.exit(1 if fails else 0)
