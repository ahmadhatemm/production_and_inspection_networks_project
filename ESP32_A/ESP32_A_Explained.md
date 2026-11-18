# ESP32 A firmware, explained line by line

This document explains every file of the `ESP32_A` project. Line numbers refer to the files as delivered. Lines whose meaning is easy to get wrong are marked **(watch)**.

**Contents**

1. What the firmware does
2. Decisions the contract does not make (please confirm)
3. A part and a command, byte by byte
4. File by file
   - `main/config.h`, `main/crc16.[ch]`, `main/slip.[ch]`, `main/atr.[ch]`, `main/prodmsg.[ch]`, `main/station.h`, `main/station.c`, `main/main.c`, build files, tests
5. Opening, building and flashing (and the "does not appear to contain CMakeLists.txt" error)

---

## 1. What the firmware does

ESP32 A is the **production station**. It makes virtual parts at a cycle time the operator sets, sends each part to the gateway, and waits for the gateway to accept it. It never inspects anything: inspection is ESP32 B's job.

Everything travels over the USB cable (UART0, 115200 baud, 8N1) in this stack, bottom to top:

| Layer | File | What it does |
| --- | --- | --- |
| UART | `main.c` | Moves bytes between the cable and RAM |
| SLIP | `slip.c` | Marks where each frame starts and ends |
| ATR record | `atr.c`, `crc16.c` | 8-byte header, payload, 2-byte CRC |
| Production message | `prodmsg.c` | The 16-byte payload: part, ack, command, heartbeat |
| Station logic | `station.c` | State machine, timers, retries, duplicate protection |

Two design points to keep in mind:

- **`station.c` contains no ESP-IDF code.** Time is passed in and frames leave through a callback. That is why the same code runs on the board and in the PC tests (`test/test_station.c`, about 900 checks).
- **There is one thread.** `app_main` loops forever: read bytes, feed them to the station, call `station_tick`. No locks are needed, because nothing runs in parallel.

States: `IDLE` (nothing running), `RUNNING` (producing parts), `PAUSED` (no new parts). The contract's fourth state, Fault, belongs to the gateway.

---

## 2. Decisions the contract does not make (please confirm)

The firmware follows Interface Contract v1 and the project description. Where those are silent, I had to choose. Tell the team about these, because the gateway code must agree.

| # | Question the contract leaves open | What the firmware does |
| --- | --- | --- |
| 1 | Byte order of the 16-byte payload fields | **Big-endian**, like the ATR header. The contract only says this for the ATR. Add it to the contract. |
| 2 | Is the first part made at Start or one cycle later? | One cycle after Start. |
| 3 | How many parts can be in flight? | **One.** A part must be accepted before the next is made (stop-and-wait). This is also how production is "blocked" when the queue is full. |
| 4 | What happens when the last part is accepted? | The station stays in `RUNNING` (or `PAUSED`) with `finished = true` and makes no more parts. Start is accepted again, and so is Stop. It never sends a "batch complete" message, because the contract has the gateway decide completion. |
| 5 | Pause while a part is in flight | The part keeps being retried and its answer is still handled. Only new parts stop. |
| 6 | Gateway goes silent (3 s without any valid frame) | Safe state: the station goes to `IDLE` and forgets the part in flight. It keeps sending heartbeats. |
| 7 | A part gets no answer after 4 tries | Same safe state: `IDLE`. |
| 8 | A command arrives twice (same `cmd_id`) | Not executed again. The earlier `CMD_ACK` and `CMD_DONE` are sent again. The last 16 `cmd_id` values are remembered. |
| 9 | What counts as a sign of life from the gateway? | Any frame with a good CRC, not only heartbeats. |
| 10 | `seq` of retransmitted parts | A retry is the **identical frame, including `seq`** ("same bytes every time"). New frames get the next `seq`; `seq` starts at 0 at boot. |
| 11 | Stop when already idle, Pause when not running, and similar | Rejected (`CMD_ACK` with arg 2, no `CMD_DONE`). |
| 12 | CRC variant | CRC-16/CCITT-FALSE (polynomial `0x1021`, start `0xFFFF`), the same as Lab 3. |
| 13 | Reporting the counters | The counters in `station_stats_t` exist for debugging, but no message sends them (the contract has none, and UART0 is the data link). |

