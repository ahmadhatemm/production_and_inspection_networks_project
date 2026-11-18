# ESP32 A: production station (MCT 431)

Firmware for the production station. It talks to the gateway over the USB cable (UART0, 115200 8N1)
using SLIP-framed ATR records with a CRC-16, exactly as in Interface Contract v1.
The full explanation of every file is in `ESP32_A_Explained.md`.

## Files

| File | Role |
| --- | --- |
| `main/main.c` | The only ESP-IDF code: UART setup, SLIP in and out, the main loop, the LED |
| `main/station.c/.h` | State machine and protocol rules (no ESP-IDF code, so it is tested on a PC) |
| `main/prodmsg.c/.h` | The 16-byte production message inside an ATR record |
| `main/atr.c/.h` | The ATR record: header, payload, CRC-16 |
| `main/slip.c/.h`, `main/crc16.c/.h` | SLIP framing and CRC-16/CCITT-FALSE, copied unchanged from Lab 3 |
| `main/config.h` | Every timing and limit number |
| `sdkconfig.defaults` | Turns the console and logs off, 1 ms RTOS tick |
| `test/` | PC tests (`test_station.c`, `test.mk`) |

## Build and flash

1. In VS Code use **File > Open Folder** on `ESP32_A` itself (not on the parent `Project` folder, or the build fails with "does not appear to contain CMakeLists.txt"), then select target `esp32` and the COM port.
2. **Delete `sdkconfig` if it exists** (or run "ESP-IDF: Full Clean"), because `sdkconfig.defaults`
   is only read when `sdkconfig` is created.
3. Build and flash. Do not use the serial monitor on this board: UART0 carries binary frames, and the
   monitor would also hold the port that the gateway needs.
4. The gateway must open the COM port without toggling DTR/RTS (see `open_port` in Lab 3's `lab3proto.py`),
   otherwise the board resets every time the port opens.

## Run the PC tests

```
cd test
make -f test.mk
```

Needs `gcc` and `make` (on Windows: WSL, or MSYS2 with gcc). It builds with warnings as errors and the
address and undefined-behaviour sanitizers, then runs about 900 checks against a fake gateway and clock.
