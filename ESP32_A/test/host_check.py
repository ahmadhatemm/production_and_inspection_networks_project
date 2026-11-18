#!/usr/bin/env python3
"""Quick hardware check for ESP32 A. Plays the part of the gateway over the COM port.

    pip install pyserial
    python host_check.py COM10            run the check against the board
    python host_check.py --selftest       check this script's own frame code (no board needed)

Close the serial monitor first. The script keeps DTR and RTS low so that opening the port does not reset the board.

What it does:
  1. listens for 3 s and expects heartbeats (about 1 per second)
  2. sends Start (cycle 300 ms, 5 parts) and expects CMD_ACK accepted + CMD_DONE running
  3. answers each PART with PART_ACK accepted, except part 3, which gets QUEUE_FULL once
     (the same part must come back about 200 ms later, byte for byte)
  4. after part 5 is acknowledged, no part 6 may arrive
  5. sends Pause / Resume / Stop and checks each answer
  6. re-sends the same Stop command id: the answer must be repeated, nothing executed twice
Every step prints PASS or FAIL.
"""
import binascii
import struct
import sys
import time

END, ESC, ESC_END, ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD

PART, PART_ACK, CMD, CMD_ACK, CMD_DONE, HEARTBEAT = 1, 2, 3, 4, 5, 6
START, PAUSE, RESUME, STOP = 1, 2, 3, 4


def crc16(data: bytes) -> int:
    return binascii.crc_hqx(data, 0xFFFF)          # CRC-16/CCITT-FALSE


def atr(seq: int, payload: bytes) -> bytes:
    body = struct.pack(">HIH", 0, seq, len(payload)) + payload
    return body + struct.pack(">H", crc16(body))


def msg(mtype, mid=0, arg=0, a=0, b=0) -> bytes:
    return struct.pack(">BIBIIH", mtype, mid, arg, a, b, 0)


def slip(frame: bytes) -> bytes:
    out = bytearray([END])
    for x in frame:
        if x == END:
            out += bytes([ESC, ESC_END])
        elif x == ESC:
            out += bytes([ESC, ESC_ESC])
        else:
            out.append(x)
    out.append(END)
    return bytes(out)


class Decoder:
    """SLIP decoder that ignores junk (the ROM boot text has no valid CRC)."""

    def __init__(self):
        self.buf, self.esc, self.bad = bytearray(), False, False

    def feed(self, data: bytes):
        for x in data:
            if x == END:
                if self.buf and not self.bad:
                    yield bytes(self.buf)
                self.buf, self.esc, self.bad = bytearray(), False, False
            elif self.bad:
                continue
            elif self.esc:
                self.esc = False
                if x == ESC_END:
                    self.buf.append(END)
                elif x == ESC_ESC:
                    self.buf.append(ESC)
                else:
                    self.bad = True
            elif x == ESC:
                self.esc = True
            else:
                self.buf.append(x)


def parse(frame: bytes):
    """Returns (seq, type, id, arg, a, b) or None when the frame is not a valid production frame."""
    if len(frame) != 26 or crc16(frame[:-2]) != struct.unpack(">H", frame[-2:])[0]:
        return None
    test_id, seq, plen = struct.unpack(">HIH", frame[:8])
    if test_id != 0 or plen != 16:
        return None
    t, i, arg, a, b, _ = struct.unpack(">BIBIIH", frame[8:24])
    return seq, t, i, arg, a, b


def selftest() -> int:
    ok = True
    part1 = bytes.fromhex("0000000000000010" "01" "00000001" "00" "00000000" "00000000" "0000" "5ada")
    start = bytes.fromhex("0000000000050010" "03" "00000064" "01" "00000064" "00000003" "0000" "c237")
    ok &= atr(0, msg(PART, 1)) == part1
    ok &= atr(5, msg(CMD, 100, START, 100, 3)) == start
    ok &= parse(part1) == (0, PART, 1, 0, 0, 0)
    ok &= crc16(b"123456789") == 0x29B1
    d = Decoder()
    tricky = atr(0xC0DB0000, msg(PART, 0xC0DBC0DB, 0xDB, 0xC0C0C0C0, 0xDBDBDBDB))
    got = list(d.feed(b"junk\r\nboot text" + slip(tricky) + slip(part1)))
    ok &= [g for g in got if parse(g)] == [tricky, part1]      # the junk comes out as a packet too; parse() rejects it
    ok &= parse(b"junk\r\nboot text") is None
    print("selftest", "PASS" if ok else "FAIL")
    return 0 if ok else 1


class Link:
    def __init__(self, port):
        import serial
        self.ser = serial.Serial()
        self.ser.port, self.ser.baudrate, self.ser.timeout = port, 115200, 0.02
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self.dec, self.seq = Decoder(), 0
        self.t0 = time.time()
        self.next_hb = time.time()

    def send(self, mtype, mid=0, arg=0, a=0, b=0):
        self.ser.write(slip(atr(self.seq, msg(mtype, mid, arg, a, b))))
        self.seq += 1

    def poll(self, seconds):
        """Collect frames for a while; keeps our heartbeat going. Returns [(time, raw, parsed)]."""
        out, end = [], time.time() + seconds
        while time.time() < end:
            if time.time() >= self.next_hb:
                self.send(HEARTBEAT, self.seq)
                self.next_hb = time.time() + 1.0
            for raw in self.dec.feed(self.ser.read(256)):
                p = parse(raw)
                if p:
                    out.append((time.time() - self.t0, raw, p))
        return out


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + detail) if detail else ""))
    return cond


