# ESP32 B firmware, explained line by line

This document explains every file of the `ESP32_B` project. Line numbers refer to the files as delivered. Lines whose meaning is easy to get wrong are marked **(watch)**.

**Contents**

1. What the firmware does
2. Decisions the contract does not make (please confirm)
3. Messages, byte by byte
4. File by file: `config.h`, `tcpframe`, `jsonmini`, `station.h`, `station.c`, `link`, `main.c`, build files, tests
5. Setting up, building, flashing and testing

---

## 1. What the firmware does

ESP32 B is the **inspection station**. It does not produce anything: the gateway sends it one job at a time, it waits the configured inspection time, decides PASS or FAIL with the examiner's rule, and sends the verdict back.

```
 Gateway (TCP server, port 5000)                      ESP32 B (TCP client)
        |  <---------- connect ---------------------------- |
        |  <---------- HELLO ------------------------------ |
        |  <---------- HEARTBEAT every 1 s ---------------> |   (both ways)
        |  ----------- CONFIG  {cmd_id, N, K, time} ------> |
        |  <---------- ACK  (accepted / rejected) --------- |   acceptance
        |  <---------- CONFIG_DONE {N, K, time} ----------- |   completion
        |  ----------- JOB_REQ {job_id, part_id} ---------> |
        |  <---------- ACK  (accepted / rejected) --------- |   acceptance
        |                   ... inspect_time_ms ...         |
        |  <---------- JOB_RESULT {verdict, N, K} --------- |   completion
        |  ----------- ACK (of the result) ---------------> |
```

The inspection rule is the one in the contract: **FAIL when `(part_id + K) mod N == 0`, otherwise PASS.** `N`, `K` and the inspection time come from the gateway at run time in a `CONFIG` message. Nothing about the rule is hard-coded, and the verdict is computed on this chip (requirement III).

Layers, bottom to top:

| Layer | File | What it does |
| --- | --- | --- |
| Wi-Fi | `main.c` | Joins the gateway's network |
| TCP socket | `link.c` | Connects, sends, receives, reconnects |
| Length-prefix framing | `tcpframe.c` | 4-byte length, then the message |
| JSON reading | `jsonmini.c` | Turns a message into fields |
| Protocol and inspection | `station.c` | Every rule: ACKs, retries, duplicates, timers, verdict |

Two design points:

- **`station.c` contains no ESP-IDF and no socket code.** Time is passed in and messages leave through a callback. The same file runs on the board and in the PC tests.
- **There is one thread.** `app_main` loops forever calling `link_step()`. No locks are needed.

The three events of requirement VII are kept apart: *delivery* is the TCP bytes arriving (visible in Wireshark), *acceptance* is the `ACK`, *completion* is `CONFIG_DONE` or `JOB_RESULT`.

---

## 2. Decisions the contract does not make (please confirm)

The firmware follows Interface Contract v1 and the project description. Where those are silent, I had to choose. The gateway code (Persons 2 and 3) must agree with these.

