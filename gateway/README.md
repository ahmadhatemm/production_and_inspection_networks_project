# Gateway

The PC between the operator dashboard and the two ESP32 boards (MCT 431 project, Milestone 1: production mode).

```
 ESP32 A  --USB serial (SLIP + ATR, 115200 8N1)-->  +---------+  --HTTP / WebSocket, port 8080-->  Dashboard
 (production)                                       | gateway |
 ESP32 B  --Wi-Fi, TCP port 5000 (length + JSON)--> +---------+
 (inspection)
```

It follows **Interface Contract v1.1** and the project description. It never computes a verdict: ESP32 B does, the gateway only counts what B reports.

Line-by-line explanation, the list of decisions the contract leaves open, and byte-level examples: **`Gateway_Explained.md`**.

## Setup (once)

Python 3.10 or newer.

```
cd D:\Networks\Project\gateway
python -m pip install -r requirements.txt
```

(`pyserial` for the COM port, `aiohttp` for HTTP and WebSocket.)

## Run

1. Close every serial monitor (VS Code, Arduino, PuTTY). Only one program can hold the COM port.
2. Plug in ESP32 A and find its port in Device Manager, Ports (COM & LPT), for example `COM10`.
3. Start the gateway:

```
python gateway.py --serial COM10
```

   To open the dashboard from the gateway itself, add `--ui-dir ..\ui\ui` and browse to `http://localhost:8080/`.
4. Set `GATEWAY_IP` in `ESP32_B/main/config.h` to this PC's IPv4 address (`ipconfig`), and power ESP32 B. It connects to TCP port 5000.
5. Allow Python through **Windows Defender Firewall** for private networks. Ports needed: **5000** (ESP32 B) and **8080** (dashboard). Without this, B shows `cannot connect ... (no answer)`.

What you should see on the console: `ESP32 B connected`, `esp32_a connected`, then the dashboard shows both devices green. Heartbeats are written to the log file only (add `-v` to see them on the console).

### Options

| Option | Meaning |
| --- | --- |
| `--serial PORT` | ESP32 A's port: `COM10`, `/dev/ttyUSB0`, or a pyserial URL such as `socket://127.0.0.1:7000`. Without it ESP32 A is shown as not connected. |
| `--http-port N` | Operator HTTP and WebSocket port (default 8080). |
| `--b-port N` | TCP port for ESP32 B (default 5000, the contract). |
| `--ui-dir DIR` | Serve the dashboard files from this folder on the HTTP port. |
| `--log-dir DIR` | Where the evidence logs go (default `logs` next to `gateway.py`). |
| `-v` | Print heartbeats on the console too. |
| `--set KEY=VALUE` | Override any value in `gw/config.py` (repeatable), for example `--set queue_capacity=3`. |

If the serial port cannot be opened the gateway keeps running and tries again every second, so unplugging and replugging ESP32 A needs no restart.

## Operator API (the contract)

```
POST /api/v1/batch/start        {cmd_id, target_parts, cycle_time_ms, inspect_time_ms}
POST /api/v1/batch/pause        {cmd_id}
POST /api/v1/batch/resume       {cmd_id}
POST /api/v1/batch/stop         {cmd_id}
POST /api/v1/config/inspection  {cmd_id, N, K}
POST /api/v1/mode               {cmd_id, mode}
POST /api/v1/fault/clear        {cmd_id}
GET  /api/v1/status
GET  /api/v1/batch/report
GET  ws://<gateway-ip>:8080/ws
```

**Order for a demonstration:** `config/inspection` (the examiner's N and K) first, then `batch/start`. Start is refused with 409 until N and K have been set.

Quick check without the dashboard (PowerShell):

```
Invoke-RestMethod -Method Post -Uri http://localhost:8080/api/v1/config/inspection -ContentType application/json -Body '{"cmd_id":1,"N":5,"K":2}'
Invoke-RestMethod -Method Post -Uri http://localhost:8080/api/v1/batch/start -ContentType application/json -Body '{"cmd_id":2,"target_parts":10,"cycle_time_ms":1000,"inspect_time_ms":500}'
Invoke-RestMethod http://localhost:8080/api/v1/status
```

Use a **new `cmd_id` for every new command**. Repeating a `cmd_id` returns the first answer with `"duplicate": true` and does not run the command again. Do not use `cmd_id` values from 4026531840 up: the gateway uses those itself.

## Evidence

Every message in both directions and every event (state changes, stages, faults, duplicates, retries) is written with a timestamp to `logs/gateway_YYYYMMDD_HHMMSS.log`:

```
2026-11-18T12:00:03.412 +    3412 ms A-TX  seq=7 CMD cmd_id=17 cmd=start cycle_time_ms=1000 target_parts=20
2026-11-18T12:00:04.101 +    4101 ms B-TX  {"type":"JOB_REQ","job_id":28,"part_id":8}
2026-11-18T12:00:04.133 +    4133 ms B-RX  {"type":"JOB_RESULT","job_id":28,"part_id":8,"verdict":"FAIL","N":5,"K":2}
```

Tags: `A-TX`/`A-RX` serial, `B-TX`/`B-RX` TCP, `OP-RX`/`OP-TX` HTTP, `EVT` events, `SYS` start-up and connections. Use these files together with the Wireshark capture of port 5000 for the evidence deliverable.

## Full-queue policy (Requirement IV)

The queue holds **5** parts (the ones waiting plus the one being inspected; `queue_capacity`). When it is full the gateway **blocks production**: it answers ESP32 A's next part with `QUEUE_FULL`, A holds that part and offers it again every 200 ms, and `queue.blocked` is `true` in the status until room is free. No part is lost and none is duplicated. Nothing is raised as a Fault.

## Tests

```
python -m unittest tests.test_codec tests.test_engine     # 139 tests, under a second
python -m unittest tests.test_e2e                         # about a minute, needs gcc (WSL or MSYS2 on Windows)
```

- `test_codec`: CRC, SLIP, ATR and TCP framing, checked against bytes made by ESP32 A's own C code.
- `test_engine`: the protocol rules with a fake clock and fake devices (state table, duplicates, retries, queue, faults, stale results).
- `test_e2e`: the real gateway against the real ESP32 A and ESP32 B logic compiled for the PC (`ESP32_A/test/sim_a.c`, `ESP32_B/test/sim_b.c`): full batches, a full queue, a lossy serial link, pause/resume/stop, a lost device, recovery, HTTP and WebSocket details, and the command line itself.

## Layout

```
gateway.py        command line entry point
requirements.txt  pyserial, aiohttp
gw/
  config.py       every tunable number (contract values marked)
  serialproto.py  CRC-16, SLIP, ATR record, the 16-byte production message
  bproto.py       TCP length-prefix framing and the JSON messages for ESP32 B
  engine.py       the protocol brain: states, queue, IDs, ACKs, retries, duplicates, faults (no I/O)
  links.py        the serial port and the TCP server
  server.py       HTTP, WebSocket, CORS, optional dashboard files
  evidence.py     the log file
  app.py          wires everything together
tests/            unit, engine and end-to-end tests
Gateway_Explained.md
```

## Not tested on real hardware yet

The tests use the firmware logic on a PC, not the boards. The real COM port, the Wi-Fi link and Windows' firewall behaviour are what the first integration run will test.
