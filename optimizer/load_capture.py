from __future__ import annotations

import argparse
import json
import os
import platform
import select
import socket
import struct
import sys
import time


def mac(raw):
    return ":".join(f"{octet:02x}" for octet in raw)


class NativeLoadDecoder:
    def __init__(self):
        self.pending = {}
        self.completed = {}

    def feed(self, frame, timestamp):
        if len(frame) < 22 or frame[12:14] != b"\x89\x3a":
            return None
        cmdu = frame[14:]
        if cmdu[0] != 0 or cmdu[2:4] != b"\x80\x0c":
            return None
        source = mac(frame[6:12])
        identifier = struct.unpack_from("!H", cmdu, 4)[0]
        key = (source, identifier)
        self.pending = {key: value for key, value in self.pending.items() if 0 <= timestamp - value["started"] <= 5}
        self.completed = {key: value for key, value in self.completed.items() if 0 <= timestamp - value <= 5}
        if key in self.completed:
            return None
        if len(self.pending) >= 1024 and key not in self.pending:
            raise ValueError("native AP metrics fragment budget exceeded")
        assembly = self.pending.setdefault(key, {"started": timestamp, "parts": {}, "last": None, "invalid": False})
        fragment, payload = cmdu[6], cmdu[8:]
        if fragment in assembly["parts"] and assembly["parts"][fragment] != payload:
            assembly["invalid"] = True
        assembly["parts"][fragment] = payload
        if cmdu[7] & 0x80:
            if assembly["last"] is not None and assembly["last"] != fragment:
                assembly["invalid"] = True
            assembly["last"] = fragment
        if sum(map(len, assembly["parts"].values())) > 65535:
            raise ValueError("oversized native AP metrics message")
        last = assembly["last"]
        if assembly["invalid"] or last is None or set(assembly["parts"]) != set(range(last + 1)):
            return None
        payload = b"".join(assembly["parts"][index] for index in range(last + 1))
        del self.pending[key]
        position = 0
        loads, traffic = [], []
        while position + 3 <= len(payload):
            kind, length = struct.unpack_from("!BH", payload, position)
            data = payload[position + 3:position + 3 + length]
            position += 3 + length
            if len(data) != length:
                return None
            if kind == 0:
                if length:
                    return None
                if len(self.completed) >= 4096:
                    raise ValueError("native AP metrics deduplication budget exceeded")
                self.completed[key] = timestamp
                return {"source": source, "message_id": identifier, "received_at": timestamp,
                        "loads": loads, "traffic": traffic}
            if kind == 0x94:
                if length < 10:
                    return None
                loads.append({"bssid": mac(data[:6]), "utilization": data[6],
                              "station_count": struct.unpack_from("!H", data, 7)[0]})
            elif kind == 0xA2:
                if length != 34:
                    return None
                sent, received = struct.unpack_from("!II", data, 14)
                traffic.append({"sta_mac": mac(data[:6]), "packets_sent": sent, "packets_received": received})
        return None


