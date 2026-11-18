#!/usr/bin/env python3
"""MCT 431 gateway: the PC between the operator dashboard and the two ESP32 boards.

    python gateway.py --serial COM10                       ESP32 A on COM10, ESP32 B connects to TCP 5000
    python gateway.py --serial COM10 --ui-dir ..\\ui\\ui    also serve the dashboard on http://localhost:8080/
    python gateway.py --help
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys

from gw.app import Gateway
from gw.config import Config


def apply_overrides(cfg: Config, pairs: list[str]) -> None:
    """--set key=value, for any Config field (for tests and experiments, e.g. --set queue_capacity=3)."""
    fields = {f.name: f for f in dataclasses.fields(Config)}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--set expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        if key not in fields:
            raise SystemExit(f"--set: unknown setting {key!r}. Known: {', '.join(sorted(fields))}")
        current = getattr(cfg, key)
        try:
            if isinstance(current, bool):
                new = value.lower() in ("1", "true", "yes")
            elif isinstance(current, int) or current is None:
                new = int(value, 0)
            else:
                new = type(current)(value)
        except ValueError:
            raise SystemExit(f"--set {key}: cannot read {value!r}")
        setattr(cfg, key, new)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="MCT 431 gateway (Milestone 1: production mode)")
    p.add_argument("--serial", metavar="PORT", help="ESP32 A's serial port, e.g. COM10 or /dev/ttyUSB0 "
                                                    "(or a pyserial URL such as socket://127.0.0.1:7000). "
                                                    "Without it the gateway runs with ESP32 A shown as not connected.")
    p.add_argument("--baud", type=int, default=115200, help="serial speed (default 115200, what ESP32 A uses)")
    p.add_argument("--http-host", default="0.0.0.0", help="address for the operator API (default 0.0.0.0)")
    p.add_argument("--http-port", type=int, default=8080, help="operator HTTP and WebSocket port (default 8080)")
    p.add_argument("--b-host", default="0.0.0.0", help="address ESP32 B connects to (default 0.0.0.0)")
    p.add_argument("--b-port", type=int, default=5000, help="TCP port for ESP32 B (default 5000)")
    p.add_argument("--ui-dir", metavar="DIR", help="folder with the dashboard (index.html); served at / on the HTTP port")
    p.add_argument("--log-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
                   help="where the evidence log files go (default: logs next to gateway.py)")
    p.add_argument("-v", "--verbose", action="store_true", help="also print heartbeats on the console")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override any setting in gw/config.py")
    args = p.parse_args(argv)

    cfg = Config()
    cfg.http_host, cfg.http_port = args.http_host, args.http_port
    cfg.b_host, cfg.b_port = args.b_host, args.b_port
    cfg.serial_baud = args.baud
    apply_overrides(cfg, args.set)
    if args.ui_dir and not os.path.isfile(os.path.join(args.ui_dir, "index.html")):
        raise SystemExit(f"--ui-dir: no index.html in {args.ui_dir}")

    gw = Gateway(cfg, args.serial, ui_dir=args.ui_dir, log_dir=args.log_dir, verbose=args.verbose)
    try:
        asyncio.run(gw.run_forever())
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    except OSError as e:
        print(f"cannot start: {e}\n(is another program using port {cfg.http_port} or {cfg.b_port}?)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
