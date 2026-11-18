"""The wire format between the gateway and ESP32 A: CRC-16, SLIP, ATR record, production message.

    one production message (16 bytes)
      -> ATR record:  test_id (2) | seq (4) | payload_len (2) | payload | crc16 (2)
      -> SLIP:        END + record with END and ESC escaped + END

Multi-byte integers are big-endian. Everything here must match ESP32 A's firmware
(crc16.c, atr.c, slip.c, prodmsg.c) and the Lab 3 host code (lab3proto.py).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# ---- CRC-16/CCITT-FALSE ---------------------------------------------------------


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    """Polynomial 0x1021, initial value 0xFFFF, no reflection. crc16_ccitt(b"123456789") is 0x29B1."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


# ---- SLIP (RFC 1055) ------------------------------------------------------------

END, ESC, ESC_END, ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD


def slip_encode(packet: bytes) -> bytes:
    """END, the packet with END and ESC bytes escaped, END. The leading END flushes line noise."""
    out = bytearray([END])
    for b in packet:
        if b == END:
            out += bytes([ESC, ESC_END])
        elif b == ESC:
            out += bytes([ESC, ESC_ESC])
        else:
            out.append(b)
    out.append(END)
    return bytes(out)


class SlipDecoder:
    """Rebuilds packets from a byte stream. Packets that are broken or too long are dropped and counted.

    ESP32 A's bootloader prints a few text lines at every reset. That text has no valid CRC,
    so it comes out of feed() as a packet that the ATR parser then rejects; it never gets further.
    """

    def __init__(self, max_len: int):
        self.max_len = max_len
        self.buf = bytearray()
        self.in_escape = False
        self.bad = False
        self.dropped = 0

    def feed(self, data: bytes) -> list[bytes]:
        packets = []
        for b in data:
            if b == END:
                if self.bad:
                    self.dropped += 1
                elif self.buf:                      # END END gives an empty packet: ignore it
                    packets.append(bytes(self.buf))
                self.buf.clear()
                self.in_escape = False
                self.bad = False
                continue
            if self.bad:
                continue                            # skip everything until the next END
            if self.in_escape:
                self.in_escape = False
                if b == ESC_END:
                    b = END
                elif b == ESC_ESC:
                    b = ESC
                else:
                    self.bad = True                 # ESC must be followed by ESC_END or ESC_ESC
                    continue
            elif b == ESC:
                self.in_escape = True
                continue
            if len(self.buf) >= self.max_len:
                self.bad = True                     # longer than any valid frame
                continue
            self.buf.append(b)
        return packets


# ---- ATR record -----------------------------------------------------------------

ATR_HEADER_LEN = 8
ATR_CRC_LEN = 2
ATR_OVERHEAD = ATR_HEADER_LEN + ATR_CRC_LEN
ATR_MAX_PAYLOAD = 128                               # benchmark sizes are 16, 64 and 128
ATR_MAX_FRAME_LEN = ATR_OVERHEAD + ATR_MAX_PAYLOAD
ATR_TEST_ID_PRODUCTION = 0
ATR_PROD_PAYLOAD_LEN = 16


class AtrError(Exception):
    """Raised by atr_parse. The text is 'short', 'crc' or 'length'."""


def atr_build(test_id: int, seq: int, payload: bytes) -> bytes:
    if len(payload) > ATR_MAX_PAYLOAD:
        raise ValueError(f"payload longer than {ATR_MAX_PAYLOAD} bytes")
    body = struct.pack(">HIH", test_id, seq & 0xFFFFFFFF, len(payload)) + bytes(payload)
    return body + struct.pack(">H", crc16_ccitt(body))


def atr_parse(raw: bytes) -> tuple[int, int, bytes]:
    """Returns (test_id, seq, payload). Checks the CRC before trusting any field, as the firmware does."""
    if len(raw) < ATR_OVERHEAD:
        raise AtrError("short")
    body = raw[:-ATR_CRC_LEN]
    (rx_crc,) = struct.unpack(">H", raw[-ATR_CRC_LEN:])
    if crc16_ccitt(body) != rx_crc:
        raise AtrError("crc")
    test_id, seq, payload_len = struct.unpack(">HIH", body[:ATR_HEADER_LEN])
    if payload_len > ATR_MAX_PAYLOAD or payload_len != len(body) - ATR_HEADER_LEN:
        raise AtrError("length")
    return test_id, seq, bytes(body[ATR_HEADER_LEN:])


