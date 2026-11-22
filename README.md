# Production and Inspection Network

An embedded industrial production and quality-inspection system built using two ESP32 stations, a Python Gateway, and a web-based operator dashboard.

The system simulates a manufacturing line in which a production station generates parts and an inspection station evaluates each part using a configurable deterministic inspection rule. The Gateway coordinates communication, manages the production workflow, tracks inspection results, and provides an interface for operators.

## 1. Project Overview

The system consists of three main components:

- **ESP32 A – Production Station:** Generates virtual parts and sends production records to the Gateway through a USB serial connection.
- **ESP32 B – Inspection Station:** Receives inspection jobs from the Gateway, evaluates each part, and returns PASS/FAIL results.
- **Python Gateway:** Coordinates both stations, manages the batch state machine and inspection queue, handles commands, and exposes an HTTP/WebSocket interface.
- **Web Dashboard:** Allows operators to configure batches, control production, monitor station connectivity, and view inspection results.

## 2. System Architecture

```text
                  Operator PC / Browser
                           |
                     HTTP / WebSocket
                       Port 8080
                           |
                    +-------------+
                    |   Python    |
                    |   Gateway   |
                    +-------------+
                      /         \
             USB Serial         Wi-Fi / TCP
             115200 baud        Port 5000
                  /                 \
          +-------------+     +-------------+
          |   ESP32 A   |     |   ESP32 B   |
          | Production  |     | Inspection  |
          |  Station    |     |  Station    |
          +-------------+     +-------------+
```

### Communication Links

| Connection | Medium | Protocol |
|---|---|---|
| ESP32 A ↔ Gateway | USB serial | SLIP-framed ATR records |
| ESP32 B ↔ Gateway | Wi-Fi / TCP | Length-prefixed JSON |
| Gateway ↔ Operator | HTTP / WebSocket | Dashboard API and live status |

### Communication Configuration

- ESP32 A serial connection: 115200 baud, 8N1.
- ESP32 B TCP connection: Gateway port 5000.
- Dashboard and API: Gateway port 8080 by default.

## 3. Main Features

### Production and Batch Management

- Configure the target number of parts.
- Configure production cycle time and inspection time.
- Start, pause, resume, and stop production.
- Track produced, inspected, accepted, and rejected parts.
- Display the current batch state and progress.

### Inspection and Quality Control

The inspection station applies the deterministic rule:

```text
FAIL if (part_id + K) % N == 0
PASS otherwise
```

Where:
- `N` is the inspection interval.
- `K` is the configurable offset.
- `part_id` is the unique identifier assigned to a produced part.

The inspection parameters can be updated at runtime.

### Gateway Coordination

The Gateway provides:

- Batch state management.
- Inspection job queue with a capacity of five.
- Command acknowledgement and lifecycle tracking.
- Unique command and job identifiers.
- Duplicate-command protection.
- Bounded retries and timeout handling.
- Station heartbeat monitoring.
- Fault detection and recovery handling.
- Timestamped event and communication logging.

### Operator Dashboard

The web dashboard provides:

- Batch configuration and control.
- Live production and inspection status.
- Station connection and heartbeat indicators.
- Inspection queue monitoring.
- PASS/FAIL results for individual parts.
- Command lifecycle tracking.
- Fault notifications and fault clearing.
- Batch completion reports.

## 4. Repository Structure

```text
production_and_inspection_networks_project/
│
├── ESP32_A/
│   ├── main/
│   ├── test/
│   └── README.md
│
├── ESP32_B/
│   ├── main/
│   ├── test/
│   └── README.md
│
├── gateway/
│   ├── gw/
│   │   ├── config.py
│   │   ├── serialproto.py
│   │   ├── bproto.py
│   │   ├── engine.py
│   │   ├── links.py
│   │   ├── server.py
│   │   └── evidence.py
│   │
│   ├── tests/
│   ├── gateway.py
│   └── .venv/
│
└── src/
    └── static/
        └── index.html
```

*The structure above highlights the main project components; build files and additional modules may also be present.*

## 5. Hardware and Software Requirements

### Hardware

- ESP32 development board for the production station.
- ESP32 development board for the inspection station.
- USB cables for programming and connecting ESP32 A to the Gateway.
- Wi-Fi network shared by ESP32 B and the Gateway computer.
- Computer running the Python Gateway.
- Browser-enabled device for the operator dashboard.

### Software

- Python 3.10 or later.
- ESP-IDF development environment.
- Visual Studio Code with the ESP-IDF extension.
- MSYS2 UCRT64 with GCC and Make for ESP32 A host-side tests.
- Modern web browser.