---

## 3. A part and a command, byte by byte

**A part.** Part number 1, first frame the station creates, so `seq` = 0:

```
ATR record (26 bytes)
 00 00            test_id = 0 (production)
 00 00 00 00      seq = 0
 00 10            payload_len = 16
 01               msg_type = PART
 00 00 00 01      id = part_id 1
 00               arg = 0
 00 00 00 00      value_a = 0
 00 00 00 00      value_b = 0
 00 00            reserved
 5A DA            CRC-16 over the 24 bytes before it
```

On the wire it is `C0` + these 26 bytes + `C0` (28 bytes). The values were checked against an independent CRC implementation in Python.

**A command.** The gateway sends Start (`cmd_id` 100, cycle 100 ms, 3 parts, `seq` 5):

```
 00 00 | 00 00 00 05 | 00 10 | 03 | 00 00 00 64 | 01 | 00 00 00 64 | 00 00 00 03 | 00 00 | C2 37
 test_id  seq          len     CMD   cmd_id=100     START cycle=100ms   target=3     reserved  CRC
```

The station answers with two frames, in this order: `CMD_ACK` (`cmd_id` 100, arg 1 = accepted), then `CMD_DONE` (`cmd_id` 100, arg 1 = running). This is how delivery, acceptance and completion stay separate: delivery is the bytes arriving, acceptance is the `CMD_ACK`, completion is the `CMD_DONE`.

**One part's life:**

```
station: PART id 1  ------------------------>  gateway
station: <------------ PART_ACK id 1, accepted        (done, next part in one cycle)

station: PART id 2  ------------------------>  gateway
station: <------------ PART_ACK id 2, QUEUE_FULL      (wait 200 ms, send the same frame again)
station: PART id 2  (identical bytes) ------->
station: <------------ PART_ACK id 2, accepted

station: PART id 3  ------------------------>  gateway       (no answer)
         500 ms later: same bytes again, up to 3 times; then give up and go IDLE
```

---

## 4. File by file

### `main/config.h` (25 lines)

| Lines | Explanation |
| --- | --- |
| 1-2, 25 | Include guard. |
| 4-6 | Comment: every tunable number lives here; "contract" marks values from Interface Contract v1. |
| 9 | `LINK_BAUD` 115200, the contract's serial speed. |
| 12 | `STATION_ACK_TIMEOUT_MS` 500: how long to wait for a `PART_ACK` before sending the part again. |
| 13 | `STATION_MAX_TRIES` 4 **(watch)**: this is the total number of sends, so one first send plus 3 retries. |
| 14 | `STATION_FULL_RETRY_MS` 200: after `QUEUE_FULL`, wait this long and try the same part again. |
| 15 | `STATION_HEARTBEAT_MS` 1000: heartbeat period. |
| 16 | `STATION_LINK_TIMEOUT_MS` 3000: silence from the gateway longer than this means the link is lost (three missed heartbeats). |
| 17 | `STATION_CMD_HISTORY` 16: how many `cmd_id` values are remembered for duplicate suppression. |
| 20-23 | Limits for the Start command: cycle 10 to 60000 ms, target 1 to 10000 parts. A Start outside these limits is rejected. |

### `main/crc16.h`, `main/crc16.c` (copied unchanged from Lab 3)

| Lines | Explanation |
| --- | --- |
| `crc16.h` 7-8 | The variant: CRC-16/CCITT-FALSE. Polynomial `0x1021`, start value `0xFFFF`, most significant bit first, no final XOR. The check value for the ASCII text `123456789` is `0x29B1`. |
| `crc16.h` 9 | `CRC16_INIT` is the start value. |
| `crc16.h` 11 | One function: pass the data, its length and the running CRC. Passing `CRC16_INIT` starts a fresh calculation. |
| `crc16.c` 5 | Loop over every byte. |
| `crc16.c` 6 | XOR the byte into the **top** 8 bits of the CRC. The `unsigned` cast avoids a signed-overflow problem when shifting a `uint8_t`. |
| `crc16.c` 7-13 | Eight times: if the top bit is 1, shift left and XOR the polynomial; otherwise just shift left. This is the bit-by-bit form of the CRC. |
| `crc16.c` 15 | Return the CRC. There is no final XOR. |

