from __future__ import annotations

import argparse
import json
import os
import select
import socket
import sys
import time


def main():
    parser = argparse.ArgumentParser(description="Bounded, owned supplicant control-event capture")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--watch-stdin", action="store_true")
    args = parser.parse_args()
    if not 0 < args.seconds <= 180:
        parser.error("capture must be bounded to 180 seconds")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
        control.bind("\0load-policy-" + str(os.getpid()))
        control.connect(args.socket)
        control.settimeout(3)
        for request in ("ATTACH", "LEVEL 0"):
            control.send(request.encode())
            while True:
                response = control.recv(65536).decode(errors="replace")
                if response.startswith("<"):
                    continue
                if response.strip() != "OK":
                    raise RuntimeError("supplicant rejected " + request + ": " + response)
                break
        print(json.dumps({"kind": "ready"}), flush=True)
        deadline = time.monotonic() + args.seconds
        recorded = 0
        try:
            while time.monotonic() < deadline:
                inputs = [control] + ([sys.stdin] if args.watch_stdin else [])
                ready, _, _ = select.select(inputs, [], [], min(1, max(0, deadline - time.monotonic())))
                if args.watch_stdin and sys.stdin in ready and not os.read(sys.stdin.fileno(), 1):
                    break
                if control in ready:
                    payload = control.recv(65536)
                    recorded += len(payload)
                    if recorded > 1048576:
                        raise RuntimeError("supplicant capture exceeded one MiB")
                    print(json.dumps({"received_at": time.time(), "monotonic_ns": time.monotonic_ns(),
                                      "event": payload.decode(errors="replace")}), flush=True)
        finally:
            control.send(b"DETACH")
        print(json.dumps({"kind": "end", "bytes": recorded}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
