#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import resource
import sys
import time
import threading


PRPL_ASSOCIATION = "_ZN3son2db18dm_add_sta_elementERK8sMacAddrS3_RN8prplmesh10controller2db7StationE"
PROCESS_NAMES = {"rdk": "onewifi_em_ctrl", "prpl": "beerocks_controller"}
PROFILES = {
    "rdk": {
        "dbb74dab3009f4e01cf0c0c9ba519760f2350997f245fb6afc0defbafc5de20d": {
            "id": "rdk-legacy", "association_addresses": (0xdf948, 0xdf989),
            "metric_address": 0x89b90, "frame_offset": 0x78,
            "station_offset": 4, "bssid_offset": 10, "associated_offset": 22,
            "rcpi_offset": 0xd4,
        },
        "4372013d624b22127ea727e365fc4bde582ad34b56f055088cd5703a21301252": {
            "id": "rdk-0913", "association_addresses": (0xdf99a, 0xdf9db),
            "metric_address": 0x89b90, "frame_offset": 0x78,
            "station_offset": 4, "bssid_offset": 10, "associated_offset": 22,
            "rcpi_offset": 0xd4,
            "instructions": ((0xdf883, "8b4510894588"),
                             (0xdf988, "8b758883c004b9e804000089c783c604f3a5eb7a"),
                             (0xdf9d6, "e8e5cef2ff83c410"),
                             (0x89b8a, "8886d400000031c0")),
        },
    },
    "prpl": {
        "5f66442074ee3fd51b7a172dad4fa7996fcb94d19db1c7a01dc2ab5ffe74673d": {
            "id": "prpl-legacy", "association_symbol": PRPL_ASSOCIATION,
            "metric_address": 0x2737c1, "station_offset": 0,
            "bss_offset": 880, "bssid_offset": 0, "rcpi_offset": 280,
        },
        "737ab07f89fec1aabcc861bd90ddafda931be2e3e1a108cc7352ea2c3ecd994c": {
            "id": "prpl-0913", "association_symbol": PRPL_ASSOCIATION,
            "metric_address": 0x274651, "station_offset": 0,
            "bss_offset": 888, "bssid_offset": 0, "rcpi_offset": 288,
            "instructions": ((0x27463b, "488b442410"),
                             (0x27464b, "8890200100004c8ba3b0050000"),
                             (0x2949f0, "f30f1efa41574156415541544989d4554889cd"),
                             (0x294e64, "4489c05b5d415c415d415e415fc3")),
        },
    },
}


def program(stack, profile):
    common = "\n".join(f"#define {name.upper()} {value}" for name, value in profile.items()
                       if name.endswith("_offset")) + "\n" + """
#include <uapi/linux/ptrace.h>
struct event {
    u64 timestamp;
    u8 station[6];
    u8 bssid[6];
    u8 associated;
    u8 rcpi;
    u8 metric;
};
BPF_PERF_OUTPUT(events);
BPF_ARRAY(count, u64, 1);
static inline int publish(struct pt_regs *context, struct event *event) {
    event->timestamp = bpf_ktime_get_ns();
    int index = 0;
    u64 *total = count.lookup(&index);
    if (total) __sync_fetch_and_add(total, 1);
    events.perf_submit(context, event, sizeof(*event));
    return 0;
}
"""
    if stack == "rdk":
        return common + """
int commit(struct pt_regs *context) {
    u32 object = 0;
    struct event event = {};
    bpf_probe_read_user(&object, sizeof(object), (void *)((u64)(u32)context->bp - FRAME_OFFSET));
    bpf_probe_read_user(event.station, 6, (void *)((u64)object + STATION_OFFSET));
    bpf_probe_read_user(event.bssid, 6, (void *)((u64)object + BSSID_OFFSET));
    bpf_probe_read_user(&event.associated, 1, (void *)((u64)object + ASSOCIATED_OFFSET));
    bpf_probe_read_user(&event.rcpi, 1, (void *)((u64)object + RCPI_OFFSET));
    return publish(context, &event);
}
int metric(struct pt_regs *context) {
    u64 object = (u32)context->si;
    struct event event = {};
    bpf_probe_read_user(event.station, 6, (void *)(object + STATION_OFFSET));
    bpf_probe_read_user(event.bssid, 6, (void *)(object + BSSID_OFFSET));
    bpf_probe_read_user(&event.associated, 1, (void *)(object + ASSOCIATED_OFFSET));
    bpf_probe_read_user(&event.rcpi, 1, (void *)(object + RCPI_OFFSET));
    event.metric = 1;
    return publish(context, &event);
}
"""
    return common + """
BPF_HASH(pending, u64, struct event, 1024);
int enter(struct pt_regs *context) {
    u64 thread = bpf_get_current_pid_tgid();
    struct event event = {};
    bpf_probe_read_user(event.station, 6, (void *)(PT_REGS_PARM4(context) + STATION_OFFSET));
    bpf_probe_read_user(event.bssid, 6, (void *)PT_REGS_PARM3(context));
    event.associated = 1;
    event.rcpi = 255;
    pending.update(&thread, &event);
    return 0;
}
int commit(struct pt_regs *context) {
    u64 thread = bpf_get_current_pid_tgid();
    struct event *event = pending.lookup(&thread);
    if (event && PT_REGS_RC(context) == 1) publish(context, event);
    pending.delete(&thread);
    return 0;
}
int metric(struct pt_regs *context) {
    u64 object = context->ax;
    u64 bss = 0;
    struct event event = {};
    bpf_probe_read_user(event.station, 6, (void *)(object + STATION_OFFSET));
    bpf_probe_read_user(&bss, 8, (void *)(object + BSS_OFFSET));
    bpf_probe_read_user(event.bssid, 6, (void *)(bss + BSSID_OFFSET));
    bpf_probe_read_user(&event.rcpi, 1, (void *)(object + RCPI_OFFSET));
    event.associated = 1;
    event.metric = 1;
    return publish(context, &event);
}
"""


