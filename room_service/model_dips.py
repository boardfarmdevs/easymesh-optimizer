"""The controller model's BSS dips (RDK): a health sample whose BSS count is below the count it
expects names the BSSes its model had in the last complete sample and lacks now, and the dip,
once the count is back, is one record: what was missing and how long the dip lasted.

The health worker samples on its interval, and every DIP_POLL_SECONDS while a dip is open, so
a dip's start is known to its interval and its end to about a second: it lasted at least
min_seconds (its first to its last short sample) and at most max_seconds (the last complete
sample before it to the first one after). A BSS row is its device (the AL in the row's ID) and
BSSID, and its kind: an access point's BSS, or a link row (the device's record of its backhaul
link, its parent's BSSID or zero when wired). shared_with names the other devices that list
the same BSSID (a child's link row of its parent's BSS); the zero BSSID is none.

The model's rows are counted in two kinds (easymesh-medium: model_ap_bsses, model_link_rows).
The controller drops and adds link rows again on a topology rewrite, unmoved links included
(rdk-1010, 14 October), so the health's verdict lets the link rows differ from their count for
a settle window (the manifest's health.link_settle_seconds, LINK_SETTLE_SECONDS by default)
and holds everything else strictly; the raw verdict, without the window, is published beside
it (health_verdict)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

DIP_POLL_SECONDS = 1.0
LINK_SETTLE_SECONDS = 30.0
ZERO_BSSID = "00:00:00:00:00:00"


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BssDips:
    def __init__(self) -> None:
        self.reference: Counter | None = None
        self.details: dict[tuple[str, str], dict] = {}
        self.complete_at: float | None = None
        self.open: dict | None = None

    def observe(self, payload: dict, now: float) -> dict | None:
        """Takes the sample's model_bss_rows out of ``payload`` and puts model_bss_missing in its
        place; returns the dip this sample closes, else None. A stack without rows is left alone."""
        rows = payload.pop("model_bss_rows", None)
        if rows is None:
            return None
        count, expected = payload.get("model_bsses"), payload.get("expected_model_bsses")
        current = Counter((row["device"], row["bssid"]) for row in rows)
        if isinstance(count, int) and isinstance(expected, int) and count < expected:
            missing = sorted((self.reference - current) if self.reference is not None else ())
            if self.open is None:
                self.open = {"last_complete": self.complete_at, "first_short": now, "expected": expected,
                             "lowest": count, "missing": {}}
            dip = self.open
            dip["last_short"], dip["lowest"] = now, min(dip["lowest"], count)
            for key in missing:
                dip["missing"].setdefault(key, {"first": now})["last"] = now
            payload["model_bss_missing"] = [{**self.details[key], "since": _stamp(dip["missing"][key]["first"])}
                                            for key in missing]
            return None
        payload["model_bss_missing"] = []
        closed = self._close(now) if self.open is not None else None
        if count == expected:
            self.reference, self.complete_at = current, now
            devices: dict[str, set[str]] = {}
            for device, bssid in current:
                devices.setdefault(bssid, set()).add(device)
            self.details = {(row["device"], row["bssid"]): {
                "device": row["device"], "band": row["band"], "bssid": row["bssid"], "ssid": row["ssid"],
                **({"kind": row["kind"]} if "kind" in row else {}),
                "shared_with": [] if row["bssid"] == ZERO_BSSID else sorted(devices[row["bssid"]] - {row["device"]})}
                for row in rows}
        return closed

    def _close(self, now: float) -> dict:
        dip, self.open = self.open, None
        before = dip["last_complete"]
        missing = sorted(dip["missing"].items(), key=lambda item: (item[1]["first"], item[0]))
        return {"last_complete": _stamp(before) if before is not None else None,
                "first_short": _stamp(dip["first_short"]), "last_short": _stamp(dip["last_short"]),
                "first_complete": _stamp(now), "expected": dip["expected"], "lowest": dip["lowest"],
                "min_seconds": round(dip["last_short"] - dip["first_short"], 1),
                "max_seconds": round(now - before, 1) if before is not None else None,
                "missing": [{**self.details[key], "first": _stamp(seen["first"]), "last": _stamp(seen["last"])}
                            for key, seen in missing]}


class LinkSettle:
    """How long the model's link rows have differed from their expectation (short or over: a
    row added again before the old one went), on a monotonic clock."""

    def __init__(self) -> None:
        self.since: float | None = None

    def observe(self, payload: dict, now: float) -> float | None:
        """Seconds since the link rows first differed in this run of samples, 0.0 while they
        match; None for a health without the split (prplMesh)."""
        actual, expected = payload.get("model_link_rows"), payload.get("expected_model_link_rows")
        if actual is None or expected is None or actual == expected:
            self.since = None
            return None if actual is None or expected is None else 0.0
        if self.since is None:
            self.since = now
        return now - self.since


def health_verdict(payload: dict, clients: int, nodes: int, link_short: float | None,
                   settle: float) -> tuple[bool, bool]:
    """(raw, settled): raw holds every count to its expectation; settled lets the link rows
    differ for ``settle`` seconds, while the clients, the topology's nodes and the model's
    devices, radios, access point BSSes and associations match. Without the split (prplMesh,
    or no link rows counted) both are the one strict verdict on model_bsses, as before."""
    base = payload.get("api_active") == clients and payload.get("topology_nodes") == nodes
    split = "model_link_rows" in payload
    keys = ("devices", "radios", "ap_bsses" if split else "bsses", "associated")
    model = all(payload.get(f"model_{key}") == payload.get(f"expected_model_{key}") for key in keys)
    links = not split or payload.get("model_link_rows") == payload.get("expected_model_link_rows")
    raw = base and payload.get("complete_nodes") == nodes and model and links
    settling = split and not links and link_short is not None and link_short <= settle
    return raw, raw or (base and model and settling)
