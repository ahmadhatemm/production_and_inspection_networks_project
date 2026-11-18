# The gateway, explained line by line

Python (3.10+, `aiohttp`, `pyserial`). It sits between the operator dashboard and the two ESP32 boards and follows Interface Contract v1.1 and the project description (Milestone 1, production mode).

Line numbers refer to the files in this folder. **(watch)** marks a place where a small change breaks the protocol.

---

## 1. What the gateway does

```
                         +--------------------------- gateway (one Python process) ---------------------------+
 ESP32 A  <--USB--> links.SerialLink --> serialproto (SLIP, ATR, CRC) --+                                      |
 ESP32 B  <--TCP--> links.BServer    --> bproto (length + JSON)  -------+--> engine.Engine  --> server (HTTP/WS) <--> Dashboard
                                                                         |        |
                                                                         |     evidence.log (file + console)
```

- **ESP32 A** (production) makes a part every `cycle_time_ms` and sends `PART`. The gateway answers `PART_ACK` (accepted, or `QUEUE_FULL`) and passes `Start / Pause / Resume / Stop` to A as `CMD`.
- **ESP32 B** (inspection) gets `CONFIG` (the examiner's N, K and the inspection time) and `JOB_REQ` for one part at a time, and returns `JOB_RESULT` with PASS or FAIL. The verdict is computed **only on B**. The gateway counts what B says and never evaluates the rule (project requirement III).
- **The operator** sends commands by HTTP and watches the WebSocket. Every command and every part shows three separate events: **delivered** (bytes arrived), **accepted** (the receiver agreed to act), **completed** (the work finished, with its result).
- **The gateway itself** keeps the four states (idle, running, paused, fault), the inspection queue (capacity 5), the unique `job_id` numbers, acknowledgement timeouts with bounded retries (500 ms, 4 tries, identical bytes), duplicate suppression, heartbeats both ways (1000 ms, lost after 3000 ms), and the safe state on a fault.

How a part travels:

```
A: PART 7 ---------------------------> gateway: queue it, answer PART_ACK(accepted)           part_event produced, queued
                                        gateway: JOB_REQ job 28 for part 7 ----------------> B   part_event delivered
                                        B: ACK accepted ------------------------------------>    part_event accepted
                                        (B waits inspect_time_ms)
                                        B: JOB_RESULT job 28 part 7 FAIL N=5 K=2 ----------->    part_event completed + verdict
                                        gateway: ACK accepted -------------------------------> B
```

---

## 2. Decisions the contract does not make (please confirm)

The contract and the project description fix the messages and the rules. Where they are silent I had to choose. Rows marked **contract change** need a line added to the Interface Contract so that all five of us agree.

| # | What is left open | What the gateway does |
| --- | --- | --- |
| 1 | Order of checks on an operator command | (1) body is a JSON object and has a valid `cmd_id` (integer 0 to 4294967295, not `true`, not `5.0`), otherwise 400 `MALFORMED` with `"cmd_id": null`; (2) the `cmd_id` was seen before: replay; (3) fields valid, otherwise 400; (4) state table, otherwise 409; (5) device and rule checks (items 3 to 5). |
| 2 | Same `cmd_id` again | Same `cmd_id` **and** same command and fields: the **original** status code and body are returned with `"duplicate": true`, nothing is executed again (also for a remembered rejection). Same `cmd_id` with a **different** command: 400 `MALFORMED`. A malformed request is not remembered, so it can be corrected and sent again with the same `cmd_id`. The last 1000 `cmd_id` values are kept. |
| 3 | Start before N and K are known | **409 `INVALID_STATE`**, message "send config/inspection (N, K) before start". The contract's `batch/start` carries no N and K, so the examiner's rule must be set first. |
| 4 | A device is missing when a command needs it | 503 `DEVICE_NOT_READY` naming the device, for `batch/start` (both A and B must be connected) and `config/inspection` (B). Nothing changes. |
| 5 | `mode` = benchmark | Accepted in idle (state table). `batch/start` in benchmark mode is refused with 409 ("Milestone 2"). |
| 6 | Which `inspect_time_ms` goes to B in a `config/inspection` | The one from the last Start. Before the first Start: 500 ms (`default_inspect_time_ms`). `batch/start` always sends its own value. |
| 7 | What `batch/start` sends | Two messages: `CONFIG` to B (N, K, `inspect_time_ms`, with a gateway-made `cmd_id`) and `CMD start` to A (the operator's `cmd_id`, cycle time, target). Its `cmd_event` stages appear when **both** have reached them; `rejected` or `failed` appear as soon as **either** fails. |
| 8 | When the first `JOB_REQ` may go out | Only after B has answered `CONFIG_DONE` with the same N, K and time (otherwise B would say `not_configured` or use old values). |
| 9 | After a B reconnect or reboot | On every `HELLO` the gateway sends `CONFIG` again if N and K are known (contract). |
| 10 | What the queue counts | Capacity 5 = parts **waiting plus the one being inspected**. `queue.depth` and `batch.queued` are that number. `batch.produced` also counts a part that was answered `QUEUE_FULL` and is being held by A. |
| 11 | Full-queue policy (Requirement IV) | **Block production.** The part that does not fit gets `PART_ACK` = `QUEUE_FULL`, `queue.blocked` becomes `true`, A offers it again every 200 ms (A's own rule) and the gateway takes it as soon as there is room. No Fault. |
| 12 | `PART` outside a batch | Ignored, no answer, logged: gateway not running or paused; `part_id` 0 or above the target. A part that was already taken (a repeat because A missed our answer) gets its `PART_ACK` again and is **not** queued twice. |
| 13 | `job_id` numbers | One counter for the whole gateway, **never reused**. It starts at a random number so that a restarted gateway cannot hit a `job_id` that B still remembers (B remembers 32, so a restart from 1 would collide). |
| 14 | B rejects a `JOB_REQ` | The part goes back to its place at the head of the queue and is offered again after 500 ms: with the **same** `job_id` after `busy`, `not_configured`, `malformed`, `out_of_range`; with a **new** one after `abandoned` or `id_conflict` (contract). After 4 rejections of one part: Fault `PROTOCOL_ERROR`. **Contract change:** a `part_event` with `stage: "rejected"` and the B's `reason` is sent for each rejection (it can occur more than once per part). |
| 15 | What the gateway answers to a `JOB_RESULT` | `ACK accepted` when it is the job in flight. `ACK rejected` with `reason`: `stale` (not the job in flight, or too late), `malformed` (missing or invalid fields), `mismatch` (wrong part, N or K). A repeated result of a finished job gets `accepted` again and is **not** counted again. **Contract change:** these three `reason` words for gateway-to-B ACKs. |
| 16 | A result that checks out | Must be for the job in flight, same `part_id`, same N and K as the job was offered with, a verdict of exactly `PASS` or `FAIL`, and arrive within `inspect_time_ms` + 3000 ms of B's ACK (contract). `FAIL` is counted as rejected, `PASS` as accepted. |
| 17 | A result for a job the gateway itself gave up on (after Stop, a Fault, or a lost connection) | Answered `rejected`/`stale`, thrown away and logged. **Not** a Fault: we cancelled it ourselves. The same result for a job we never had is `STALE_RESULT`. |
| 18 | A job that never reports | If no result arrives within `inspect_time_ms` + 3000 ms after B's ACK, the gateway raises `STALE_RESULT` itself. |
| 19 | When a Fault can happen | Only from running or paused (contract). In idle a problem is logged and shows in the status (`connected: false`). |
| 20 | What entering Fault does | State `fault`, one `fault` WebSocket message, queue emptied, job in flight cancelled, a `CMD stop` sent to A (`cmd_id` from 4026531840 up, no `cmd_event`). The counters stay so the operator can see where it stopped. `fault/clear` goes to idle. A dashboard that connects during a Fault is sent the `fault` message right after the first `status`. |
| 21 | `HEARTBEAT_LOST` | A device is silent for 3000 ms. **Any valid message** counts as a sign of life, not only heartbeats. When B's TCP connection closes it is raised immediately (the connection itself is the evidence). |
| 22 | Fault codes | `HEARTBEAT_LOST`; `RETRIES_EXHAUSTED` (4 sends without an answer); `STALE_RESULT` (items 15 to 18); `PROTOCOL_ERROR` (a device rejected a command, `CONFIG_DONE` with other values, `CMD_DONE` with another state than expected, a result for the wrong part or N and K, B rejected a part 4 times). |
| 23 | Messages to A | A `CMD` is re-sent (same bytes, same ATR `seq`) until A's `CMD_DONE`. A's `CMD_ACK` = `accepted`, then `CMD_DONE` = `running`, `paused` or `stopped` matching the command. A's `CMD_ACK` = `rejected` is a failure (`reason: "device_rejected"`) and a Fault. |
| 24 | Reason words in `cmd_event` | From B: passed through unchanged (contract). From the gateway: `retries_exhausted`, `device_lost`, `device_rejected`, `unexpected_state`, `config_mismatch`. |
| 25 | Pause | The gateway stops dispatching and A stops producing. Parts already on the wire are still queued; the job in flight finishes and is counted. Resume checks again whether the batch is complete. |
| 26 | Batch completion (Requirement VI) | `produced == inspected == target` while running: state `idle`, one `batch_complete` message, report kept for `GET /api/v1/batch/report` (409 before any batch and after the next Start). **Stop is not completion.** |
| 27 | `status` | At connect, every 1000 ms, and as soon as anything shown changes. `heartbeat_age_ms` is the time since the last valid message from the device (`null` if none yet). |
| 28 | Heartbeats from the gateway | To A whenever the COM port is open, to B once it has said `HELLO`. Counters start at 0. |
| 29 | `ESP32 A` timing seen from the gateway | A also stops by itself after 3000 ms without a valid frame from the gateway, so if the gateway dies A goes idle. |
| 30 | The dashboard is a web page | All responses carry CORS headers (`*`) and OPTIONS requests are answered. `--ui-dir` serves the dashboard from port 8080. |
| 31 | What reaches the evidence log | Everything, with timestamps (see README). The console hides heartbeats unless `-v`. |
| 32 | COM port problems | Not found or in use: reported once, retried every second. Unplugged while running: closed, retried every second, A shows as disconnected after 3 s. Opening never resets the board (DTR and RTS are kept off). |
| 33 | The operator's `cmd_id` and the boards' memory | A remembers the last 16 `cmd_id` values and B the last 32. If the dashboard reused an old `cmd_id` while the boards stay powered, they would treat it as a repeat. Use a new number every time. Gateway-made IDs start at 4026531840. |
| 34 | Not implemented | Benchmark mode and everything in Milestone 2 (MQTT, Modbus, authentication). |

---

## 3. Messages, byte by byte

### 3.1 ESP32 A, a Start command

`CMD start`, `cmd_id` 17, cycle 1000 ms, target 20 parts, ATR `seq` 3. These bytes were produced by ESP32 A's own C code and the gateway's encoder gives the same ones (`tests/test_codec.py`).

```
payload (16 bytes)   03 | 00 00 00 11 | 01 | 00 00 03 E8 | 00 00 00 14 | 00 00
                     type  cmd_id=17   arg   cycle=1000     target=20     reserved
                           (CMD)       (start)
ATR record (26)      00 00 | 00 00 00 03 | 00 10 | <payload> | 5B 32
                     test_id  seq=3        len=16              CRC-16
on the wire (SLIP)   C0 <ATR record, with C0 -> DB DC and DB -> DB DD> C0
```

The CRC is CRC-16/CCITT-FALSE (polynomial 0x1021, start value 0xFFFF) over the first 24 bytes. `crc16("123456789")` must be `0x29B1`.

Message types in the payload: `0x01` PART (A to gateway), `0x02` PART_ACK, `0x03` CMD, `0x04` CMD_ACK, `0x05` CMD_DONE, `0x06` HEARTBEAT.

### 3.2 ESP32 B, one job

TCP port 5000, each message preceded by its length as 4 bytes, big-endian (`00 00 00 2A` for a 42-byte body):

```
G -> B  {"type":"CONFIG","cmd_id":19,"N":5,"K":2,"inspect_time_ms":500}
B -> G  {"type":"ACK","ref":"CONFIG","cmd_id":19,"status":"accepted"}
B -> G  {"type":"CONFIG_DONE","cmd_id":19,"N":5,"K":2,"inspect_time_ms":500}
G -> B  {"type":"ACK","ref":"CONFIG_DONE","cmd_id":19,"status":"accepted"}

G -> B  {"type":"JOB_REQ","job_id":28,"part_id":8}
B -> G  {"type":"ACK","ref":"JOB_REQ","job_id":28,"status":"accepted"}
        ... 500 ms ...
B -> G  {"type":"JOB_RESULT","job_id":28,"part_id":8,"verdict":"FAIL","N":5,"K":2}      (8 + 2) mod 5 = 0
G -> B  {"type":"ACK","ref":"JOB_RESULT","job_id":28,"status":"accepted"}
```

### 3.3 The operator

```
POST /api/v1/batch/start   {"cmd_id":17,"target_parts":20,"cycle_time_ms":1000,"inspect_time_ms":500}
200  {"ok":true,"cmd_id":17,"stage":"accepted","state":"running","duplicate":false}

WS   {"v":1,"type":"cmd_event","ts":...,"cmd_id":17,"cmd":"batch/start","stage":"delivered"}
WS   {"v":1,"type":"cmd_event","ts":...,"cmd_id":17,"cmd":"batch/start","stage":"accepted"}
WS   {"v":1,"type":"cmd_event","ts":...,"cmd_id":17,"cmd":"batch/start","stage":"completed"}
WS   {"v":1,"type":"part_event","ts":...,"part_id":1,"job_id":null,"stage":"produced"}
WS   {"v":1,"type":"part_event","ts":...,"part_id":1,"job_id":null,"stage":"queued"}
WS   {"v":1,"type":"part_event","ts":...,"part_id":1,"job_id":28,"stage":"delivered"}
WS   {"v":1,"type":"part_event","ts":...,"part_id":1,"job_id":28,"stage":"accepted"}
WS   {"v":1,"type":"part_event","ts":...,"part_id":1,"job_id":28,"stage":"completed","verdict":"PASS"}
```

`job_id` is `null` until the part is offered to B. Errors use `{"ok":false,"cmd_id":..,"error":{"code","message"}}` with 400, 409 or 503.

---

## 4. File by file

### `gw/config.py` (53 lines)

| Lines | Explanation |
| --- | --- |
| 12 | `U32_MAX`: the largest value of an unsigned 32-bit field. All IDs and counters are checked against it. |
| 15-53 | One dataclass with every tunable number. Lines 17-42 are values from the contract (ports, 500 ms, 4 tries, 1000 ms, 3000 ms, queue 5, 1000 remembered `cmd_id` values, field limits, 1024-byte TCP body). Lines 44-53 are my own choices (items in section 2): reject retry 500 ms and limit 4, status period, loop period 5 ms, default times, the range of gateway-made `cmd_id` values, a random `job_id` start (`None`), and how many finished jobs are remembered to answer repeated results. Any of them can be changed from the command line with `--set key=value`. |

### `gw/serialproto.py` (216 lines): bytes to and from ESP32 A

| Lines | Explanation |
| --- | --- |
| 18-27 | `crc16_ccitt`: bit by bit, 0x1021, initial 0xFFFF. Same result as `crc16.c` on the board and `lab3proto.py` from Lab 3. |
| 35-46 | `slip_encode`: `C0`, the packet with `C0` replaced by `DB DC` and `DB` by `DB DD`, `C0`. The **leading** `C0` is deliberate: it ends any junk that is already on the line. |
| 49-93 | `SlipDecoder`. Rebuilds packets from the byte stream. `C0 C0` gives an empty packet, which is ignored (69). An `ESC` followed by anything but `DC` or `DD` marks the packet bad and everything is skipped to the next `C0` (83-85). A packet longer than `max_len` is treated the same way (89-91), and `dropped` counts them (68). **(watch)** The bootloader of ESP32 A prints text at reset. That text becomes a "packet" with no valid CRC and is discarded one layer up. |
| 98-104 | Sizes: header 8, CRC 2, payload at most 128 (the benchmark sizes), production payload exactly 16. |
| 107-129 | `AtrError`, `atr_build` (111-115) and `atr_parse` (118-129). Parsing checks the length and the **CRC first** and only then reads the fields, in this order: too short, CRC, length field equals what arrived. Big-endian everywhere (`struct` format `>`). |
| 141-163 | Message type numbers and argument values, copied from `ESP32_A/main/prodmsg.h`. `EXPECTED_DONE` says which state A must report in `CMD_DONE` after each command. |
| 167-184 | `ProdMsg` and its 16-byte encoding `>BIBIIH` (type, id, arg, value_a, value_b, reserved 0). |
| 187-203 | `describe`: one readable line per message for the evidence log. |
| 206-216 | `AFramer`: the ATR `seq` of everything the gateway sends, counting from 0 (contract). The `seq` is fixed when the record is built, so a re-send is byte for byte the same record. |

### `gw/bproto.py` (99 lines): bytes to and from ESP32 B

| Lines | Explanation |
| --- | --- |
| 20-54 | `FrameError`, `frame_encode` and `FrameDecoder`: 4-byte big-endian length, then the body. A length over 1024 raises `FrameError`; a TCP stream cannot be re-synchronised, so the connection is dropped. A zero-length body is legal. |
| 59-67 | `parse`: the body must be UTF-8 JSON, an object, with a string `type`; otherwise `None` and the message is ignored. |
| 70-74 | `u32`: the value only if it is a JSON **integer** from 0 to 4294967295. `True`, `5.0` and `"5"` are not. **(watch)** Python treats `True` as the integer 1, so `bool` is excluded explicitly. |
| 79-99 | The builders (`heartbeat`, `config`, `job_req`, `ack`) write compact JSON in the field order of the contract. |

### `gw/engine.py` (988 lines): the protocol brain

No input or output of its own. Time is passed in as `now` (milliseconds); bytes leave through an `io` object (described in the file's first lines); events for the operator leave through `io.emit`. This is why the whole protocol can be tested with a fake clock.

**Data (50-126)**

| Lines | Explanation |
| --- | --- |
| 50-55 | `Device`: connected flag, time of the last valid message, next heartbeat time and counter. |
| 59-113 | `Leg`, `Command`, `Part`, `Job`, `Tx`. A **leg** is one message to one device that a command needs (Start has two). A **tx** is a message that waits for an answer and is re-sent. `eq=False` on all of them: they point at each other, and comparing them field by field would never finish. **(watch)** |
| 117-126 | `Batch`: the counters (target, produced, inspected, accepted, rejected), the highest part seen, the highest part taken into the queue, and the part being held after `QUEUE_FULL`. |

**Set-up and helpers (129-196)**

| Lines | Explanation |
| --- | --- |
| 130-158 | `__init__`, the state: both devices, the state (`idle`), mode, parameters (N and K start as `null`), queue, the job in flight, cancelled jobs, the list of waiting messages (`txs`), the `cmd_id` memory (1000), the finished-job memory, the report, the fault, and the counters for `job_id` (random start unless configured) and gateway-made `cmd_id`. |
| 168-172 | `_remember`: an ordered memory that forgets the oldest entry. |
| 174-177 | Gateway-made `cmd_id` values count up from 4026531840 and wrap inside that range. |
| 179-182 | `_set_state` logs every change. |
| 184-196 | `take_dirty`: true when anything visible in the status changed since the last call. The main loop then sends a `status` at once. |

**What the operator sees (198-227)**: `status` (200-213) builds the exact object from the contract, `fault_message` (215) returns the active fault for late-joining dashboards, `get_report` (219) answers 200 or 409.

**Operator commands (229-351)**

| Lines | Explanation |
| --- | --- |
| 231-261 | `handle_command` runs the checks in the order of section 2, item 1. The duplicate check (246-256) compares a signature (command and its fields), so the same `cmd_id` with another command is `MALFORMED`. The result is stored (258) so a repeat can be answered; the memory is trimmed to 1000 entries (259-260). |
| 263-286 | `_validate`: each number must be a real integer within its limit (`True`, `5.0`, `"5"` are refused), `mode` must be one of two words. Limits come from `Config`. |
| 288-330 | `_decide`: the state table (`ALLOWED_STATES`, line 37) first, then Start's extra checks (benchmark mode 294, N and K, devices 299) and `config/inspection`'s device check (302). Then the command is carried out and the contract's `accepted` body returned. |
| 332-345 | `_do_start`: stores the cycle and inspect time, resets the batch and queue, enters running, and builds **both** legs (CONFIG to B, CMD to A) before sending either. **(watch)** If the first were sent before the second leg existed, "delivered" would be reported after only one device had it. |
| 347-351 | `_do_stop`: drop the queue, cancel the job in flight, enter idle, tell A. |

**Commands and their legs (353-426)**

| Lines | Explanation |
| --- | --- |
| 358-361 | `_finish_local`: a command with no device leg (`mode`, `fault/clear`) is complete as soon as it is applied. |
| 363-368 | `_cmd_event` builds the WebSocket message and the log line. |
| 370-377 | `_progress` reports a stage once **every** leg has reached it, always in the order delivered, accepted, completed. |
| 379-391 | `_leg_rejected` and `_leg_failed`: a rejected or failed leg ends the command with one `rejected` or `failed` event and a reason. Internal commands (made by the gateway) send no events. |
| 393-426 | `_send_a_cmd`, `_send_config`, `_send_safe_stop` create the leg and the waiting message. |

**Reliable sending (428-480)**

| Lines | Explanation |
| --- | --- |
| 430-448 | `_tx_send` writes the stored bytes, starts the 500 ms timer, and counts the try. The first successful write is the "delivered" event. A write that fails (port closed, no connection) is not "delivered" and simply waits for the next try. |
| 456-464 | `_tx_tick` re-sends after 500 ms, up to 4 sends in total. **(watch)** The bytes are never rebuilt, so an A-bound retry has the same ATR `seq` and CRC; that is how A recognises a repeat. |
| 466-480 | `_tx_exhausted`: the message is reported as failed (never as a success) and a `RETRIES_EXHAUSTED` Fault follows. |

**Faults (482-536)**

| Lines | Explanation |
| --- | --- |
| 484-495 | `_enter_fault`: only from running or paused. Sets the state, stores and emits the `fault` message, drops the work, and tells A to stop. |
| 497-504 | `_cancel_job`: B may still be inspecting a job we gave up on, so its id is kept in `cancelled` until `inspect time + 3000 ms + 2000 ms`. A late result for it is then thrown away without a Fault, and no new job is offered until it has reported or expired (B would answer `busy`). |
| 506-517 | `_drop_work` clears the queue and the held part; `_cancelled_outstanding` forgets cancelled ids that have expired. |
| 519-536 | `_device_lost`: marks the device disconnected, fails everything that was waiting for it, closes B's socket, then raises `HEARTBEAT_LOST`. |

**Time (538-570)**: `tick` is called every 5 ms. It sends the two heartbeats (543-556), checks the silence of both devices (557-560), runs the retry timers (561-562), checks that the job in flight reports within `inspect time + 3000 ms` (563-569), and offers the next part (570).

**ESP32 A (572-701)**

| Lines | Explanation |
| --- | --- |
| 574-613 | `on_a_frame`: parse the ATR record (576-581: bad frames are counted and logged, they do **not** count as a sign of life), accept only `test_id` 0 with a 16-byte payload (582-589), note the `seq` (592-598: a repeat or a jump is logged, never fatal), mark A as alive (600), and dispatch by message type (602-612). |
| 631-666 | `_on_part`: ignore if not running or paused (633-636) or the id is outside the batch (637-640); re-answer a part that was already taken (641-645); count a new part as produced (646-651); if the queue is full answer `QUEUE_FULL`, set `blocked`, remember the held part (652-657); otherwise queue it, answer `accepted` and report `queued` (658-666). **(watch)** `produced` counts a part once even when A offers it again and again. |
| 668-701 | `_on_cmd_ack` (668-684) and `_on_cmd_done` (686-701). `CMD_DONE` also counts as "accepted" in case `CMD_ACK` was lost. A state other than the expected one is a protocol error. |

**ESP32 B (703-940)**

| Lines | Explanation |
| --- | --- |
| 705-739 | `on_b_message` (705-733) and `on_b_closed` (735-739): unparsable messages are ignored and counted; before `HELLO` nothing else is accepted; every valid message is a sign of life. |
| 741-761 | `_on_b_hello`: only `device: "esp32_b"`; a second `HELLO` on one connection drops it; then B counts as connected and CONFIG is sent if N and K are known. |
| 768-807 | `_on_b_ack`: match the ACK to the waiting `CONFIG` or `JOB_REQ`. Accepted: report the stage (for a job: `part_event accepted`, start its age clock). Rejected: pass the reason on. An ACK that matches nothing is logged and ignored. |
| 809-822 | `_job_rejected`: see section 2, item 14. |
| 824-855 | `_on_config_done`: the N, K and time must be what was sent; then `ACK` back, and the first `JOB_REQ` is allowed. A repeated `CONFIG_DONE` is answered again. |
| 857-913 | `_on_job_result`: the classification of section 2, items 15 to 17. **(watch)** The gateway checks `part_id`, N, K and the verdict word, never the verdict itself against the rule. |
| 915-940 | `_complete_job` (915-928) counts the verdict; `_check_complete` (930-940) ends the batch when `produced == inspected == target` and builds the report. |

**Part events and dispatch (942-988)**

| Lines | Explanation |
| --- | --- |
| 944-965 | Part events are sent at most once per stage and part (except `rejected`). |
| 967-988 | `_dispatch`: only in running, one job in flight, B connected and configured, no cancelled job still outstanding, and the **first** waiting part (first in, first inspected), which waits its 500 ms again after a rejection instead of being overtaken. A new `job_id` is taken only if the part has none yet. |

### `gw/links.py` (210 lines): the two physical links

| Lines | Explanation |
| --- | --- |
| 20-106 | `SerialLink`. `ensure_open` (42-61) opens the port without blocking, with DTR and RTS off **before** `open()` so the board is not reset, and tries again every second. `poll` (77-91) reads whatever is there (`timeout=0`) and hands it to the SLIP decoder. `write` (93-102) SLIP-frames one ATR record. Any error closes the port and the loop reopens it. |
| 109-210 | `BServer`, the TCP server for ESP32 B. One connection at a time: a **new connection replaces the old one** (the engine is told the old one was lost). A generation number makes a late event from a replaced connection harmless. `TCP_NODELAY` keeps the small messages from waiting. A framing error closes the connection. Everything runs in the one asyncio thread, so the engine needs no locks. |

### `gw/server.py` (132 lines): HTTP and WebSocket

| Lines | Explanation |
| --- | --- |
| 25-51 | CORS headers (25-30, middleware 41-51) on every response, `OPTIONS` answered with 204, so the dashboard (a web page) can call the gateway from another address. |
| 60-71 | One handler for all seven POST commands: the body is read, logged (`OP-RX`), parsed (invalid JSON, `NaN` and similar give 400 `MALFORMED`), handed to the engine, and the answer is logged (`OP-TX`). |
| 73-78 | `GET /status` and `GET /batch/report`. |
| 80-111 | `/ws`: sends a `status` at once and the active `fault` if there is one, then everything the engine emits. Each client has its own queue so one slow dashboard cannot hold up the others; a client that stops reading is dropped. v1 is one-way, incoming messages are ignored. |
| 113-132 | Routes, and the optional dashboard files, added **last** so they never hide the API. |

### `gw/evidence.py` (49 lines)

One line per record: wall-clock time, milliseconds since start, a tag, the text. Written to the log file always (flushed per line) and to the console, except heartbeats unless `-v`. A console that cannot show a character does not stop the gateway.

### `gw/app.py` (153 lines)

| Lines | Explanation |
| --- | --- |
| 23-48 | `_IO`: how the engine reaches the serial port, the TCP server, the WebSocket queues, the log and the clock. |
| 52-91 | `Gateway`: owns the engine, links and evidence log; `broadcast` puts a message in every WebSocket queue. |
| 95-130 | `start` and `stop`: the TCP server, then the HTTP server, then the 5 ms loop; and the reverse. |
| 132-153 | `_tick_loop`: every 5 ms open the port if needed, read serial frames, call `engine.tick`, and send a `status` on a change or every 1000 ms. An unexpected exception is written to the evidence log with its traceback and the loop **continues**; a bug must not silently stop a demonstration. |

### `gateway.py` (79 lines)

Command line: `--serial`, ports, `--ui-dir`, `--log-dir`, `-v`, `--set key=value` (any `Config` value). A port that is already in use gives a one-line message. Windows' timer resolution (about 15 ms) makes the 5 ms loop slower than 5 ms there; every timeout in the protocol is at least 500 ms, so this has no effect.

---

## 5. Tests

| File | What it checks |
| --- | --- |
| `tests/test_codec.py` (29) | CRC check value `0x29B1`; ATR, SLIP and payload bytes **identical to ESP32 A's C code** (four frames, including one full of `C0` and `DB` bytes); every single-bit flip is caught; SLIP junk, oversize packets and the bootloader text; TCP framing at 1 byte at a time, 1024 limit; exact JSON bytes. |
| `tests/test_engine.py` (110) | Fake clock and fake devices. Every cell of the contract's state table; every field limit; duplicates; the Start flow and its events; queue full and recovery; retries (identical bytes, 500 ms, 4 tries); heartbeat loss (exactly 3000 ms); stale, duplicate and mismatching results; B's rejections; garbage input. |
| `tests/test_e2e.py` (13) | The real gateway against the **real** `station.c` of ESP32 A and ESP32 B compiled for the PC: full batches with the verdicts checked against `(part_id + K) mod N == 0` computed in the test; a full queue; a serial link that loses every 3rd frame to A and every 4th from A; pause, resume, stop and a new batch; B and A killed mid-batch and recovery; the serial port closing and reopening; a gateway restart against a B that remembers old job ids; HTTP, CORS, WebSocket and `--ui-dir`; the command line and the evidence log. |

I also broke the gateway on purpose in 16 ways (five tries instead of four, no duplicate suppression, the gateway computing the verdict, queue capacity off by one, no result age check, two jobs in flight, and so on) and confirmed that the unit tests fail for every one.

**What is not covered:** real COM ports, real Wi-Fi, Windows' firewall, and timing on the boards themselves.

---

## 6. Running it and troubleshooting

See `README.md` for the steps. Typical problems:

| Symptom | Cause and fix |
| --- | --- |
| `cannot open COM10: ... PermissionError` | Another program holds the port (serial monitor, a previous gateway). Close it. |
| `esp32_a` stays disconnected | Wrong COM port, or the board did not boot (press EN). Frames of ESP32 A show as `A-RX` lines in the log; `frame discarded (crc)` right after a reset is normal (bootloader text). |
| ESP32 B prints `cannot connect to ...:5000 (no answer)` | `GATEWAY_IP` is not this PC's address, or the firewall blocks port 5000, or the PC and the board are not on the same network. |
| Start returns 409 "send config/inspection" | Set N and K first. |
| Start returns 503 | A or B is not connected yet; see the status. |
| The dashboard shows `WS: DISCONNECTED` | Check the gateway address and port in the dashboard header; port 8080 may be blocked by the firewall. |
| Fault `HEARTBEAT_LOST` | A device was silent for 3 seconds (cable, Wi-Fi, a board reset). Clear the fault, wait for both devices, start again. |