### `main/slip.h`, `main/slip.c` (copied unchanged from Lab 3)

SLIP (RFC 1055) turns a byte stream into packets. A packet ends with `END` (`0xC0`). To keep an `END` byte inside the data from ending the packet, the sender replaces it.

| Replace | With |
| --- | --- |
| `0xC0` (END) in the data | `0xDB 0xDC` |
| `0xDB` (ESC) in the data | `0xDB 0xDD` |

| Lines | Explanation |
| --- | --- |
| `slip.h` 9-12 | The four special byte values. |
| `slip.h` 14-16 | `slip_encode`: writes `END`, the escaped packet, `END`. It returns the number of bytes written, or 0 if the output buffer is too small. |
| `slip.h` 18-23 | What the decoder reports after each byte: nothing yet, a complete packet, or a packet that was thrown away (bad escape or too long). |
| `slip.h` 25-31 | Decoder state: the buffer and its capacity, the bytes collected so far, whether the previous byte was `ESC`, and whether this packet is already known to be bad. |
| `slip.c` 7-9 | Output needs room for at least the two `END` bytes. |
| `slip.c` 10 | A leading `END` first ends whatever garbage the receiver might have collected. |
| `slip.c` 11-24 | For each input byte: if it is `END` or `ESC`, write two bytes (`ESC` then the replacement code). Line 13 checks room for those 2 bytes plus the closing `END` (so `n + 3`). Otherwise write the byte (line 19: 1 byte plus the closing `END`, so `n + 2`). If there is no room, return 0. |
| `slip.c` 25-26 | Closing `END`, return the length. |
| `slip.c` 29-36 | Initialise a decoder with its buffer. |
| `slip.c` 40-53 | A received `END` finishes a packet. If the packet was bad: report `SLIP_DROPPED`. If at least one byte was collected: report `SLIP_PACKET` and its length. Two `END`s in a row give an empty packet, which is ignored (reported as nothing). Either way the decoder resets for the next packet. |
| `slip.c` 55-57 | Once a packet is bad, skip everything until the next `END`. |
| `slip.c` 59-68 | The previous byte was `ESC`: `DC` becomes `END`, `DD` becomes `ESC`, anything else is illegal and marks the packet bad. |
| `slip.c` 69-72 | A new `ESC` byte: remember it and wait for the next byte. It is not stored. |
| `slip.c` 74-77 | Buffer full: the packet is longer than any valid frame, so mark it bad. |
| `slip.c` 78 | Store the byte. |

**(watch)** After `slip_decode_byte` returns `SLIP_PACKET`, the packet is in the decoder's buffer only until the next byte is fed. `main.c` uses it immediately, so this is safe.

### `main/atr.h` (45 lines)

| Lines | Explanation |
| --- | --- |
| 7-12 | The ATR layout from the project description: `test_id (2) | seq (4) | payload_len (2) | payload | crc16 (2)`, big-endian. |
| 13-15 | Header 8 bytes, CRC 2 bytes, so 10 bytes overhead. |
| 16 | Largest payload: 128 (the biggest benchmark size). Production uses only 16. |
| 17 | `ATR_MAX_FRAME_LEN` = 138, the largest valid frame. `main.c` sizes its buffers from it. |
| 20 | `test_id` 0 marks production traffic. Benchmark runs in Milestone 2 will use other values. |
| 21-22 | Production payload is always 16 bytes, so a production frame is 26 bytes. |
| 24-29 | A parsed frame. **(watch)** `payload` is a pointer into the buffer that was parsed, not a copy, so it is only valid while that buffer is unchanged. |
| 31-36 | Parse results. |
| 38-41 | `atr_build` returns the frame length, or 0 if the payload is too long or the output buffer too small. |
| 43 | `atr_parse` checks a frame and fills in an `atr_t`. |

### `main/atr.c` (68 lines)

