"""Byte-level tests: the gateway's encoders and decoders against vectors made by ESP32 A's own C code.

The vectors below were printed by a small C program built from ESP32_A/main (atr.c, prodmsg.c, crc16.c, slip.c),
so a match here means the gateway and the firmware agree byte for byte.
Run:  python -m unittest tests.test_codec -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gw import bproto
from gw import serialproto as sp

# name: (ProdMsg fields, ATR seq, payload hex, ATR record hex, SLIP hex)   all produced by ESP32 A's C code
VECTORS = {
    "heartbeat": (sp.ProdMsg(sp.PM_HEARTBEAT, 5), 7,
                  "06000000050000000000000000000000",
                  "000000000007001006000000050000000000000000000000058e",
                  "c0000000000007001006000000050000000000000000000000058ec0"),
    "start": (sp.ProdMsg(sp.PM_CMD, 17, sp.CMD_START, 1000, 20), 3,
              "030000001101000003e8000000140000",
              "0000000000030010030000001101000003e80000001400005b32",
              "c00000000000030010030000001101000003e80000001400005b32c0"),
    "part": (sp.ProdMsg(sp.PM_PART, 3), 9,
             "01000000030000000000000000000000",
             "0000000000090010010000000300000000000000000000001211",
             "c00000000000090010010000000300000000000000000000001211c0"),
    "escapes": (sp.ProdMsg(sp.PM_CMD, 0xC0DBC0DB, 1, 0xDBC0, 0xC0DB), 0xC0,
                "03c0dbc0db010000dbc00000c0db0000",
                "0000000000c0001003c0dbc0db010000dbc00000c0db000031bf",
                "c00000000000dbdc001003dbdcdbdddbdcdbdd010000dbdddbdc0000dbdcdbdd000031bfc0"),
}


class Crc(unittest.TestCase):
    def test_check_value(self):
        self.assertEqual(sp.crc16_ccitt(b"123456789"), 0x29B1)

    def test_empty_is_initial_value(self):
        self.assertEqual(sp.crc16_ccitt(b""), 0xFFFF)


class Vectors(unittest.TestCase):
    def test_payload_matches_firmware(self):
        for name, (msg, seq, payload, atr, slip) in VECTORS.items():
            with self.subTest(name):
                self.assertEqual(sp.prod_encode(msg).hex(), payload)

    def test_atr_matches_firmware(self):
        for name, (msg, seq, payload, atr, slip) in VECTORS.items():
            with self.subTest(name):
                self.assertEqual(sp.atr_build(0, seq, bytes.fromhex(payload)).hex(), atr)

    def test_slip_matches_firmware(self):
        for name, (msg, seq, payload, atr, slip) in VECTORS.items():
            with self.subTest(name):
                self.assertEqual(sp.slip_encode(bytes.fromhex(atr)).hex(), slip)

    def test_decode_firmware_frames(self):
        for name, (msg, seq, payload, atr, slip) in VECTORS.items():
            with self.subTest(name):
                dec = sp.SlipDecoder(sp.ATR_MAX_FRAME_LEN)
                packets = dec.feed(bytes.fromhex(slip))
                self.assertEqual(len(packets), 1)
                test_id, rx_seq, rx_payload = sp.atr_parse(packets[0])
                self.assertEqual((test_id, rx_seq), (0, seq))
                self.assertEqual(sp.prod_decode(rx_payload), msg)

    def test_framer_numbers_every_frame(self):
        f = sp.AFramer()
        rec0, s0 = f.build(sp.ProdMsg(sp.PM_HEARTBEAT, 0))
        rec1, s1 = f.build(sp.ProdMsg(sp.PM_HEARTBEAT, 1))
        self.assertEqual((s0, s1), (0, 1))
        self.assertEqual(sp.atr_parse(rec1)[1], 1)


class AtrParse(unittest.TestCase):
    GOOD = bytes.fromhex(VECTORS["start"][3])

    def test_good(self):
        self.assertEqual(sp.atr_parse(self.GOOD)[1], 3)

    def test_bad_crc(self):
        bad = bytearray(self.GOOD)
        bad[12] ^= 1
        with self.assertRaises(sp.AtrError) as e:
            sp.atr_parse(bytes(bad))
        self.assertEqual(str(e.exception), "crc")

    def test_every_single_bit_flip_is_caught(self):
        for i in range(len(self.GOOD) * 8):
            bad = bytearray(self.GOOD)
            bad[i // 8] ^= 1 << (i % 8)
            with self.assertRaises(sp.AtrError):
                sp.atr_parse(bytes(bad))

    def test_short(self):
        with self.assertRaises(sp.AtrError) as e:
            sp.atr_parse(b"\x00" * 9)
        self.assertEqual(str(e.exception), "short")

    def test_length_field_must_match(self):
        body = bytes.fromhex("0000" "00000001" "0005") + b"\x01\x02\x03"      # says 5, carries 3
        rec = body + sp.crc16_ccitt(body).to_bytes(2, "big")
        with self.assertRaises(sp.AtrError) as e:
            sp.atr_parse(rec)
        self.assertEqual(str(e.exception), "length")

    def test_payload_over_128_rejected(self):
        with self.assertRaises(ValueError):
            sp.atr_build(0, 0, b"\x00" * 129)

    def test_prod_decode_needs_16_bytes(self):
        with self.assertRaises(ValueError):
            sp.prod_decode(b"\x00" * 15)


class Slip(unittest.TestCase):
    def test_roundtrip_all_byte_values(self):
        pkt = bytes(range(256))
        dec = sp.SlipDecoder(300)
        self.assertEqual(dec.feed(sp.slip_encode(pkt)), [pkt])

    def test_byte_at_a_time(self):
        pkt = bytes.fromhex(VECTORS["escapes"][3])
        dec = sp.SlipDecoder(sp.ATR_MAX_FRAME_LEN)
        out = []
        for b in sp.slip_encode(pkt):
            out += dec.feed(bytes([b]))
        self.assertEqual(out, [pkt])

    def test_two_packets_in_one_read(self):
        a, b = b"\x01\x02", b"\x03\xc0\x04"
        dec = sp.SlipDecoder(16)
        self.assertEqual(dec.feed(sp.slip_encode(a) + sp.slip_encode(b)), [a, b])

    def test_empty_packets_ignored(self):
        dec = sp.SlipDecoder(16)
        self.assertEqual(dec.feed(bytes([0xC0, 0xC0, 0xC0])), [])
        self.assertEqual(dec.dropped, 0)

    def test_bad_escape_drops_packet_and_resyncs(self):
        dec = sp.SlipDecoder(16)
        out = dec.feed(bytes([0xC0, 1, 0xDB, 0x42, 3, 0xC0]) + sp.slip_encode(b"\x09"))
        self.assertEqual(out, [b"\x09"])
        self.assertEqual(dec.dropped, 1)

    def test_oversize_packet_dropped(self):
        dec = sp.SlipDecoder(4)
        out = dec.feed(bytes([0xC0, 1, 2, 3, 4, 5, 0xC0]) + sp.slip_encode(b"\x07"))
        self.assertEqual(out, [b"\x07"])
        self.assertEqual(dec.dropped, 1)

    def test_boot_text_never_becomes_a_valid_frame(self):
        text = b"ets Jun  8 2016 00:22:57\r\nrst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n"
        dec = sp.SlipDecoder(sp.ATR_MAX_FRAME_LEN)
        for pkt in dec.feed(text + bytes([0xC0])):
            with self.assertRaises(sp.AtrError):
                sp.atr_parse(pkt)


class TcpFraming(unittest.TestCase):
    def test_contract_example(self):
        # The contract's example: a 42-byte body is sent as 00 00 00 2A + body
        body = b'{"type":"JOB_REQ","job_id":28,"part_id":8}'
        self.assertEqual(len(body), 42)
        self.assertEqual(bproto.frame_encode(body)[:4].hex(), "0000002a")

    def test_split_and_joined(self):
        m1, m2 = b'{"type":"A"}', b'{"type":"BB"}'
        stream = bproto.frame_encode(m1) + bproto.frame_encode(m2)
        dec = bproto.FrameDecoder()
        got = []
        for i in range(len(stream)):
            got += dec.feed(stream[i:i + 1])
        self.assertEqual(got, [m1, m2])
        self.assertEqual(bproto.FrameDecoder().feed(stream), [m1, m2])

    def test_oversize_length_is_an_error(self):
        dec = bproto.FrameDecoder(1024)
        with self.assertRaises(bproto.FrameError):
            dec.feed((1025).to_bytes(4, "big"))

    def test_max_body_accepted(self):
        body = b"x" * 1024
        self.assertEqual(bproto.FrameDecoder(1024).feed(bproto.frame_encode(body)), [body])
        with self.assertRaises(ValueError):
            bproto.frame_encode(b"x" * 1025)

    def test_zero_length_body(self):
        self.assertEqual(bproto.FrameDecoder().feed(b"\x00\x00\x00\x00"), [b""])


class BMessages(unittest.TestCase):
    def test_exact_bytes(self):
        self.assertEqual(bproto.heartbeat(3), b'{"type":"HEARTBEAT","seq":3}')
        self.assertEqual(bproto.config(19, 5, 2, 500), b'{"type":"CONFIG","cmd_id":19,"N":5,"K":2,"inspect_time_ms":500}')
        self.assertEqual(bproto.job_req(28, 8), b'{"type":"JOB_REQ","job_id":28,"part_id":8}')
        self.assertEqual(bproto.ack("JOB_RESULT", "job_id", 28, True),
                         b'{"type":"ACK","ref":"JOB_RESULT","job_id":28,"status":"accepted"}')
        self.assertEqual(bproto.ack("JOB_RESULT", "job_id", 28, False, "stale"),
                         b'{"type":"ACK","ref":"JOB_RESULT","job_id":28,"status":"rejected","reason":"stale"}')

    def test_parse(self):
        self.assertEqual(bproto.parse(b'{"type":"X","a":1}'), {"type": "X", "a": 1})
        for bad in (b"", b"[]", b'{"a":1}', b'{"type":5}', b"\xff\xfe", b"not json"):
            self.assertIsNone(bproto.parse(bad), bad)

    def test_u32(self):
        self.assertEqual(bproto.u32(0), 0)
        self.assertEqual(bproto.u32(4294967295), 4294967295)
        for bad in (-1, 4294967296, True, 1.0, "1", None, [1]):
            self.assertIsNone(bproto.u32(bad), bad)


if __name__ == "__main__":
    unittest.main()
