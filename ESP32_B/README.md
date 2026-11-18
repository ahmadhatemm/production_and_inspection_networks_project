# ESP32 B: inspection station

Firmware (C, ESP-IDF) for the inspection station of the MCT 431 project. It connects over Wi-Fi and TCP to the gateway (port 5000), receives `CONFIG` and `JOB_REQ`, waits the configured inspection time, and returns a `JOB_RESULT` with PASS or FAIL: **FAIL when `(part_id + K) mod N == 0`**.

Full line-by-line explanation, the list of decisions the contract leaves open, and byte-level examples: **`ESP32_B_Explained.md`**.

## Before you build

1. Edit `main/config.h`: `WIFI_SSID`, `WIFI_PASSWORD`, `GATEWAY_IP`. (`GATEWAY_PORT` is 5000 from the contract.)
2. In VS Code open **this folder** (`ESP32_B`) with File > Open Folder, select target `esp32` and your COM port.
3. ESP-IDF: Build Project, Flash Device, Monitor Device.

If you change `sdkconfig.defaults`, delete `sdkconfig` first (it is only read when `sdkconfig` is created).

## What you should see

Monitor lines such as `EVT link up`, `TX {"type":"HELLO",...}`, a heartbeat each second, and for each job an `RX` of `JOB_REQ`, an `ACK`, and after the inspection time a `JOB_RESULT`. LED: off = not connected, steady = idle, fast blink = inspecting.

## Layout

```
CMakeLists.txt, sdkconfig.defaults
main/
  config.h      all tunable numbers and the Wi-Fi/gateway settings
  main.c        Wi-Fi start-up, LED, app_main (the only ESP-IDF file)
  link.c/.h     TCP connection, reconnect (lwIP on the board, POSIX on a PC)
  tcpframe.*    4-byte big-endian length framing
  jsonmini.*    flat JSON object reader
  station.*     all protocol rules: ACKs, retries, duplicates, timers, verdict
test/
  test_b.c, test.mk   PC unit tests:  make -f test.mk
  sim_b.c             the station on a PC:  make -f test.mk sim
  host_check_b.py     fake gateway, 24 PASS/FAIL checks:  python host_check_b.py
```

## Testing

- Without a board: `cd test && make -f test.mk` (needs gcc; use WSL or MSYS2 on Windows).
- With a board and no gateway yet: run `python test/host_check_b.py` on a PC on the same network, set `GATEWAY_IP` to that PC's address, allow Python through the firewall, flash, and watch the 24 checks.
- The Wi-Fi/lwIP code could only be syntax-checked off the board. The first flash is its first real test.

## Gateway must know

- Send `CONFIG` after every `HELLO` from a rebooted board. Before that, jobs are rejected with `not_configured`.
- Reject reasons used: `malformed`, `out_of_range`, `not_configured`, `busy`, `abandoned`, `id_conflict`.