# ---- Production message (the 16-byte ATR payload) ---------------------------------
#   offset size field
#        0    1 msg_type
#        1    4 id        part_id for part messages, cmd_id for commands, counter for heartbeats
#        5    1 arg
#        6    4 value_a   CMD start: cycle_time_ms, otherwise 0
#       10    4 value_b   CMD start: target_parts, otherwise 0
#       14    2 reserved  0

PM_PART = 0x01          # A -> gateway
PM_PART_ACK = 0x02      # gateway -> A
PM_CMD = 0x03           # gateway -> A
PM_CMD_ACK = 0x04       # A -> gateway
PM_CMD_DONE = 0x05      # A -> gateway
PM_HEARTBEAT = 0x06     # both

PM_NAMES = {PM_PART: "PART", PM_PART_ACK: "PART_ACK", PM_CMD: "CMD", PM_CMD_ACK: "CMD_ACK",
            PM_CMD_DONE: "CMD_DONE", PM_HEARTBEAT: "HEARTBEAT"}

ACK_ACCEPTED = 1        # arg of PART_ACK
ACK_QUEUE_FULL = 2

CMD_START, CMD_PAUSE, CMD_RESUME, CMD_STOP = 1, 2, 3, 4      # arg of CMD
CMD_NAMES = {CMD_START: "start", CMD_PAUSE: "pause", CMD_RESUME: "resume", CMD_STOP: "stop"}

CMDACK_ACCEPTED, CMDACK_REJECTED = 1, 2                      # arg of CMD_ACK

DONE_RUNNING, DONE_PAUSED, DONE_STOPPED = 1, 2, 3            # arg of CMD_DONE: the state A is in now
DONE_NAMES = {DONE_RUNNING: "running", DONE_PAUSED: "paused", DONE_STOPPED: "stopped"}

# The state ESP32 A must report in CMD_DONE after each command
EXPECTED_DONE = {CMD_START: DONE_RUNNING, CMD_PAUSE: DONE_PAUSED, CMD_RESUME: DONE_RUNNING, CMD_STOP: DONE_STOPPED}


@dataclass
class ProdMsg:
    type: int
    id: int
    arg: int = 0
    value_a: int = 0
    value_b: int = 0


def prod_encode(m: ProdMsg) -> bytes:
    return struct.pack(">BIBIIH", m.type, m.id, m.arg, m.value_a, m.value_b, 0)


def prod_decode(payload: bytes) -> ProdMsg:
    """Reads exactly 16 bytes. The reserved bytes are ignored."""
    if len(payload) != ATR_PROD_PAYLOAD_LEN:
        raise ValueError("production payload must be 16 bytes")
    t, ident, arg, a, b, _reserved = struct.unpack(">BIBIIH", payload)
    return ProdMsg(t, ident, arg, a, b)


def describe(m: ProdMsg) -> str:
    """One readable line for the evidence log."""
    name = PM_NAMES.get(m.type, f"type_0x{m.type:02x}")
    if m.type == PM_PART:
        return f"PART part_id={m.id}"
    if m.type == PM_PART_ACK:
        return f"PART_ACK part_id={m.id} arg={'accepted' if m.arg == ACK_ACCEPTED else 'queue_full' if m.arg == ACK_QUEUE_FULL else m.arg}"
    if m.type == PM_CMD:
        extra = f" cycle_time_ms={m.value_a} target_parts={m.value_b}" if m.arg == CMD_START else ""
        return f"CMD cmd_id={m.id} cmd={CMD_NAMES.get(m.arg, m.arg)}{extra}"
    if m.type == PM_CMD_ACK:
        return f"CMD_ACK cmd_id={m.id} arg={'accepted' if m.arg == CMDACK_ACCEPTED else 'rejected' if m.arg == CMDACK_REJECTED else m.arg}"
    if m.type == PM_CMD_DONE:
        return f"CMD_DONE cmd_id={m.id} state={DONE_NAMES.get(m.arg, m.arg)}"
    if m.type == PM_HEARTBEAT:
        return f"HEARTBEAT counter={m.id}"
    return f"{name} id={m.id} arg={m.arg}"


class AFramer:
    """Builds the ATR records the gateway sends to ESP32 A. The ATR seq counts every frame we send, from 0."""

    def __init__(self) -> None:
        self.tx_seq = 0

    def build(self, m: ProdMsg) -> tuple[bytes, int]:
        """Returns (record, seq). A retry sends these same bytes again, so the seq is fixed here."""
        seq = self.tx_seq
        self.tx_seq = (self.tx_seq + 1) & 0xFFFFFFFF
        return atr_build(ATR_TEST_ID_PRODUCTION, seq, prod_encode(m)), seq
