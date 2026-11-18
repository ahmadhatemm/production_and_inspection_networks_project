"""Evidence log: every message and every event with a timestamp, a direction tag, the IDs and the stage.

Project deliverable ("serial logs, Wireshark captures"): this file is the gateway's side of that evidence.
One line per record, written to a file (always) and to the console (heartbeats only with -v):

    2026-11-18T12:00:03.412 +   3412 ms A-TX  seq=7 CMD cmd_id=17 cmd=start cycle_time_ms=1000 target_parts=20

Tags: A-TX / A-RX (serial to/from ESP32 A), B-TX / B-RX (TCP to/from ESP32 B), OP-RX / OP-TX (HTTP from/to the
operator), EVT (state changes, stages, faults, duplicates), SYS (start-up, connections, errors).
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from typing import Optional


class Evidence:
    def __init__(self, log_dir: Optional[str], verbose: bool = False, console: bool = True) -> None:
        self.t0 = time.monotonic()
        self.verbose = verbose
        self.console = console
        self.path: Optional[str] = None
        self._file = None
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            self.path = os.path.join(log_dir, datetime.now().strftime("gateway_%Y%m%d_%H%M%S.log"))
            self._file = open(self.path, "a", encoding="utf-8", buffering=1)

    def ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def log(self, tag: str, text: str) -> None:
        now = datetime.now()
        line = f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{now.microsecond // 1000:03d} +{self.ms():>8} ms {tag:<5} {text}"
        if self._file is not None:
            self._file.write(line + "\n")
        if self.console and (self.verbose or "HEARTBEAT" not in text):
            try:
                print(line, flush=True)
            except UnicodeEncodeError:      # a console that cannot show a character must not stop the gateway
                print(line.encode("ascii", "replace").decode("ascii"), flush=True)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
