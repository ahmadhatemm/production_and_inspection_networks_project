#!/usr/bin/env python3
"""Quick hardware check for ESP32 B. Plays the part of the gateway on a TCP port.

    python host_check_b.py              listen on port 5000 and wait for ESP32 B to connect
    python host_check_b.py 5000         the same, with the port given
    python host_check_b.py --selftest   check this script's own framing code (no board needed)

Before running it against the real board:
  * the PC must be on the same network as the board, and GATEWAY_IP in config.h must be the PC's
    address on that network (then rebuild and flash);
  * Windows Firewall must allow incoming connections to this port for Python.

What it does (about 40 s). Every step prints PASS or FAIL:
  1. ESP32 B connects and sends HELLO, then about one heartbeat per second
  2. a job before any CONFIG is rejected ("not_configured")
  3. CONFIG is answered with ACK, then CONFIG_DONE carrying the same values
  4. a job gets an ACK at once and, after inspect_time_ms, a JOB_RESULT with the right verdict
     (the example of the contract: part 8, N=5, K=2 -> FAIL; part 9 -> PASS)
  5. a second job while one is running is rejected ("busy")
  6. the same job_id sent again is acknowledged again and not inspected again
  7. new N, K and time are applied live; a repeated cmd_id is answered but not applied again
  8. an unacknowledged JOB_RESULT is sent 4 times in all, 500 ms apart, with identical bytes
  9. invalid CONFIG values are rejected and not applied
 10. garbage and an oversized length make ESP32 B close the connection and reconnect
 11. a silent gateway (no heartbeats) makes ESP32 B drop the link after about 3 s and reconnect
 12. a job that was running when the connection dropped is never completed late and is
     rejected as "abandoned" if the gateway offers it again
"""
import json
import select
import socket
import struct
import sys
import time

MAX_BODY = 1024


def frame(obj_or_bytes) -> bytes:
    body = obj_or_bytes if isinstance(obj_or_bytes, bytes) else json.dumps(obj_or_bytes, separators=(",", ":")).encode()
    return struct.pack(">I", len(body)) + body


def selftest() -> int:
    ok = True
    m = b'{"type":"JOB_REQ","job_id":28,"part_id":8}'
    ok &= len(m) == 42 and frame(m)[:4] == b"\x00\x00\x00\x2a" and frame(m)[4:] == m
    ok &= frame({"type": "HEARTBEAT", "seq": 1}) == b'\x00\x00\x00\x1c{"type":"HEARTBEAT","seq":1}'
    print("selftest", "PASS" if ok else "FAIL")
    return 0 if ok else 1


class Gateway:
    def __init__(self, port):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("0.0.0.0", port))
        self.srv.listen(1)
        self.conn = None
        self.buf = b""
        self.inbox = []             # (time, object) of everything except heartbeats
        self.hbs = []               # (time, seq) of heartbeats from ESP32 B
        self.hb_on = True
        self.next_hb = 0.0
        self.hb_seq = 0
        self.closed = False
        self.t0 = time.time()

    def now(self):
        return time.time() - self.t0

    def accept(self, timeout):
        self.srv.settimeout(timeout)
        try:
            c, addr = self.srv.accept()
        except socket.timeout:
            return False
        if self.conn:
            self.conn.close()
        self.conn = c
        self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf, self.inbox, self.hbs, self.closed = b"", [], [], False
        self.next_hb = time.time() + 1.0
        return True

    def send(self, obj):
        self.conn.sendall(frame(obj))

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end and not self.closed:
            if self.hb_on and time.time() >= self.next_hb:
                self.hb_seq += 1
                try:
                    self.send({"type": "HEARTBEAT", "seq": self.hb_seq})
                except OSError:
                    self.closed = True
                self.next_hb = time.time() + 1.0
            r, _, _ = select.select([self.conn], [], [], 0.01)
            if r:
                try:
                    d = self.conn.recv(4096)
                except OSError:
                    d = b""
                if not d:
                    self.closed = True
                    break
                self.buf += d
                self.parse()

    def parse(self):
        while len(self.buf) >= 4:
            n = struct.unpack(">I", self.buf[:4])[0]
            if len(self.buf) < 4 + n:
                return
            body, self.buf = self.buf[4:4 + n], self.buf[4 + n:]
            try:
                o = json.loads(body)
            except ValueError:
                o = {"type": "?BAD_JSON", "raw": body}
            if o.get("type") == "HEARTBEAT":
                self.hbs.append((self.now(), o.get("seq")))
            else:
                self.inbox.append((self.now(), o))

    def wait(self, pred, timeout):
        """Returns (time, message) of the first matching message, removing it from the inbox, or None."""
        end = time.time() + timeout
        while True:
            for i, (t, o) in enumerate(self.inbox):
                if pred(o):
                    del self.inbox[i]
                    return t, o
            if time.time() >= end or self.closed:
                return None
            self.pump(0.02)

    def take_all(self, pred):
        got = [(t, o) for t, o in self.inbox if pred(o)]
        self.inbox = [(t, o) for t, o in self.inbox if not pred(o)]
        return got