## 6. Setup and Configuration

### 6.1 Configure ESP32 A

1. Open the `ESP32_A` project using the ESP-IDF extension in Visual Studio Code.
2. Select the appropriate ESP32 target and serial port.
3. Build and flash the firmware.
4. Connect ESP32 A to the Gateway computer through USB.

The Gateway communicates with ESP32 A through its USB serial interface.

### 6.2 Configure ESP32 B

1. Open the `ESP32_B` project in the ESP-IDF environment.
2. Configure the Wi-Fi credentials.
3. Set `GATEWAY_IP` in `ESP32_B/main/config.h` to the Gateway computer's Wi-Fi IPv4 address.
4. Build and flash the firmware.
5. Power ESP32 B and verify that it connects to the Gateway over TCP port 5000.

Both devices must be connected to the appropriate network and powered during physical integration testing.

### 6.3 Configure the Python Gateway

Open a terminal in the `gateway` directory.

Create and activate a virtual environment:

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the Python dependencies using the project's dependency file, if provided.

Connect ESP32 A and identify its serial port (for example, `COM3`).

Start the Gateway:

```powershell
python gateway.py --serial COM3 --ui-dir ..\src\static -v
```

Replace `COM3` with the actual serial port assigned to ESP32 A.

The Gateway starts the serial connection, TCP server, HTTP API, and WebSocket services.

### 6.4 Open the Dashboard

On the Gateway computer, open:

```text
http://localhost:8080/
```

To access the dashboard from another computer on the same network, use:

```text
http://GATEWAY_IP:8080/
```

Replace `GATEWAY_IP` with the Gateway computer's Wi-Fi IPv4 address.

Ensure that the computer's firewall permits the required network connections.

## 7. Operating the System

1. Flash and power both ESP32 stations.
2. Start the Python Gateway.
3. Open the operator dashboard.
4. Verify that both stations show as connected.
5. Configure the batch target, production cycle time, and inspection time.
6. Configure the inspection parameters `N` and `K`.
7. Start the batch.
8. Monitor production progress, inspection results, and queue occupancy.
9. Use Pause, Resume, or Stop when required.
10. Review the final batch report after completion.

A successfully completed batch should satisfy:

```text
Produced = Inspected = Target
```

And:

```text
Accepted + Rejected = Inspected
```

## 8. Testing and Verification

The project was verified through automated software tests, physical integration tests, GUI validation, and network-traffic inspection.

### Automated Tests

| Component | Verification result |
|---|---|
| ESP32 A host-side tests | 921 checks passed, 0 failures |
| Gateway codec and engine | 139 tests passed |
| Duplicate-command protection | 5 focused tests passed |
| Gateway host-check utility | Syntax check and self-test passed |

### Physical Integration Tests

The physical testing covered:

- Normal end-to-end batch execution.
- Pause and Resume.
- Stop command.
- Runtime inspection-rule reconfiguration.
- Queue capacity and backpressure observation.
- Inspection faster than production.
- Production station disconnection and recovery.
- Inspection station disconnection.
- Dashboard state-based command validation.

A normal 20-part batch using `N = 5` and `K = 2` completed with:

- 20 parts produced.
- 20 parts inspected.
- 16 accepted.
- 4 rejected.

### Network Verification

Wireshark was used to inspect Gateway-to-ESP32 B traffic over TCP port 5000.

The captured communication included:

- HEARTBEAT messages.
- CONFIG and CONFIG_DONE messages.
- JOB_REQ and JOB_RESULT exchanges.
- Acknowledgements.

Matching job identifiers and inspection verdicts were checked against the configured inspection rule.

## 9. Known Scope and Limitations

This project was developed in milestones.

The Milestone 1 implementation focuses on the production and inspection workflow, device communication, Gateway coordination, and operator dashboard.

Features assigned to later milestones, including formal communication benchmarking and MQTT authentication, should be considered separately from the current Milestone 1 implementation.

Some physical behaviours, such as the exact queue-full blocking and recovery sequence, were not conclusively established by the recorded physical evidence and should not be interpreted as fully verified.

## 10. Drive Link for Videos and Screenshots** 
https://drive.google.com/drive/folders/1U3tSpgfAAdWNYgtdZGjCkuK5Q9MUFsst?usp=sharing

**Project:** Production and Inspection Network  
**Course:** MCT 431 – Industrial Networks  
**Main Technologies:** ESP32, ESP-IDF, C, Python, TCP, Wi-Fi, HTTP, WebSocket, HTML, CSS, JavaScript