| Lines | Explanation |
| --- | --- |
| 6-28 | Four helpers that write and read 16-bit and 32-bit numbers one byte at a time, most significant byte first. Doing it byte by byte means the code works the same whatever the CPU's own byte order, and does not read unaligned memory. |
| 22 | **(watch)** `p[0] << 8` is computed as an `int`, so there is no overflow for a byte value. The result is cast back to 16 bits. |
| 27 | Every byte is cast to `uint32_t` **before** shifting. Without the cast, `p[0] << 24` on a byte above 127 would be a signed overflow. |
| 33 | `body_len` is header plus payload, which is the part the CRC covers. |
| 35-37 | Refuse if the payload is over 128 bytes or the frame (body plus CRC) does not fit in the output buffer. |
| 38-40 | Write `test_id`, `seq`, `payload_len` into bytes 0-7. |
| 41-43 | Copy the payload after the header. (The `if` avoids calling `memcpy` with a possibly null pointer when the length is 0.) |
| 44 | Compute the CRC over header and payload and write it at the end. |
| 45 | Return the total length. |
| 50-52 | A frame shorter than 10 bytes cannot be valid. |
| 54-58 | **(watch)** The CRC is checked first. Until it passes, no field in the frame (not even its length) can be trusted. The CRC is the last two bytes. |
| 60-62 | Read the three header fields. |
| 63-65 | Now that the frame is known intact, check that `payload_len` really equals the number of bytes between header and CRC, and does not exceed 128. A frame can have a correct CRC and still be inconsistent (a sender bug), so this is a separate check. |
| 66-67 | Point `payload` into the buffer and return OK. |

### `main/prodmsg.h` (61 lines)

| Lines | Explanation |
| --- | --- |
| 9-18 | The 16-byte payload layout (Interface Contract v1): type at 0, id at 1-4, arg at 5, `value_a` at 6-9, `value_b` at 10-13, 2 reserved bytes at 14-15. Big-endian (see decision 1). |
| 21-26 | Message types. `PART` and `CMD_ACK` and `CMD_DONE` go from ESP32 A to the gateway. `PART_ACK` and `CMD` come from the gateway. `HEARTBEAT` goes both ways. |
| 29-30 | `PART_ACK` arg: 1 = the gateway accepted the part, 2 = the queue is full. |
| 33-36 | `CMD` arg: the command (1 Start, 2 Pause, 3 Resume, 4 Stop). For Start, `value_a` is the cycle time in ms and `value_b` the number of parts. |
| 39-40 | `CMD_ACK` arg: 1 accepted, 2 rejected. This is **acceptance**. |
| 43-45 | `CMD_DONE` arg: the state the station is in now. This is **completion**. |
| 47-53 | The message as a C struct. Unused fields are 0. |
| 56, 59 | Encode and decode functions. The `[ATR_PROD_PAYLOAD_LEN]` in the parameter is documentation: C treats it as a pointer, so the callers must pass 16 bytes. |

The prefix `PM_` (production message) avoids clashing with names in ESP-IDF's socket headers, which define `MSG_` constants.

### `main/prodmsg.c` (36 lines)

| Lines | Explanation |
| --- | --- |
| 5-16 | 32-bit read/write helpers, same idea as in `atr.c`. |
| 20-24 | Write the fields at their offsets. |
| 25-26 | Reserved bytes are always written as 0. |
| 31-35 | Read the fields. The reserved bytes are ignored, so a sender that puts something there does not break anything. |

### `main/station.h` (101 lines)

| Lines | Explanation |
| --- | --- |
| 11-15 | Why this file exists: the logic has no ESP-IDF code, so it can be tested on a PC. |
| 17-21 | The three states. `IDLE` is 0, so a zeroed struct starts idle. |
| 24 | The "transmit" callback type. The station hands over one complete ATR frame (not SLIP-encoded). `ctx` is a user pointer the station passes back untouched. |
| 26-47 | Counters for debugging and tests. Each comment says when it increases. They are never reset. |
| 49-54 | One remembered command: its `cmd_id`, and what we answered (`ack_arg`) and `done_arg`. `done_arg` is 0 when the command was rejected, which is how the code knows to send no `CMD_DONE`. `used` tells an empty slot from a used one, because `cmd_id` 0 is a legal value. |
| 56-88 | Everything the station remembers: |
| 57-58 | The transmit callback and its context. |
| 60-61 | The state, and `finished` (all parts made and accepted). |
| 64-67 | The batch: how many parts, the cycle time, how many parts have been made (also the last `part_id`), and when the next one is due. |
| 70-75 | The one part waiting for an answer: whether there is one, whether we are in the `QUEUE_FULL` wait, its id, when to act next, how many times it was sent, and the exact frame to re-send (26 bytes). |
| 78-82 | Link: the `seq` for the next new frame, the heartbeat counter and its next due time, whether the gateway is considered present, and when we last heard a valid frame. |
| 84-85 | The command history, used as a ring: `history_next` is the slot that will be overwritten next. |
| 87 | The counters. |
| 90-99 | The four public functions: init, "a packet arrived", "SLIP dropped a packet", and the periodic tick. All times are `uint32_t` milliseconds. |