class PrplBrokerDecoder:
    HEADER = struct.Struct("<III")
    MAGIC = 0xB8C16F47
    METADATA_SIZE = 40
    MAX_MESSAGE_SIZE = 8192

    def __init__(self):
        self.buffer = bytearray()
        self.decoder = NativeLoadDecoder()

    @classmethod
    def subscription(cls):
        payload = bytes((0, 1, 1, 0)) + struct.pack("<I", 0x800C0000) + bytes(252)
        return cls.HEADER.pack(cls.MAGIC, 3, len(payload)) + payload

    def feed(self, chunk, *, received_at, monotonic_ns):
        self.buffer.extend(chunk)
        if len(self.buffer) > 2 * self.MAX_MESSAGE_SIZE + self.HEADER.size:
            raise ValueError("native broker receive budget exceeded")
        reports = []
        while len(self.buffer) >= self.HEADER.size:
            magic, kind, size = self.HEADER.unpack_from(self.buffer)
            if (magic != self.MAGIC or kind != 1
                    or not self.METADATA_SIZE + 8 <= size <= self.MAX_MESSAGE_SIZE):
                raise ValueError("unsupported native broker envelope")
            end = self.HEADER.size + size
            if len(self.buffer) < end:
                break
            body = bytes(self.buffer[self.HEADER.size:end])
            del self.buffer[:end]
            ether_type, message_type = struct.unpack_from("<HH", body, 16)
            length = struct.unpack_from("<H", body, 28)[0]
            timestamp = struct.unpack_from("<Q", body, 32)[0]
            payload = body[self.METADATA_SIZE:]
            if (body[0] != 0 or body[21] not in (1, 2)
                    or ether_type != 0x893A or message_type != 0x800C
                    or length != len(payload) or payload[:4] != bytes.fromhex("0000800c")):
                raise ValueError("unsupported native broker CMDU metadata")
            age = received_at - timestamp
            if not 0 <= age <= 5:
                continue
            frame = body[4:10] + body[10:16] + bytes.fromhex("893a") + payload
            report = self.decoder.feed(frame, monotonic_ns / 1e9)
            if report is not None:
                report.update(kind="ap_metrics", received_at=timestamp,
                    monotonic_ns=monotonic_ns - round(age * 1e9),
                    transport="prpl-local-broker" if body[21] == 2 else "prpl-1905-broker")
                reports.append(report)
        return reports


def capture_broker(path, watch_stdin):
    if sys.byteorder != "little" or platform.machine() != "x86_64":
        raise ValueError("native broker ABI is qualified only for x86_64 little endian")
    decoder = PrplBrokerDecoder()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as receiver:
        receiver.settimeout(3)
        receiver.connect(path)
        receiver.sendall(decoder.subscription())
        print(json.dumps({"kind": "ready"}), flush=True)
        while True:
            inputs = [receiver] + ([sys.stdin] if watch_stdin else [])
            ready, _, _ = select.select(inputs, [], [], 1)
            if watch_stdin and sys.stdin in ready and not os.read(sys.stdin.fileno(), 1):
                return 0
            if receiver not in ready:
                continue
            chunk = receiver.recv(decoder.MAX_MESSAGE_SIZE)
            if not chunk:
                raise RuntimeError("native broker disconnected; observations unavailable")
            for report in decoder.feed(chunk, received_at=time.time(), monotonic_ns=time.monotonic_ns()):
                print(json.dumps(report, separators=(",", ":")), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Opt-in native 1905 AP metrics receiver; no RF oracle")
    parser.add_argument("--watch-stdin", action="store_true")
    parser.add_argument("--broker-socket")
    args = parser.parse_args(argv)
    if os.geteuid():
        parser.error("requires root in the controller network namespace")
    if args.broker_socket:
        return capture_broker(args.broker_socket, args.watch_stdin)
    decoder = NativeLoadDecoder()
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x893a)) as receiver:
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
        print(json.dumps({"kind": "ready"}), flush=True)
        checked_at = time.monotonic()
        while True:
            inputs = [receiver] + ([sys.stdin] if args.watch_stdin else [])
            ready, _, _ = select.select(inputs, [], [], 1)
            if args.watch_stdin and sys.stdin in ready and not os.read(sys.stdin.fileno(), 1):
                return 0
            now = time.monotonic()
            if now - checked_at >= 1:
                _packets, drops = struct.unpack("II", receiver.getsockopt(263, 6, 8))
                if drops:
                    raise RuntimeError("native load receiver dropped packets; refusing incomplete observations")
                checked_at = now
            if receiver not in ready:
                continue
            frame = receiver.recv(65536)
            timestamp = time.time()
            result = decoder.feed(frame, time.monotonic())
            if result is not None:
                result["received_at"] = timestamp
                result["kind"] = "ap_metrics"
                result["transport"] = "ieee1905-ethernet"
                result["monotonic_ns"] = time.monotonic_ns()
                print(json.dumps(result, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