def main(port) -> int:
    L = Link(port)
    results = []

    # 1. heartbeats
    f = L.poll(3.2)
    hbs = [x for x in f if x[2][1] == HEARTBEAT]
    results.append(check("heartbeats arrive about once a second", 2 <= len(hbs) <= 5, f"{len(hbs)} in 3.2 s"))
    seqs = [x[2][0] for x in f]
    results.append(check("frame seq numbers count up by 1", all(b - a == 1 for a, b in zip(seqs, seqs[1:])) or len(seqs) < 2, str(seqs[:8])))

    # 2. Start
    cid = int(time.time()) & 0xFFFFFF
    L.send(CMD, cid, START, 300, 5)
    f = L.poll(0.5)
    ack = [x[2] for x in f if x[2][1] == CMD_ACK]
    done = [x[2] for x in f if x[2][1] == CMD_DONE]
    results.append(check("Start -> CMD_ACK accepted", len(ack) == 1 and ack[0][2] == cid and ack[0][3] == 1))
    results.append(check("Start -> CMD_DONE running", len(done) == 1 and done[0][2] == cid and done[0][3] == 1))
    t_start = time.time()

    # 3/4. five parts
    got, full_done, last_part_frame, resent_ok, times = [], False, None, None, {}
    deadline = time.time() + 8
    acked = 0
    while time.time() < deadline and acked < 5:
        for t, raw, p in L.poll(0.05):
            if p[1] != PART:
                continue
            pid = p[2]
            times.setdefault(pid, []).append(t)
            if pid == 3 and not full_done:
                full_done = True
                last_part_frame = raw
                L.send(PART_ACK, pid, 2)             # QUEUE_FULL
            else:
                if pid == 3 and last_part_frame is not None:
                    resent_ok = (raw == last_part_frame)
                L.send(PART_ACK, pid, 1)
                if pid not in got:
                    got.append(pid)
                acked = len(got)
    results.append(check("parts 1..5 arrive in order", got == [1, 2, 3, 4, 5], str(got)))
    if 1 in times:
        first_delay = times[1][0] - (t_start - L.t0)
        results.append(check("first part comes about one cycle (300 ms) after Start", 0.2 < first_delay < 0.6, f"{first_delay*1000:.0f} ms"))
    if len(times.get(3, [])) >= 2:
        gap = times[3][1] - times[3][0]
        results.append(check("QUEUE_FULL: part 3 comes again after about 200 ms", 0.15 < gap < 0.35, f"{gap*1000:.0f} ms"))
        results.append(check("QUEUE_FULL: the re-sent part is byte-identical", resent_ok is True))
    else:
        results.append(check("QUEUE_FULL: part 3 was re-sent", False))
    extra = [x for x in L.poll(1.5) if x[2][1] == PART]
    results.append(check("no part 6 after the batch is complete", not extra))

    # 5. Pause / Resume / Stop
    def command(code, expect_done):
        cid2 = (int(time.time() * 1000) & 0xFFFFFF) + code
        L.send(CMD, cid2, code)
        fr = L.poll(0.4)
        a = [x[2] for x in fr if x[2][1] == CMD_ACK and x[2][2] == cid2]
        d = [x[2] for x in fr if x[2][1] == CMD_DONE and x[2][2] == cid2]
        return cid2, a, d

    _, a, d = command(PAUSE, 2)
    results.append(check("Pause -> accepted, done=paused", len(a) == 1 and a[0][3] == 1 and len(d) == 1 and d[0][3] == 2))
    _, a, d = command(RESUME, 1)
    results.append(check("Resume -> accepted, done=running", len(a) == 1 and a[0][3] == 1 and len(d) == 1 and d[0][3] == 1))
    cid3, a, d = command(STOP, 3)
    results.append(check("Stop -> accepted, done=stopped", len(a) == 1 and a[0][3] == 1 and len(d) == 1 and d[0][3] == 3))

    # 6. duplicate Stop: same id again. Answers repeated, not executed again.
    L.send(CMD, cid3, STOP)
    fr = L.poll(0.4)
    a = [x[2] for x in fr if x[2][1] == CMD_ACK and x[2][2] == cid3]
    d = [x[2] for x in fr if x[2][1] == CMD_DONE and x[2][2] == cid3]
    results.append(check("duplicate Stop id -> same answers repeated (accepted + stopped)", len(a) == 1 and a[0][3] == 1 and len(d) == 1 and d[0][3] == 3))
    cid4, a, d = command(STOP, 3)
    results.append(check("a NEW Stop while idle -> rejected", len(a) == 1 and a[0][3] == 2 and not d))

    bad = results.count(False)
    print(f"\n{len(results) - bad} passed, {bad} failed")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