### `main/station.c` (312 lines)

**Time helpers (6-17)**

| Lines | Explanation |
| --- | --- |
| 8 | The millisecond clock is a 32-bit number that wraps after about 49.7 days. |
| 9-12 | **(watch)** `time_reached` computes `now - deadline` in unsigned arithmetic (which wraps correctly) and then reads the result as a signed number. A deadline in the past or present gives a result of 0 or more; a deadline in the future gives a negative result. This is correct as long as the two times are less than about 24.8 days apart. A plain `now >= deadline` would break at the wrap. |
| 14-17 | `time_since` is the elapsed time, also wrap-safe. |

**Sending (19-44)**

| Lines | Explanation |
| --- | --- |
| 22-28 | `build_frame`: encode the message into the 16-byte payload (26), then wrap it in an ATR record with `test_id` 0 (27). **(watch)** `s->tx_seq++` uses the current `seq` and then increases it, so every frame we create has a new `seq`, starting at 0. |
| 30-39 | `send_msg`: build a message with only a type, id and arg (`value_a` and `value_b` are 0) and send it. Line 36 only guards against a build failure, which cannot happen with these sizes. |
| 41-44 | `send_pending_part`: sends the saved part frame again, **byte for byte**, without touching `tx_seq`. This is what "same bytes every time" means. |

**Batch control (46-88)**

| Lines | Explanation |
| --- | --- |
| 48-55 | `enter_idle`: the safe state. Back to `IDLE`, clear `finished`, and forget the part in flight. Used by Stop, link loss, and a part that is never answered. It does not reset `produced` or the counters. |
| 57-67 | `begin_batch`: start a batch. State `RUNNING`, remember the cycle and the target, `produced` back to 0 (so part ids restart at 1), no part in flight, and the first part due after one cycle (66). |
| 71 | **(watch)** The new part's id is `produced + 1`, computed **before** `produced` is incremented on line 73. So the first part is 1. |
| 73 | `produced` goes up: this part now counts as made. |
| 74 | Build the part frame straight into `pending_frame`, so it is available for retries. |
| 75-80 | Mark it as in flight, first try, answer due in 500 ms. |
| 82-86 | Schedule the next part. It adds one cycle to the old due time (this keeps a steady rate with no drift). If that time is already in the past (the station was held up by retries or `QUEUE_FULL`) it uses "now plus one cycle" instead, so there is no burst of catch-up parts. |
| 87 | Send it. |

**Commands (90-178)**

| Lines | Explanation |
| --- | --- |
| 92-100 | `find_cmd`: search the history for a `cmd_id`. `used` is checked so an empty slot is never matched. |
| 102-112 | `remember_cmd`: take the next ring slot (104), advance the ring position with wrap-around (106), and record the command. **(watch)** `ack_arg` starts as *rejected* and `done_arg` as 0, so a command that does nothing is rejected by default; only the code that really carries a command out changes them. When the ring is full, the oldest entry is overwritten. |
| 114-121 | `reply_to_cmd`: always send the `CMD_ACK` (acceptance). Send `CMD_DONE` (completion) only if the command was carried out. |
| 123-164 | `execute_cmd`: |
| 126-134 | Start. Allowed when idle, or when the previous batch finished (line 128). The cycle and target must be within the limits of `config.h` (129-130). If so, begin the batch and set `done_arg` to "running". Otherwise nothing changes and the default (rejected) stays. |
| 135-140 | Pause: only from `RUNNING`. |
| 141-147 | Resume: only from `PAUSED`. The next part is due one cycle from now (144). |
| 148-153 | Stop: from `RUNNING` or `PAUSED`, back to `IDLE`. |
| 154-155 | Unknown command code: nothing happens, so it is rejected. |
| 158-163 | If a `done_arg` was set, the command was carried out: mark it accepted and count it. Otherwise count a rejection. |
| 166-178 | `handle_cmd`: look the `cmd_id` up. **(watch)** If it is found (170-174), the command is a repeat: count it, **do not execute it**, and send the same answers again. This is what makes "at most once" true even if the gateway re-sends because it missed our reply. If it is new, remember it, execute it, and answer (175-177). A rejected command is remembered too, so a repeat of a rejected command is rejected the same way. |