| # | What the contract leaves open | What the firmware does |
| --- | --- | --- |
| 1 | The words in `ACK.reason` | Fixed tokens: `malformed` (field missing or wrong type), `out_of_range` (value outside its limit), `not_configured` (no `CONFIG` yet), `busy` (a job is already running), `abandoned` (this job was cut off by a lost connection), `id_conflict` (same `job_id`, different `part_id`). **Add them to the contract**; the gateway should treat `reason` as text. |
| 2 | Content of `HELLO` | `{"type":"HELLO","device":"esp32_b","fw":"1.0.0"}`. The `device` name matches the `devices` keys in the contract's `status` message. |
| 3 | `HEARTBEAT.seq` | Starts at 0 when the board boots and keeps counting across reconnects. |
| 4 | When does the inspection time start? | When the job is accepted, that is, when the `ACK` is sent. |
| 5 | Parameters before the first `CONFIG` | A `JOB_REQ` is rejected (`not_configured`). The board has no default `N`, `K` or time. |
| 6 | Parameters after a reconnect or a reboot | Kept in RAM across a **reconnect**. Lost on a **reboot**, so the gateway must send `CONFIG` again after a `HELLO` from a rebooted board (the board will say `not_configured` if it forgets). |
| 7 | `CONFIG` while a job is running | Accepted. It applies to **later** jobs. The running job keeps the `N` and `K` it was accepted with, and its `JOB_RESULT` echoes those. |
| 8 | Limits for `CONFIG` | `N` at least 1, `K` at least 0, `inspect_time_ms` 10 to 60000 (the contract's field limits). All three must be JSON integers that fit in 32 bits. `5.0` and `"5"` are rejected as `malformed`. |
| 9 | Limits for `job_id` and `part_id` | Any value 0 to 4294967295. |
| 10 | Duplicate protection | Two memories of 32: one for `job_id`, one for `cmd_id` (the contract says "the last 32 `job_id` and `cmd_id` values"). Only **accepted** jobs and commands are remembered, so a job that was rejected as `busy` can be offered again. A repeated accepted `CONFIG` gets its `ACK` and `CONFIG_DONE` again and is **not applied again**. A repeated accepted job gets its `ACK` (and its `JOB_RESULT` again if it is finished) and is **not inspected again**. |
| 11 | Safe state when the connection is lost | The job in progress is **abandoned** (no late verdict is ever sent), everything waiting for an `ACK` is dropped, and nothing more is sent until the connection is back. If the gateway offers the abandoned job again it is answered `rejected` with `abandoned`. |
| 12 | What counts as "link lost" | Nothing valid from the gateway for 3000 ms (the contract's "no heartbeat for 3000 ms"). Any message that is a JSON object with a `type` counts as a sign of life, not only heartbeats. The board then closes the TCP connection and reconnects every second. |
| 13 | A result or `CONFIG_DONE` that is never acknowledged | Sent up to 4 times (500 ms apart, identical bytes), then reported as failed in the log and dropped. ESP32 B does not enter a Fault state itself: the gateway does (`RETRIES_EXHAUSTED`). |
| 14 | A message that is not valid JSON, or has no string `type` | Ignored and counted. The connection stays up. |
| 15 | A length prefix above 1024 | The board closes the connection (a TCP stream cannot be re-synchronised) and reconnects. |
| 16 | Extra fields, key order, white space | Extra fields are ignored. Key order and spaces do not matter on input. A key that appears twice makes the message `malformed`. Messages the board sends are compact, in the field order of the contract. |
| 17 | Where the gateway's address comes from | `GATEWAY_IP` and `GATEWAY_PORT` in `config.h`, plus `WIFI_SSID` and `WIFI_PASSWORD`. **You must edit these before building.** |
| 18 | `ACK` of `HELLO` and of `HEARTBEAT` | None is sent or expected. |

---

## 3. Messages, byte by byte

**One message on the wire.** The contract's example, `JOB_REQ` for job 28, part 8, is 42 bytes of JSON and is sent as:

```
00 00 00 2A  7B 22 74 79 70 65 22 3A 22 4A 4F 42 5F 52 45 51 22 2C 22 6A 6F 62 5F 69 64 22 3A 32 38 2C 22 70 61 72 74 5F 69 64 22 3A 38 7D
 length = 42  {  "  t  y  p  e  "  :  "  J  O  B  _  R  E  Q  "  ,  "  j  o  b  _  i  d  "  :  2  8  ,  "  p  a  r  t  _  i  d  "  :  8  }
```

**One part's life (N = 5, K = 2, inspect_time_ms = 500, part 8: (8 + 2) mod 5 = 0, so FAIL).**

```
G -> B  {"type":"JOB_REQ","job_id":28,"part_id":8}
B -> G  {"type":"ACK","ref":"JOB_REQ","job_id":28,"status":"accepted"}
        ... 500 ms ...
B -> G  {"type":"JOB_RESULT","job_id":28,"part_id":8,"verdict":"FAIL","N":5,"K":2}
G -> B  {"type":"ACK","ref":"JOB_RESULT","job_id":28,"status":"accepted"}
```

**If the gateway does not acknowledge the result:** the same bytes are sent again after 500 ms, 1000 ms and 1500 ms (4 sends in all), and at 2000 ms the board gives up and writes `stage=failed` in its log.

**A rejection:**

```
G -> B  {"type":"JOB_REQ","job_id":31,"part_id":11}          (while job 30 is running)
B -> G  {"type":"ACK","ref":"JOB_REQ","job_id":31,"status":"rejected","reason":"busy"}
```

**The log on the serial console** (open **ESP-IDF: Monitor Device**) shows every message and every decision, one line each, with the time since boot in ms:

```
[     12034 ms] RX  {"type":"JOB_REQ","job_id":28,"part_id":8}
[     12034 ms] EVT job_id=28 part_id=8 stage=accepted N=5 K=2 inspect_time_ms=500
[     12034 ms] TX  {"type":"ACK","ref":"JOB_REQ","job_id":28,"status":"accepted"}
[     12535 ms] EVT job_id=28 part_id=8 stage=completed verdict=FAIL N=5 K=2
[     12535 ms] TX  {"type":"JOB_RESULT","job_id":28,"part_id":8,"verdict":"FAIL","N":5,"K":2}
[     12547 ms] RX  {"type":"ACK","ref":"JOB_RESULT","job_id":28,"status":"accepted"}
[     12547 ms] EVT JOB_RESULT job_id=28 acknowledged by the gateway (accepted)
```

`RX` is received, `TX` is sent, `EVT` is what the station decided. This is the "serial log" evidence the project asks for: a job request and a matching result with the same `job_id` and `part_id`.

---

## 4. File by file

### `main/config.h` (43 lines)

| Lines | Explanation |
| --- | --- |
| 4-7 | Every tunable number lives here. "contract" marks values from Interface Contract v1. "EDIT" marks what you must set for your own network. |
| 10-11 | `WIFI_SSID`, `WIFI_PASSWORD`: the gateway's hotspot. An empty password means an open network. |
| 12 | `GATEWAY_IP`: the gateway's fixed address on that network, as text. |
| 13 | `GATEWAY_PORT` 5000, from the contract. |
| 16-17 | The name and firmware version sent in `HELLO`. |
| 20 | `STATION_ACK_TIMEOUT_MS` 500: how long to wait for the gateway's `ACK`. |
| 21 | `STATION_MAX_TRIES` 4 **(watch)**: the total number of sends, so one send plus 3 retries. |
| 22 | Heartbeat period, 1000 ms. |
| 23 | `STATION_LINK_TIMEOUT_MS` 3000: silence from the gateway longer than this means the link is lost. |
| 24 | `STATION_HISTORY` 32: remembered `job_id` values, and separately remembered `cmd_id` values. |
| 27-28 | Allowed `inspect_time_ms`: 10 to 60000. |
| 31 | Largest message body, 1024 bytes. |
| 34 | `STATION_MSG_MAX` 192: the size of the buffers for messages the board builds. The longest it ever builds is a `JOB_RESULT` with every number at its maximum, about 110 bytes. |
| 35 | `STATION_OUTBOX` 4: how many messages can wait for an `ACK` at the same time. In practice at most two. |
| 36 | Set to 0 to leave heartbeats out of the log. |
| 39-41 | Connection handling: wait 1 s between attempts, give up one attempt after 3 s, and wake the main loop at least every 5 ms. |

### `main/tcpframe.h`, `main/tcpframe.c` (length-prefix framing)

TCP is a stream of bytes with no message boundaries: one `recv()` can return half a message or three messages. The contract fixes the boundary: **a 4-byte big-endian length, then that many bytes.**

| Lines | Explanation |
| --- | --- |
| `tcpframe.h` 15-16 | 4-byte header, 1024-byte maximum body. |
| `tcpframe.h` 21 | `tcpframe_encode`: write header and body into a buffer. Returns the length, or 0 if the body is too long or the buffer too small. |
| `tcpframe.h` 23-27 | Results of feeding bytes: need more, a whole message is ready, or an error. |
| `tcpframe.h` 29-37 | Decoder state: where bodies are collected, the header bytes so far, the announced length, the bytes collected, and a sticky `failed` flag. |
| `tcpframe.h` 44-57 | How to call `tcpframe_feed` in a loop. It stops after each message and reports in `*used` how many bytes it took, so the caller feeds the rest again. The body is valid only until the next call. |
| `tcpframe.c` 7 | Refuse a body over 1024 bytes, or a buffer that cannot hold header plus body. |
| `tcpframe.c` 10-13 | Write the length, most significant byte first (big-endian). |
| `tcpframe.c` 14-17 | Copy the body (skipped for an empty body) and return the total. |
| `tcpframe.c` 20-33 | Initialise and reset. Reset forgets a half-received message, which is what a new connection needs. |
| `tcpframe.c` 40-44 | Clear the outputs. After an error the decoder stays failed. |
| `tcpframe.c` 47-65 | Header phase: take one byte at a time (48). When the fourth arrives (49), assemble the length (50-51). **(watch)** If it is over 1024, or over the buffer size, mark the decoder failed and report the error (53-57): there is no way to find the next message in a TCP stream. Otherwise remember the length (58-59). A length of 0 is an empty message and is returned at once (60-64). |
| `tcpframe.c` 66-82 | Body phase: copy as many bytes as are still needed and available (67-73). When the body is complete (74), report it and reset for the next header (75-80). |
| `tcpframe.c` 84-85 | All bytes taken, message not finished. |

### `main/jsonmini.h`, `main/jsonmini.c` (reading the JSON messages)

ESP-IDF has a JSON library, but this one is small, needs no memory allocation, and works the same on a PC, so the logic can be tested there. It reads **one flat JSON object**, which is all the contract uses.

Rules, chosen so that a wrong message is caught rather than guessed at:

- the text must be exactly one object, and nothing may follow it;
- integers must be written as integers (`5`, not `5.0`, `5e0` or `"5"`);
- nested objects and arrays are accepted and skipped, so a future extra field does not break the board;
- a key that appears twice is an error;
- strings longer than 48 characters are cut, and then count as the wrong type;
- `\u0000` is refused, because a zero byte would end a C string early.

| Lines | Explanation |
| --- | --- |
| `jsonmini.h` 21-23 | Limits: 16 fields, 24-character keys, 48-character string values. |
| `jsonmini.h` 25-29 | Value kinds: string, integer, other. |
| `jsonmini.h` 31-42 | A stored field and an object (a table of fields). |
| `jsonmini.h` 46 | `json_parse_object`: true only for one valid object. |
| `jsonmini.h` 48-59 | The getters. `json_get_u32` tells "missing", "wrong type" and "out of range" apart, which is how the station chooses between the reasons `malformed` and `out_of_range`. |
| `jsonmini.c` 7-13 | Skip spaces, tabs, carriage returns and new lines. |
| `jsonmini.c` 32-102 | `parse_string`: read a quoted string. Raw control characters are refused (49 to 51), the escapes of RFC 8259 are decoded (58-78), characters above 127 written as `\uXXXX` become `?` (only ASCII names matter here), and too-long strings are cut and flagged. |
| `jsonmini.c` 110-176 | `parse_number`: the number grammar of RFC 8259 (no leading zeros, digits after a point or exponent). It tracks whether the number is an integer. **(watch)** Huge integers cannot overflow: the sum stops growing (130-135) and the value is clamped to the 64-bit limits, so the getter reports "out of range" instead of a wrapped-around number. |
| `jsonmini.c` 178-188 | Match the words `true`, `false`, `null` exactly. |
| `jsonmini.c` 190-283 | `skip_value`: step over any value, including nested objects and arrays. It stops at depth 8 so a maliciously deep message cannot exhaust the stack. |
| `jsonmini.c` 285-293 | Find a field by key. |
| `jsonmini.c` 295-377 | `json_parse_object`: expects `{`, then key, colon, value, comma or `}`. A key longer than 24 characters is skipped, because no key in use is that long (351). A repeated key is an error (354). More than 16 fields is an error (357). The last line (376) refuses any text after the closing brace. |
| `jsonmini.c` 379-408 | The getters. `json_get_u32` accepts only an integer from 0 to 4294967295. |

### `main/station.h` (170 lines)

| Lines | Explanation |
| --- | --- |
| 1-24 | What ESP32 B does, and the note that this file has no ESP-IDF or socket code. |
| 26-30 | Three states: `DISCONNECTED`, `IDLE` (connected, nothing running), `INSPECTING` (one job running). These are ESP32 B's own states; Idle/Running/Paused/Fault belong to the gateway. |
| 33 | The "transmit" callback: it receives one message body (not framed). |
| 37 | The "log" callback, with the tags `TX`, `RX`, `EVT`. |
| 39-68 | Counters, for the log and the tests. Each comment says when it counts. They are never reset. |
| 70-74 | A job is `RUNNING`, `DONE` or `ABANDONED`. |
| 77-85 | A remembered job: its two ids, its state, its verdict, and the `N` and `K` it was accepted with. |
| 88-94 | A remembered `CONFIG`: its `cmd_id` and the values, so a repeat can be answered with the same values. |
| 96-109 | An outbox entry: a message waiting for its `ACK`, with the **exact bytes** (so a retry is identical), the number of tries and the next deadline. |
| 111-141 | Everything the station remembers: the link (up, drop requested, last time heard, next heartbeat), the parameters set by `CONFIG` (`configured`, `n`, `k`, `inspect_ms`), the one job in progress, the two memories of 32 entries, the outbox, a parse buffer (kept here so the stack stays small), and the counters. |
| 143-168 | The public functions, each with its contract in a comment. |

### `main/station.c` (603 lines)

**Helpers (1-116)**

| Lines | Explanation |
| --- | --- |
| 7 | `UL(x)` casts a number to `unsigned long` so that `%lu` is correct on both the ESP32 (where `uint32_t` is `unsigned long`) and a PC. |
| 12-19 | **(watch)** `time_reached` compares times with a signed difference, so it stays correct when the 32-bit millisecond clock wraps (after 49 days). A plain `now >= deadline` would break there. `time_since` is the wrap-safe elapsed time. |
| 24-49 | Logging helpers. `evt` formats a text line and tags it `EVT`. The `format(printf)` attribute makes the compiler check every format string. |
| 51-60 | `send_body`: sends only while connected (53), logs the message (55-57), then hands it to the transmit callback. |
| 64-107 | The builders. Each writes a message with `snprintf` in the field order of the contract, and returns 0 if it did not fit (66-67). `build_ack` makes an "accepted" ACK when `reason` is `NULL`, otherwise a "rejected" ACK with the reason (81-92). |
| 111-115 | **(watch)** The inspection rule. The sum `part_id + K` is done in 64 bits, because two 32-bit numbers can add up to more than 2^32 and a 32-bit sum would wrap and give a different answer. |

**Records and the outbox (117-185)**

| Lines | Explanation |
| --- | --- |
| 119-148 | Look up a `job_id`, a `cmd_id`, or an outbox entry. `used` is checked so an empty slot is never matched (id 0 is a legal value). |
| 151-178 | `out_add`: send a message that needs an `ACK` and keep its exact bytes. If the same message is already waiting, nothing is added (155), so a repeated request cannot start a second wait. If all four slots are full (cannot happen with one job and one command at a time) the first slot is reused and counted (169-173). The first send counts as try 1 (174-175), and the answer is due in 500 ms (176). |
| 180-185 | Forget everything waiting for an ACK. |

**Connection (187-257)**

| Lines | Explanation |
| --- | --- |
| 189-205 | `go_disconnected`, the **safe state**. Mark the link down (193), and if a job was running (195) mark it `ABANDONED` (198), so no verdict for it is ever sent. Drop everything waiting for an ACK (203). The wording of line 189-190: a job or command that comes again after reconnecting is recognised by its id. |
| 207-213 | `station_init`: zero everything. The station starts disconnected. |
| 215-229 | `station_on_connect`: link up, timers started, first heartbeat due at once (223), then **HELLO** is sent (227-228). |
| 231-238 | `station_on_disconnect`: if the link was up, enter the safe state; in any case clear the "drop requested" flag. Calling it twice is harmless. |
| 240-244 | Count a framing error. |
| 246-257 | Two small getters. The state is derived: not connected, or connected and busy or idle. |

**CONFIG (259-337)**

| Lines | Explanation |
| --- | --- |
| 261-267 | `send_ack`: build and send an ACK for a `CONFIG` or a `JOB_REQ`. |
| 269-275 | `queue_cfgdone`: build `CONFIG_DONE` from a remembered `CONFIG` and send it with ACK tracking. |
| 277-282 | `reject_cmd`: count, log and send a rejected ACK. |
| 290-294 | **(watch)** Without a usable `cmd_id` there is nothing to put in an ACK, so the message is ignored and counted. |
| 296-303 | **Duplicate command.** If this `cmd_id` was accepted before: count it, send the ACK again, send `CONFIG_DONE` again with the **remembered** values, and return **without applying anything**. This is how "at most once" holds even when the gateway resends because it missed our answer. |
| 305-312 | Read `N`, `K`, `inspect_time_ms`. If any is missing or is not an integer, the reason is `malformed`. |
| 313-317 | If a value is outside its limit (`N` below 1, time not in 10 to 60000, or a number outside 32 bits), the reason is `out_of_range`. `K` has no upper limit other than 32 bits. A rejected command changes nothing and no `CONFIG_DONE` is sent. |
| 319-331 | Valid: apply (`configured`, `n`, `k`, `inspect_ms`), remember the command in the ring of 32 (324-330), count it. |
| 333-336 | Log "accepted", send the ACK (acceptance), log "completed", send `CONFIG_DONE` (completion). The ACK always goes out first. |

**JOB_REQ (339-435)**

| Lines | Explanation |
| --- | --- |
| 341-346 | `reject_job`: count, log and send a rejected ACK. |
| 348-354 | `queue_result`: build a `JOB_RESULT` from a job record and send it with ACK tracking. |
| 361-365 | No usable `job_id`: nothing to answer, ignore and count. |
| 366-371 | `job_id` is fine but `part_id` is missing or not an integer: reject as `malformed`. |
| 373-389 | **Duplicate job.** The same `job_id` as an accepted one: it is **never inspected again**. A different `part_id` is a conflict (376-377). An abandoned job is rejected as `abandoned` (378-379). Otherwise the ACK is sent again (383) and, if the job is already finished, so is its result (384-386). |
| 391-394 | **(watch)** No `CONFIG` yet: reject (`not_configured`). This is what guarantees that no verdict is ever computed from default values. |
| 395-398 | A job is already running: reject (`busy`). The gateway keeps at most one in flight, so this is a safety net. Rejected jobs are not remembered, so the gateway may offer the same `job_id` again later. |
| 400-402 | Take the next slot of the ring. |
| 403-409 | **(watch)** Record the job with a **snapshot** of the current `N` and `K` (408-409). A `CONFIG` that arrives later changes the parameters for later jobs, but this job keeps the values it was accepted with, and its result reports them. |
| 410-412 | Mark the station busy, and set the time the verdict is due: now plus the inspection time. |
| 414-416 | Log "accepted" and send the ACK. |
| 419-435 | `complete_job`: compute the verdict from the job's own `part_id`, `N`, `K` (423), mark the job done, free the station (425), count PASS or FAIL, log "completed" and send the `JOB_RESULT` with ACK tracking. |

**ACK from the gateway (437-501)**

| Lines | Explanation |
| --- | --- |
| 449-453 | An ACK without `ref` or `status` is ignored. |
| 454-464 | Which of our messages is it about? `JOB_RESULT` (id `job_id`) or `CONFIG_DONE` (id `cmd_id`). Any other `ref` matches nothing we wait for. |
| 465-469 | The id must be a valid number. |
| 470-478 | `status` must be `accepted` or `rejected`. |
| 480-485 | Find the message waiting for it. None: a late or repeated ACK, counted and ignored. |
| 486-500 | Found: stop waiting (486). Count it as acknowledged or rejected. **A rejection also stops the retries**: the gateway has answered, only not with what we hoped. |

**Input (503-537)**

| Lines | Explanation |
| --- | --- |
| 510-512 | Ignore anything that arrives while we think we are disconnected. |
| 513-519 | If the body is over 1024 bytes, is not one JSON object, or has no string `type`, log it, count it as `malformed` and ignore it. The connection stays up. |
| 520-522 | Log the received message. |
| 523 | **(watch)** Only a valid message refreshes `last_rx_ms`, the time of the last sign of life. Garbage does not keep the link "alive". |
| 525-536 | Act on the `type`: `HEARTBEAT` is counted; `CONFIG`, `JOB_REQ` and `ACK` go to their handlers; anything else (including `HELLO`, `JOB_RESULT` and `CONFIG_DONE`, which the gateway must never send) is counted as unexpected. |

**Time-driven work (539-603)**

| Lines | Explanation |
| --- | --- |
| 541-574 | `tick_outbox`: for each message waiting for an ACK whose deadline has passed (547): if tries remain (550) count another try, set the next deadline, log it, and send the **same bytes** again (558-561). If not, count it as failed, log `stage=failed` and stop (562-571): the job is reported as failed, never as a success. |
| 578-580 | Nothing to do while disconnected. |
| 582-588 | **(watch)** The link watchdog. If nothing valid has arrived for 3000 ms: count it, enter the safe state, and ask the caller to close the socket (`drop_request`). The state is set to disconnected first, so the function returns before sending anything on a dead link. |
| 590-596 | Send a heartbeat when due; its number is sent and then increased. |
| 598-600 | If the job's time is up, complete it. |
| 602 | Then check the messages waiting for an ACK. The order matters: a result created in line 599 gets its first deadline from `now`, so it is not retried in the same tick. |

### `main/link.h`, `main/link.c` (the TCP connection)

`link.c` is compiled for the ESP32 (lwIP sockets) and for a PC (POSIX sockets) from the same source, so the real connection code is tested on a PC against a fake gateway. Everything that differs (clock, sleeping, "is Wi-Fi up", LED) comes in through `link_env_t`.

| Lines | Explanation |
| --- | --- |
| `link.h` 22-28 | The environment: a clock, a sleep function, a "network ready" function and an idle hook, plus a user pointer. |
| `link.h` 30-44 | The connection state: the socket (-1 when none), a `broken` flag, the time of the last connection attempt, the framing decoder and the buffers. The buffers are inside the structure so nothing large sits on the stack. |
| `link.c` 4-25 | The only platform differences: which headers, and the names for `select`, `ioctl` and `close`. **(watch)** On the ESP32 the `lwip_` names are used because ESP-IDF sends the plain names through its file-system layer. `NET_NOSIGNAL` stops a send on a closed socket from killing a PC program. |
| `link.c` 29-43 | Small helpers: wrap-safe elapsed time, "would block" test, millisecond to `timeval` conversion. |
| `link.c` 48-95 | `sock_connect`: parse the IP (61-63), create the socket (65), make it **non-blocking** (69), start the connection (74). A non-blocking connect returns "in progress" (75), then `select` waits for it to finish, at most `timeout_ms` (79-85), and `SO_ERROR` tells whether it worked (86). **(watch)** Without this, connecting to an unreachable gateway would block for a long time. `TCP_NODELAY` (93) makes each small message leave at once instead of waiting to fill a packet. |
| `link.c` 98-121 | `sock_send_all`: send until every byte is gone. If the socket's buffer is full (106) wait up to 1 s for room (107-115); any other error means the connection is broken (116-117). |
| `link.c` 125-137 | `link_tx`, the station's way out: add the length prefix (133) and send (134). On failure set `broken`, and `link_step` closes the connection. |
| `link.c` 139-150 | `link_init`: set up the structure and **connect the station's transmit callback to this connection** (148-149). |
| `link.c` 152-161 | `close_connection`: close the socket, forget a half-received message, and tell the station (which enters the safe state). |
| `link.c` 165-186 | `feed_bytes`: cut received bytes into messages. A framing error is counted and the connection is closed (174-177). After each message, if the station asked to drop the link, stop (180-182). |
| `link.c` 188-253 | `link_step`, one pass of the main loop. |
| `link.c` 192-198 | No Wi-Fi: close any connection and sleep. |
| `link.c` 200-217 | Not connected: wait at least `LINK_RECONNECT_MS` between attempts (203-206), try to connect (209), and if it worked tell the station (214), which sends `HELLO`. |
| `link.c` 219-246 | Connected: wait for data, but at most 5 ms, so the timers keep running (227-228). If data is ready, read it (231) and feed it. A read of 0 bytes means the gateway closed the connection (238); a real error does too. A would-block result is not an error. |
| `link.c` 248-252 | Run the station's timers, close the connection if it is broken or the watchdog fired, and call the idle hook (the LED). |

### `main/main.c` (170 lines): the only ESP-IDF file

| Lines | Explanation |
| --- | --- |
| 1-17 | Overview. Unlike ESP32 A, this board's USB serial port is free, so it carries the readable log. |
| 36 | LED on GPIO 2 (set to -1 if your board has none). |
| 38-40 | The station and the connection are static, so nothing large is on the stack. `s_ip_ready` is `volatile` because the Wi-Fi event task writes it while the main task reads it. |
| 42-45 | Milliseconds since boot, from the 64-bit microsecond timer. The cast to 32 bits is the wrap that `station.c` handles. |
| 50-54 | The log callback: one line per message or decision, with the time, the tag and the text. `%.*s` prints exactly `len` characters, because the text is not NUL-terminated. |
| 58-65 | LED set-up. |
| 68-88 | LED meaning: off = not connected to the gateway; steady on = connected and idle; fast blink = inspecting. |
| 92-97 | The sleep function. **(watch)** The tick count is never 0 (96): a sleep of zero ticks would let the loop spin without letting the idle task run, and trigger the watchdog. |
| 99-102 | "Is the network up" is the flag set by the event handler. |
| 106-118 | The Wi-Fi event handler: when the Wi-Fi driver starts, connect (110-111); when it loses the network, clear the flag and **try to rejoin** (112-114); when an IP address arrives, set the flag (115-116). `link.c` closes the TCP connection by itself when the flag drops. |
| 120-150 | `wifi_start`: set up the flash storage the Wi-Fi driver needs (124-130, with the standard erase-and-retry), the network stack (132-134), the Wi-Fi driver (135), the event handlers (136-137) and the Wi-Fi settings (139-144). **(watch)** The security threshold (142) is WPA2 when there is a password and open otherwise. Management-frame protection is allowed but not required (143-144), so both WPA2 and WPA3 hotspots work. Finally switch to station mode, start, and **turn Wi-Fi power saving off** (149): power saving adds delay to every message. |
| 152-170 | `app_main` set-up order: LED, station (with the log callback), connection (to `GATEWAY_IP`:`GATEWAY_PORT`), Wi-Fi. Then loop forever calling `link_step`. |

### Build files

| File | Explanation |
| --- | --- |
| `CMakeLists.txt` | Same form as the labs: minimum CMake version, load ESP-IDF's build system from `IDF_PATH`, name the project `esp32_b`. It must sit in the **root of the project folder**, next to `main/`. |
| `main/CMakeLists.txt` | Registers the component `main` with its five source files and says the headers are in the same folder. `PRIV_REQUIRES` names the ESP-IDF components used: Wi-Fi, network interface, events, flash storage, sockets, timer and GPIO. **(watch)** A new `.c` file must be added to this list, or the linker reports undefined references. |
| `sdkconfig.defaults` | A 1 ms RTOS tick (so the loop can wait 5 ms at a time) and an 8 KB stack for `app_main`, because Wi-Fi, sockets and the log calls need more than the 3.5 KB default. Unlike ESP32 A, the console is **left on**. Applied only when `sdkconfig` is created: delete `sdkconfig` (or run Full Clean) after changing it. |

### Tests (`test/`)

| File | What it is |
| --- | --- |
| `test_b.c` | PC unit tests of the real `station.c`, `jsonmini.c` and `tcpframe.c`, with a fake gateway and a fake clock. About 18,700 checks; most are a table of every part from 1 to 200 against a plain loop for many `N` and `K`. |
| `sim_b.c` | ESP32 B on a PC: the real `station.c` and `link.c` behind POSIX sockets. |
| `host_check_b.py` | A fake gateway that talks real TCP to the board (or to `sim_b`) and prints PASS or FAIL for 24 checks. |
| `test.mk` | `make -f test.mk` builds and runs the unit tests (warnings are errors, with the address and undefined-behaviour sanitizers); `make -f test.mk sim` builds `sim_b`. |

The unit tests cover: the JSON reader (valid and invalid forms, escapes, huge numbers, nesting, duplicate keys), the framing (the contract's 42-byte example, every possible split of three messages, 1024 versus 1025 bytes, empty bodies), the verdict rule (including sums above 2^32), connection and heartbeats, `CONFIG` (every kind of invalid value, limits, repeats), jobs (before `CONFIG`, the contract's example, busy, snapshot of `N` and `K`, repeats, id conflicts), retries (500 ms, four tries, identical bytes), the two memories of 32 (the 33rd entry pushes out the first), the link watchdog, abandoned jobs, bad input, and the 32-bit clock wrap.

I also broke twelve things on purpose (number of tries, timeouts, history size, the limit on inspection time, a 32-bit sum, swapped PASS and FAIL, duplicate checks removed, no snapshot, and others) and confirmed the tests fail every time.

**What is not tested:** the ESP-IDF calls in `main.c` and the lwIP branch of `link.c`. They were only syntax-checked against stand-in headers I wrote, so the first real build is the real check, and the first connection to a real Wi-Fi network is the first real test of the Wi-Fi code.

---

## 5. Setting up, building, flashing and testing

1. **Edit `main/config.h`:** `WIFI_SSID`, `WIFI_PASSWORD`, and `GATEWAY_IP` (the gateway's address on that Wi-Fi network). Everything else can stay.
2. In VS Code use **File > Open Folder** on `D:\Networks\Project\ESP32_B` itself (not on the parent `Project` folder, which has no `CMakeLists.txt`). Select target `esp32` and the COM port again, because these settings are stored per folder.
3. **ESP-IDF: Build Project**, then **ESP-IDF: Flash Device**.
4. **ESP-IDF: Monitor Device** shows the log. The first lines show the Wi-Fi joining. Once the gateway is listening you should see `EVT link up` and a `TX` of `HELLO`, then a heartbeat each second. The LED turns steady on when connected.
5. To test the board **before the real gateway exists**, run the fake gateway on your PC: `python test\host_check_b.py` (it listens on port 5000). Set `GATEWAY_IP` to the PC's address on the Wi-Fi network the board joins (for example the PC's own hotspot address), rebuild and flash, and allow Python through Windows Firewall for incoming connections. It prints PASS or FAIL for 24 checks and takes about 40 seconds. If the board is already connected to something else, reset it.
6. To run the tests without a board: `cd test`, then `make -f test.mk` (needs `gcc`; on Windows use WSL or MSYS2). `python test\host_check_b.py --selftest` checks only the script's own framing code.

**If the board does not connect:** check in this order that the SSID and password are right (the monitor shows Wi-Fi disconnect messages), that the board and the gateway are on the same network, that `GATEWAY_IP` is the gateway's address on that network, that the gateway is really listening on port 5000, and that its firewall allows incoming connections on that port. The board retries every second, so no reset is needed once the cause is fixed.