def is_type(t, **kw):
    return lambda o: o.get("type") == t and all(o.get(k) == v for k, v in kw.items())


results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + str(detail)) if detail else ""))
    return cond


def main(port) -> int:
    g = Gateway(port)
    print(f"waiting for ESP32 B to connect to port {port} ...")
    if not g.accept(120):
        print("FAIL  ESP32 B did not connect within 120 s")
        return 1

    # 1. HELLO and heartbeats
    m = g.wait(is_type("HELLO"), 3)
    check("HELLO arrives first", m is not None and m[1].get("device") == "esp32_b" and isinstance(m[1].get("fw"), str), m and m[1])
    g.pump(2.6)
    seqs = [s for _, s in g.hbs]
    check("heartbeats about once a second, seq counting up", 2 <= len(seqs) <= 4 and seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs)

    # 2. job before CONFIG
    g.send({"type": "JOB_REQ", "job_id": 1, "part_id": 1})
    m = g.wait(is_type("ACK", ref="JOB_REQ", job_id=1), 1)
    check("job before CONFIG is rejected (not_configured)", m and m[1].get("status") == "rejected" and m[1].get("reason") == "not_configured", m and m[1])

    # 3. CONFIG
    g.send({"type": "CONFIG", "cmd_id": 18, "N": 5, "K": 2, "inspect_time_ms": 500})
    a = g.wait(is_type("ACK", ref="CONFIG", cmd_id=18), 1)
    d = g.wait(is_type("CONFIG_DONE", cmd_id=18), 1)
    check("CONFIG -> ACK accepted", a and a[1].get("status") == "accepted", a and a[1])
    check("CONFIG -> CONFIG_DONE with the same N, K, inspect_time_ms",
          d and (d[1].get("N"), d[1].get("K"), d[1].get("inspect_time_ms")) == (5, 2, 500), d and d[1])
    g.send({"type": "ACK", "ref": "CONFIG_DONE", "cmd_id": 18, "status": "accepted"})

    # 4. the contract example and a PASS
    t_req = g.now()
    g.send({"type": "JOB_REQ", "job_id": 28, "part_id": 8})
    a = g.wait(is_type("ACK", ref="JOB_REQ", job_id=28), 1)
    check("JOB_REQ -> ACK accepted at once", a and a[1].get("status") == "accepted" and a[0] - t_req < 0.3, a and round(a[0] - t_req, 3))
    r = g.wait(is_type("JOB_RESULT", job_id=28), 2)
    check("JOB_RESULT part 8 = FAIL with N=5 K=2", r and (r[1].get("part_id"), r[1].get("verdict"), r[1].get("N"), r[1].get("K")) == (8, "FAIL", 5, 2), r and r[1])
    check("result comes after inspect_time_ms (about 500 ms)", r and 0.45 < r[0] - t_req < 0.9, r and round(r[0] - t_req, 3))
    g.send({"type": "ACK", "ref": "JOB_RESULT", "job_id": 28, "status": "accepted"})

    g.send({"type": "JOB_REQ", "job_id": 29, "part_id": 9})
    g.wait(is_type("ACK", ref="JOB_REQ", job_id=29), 1)
    r = g.wait(is_type("JOB_RESULT", job_id=29), 2)
    check("JOB_RESULT part 9 = PASS", r and r[1].get("verdict") == "PASS", r and r[1])
    g.send({"type": "ACK", "ref": "JOB_RESULT", "job_id": 29, "status": "accepted"})

    # 5. busy
    g.send({"type": "JOB_REQ", "job_id": 30, "part_id": 10})
    g.send({"type": "JOB_REQ", "job_id": 31, "part_id": 11})
    a30 = g.wait(is_type("ACK", ref="JOB_REQ", job_id=30), 1)
    a31 = g.wait(is_type("ACK", ref="JOB_REQ", job_id=31), 1)
    check("second job while busy is rejected (busy)", a30 and a30[1].get("status") == "accepted" and a31 and a31[1].get("status") == "rejected" and a31[1].get("reason") == "busy",
          (a30 and a30[1], a31 and a31[1]))
    r30 = g.wait(is_type("JOB_RESULT", job_id=30), 2)
    check("the first job still finishes (part 10, PASS)", r30 and r30[1].get("verdict") == "PASS", r30 and r30[1])
    g.send({"type": "ACK", "ref": "JOB_RESULT", "job_id": 30, "status": "accepted"})

    # 6. duplicate job
    g.send({"type": "JOB_REQ", "job_id": 30, "part_id": 10})
    a = g.wait(is_type("ACK", ref="JOB_REQ", job_id=30), 1)
    r = g.wait(is_type("JOB_RESULT", job_id=30), 1)
    g.send({"type": "ACK", "ref": "JOB_RESULT", "job_id": 30, "status": "accepted"})
    check("repeated job_id -> ACK again and the same result again, not inspected twice",
          a and a[1].get("status") == "accepted" and r and r[1] == r30[1] and g.wait(is_type("JOB_RESULT", job_id=30), 1.2) is None,
          (a and a[1], r and r[1]))

    # 7. live reconfiguration and a repeated cmd_id
    g.send({"type": "CONFIG", "cmd_id": 19, "N": 3, "K": 0, "inspect_time_ms": 100})
    g.wait(is_type("ACK", ref="CONFIG", cmd_id=19), 1)
    d = g.wait(is_type("CONFIG_DONE", cmd_id=19), 1)
    g.send({"type": "ACK", "ref": "CONFIG_DONE", "cmd_id": 19, "status": "accepted"})
    t_req = g.now()
    g.send({"type": "JOB_REQ", "job_id": 32, "part_id": 9})
    g.wait(is_type("ACK", ref="JOB_REQ", job_id=32), 1)
    r = g.wait(is_type("JOB_RESULT", job_id=32), 2)
    check("new parameters applied live: part 9, N=3 K=0 -> FAIL after about 100 ms",
          r and (r[1].get("verdict"), r[1].get("N"), r[1].get("K")) == ("FAIL", 3, 0) and r[0] - t_req < 0.4, r and (r[1], round(r[0] - t_req, 3)))
    g.send({"type": "ACK", "ref": "JOB_RESULT", "job_id": 32, "status": "accepted"})

    g.send({"type": "CONFIG", "cmd_id": 18, "N": 5, "K": 2, "inspect_time_ms": 500})
    a = g.wait(is_type("ACK", ref="CONFIG", cmd_id=18), 1)
    d = g.wait(is_type("CONFIG_DONE", cmd_id=18), 1)
    g.send({"type": "ACK", "ref": "CONFIG_DONE", "cmd_id": 18, "status": "accepted"})
    g.send({"type": "JOB_REQ", "job_id": 34, "part_id": 9})
    g.wait(is_type("ACK", ref="JOB_REQ", job_id=34), 1)
    r = g.wait(is_type("JOB_RESULT", job_id=34), 2)
    check("repeated cmd_id: answered again with its old values, not applied again (N=3 K=0 still in use)",
          a and d and (d[1].get("N"), d[1].get("K")) == (5, 2) and r and (r[1].get("N"), r[1].get("K")) == (3, 0), (d and d[1], r and r[1]))
    g.send({"type": "ACK", "ref": "JOB_RESULT", "job_id": 34, "status": "accepted"})

    # 8. result retries
    g.send({"type": "JOB_REQ", "job_id": 33, "part_id": 3})
    g.wait(is_type("ACK", ref="JOB_REQ", job_id=33), 1)
    first = g.wait(is_type("JOB_RESULT", job_id=33), 2)
    more = [first] if first else []
    while True:
        m = g.wait(is_type("JOB_RESULT", job_id=33), 0.9)
        if m is None:
            break
        more.append(m)
    gaps = [round(more[i + 1][0] - more[i][0], 2) for i in range(len(more) - 1)]
    check("unacknowledged JOB_RESULT: 4 sends in all, same bytes", len(more) == 4 and all(x[1] == more[0][1] for x in more), len(more))
    check("... about 500 ms apart", len(gaps) == 3 and all(0.4 < x < 0.7 for x in gaps), gaps)

    # 9. invalid CONFIG
    g.send({"type": "CONFIG", "cmd_id": 60, "N": 0, "K": 2, "inspect_time_ms": 500})
    a = g.wait(is_type("ACK", ref="CONFIG", cmd_id=60), 1)
    g.send({"type": "CONFIG", "cmd_id": 61, "K": 2, "inspect_time_ms": 500})
    b = g.wait(is_type("ACK", ref="CONFIG", cmd_id=61), 1)
    g.send({"type": "CONFIG", "cmd_id": 62, "N": 5, "K": 2, "inspect_time_ms": 5})
    c = g.wait(is_type("ACK", ref="CONFIG", cmd_id=62), 1)
    check("invalid CONFIG rejected (N=0, N missing, time 5 ms), never completed",
          a and a[1].get("reason") == "out_of_range" and b and b[1].get("reason") == "malformed" and c and c[1].get("reason") == "out_of_range"
          and not g.take_all(is_type("CONFIG_DONE")) and all(x and x[1].get("status") == "rejected" for x in (a, b, c)), [x and x[1] for x in (a, b, c)])

    # 10. garbage in a good frame is ignored; an oversized length ends the connection
    g.conn.sendall(frame(b"this is not json"))
    g.send({"type": "JOB_REQ", "job_id": 35, "part_id": 4})
    m = g.wait(is_type("ACK", ref="JOB_REQ", job_id=35), 1)
    check("a non-JSON message is ignored and the connection stays up", m and m[1].get("status") == "accepted" and not g.closed, m and m[1])
    g.wait(is_type("JOB_RESULT", job_id=35), 2)
    g.send({"type": "ACK", "ref": "JOB_RESULT", "job_id": 35, "status": "accepted"})
    g.conn.sendall(struct.pack(">I", 2000))
    g.pump(1.0)
    check("an announced length of 2000 makes ESP32 B close the connection", g.closed)
    ok = g.accept(6)
    m = ok and g.wait(is_type("HELLO"), 2)
    check("... and it reconnects and says HELLO again", ok and m is not None)

    # parameters are kept in RAM across a reconnect
    g.send({"type": "JOB_REQ", "job_id": 36, "part_id": 9})
    g.wait(is_type("ACK", ref="JOB_REQ", job_id=36), 1)
    r = g.wait(is_type("JOB_RESULT", job_id=36), 2)
    check("parameters survive the reconnect (N=3 K=0 from the last CONFIG that was applied)", r and (r[1].get("N"), r[1].get("K")) == (3, 0), r and r[1])
    g.send({"type": "ACK", "ref": "JOB_RESULT", "job_id": 36, "status": "accepted"})

    # 11. silent gateway
    g.hb_on = False
    t_silent = time.time()
    while not g.closed and time.time() - t_silent < 6:
        g.pump(0.05)
    waited = time.time() - t_silent
    check("no heartbeat from the gateway: ESP32 B drops the link after about 3 s", g.closed and 2.7 < waited < 4.2, round(waited, 2))
    g.hb_on = True
    ok = g.accept(6)
    m = ok and g.wait(is_type("HELLO"), 2)
    check("... and reconnects", ok and m is not None)

    # 12. a job interrupted by a lost connection
    g.send({"type": "CONFIG", "cmd_id": 70, "N": 5, "K": 2, "inspect_time_ms": 3000})
    g.wait(is_type("ACK", ref="CONFIG", cmd_id=70), 1)
    g.wait(is_type("CONFIG_DONE", cmd_id=70), 1)
    g.send({"type": "ACK", "ref": "CONFIG_DONE", "cmd_id": 70, "status": "accepted"})
    g.send({"type": "JOB_REQ", "job_id": 50, "part_id": 8})
    g.wait(is_type("ACK", ref="JOB_REQ", job_id=50), 1)
    g.pump(0.5)
    g.conn.close()                                              # the connection drops while job 50 is being inspected
    ok = g.accept(6)
    m = ok and g.wait(is_type("HELLO"), 2)
    g.send({"type": "JOB_REQ", "job_id": 50, "part_id": 8})
    a = g.wait(is_type("ACK", ref="JOB_REQ", job_id=50), 1)
    g.pump(3.5)
    late = g.take_all(is_type("JOB_RESULT", job_id=50))
    check("interrupted job: offered again, rejected as 'abandoned'; no late verdict",
          ok and m is not None and a and a[1].get("status") == "rejected" and a[1].get("reason") == "abandoned" and not late, a and a[1])

    bad = results.count(False)
    print(f"\n{len(results) - bad} passed, {bad} failed")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    port = int(sys.argv[1]) if len(sys.argv) == 2 else 5000
    sys.exit(main(port))