**Answers to parts (180-204)**

| Lines | Explanation |
| --- | --- |
| 184-187 | An answer for a part that is not the one in flight (or when none is) is stale: count it and ignore it. This happens when the gateway answers both the original and a retry of the same part. |
| 188-195 | Accepted: the part is done. If it was the last one (`produced >= target`), the batch is finished. |
| 196-200 | `QUEUE_FULL`: enter the hold state and try again in 200 ms. **(watch)** Setting `pending_tries` to 0 means the wait does not use up the retries: the station can wait through any number of `QUEUE_FULL` answers and never gives up because of them. |
| 201-203 | Any other `arg`: count it and keep waiting for a real answer. |

**Input (206-259)**

| Lines | Explanation |
| --- | --- |
| 208-211 | Count a packet the SLIP decoder dropped. |
| 213-259 | `station_on_packet`, the one entry point for received frames. |
| 218-231 | Parse the ATR record. Each failure kind is counted and the frame is ignored. The `default:` on 228 is a safety net: it shares the code of `ATR_BAD_LENGTH`. |
| 232-234 | The frame is valid, so count it and note that the gateway is alive (`link_up`, `last_rx_ms`). Any frame counts, not only heartbeats. |
| 236-239 | Not `test_id` 0: some other traffic, ignore it (counted). |
| 240-243 | Payload is not 16 bytes: ignore it (counted). |
| 244 | Decode the 16-byte message. |
| 246-258 | Act on the message type. `PART_ACK` and `CMD` are handled; `HEARTBEAT` needs nothing more (it already refreshed `last_rx_ms`); anything else (including `PART`, `CMD_ACK`, `CMD_DONE`, which the gateway must never send) is counted as unexpected. |

**Time-driven work (261-303)**

| Lines | Explanation |
| --- | --- |
| 263-282 | `tick_pending`: handle the part in flight. |
| 265-267 | Nothing to do if there is no part, or its deadline has not come yet. |
| 268-272 | The `QUEUE_FULL` wait is over: try again as a fresh send (`tries` = 1, 500 ms answer deadline). |
| 273-277 | The answer is late and tries remain: send the same frame again and count a retry. `tries` 1 to 3 allows 3 retries, so 4 sends in all. |
| 278-281 | No tries left: count a failed part and go to the safe state. |
| 286-289 | Send a heartbeat when due. The counter is sent, then incremented (`hb_counter++`). The next one is due 1000 ms from now. |
| 291-295 | If a frame has been received before (`link_up`) and nothing valid has arrived for 3000 ms, declare the link lost: counted once (because `link_up` becomes false), and go to the safe state. **(watch)** The check needs `link_up`, so a station that has never heard from the gateway does not count a loss at boot. |
| 297 | Handle the part in flight (retry or give up). |
| 299-302 | Make a new part only if: running, no part in flight, batch not finished, parts remain, and the due time has come. The order matters: lines 291-297 run first, so a link loss or a given-up part (both set `IDLE`) stops production in the same tick. |
| 305-312 | `station_init`: zero everything (307), set the callback, state `IDLE` (redundant, because `IDLE` is 0, but it makes the intent clear), and make the first heartbeat due immediately (311). |

### `main/main.c` (145 lines): the only ESP-IDF file