def qualify_binary(stack, data):
    digest = hashlib.sha256(data).hexdigest()
    profile = PROFILES[stack].get(digest)
    if profile is None:
        raise ValueError(f"unsupported native binary SHA256: {digest}; qualify a new probe profile")
    for address, encoded in profile.get("instructions", ()):
        expected = bytes.fromhex(encoded)
        if data[address:address + len(expected)] != expected:
            raise ValueError(f"probe profile {profile['id']} instruction mismatch at {address:#x}")
    return digest, profile


def controller(stack):
    name = PROCESS_NAMES[stack]
    matches = []
    for entry in Path("/proc").glob("[0-9]*/exe"):
        try:
            if entry.resolve().name == name:
                matches.append(entry)
        except OSError:
            continue
    if len(matches) != 1:
        raise ValueError(f"requires exactly one {name} process, found {len(matches)}")
    binary = matches[0]
    digest, _profile = qualify_binary(stack, binary.read_bytes())
    return str(binary), digest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded, opt-in native association commit probes")
    parser.add_argument("--stack", choices=tuple(PROFILES), required=True)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--watch-stdin", action="store_true")
    parser.add_argument("--metrics", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() or not 5 <= args.seconds <= 180:
        parser.error("requires root and 5..180 seconds")
    binary, digest = controller(args.stack)
    profile = PROFILES[args.stack][digest]
    from bcc import BPF
    records = 0
    lost = 0
    stopping = False
    output_lock = threading.Lock()

    def emit(value):
        with output_lock:
            print(json.dumps(value), flush=True)

    def stop(_signal=None, _frame=None):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    probe = BPF(text=program(args.stack, profile))
    try:
        def consume(_cpu, data, _size):
            nonlocal records
            event = probe["events"].event(data)
            records += 1
            emit({"kind": "metric" if event.metric else "commit", "monotonic_ns": event.timestamp,
                              "sta": ":".join(f"{octet:02x}" for octet in event.station),
                              "bssid": ":".join(f"{octet:02x}" for octet in event.bssid),
                              "associated": bool(event.associated),
                              "rcpi": event.rcpi if event.rcpi <= 220 else None})

        def dropped(amount):
            nonlocal lost
            lost += amount

        probe["events"].open_perf_buffer(consume, page_cnt=64, lost_cb=dropped)
        if args.stack == "rdk":
            for address in profile["association_addresses"]:
                probe.attach_uprobe(name=binary, addr=address, fn_name="commit")
        else:
            probe.attach_uprobe(name=binary, sym=profile["association_symbol"], fn_name="enter")
            probe.attach_uretprobe(name=binary, sym=profile["association_symbol"], fn_name="commit")
        metric_address = profile["metric_address"]
        if args.metrics:
            probe.attach_uprobe(name=binary, addr=metric_address, fn_name="metric")
        emit({"kind": "identity", "stack": args.stack, "sha256": digest,
                          "profile": profile,
                          "binary": binary, "clock": "guest-monotonic",
                          "boundary": "native-model-association-commit",
                          "metric_boundary": "native-model-rcpi-store" if args.metrics else None})
        emit({"kind": "ready"})
        ready_cpu = time.process_time()
        ready_time = time.monotonic()

        def control():
            for raw in sys.stdin:
                request = raw.strip()
                if request.startswith("clock:") and request[6:].isdigit():
                    emit({"kind": "clock", "request": request, "monotonic_ms": time.monotonic_ns() / 1000000})
                else:
                    stop()
                    return
            stop()

        if args.watch_stdin:
            threading.Thread(target=control, daemon=True).start()
        deadline = time.monotonic() + args.seconds
        while not stopping and time.monotonic() < deadline:
            probe.perf_buffer_poll(timeout=100)
            if records > 100000:
                raise ValueError("native trace exceeded event budget")
        if args.stack == "rdk":
            for address in profile["association_addresses"]:
                probe.detach_uprobe(name=binary, addr=address)
        else:
            probe.detach_uprobe(name=binary, sym=profile["association_symbol"])
            probe.detach_uretprobe(name=binary, sym=profile["association_symbol"])
        if args.metrics:
            probe.detach_uprobe(name=binary, addr=metric_address)
        probe.perf_buffer_poll(timeout=100)
        emitted = probe["count"][0].value
        if lost or emitted != records:
            raise RuntimeError(f"incomplete trace: received={records}, emitted={emitted}, lost={lost}")
        if controller(args.stack) != (binary, digest):
            raise RuntimeError("controller changed during trace")
        emit({"kind": "end", "records": records, "emitted": emitted, "lost": lost,
              "receiver_cpu_seconds": time.process_time() - ready_cpu,
              "receiver_elapsed_seconds": time.monotonic() - ready_time,
              "receiver_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
    finally:
        probe.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
