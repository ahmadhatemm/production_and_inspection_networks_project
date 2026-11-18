"""Every tunable number of the gateway, in one place.

Values marked "contract" come from Interface Contract v1.1 (Conventions, Field limits,
Reliability rules). If the team changes the contract, change them here. The tester can
also override any value from the command line without editing this file:

    python gateway.py --serial COM10 --set ack_timeout_ms=200 --set queue_capacity=3
"""
from dataclasses import dataclass
from typing import Optional

U32_MAX = 0xFFFFFFFF


@dataclass
class Config:
    # ---- Network (contract) ---------------------------------------------------
    http_host: str = "0.0.0.0"          # all interfaces, so the operator PC can reach the gateway
    http_port: int = 8080               # HTTP and WebSocket
    b_host: str = "0.0.0.0"
    b_port: int = 5000                  # ESP32 B connects here
    serial_baud: int = 115200           # ESP32 A, 8N1

    # ---- Reliability (contract) -----------------------------------------------
    ack_timeout_ms: int = 500           # wait this long for an acknowledgement
    max_tries: int = 4                  # first send + 3 retries, same bytes every time
    heartbeat_ms: int = 1000            # send a heartbeat to each device this often
    device_timeout_ms: int = 3000       # nothing valid from a device for this long = disconnected
    queue_capacity: int = 5             # inspection queue (parts waiting + the one being inspected)
    result_age_margin_ms: int = 3000    # a result is valid within inspect_time_ms + this, after B's ACK
    cmd_history: int = 1000             # remembered operator cmd_id values (duplicate suppression)

    # ---- Field limits (contract) ----------------------------------------------
    target_min: int = 1
    target_max: int = 10000
    time_min_ms: int = 10               # cycle_time_ms and inspect_time_ms
    time_max_ms: int = 60000
    n_min: int = 1
    k_min: int = 0

    # ---- Framing (contract) ---------------------------------------------------
    tcp_max_body: int = 1024            # largest TCP message body in bytes

    # ---- Not in the contract (implementation choices) --------------------------
    job_reject_retry_ms: int = 500      # wait this long before offering a rejected job again
    job_reject_limit: int = 4           # offers of one part that ESP32 B may reject before PROTOCOL_ERROR
    status_period_ms: int = 1000        # contract: a status message every 1000 ms
    poll_ms: int = 5                    # the main loop wakes this often
    default_cycle_time_ms: int = 1000   # shown in the status until the operator sets a value (Start always sets it)
    default_inspect_time_ms: int = 500  # sent to B with a config/inspection that comes before the first Start
    internal_cmd_id_base: int = 0xF0000000   # cmd_id values the gateway makes up itself (safe-state Stop, CONFIG after HELLO)
    job_id_start: Optional[int] = None  # None: a random start, so a restarted gateway cannot reuse an ID that ESP32 B still remembers
    completed_memory: int = 256         # remembered finished job_id and cmd_id values (to answer repeated results)