| Lines | Explanation |
| --- | --- |
| 1-13 | Overview comment. Note line 12: a serial monitor would also hold the COM port. |
| 14-28 | Includes. `soc/uart_pins.h` (23) provides the fixed GPIO numbers of UART0 (1 and 3). |
| 30 | The link uses UART0. |
| 31 | Receive buffer inside the driver: 2048 bytes. |
| 32 | The loop wakes at least every 5 ms, so time-based work (parts, retries, heartbeats) runs with about 5 ms resolution. |
| 33 | LED pin 2. Set to -1 if your board has none. |
| 35-38 | Buffers. `s_rx`: bytes read from the UART at one time (256). `s_packet`: one decoded frame (138 bytes, the largest valid ATR frame). `s_tx` **(watch)**: worst case for an encoded frame is every byte escaped (2 bytes each) plus the two `END`s, so `2 * 138 + 2`. `s_station`: all the state. |
| 40-43 | Milliseconds since boot. `esp_timer_get_time()` is microseconds as a 64-bit number. Dividing by 1000 and casting to 32 bits is the wrap the time helpers in `station.c` handle. |
| 45-62 | `uart_setup`. |
| 47-54 | 115200 baud, 8 data bits, no parity, 1 stop bit, no flow control, default clock. |
| 56 | Install the driver: port 0, 2048-byte receive buffer, **0 transmit buffer** (so `uart_write_bytes` blocks until the bytes are in the hardware), no event queue (0, NULL), no flags. |
| 57 | Apply the settings. |
| 58-61 | Re-assign the TX and RX pins. **(watch)** With the console turned off, the bootloader disconnects UART0 from its pins, so they must be named again. `UART_PIN_NO_CHANGE` for the last two arguments means the RTS and CTS pins are left alone. |
| 64-71 | Set up the LED as an output, off. `gpio_reset_pin` first returns the pin to a known state. |
| 73-79 | LED on while the station is `RUNNING`. |
| 81-90 | `link_tx`, the callback the station uses. It SLIP-encodes the frame into `s_tx` and writes it to the UART. `(void)ctx` (86) silences an unused-parameter warning. If encoding fails (returns 0) nothing is sent. |
| 92-118 | `read_available`: |
| 96-102 | Wait time in RTOS ticks. **(watch)** If the tick rate were only 100 Hz, 5 ms would round down to 0 ticks and the loop would spin without ever sleeping, starving the idle task and triggering the watchdog. The `if` prevents that. `sdkconfig.defaults` sets 1000 Hz, so this is only a safety net. |
| 103-106 | Wait up to 5 ms for **one** byte. If none arrives, return 0 (the loop still runs the tick). |
| 107-117 | Then take whatever else is already in the driver buffer, without waiting (timeout 0): ask how many bytes are waiting, cap it to fit `s_rx` (one slot is already used, so `sizeof - 1`), and read them into the next positions. This way a frame is processed as soon as it arrives, instead of waiting to fill a buffer. |
| 120-127 | `app_main` start-up order: LED, UART, station (first heartbeat due at once), SLIP decoder. |
| 129-144 | The main loop, forever: read bytes; for each, feed the SLIP decoder (134); a finished packet goes to the station (137), a dropped one is counted (139); then run the tick (142) and update the LED (143). **(watch)** `station_on_packet` runs before the next byte is fed, because the packet lives in the decoder's buffer. `now_ms()` is called separately for each use, so times are always fresh. |

### Build files

| File | Explanation |
| --- | --- |
| `CMakeLists.txt` | Standard ESP-IDF project file, identical in form to the one in `lab3_bc_framed_link`. Line 1 is the minimum CMake version, line 2 loads ESP-IDF's build system from the `IDF_PATH` environment variable, and line 3 names the project `esp32_a`. It must sit in the **root of the project folder**, next to `main/`. |
| `main/CMakeLists.txt` | Same form as the lab's: registers the component `main` with its six source files (`main.c`, `crc16.c`, `slip.c`, `atr.c`, `prodmsg.c`, `station.c`) and says the headers are in the same folder (`INCLUDE_DIRS "."`). **(watch)** Every `.c` file must be listed here, or the linker reports undefined references. |
| `sdkconfig.defaults` lines 4-6 | Turn off the ESP-IDF console and the log output of the app and the bootloader, so nothing but our frames travels on UART0. |
| `sdkconfig.defaults` line 8 | 1 ms RTOS tick. **(watch)** These settings only apply when `sdkconfig` is first created. If a `sdkconfig` file already exists, delete it (or run Full Clean) before building. |

The ROM bootloader (inside the chip) still prints a few text lines on every reset, before our code runs. The gateway's SLIP decoder must drop that junk. It does: the text has no valid CRC.

### Tests (`test/`)

`test/test_station.c` compiles the real `station.c`, `atr.c`, `prodmsg.c`, `crc16.c` and `slip.c` on a PC and drives them with a fake gateway and a fake clock. Run it with `make -f test.mk` inside `test/` (needs gcc). It checks:

- CRC check value, frame bytes against values computed independently in Python, field widths and byte order, and every error kind.
- SLIP round trip with frames that contain `C0` and `DB` bytes.
- Heartbeat timing and `seq` numbering.
- A normal 3-part batch: first part after one cycle, one part per cycle, completion, and no extra part afterwards.
- Duplicate commands, and starting a new batch after a finished one.
- `QUEUE_FULL` five times in a row (more than the 4 tries), with identical re-sent frames, and no burst afterwards.
- Retries at 500, 1000 and 1500 ms, then failure at 2000 ms, with identical bytes each time.
- Pause, Resume and Stop, including pause and stop while a part is in flight.
- Rejections: limits, wrong state, unknown command, and a repeat of a rejected command.
- The command history ring (the 17th command pushes out the oldest).
- Link loss after 3000 ms and recovery.
- Corrupted, short, mis-sized and foreign frames.
- The 32-bit clock wrapping.

I also broke three constants on purpose (tries, queue-full wait, link timeout) and confirmed the tests fail each time, so they do detect real mistakes.

**What the tests do not cover:** the ESP-IDF calls in `main.c`. That file was only syntax-checked against stand-in headers, copying the exact calls that Lab 3's firmware uses. The first real check is flashing the board and watching it answer the gateway.

---

## 5. Opening, building and flashing

### The folder layout is the same as in the labs

Each lab firmware is a self-contained ESP-IDF project: its own folder with `CMakeLists.txt` and `main/` directly inside. `ESP32_A` has exactly that shape:

```
Lab_Code/firmware/lab3_bc_framed_link/        ESP32_A/
    CMakeLists.txt                                CMakeLists.txt
    sdkconfig.defaults                            sdkconfig.defaults
    main/                                         main/
        CMakeLists.txt                                CMakeLists.txt
        main.c, slip.c, ...                           main.c, station.c, ...
```

### The error "The source directory D:/Networks/Project does not appear to contain CMakeLists.txt"

The ESP-IDF extension builds **the folder that is open in VS Code**. If you open `D:\Networks\Project` (the folder that holds all labs, the PDFs and `ESP32_A`), it looks for `CMakeLists.txt` there. There is none, because that folder is not a project: it only contains projects. So the `CMakeLists.txt` files were not the problem. Adding one at `D:\Networks\Project` would be wrong, because it would try to build the whole folder as one project.

The `cmake.sourceDirectory` entry in `.vscode/settings.json` does not help: that setting belongs to the separate "CMake Tools" extension, and the ESP-IDF Compile command ignores it.

### Fix (do this once)

1. In VS Code: **File > Open Folder...** and choose `D:\Networks\Project\ESP32_A` itself, so that `CMakeLists.txt` and `main` are visible at the top level of the Explorer panel. Do the same for any lab you want to build.
2. The settings are stored per opened folder, so choose them again in this window:
   - **ESP-IDF: Set Espressif Device Target** > `esp32`
   - **ESP-IDF: Select Port to Use** > your COM port (it was COM10)
   - If asked, choose the ESP-IDF setup you already use (`v6.1.1`) and the `ESP-IDF` install it points to.
3. Optional clean-up: delete the empty `D:\Networks\Project\build` folder and the old `D:\Networks\Project\.vscode\settings.json` that the failed attempt created. They belong to the wrong folder and do nothing useful.
4. **ESP-IDF: Build Project**, then **ESP-IDF: Flash Device** (UART method). The first build creates `sdkconfig` from `sdkconfig.defaults`. Afterwards `sdkconfig` should contain `CONFIG_ESP_CONSOLE_NONE=y`.

### Notes about flashing and testing

- Flashing is done by the extension with `esptool` over the same COM port. Do it before the gateway opens the port, and do not use **ESP-IDF: Monitor Device**: with the console turned off there is nothing readable to see, and the monitor keeps the port busy.
- Because the console is off, a good sign is the LED (pin 2) turning on while the station is in the Running state, and the gateway receiving a heartbeat frame about once a second.
- If you ever change `sdkconfig.defaults`, delete `sdkconfig` (or run **ESP-IDF: Full Clean**) and build again. The defaults file is only read when `sdkconfig` is created.
