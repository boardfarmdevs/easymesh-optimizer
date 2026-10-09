from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import itertools
import re
import subprocess
import time
from typing import Any


MINIMUM_TREE_GAIN_DB_PER_CHANGE = 2
# A backhaul link in reach: the native planner's bar for a parent (select_parent)
USABLE_SNR_DB = 5


def path_quality(role, parents, strengths, hop_penalty=3):
    """Conservative bidirectional path SNR, with a 3 dB extra-hop penalty."""
    visited = set()
    bottleneck = 60
    while role != "gateway":
        if role in visited or role not in parents:
            return None
        visited.add(role)
        parent = parents[role]
        strength = strengths.get((role, parent))
        reverse = strengths.get((parent, role))
        if strength is None or reverse is None:
            return None
        bottleneck = min(bottleneck, strength, reverse)
        role = parent
    return bottleneck - hop_penalty * max(0, len(visited) - 1)


def select_parent(parents, links):
    """Choose one loop-free improvement; never sacrifice a downstream node."""
    strengths = {(link["source_role"], link["destination_role"]): link["snr_db"]
                 for link in links if link["band"] == "5"}
    current = {role: path_quality(role, parents, strengths) for role in parents}
    if not current or any(value is None for value in current.values()):
        raise ValueError("Backhaul graph or bidirectional applied RF is incomplete; selection paused")
    if len(parents) > 4:
        raise ValueError("room backhaul planner is bounded to four extenders")
    roles = sorted(parents)
    options = [sorted(parent for parent in {"gateway", *parents} - {child}
                      if min(strengths.get((child, parent), -20), strengths.get((parent, child), -20)) >= 5)
               for child in roles]
    best, best_total = parents, sum(current.values())
    for candidates in itertools.product(*options):
        proposed = dict(zip(roles, candidates))
        scores = {role: path_quality(role, proposed, strengths) for role in roles}
        if any(value is None or value < current[role] for role, value in scores.items()):
            continue
        changes = sum(proposed[role] != parents[role] for role in roles)
        total = sum(scores.values())
        if total > best_total and total - sum(current.values()) >= MINIMUM_TREE_GAIN_DB_PER_CHANGE * changes:
            best, best_total = proposed, total
    choices = []
    for child in roles:
        parent = best[child]
        if parent == parents[child]:
            continue
        intermediate = {**parents, child: parent}
        scores = {role: path_quality(role, intermediate, strengths, hop_penalty=0) for role in roles}
        if any(value is None or value < path_quality(role, parents, strengths, hop_penalty=0)
               for role, value in scores.items()):
            continue
        score = path_quality(child, intermediate, strengths)
        choices.append({"child": child, "parent": parent, "previous_parent": parents[child],
                        "gain_db": score - current[child], "path_score_before": current[child],
                        "path_score_after": score, "fleet_gain": best_total - sum(current.values()),
                        "immediate_fleet_gain": sum(path_quality(role, intermediate, strengths) - current[role] for role in roles),
                        "planned_parents": best})
    return max(choices, key=lambda choice: (choice["immediate_fleet_gain"], choice["gain_db"]), default=None)


class RdkBackhaulAdapter:
    """Existing OneWifi lab BSSID control, not an EasyMesh backhaul-steering CMDU."""

    def __init__(self, plan):
        # Adapter-managed APs (OpenSync pods through EMOSA) and an extender on a
        # wired backhaul have no OneWifi backhaul in the room; this adapter
        # controls the lab's own five.
        self.containers = {role: binding["container"] for role, binding in plan["bindings"].items()
                           if binding["role_type"] == "fronthaul_ap" and not binding.get("adapter")
                           and binding.get("backhaul") != "wired"}
        expected = {"gateway": "bpibroadband", "extender_1": "bpiap", "extender_2": "bpiap-001",
                    "extender_3": "bpiap-002", "extender_4": "bpiap-003"}
        if self.containers != expected:
            raise ValueError("adaptive backhaul supports only the fixed five-device RDK OneWifi lab")
        for role in self.containers:
            if plan["bindings"][role].get("fronthaul_frequencies_mhz", {}).get("5") != 5180:
                raise ValueError("adaptive backhaul requires the lab's shared 5 GHz channel 36")
        self.bssids = {}

    def command(self, role, *arguments, timeout=4):
        result = subprocess.run(["lxc", "exec", self.containers[role], "--", *arguments],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"{role}: {result.stderr.strip() or result.stdout.strip()}")
        return result.stdout

    def observe(self):
        parents = {}
        observed = {}
        for role in self.containers:
            output = self.command(role, "sh", "-c", "iw dev wifi1.1 info; iw dev wifi1.3 link || true")
            address = re.search(r"\baddr ([0-9a-f:]{17})", output, re.I)
            link = re.search(r"Connected to ([0-9a-f:]{17})", output, re.I)
            if not address:
                raise RuntimeError(f"{role}: missing backhaul AP identity")
            self.bssids[role] = address.group(1).lower()
            if role != "gateway":
                if not link:
                    raise RuntimeError(f"{role}: backhaul is disconnected; automatic reparenting paused")
                observed[role] = link.group(1).lower()
        owners = {address: role for role, address in self.bssids.items()}
        for role, address in observed.items():
            if address not in owners:
                raise RuntimeError(f"{role}: unknown parent BSSID {address}")
            parents[role] = owners[address]
        return parents

    def prepare_radios(self):
        def prepare(role):
            actions = []

            def ap_ready():
                output = self.command(role, "iw", "dev", "wifi1.1", "info")
                return bool(re.search(r"^\s*ssid mesh_backhaul\s*$", output, re.M)
                            and re.search(r"^\s*channel 36 \(5180 MHz\)", output, re.M))

            if not ap_ready():
                enabled = self.command(role, "rbuscli", "getv", "Device.WiFi.AccessPoint.14.Enable")
                if not re.search(r"Value\s*:\s*true\b", enabled):
                    raise RuntimeError(f"{role}: backhaul AP is not administratively enabled")
                self.command(role, "rbuscli", "setvalues", "Device.WiFi.AccessPoint.14.ForceApply",
                             "boolean", "true", timeout=6)
                for attempt in range(8):
                    if ap_ready():
                        break
                    time.sleep(0.5)
                else:
                    raise RuntimeError(f"{role}: enabled backhaul AP did not start on channel 36")
                actions.append("apply_enabled_backhaul_ap")
            if role != "gateway":
                flags = self.command(role, "cat", "/sys/class/net/wifi1.3/flags")
                if not int(flags.strip(), 0) & 1:
                    self.command(role, "ip", "link", "set", "wifi1.3", "up")
                    flags = self.command(role, "cat", "/sys/class/net/wifi1.3/flags")
                    if not int(flags.strip(), 0) & 1:
                        raise RuntimeError(f"{role}: backhaul STA interface remains administratively down")
                    actions.append("bring_backhaul_sta_up")
            return role, {"ap_ready": True, "actions": actions}

        with ThreadPoolExecutor(max_workers=len(self.containers)) as executor:
            radios = dict(executor.map(prepare, self.containers))
        return {"radios": radios, "parent_selection": False}

    def set_bssid(self, child, address):
        if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", address):
            raise ValueError("invalid backhaul BSSID")
        output = self.command(child, "rbuscli", "setvalues", "Device.WiFi.STA.2.Bssid",
                              "bytes", address.replace(":", ""), timeout=6)
        if not re.search(r"set(values)? succeeded|set successful", output, re.I):
            raise RuntimeError(f"{child}: OneWifi rejected parent selection: {output.strip()}")

    def verify(self, child, parent):
        deadline = time.monotonic() + 30
        stable = 0
        last_link = "not sampled"
        last_error = "association not ready"
        while time.monotonic() < deadline:
            try:
                output = self.command(child, "iw", "dev", "wifi1.3", "link")
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                stable = 0
                last_error = str(error)
                time.sleep(0.5)
                continue
            last_link = output.splitlines()[0] if output else "no link"
            if f"Connected to {self.bssids[parent]}" in output:
                try:
                    self.command(child, "ping", "-I", "brlan0", "-q", "-c", "1", "-W", "1", "10.0.0.1")
                    stable += 1
                    if stable >= 2:
                        return
                except (RuntimeError, subprocess.TimeoutExpired) as error:
                    stable = 0
                    last_error = str(error)
            else:
                stable = 0
            time.sleep(0.5)
        raise RuntimeError(f"{child}: backhaul not verified after 30 s: {last_link}; {last_error}")

    def switch(self, choice, expected_parents):
        if self.observe() != expected_parents:
            raise RuntimeError("actual parent graph changed before the backhaul action")
        child, parent, previous = choice["child"], choice["parent"], choice["previous_parent"]
        output = self.command(parent, "iw", "dev", "wifi1.1", "info")
        if not re.search(r"\bssid mesh_backhaul\b", output):
            self.command(parent, "rbuscli", "setvalues", "Device.WiFi.AccessPoint.14.ForceApply",
                         "boolean", "true", timeout=6)
            ready = False
            for attempt in range(8):
                if re.search(r"\bssid mesh_backhaul\b", self.command(parent, "iw", "dev", "wifi1.1", "info")):
                    ready = True
                    break
                time.sleep(0.5)
            if not ready:
                raise RuntimeError(f"{parent}: backhaul AP did not activate")
        try:
            self.set_bssid(child, self.bssids[parent])
            self.verify(child, parent)
        except Exception as error:
            try:
                self.set_bssid(child, self.bssids[previous])
                self.verify(child, previous)
            except Exception as rollback_error:
                raise RuntimeError(f"{error}; ROLLBACK FAILED: {rollback_error}") from error
            raise RuntimeError(f"{error}; previous parent restored") from error


class BackhaulManager:
    def __init__(self, adapter, transaction, store, clock=time.monotonic):
        self.adapter = adapter
        self.transaction = transaction
        self.store = store
        self.clock = clock
        self.next_check = 0
        self.attempts = 0
        self.failures = 0
        self.disabled = False
        self.status: dict[str, Any] = {"status": "waiting", "reason": "Waiting for settled room RF"}

    def snapshot(self, room=None):
        status = copy.deepcopy(self.status)
        if room and status.get("status") == "stable" and status.get("backhaul_epoch", status.get("environment_epoch")) != room.get("backhaul_epoch", room.get("environment_epoch")):
            status.update(status="waiting", reason="Room RF changed; waiting for a settled backhaul evaluation")
        return {"authority": "RDK OneWifi lab backhaul control", "band": "5",
                "minimum_tree_gain_db_per_change": MINIMUM_TREE_GAIN_DB_PER_CHANGE,
                "attempts": self.attempts, "failures": self.failures, **status}

    def reconcile(self, room, world_time):
        epoch = room.get("backhaul_epoch", room["environment_epoch"])
        unchanged = self.status.get("backhaul_epoch", self.status.get("environment_epoch")) == epoch
        if self.disabled or self.clock() < self.next_check and (
            unchanged or self.status["status"] in {"retry", "paused", "verified", "switching"}
        ):
            return False
        stable_for = room.get("backhaul_stable_for_seconds", room.get("stable_for_seconds"))
        if stable_for is None or stable_for < 2:
            self.status = {"status": "waiting", "reason": "Waiting for 2 seconds of settled backhaul RF; client measurements continue"}
            return False
        self.next_check = self.clock() + 30
        attempted = False
        try:
            parents = self.adapter.observe()
            choice = select_parent(parents, room.get("backhaul_links", []))
            self.status = {"status": "stable", "reason": "No loop-free tree improvement of at least 2 dB per changed parent",
                           "parents": parents, "environment_epoch": room["environment_epoch"], "backhaul_epoch": epoch}
            if choice is None:
                return False
            if self.attempts >= 100:
                self.disabled = True
                self.status = {"status": "paused", "reason": "Backhaul action budget exhausted; restart required"}
                return False
            self.status = {"status": "switching", "reason": "Verifying association and gateway traffic", **choice}
            self.store.emit("backhaul.action.started", world_time, self.snapshot(), producer="backhaul")
            self.attempts += 1
            attempted = True
            self.transaction(lambda: self.adapter.switch(choice, parents),
                             expected_epoch=room["environment_epoch"])
            self.failures = 0
            self.next_check = self.clock()
            self.status = {"status": "verified", "reason": "Actual parent and gateway traffic verified; controller topology may lag",
                           **choice}
            self.store.emit("backhaul.action.verified", world_time, self.snapshot(), producer="backhaul")
        except Exception as error:
            self.failures += 1
            self.disabled = self.failures >= 3 or "ROLLBACK FAILED" in str(error)
            self.status = {"status": "paused" if self.disabled else "retry",
                           "reason": str(error), "retry_seconds": None if self.disabled else 60}
            self.next_check = self.clock() + 60
            self.store.emit("backhaul.action.failed", world_time, self.snapshot(), producer="backhaul")
        return attempted

    def blocks_client_measurement(self, room):
        return self.snapshot(room)["status"] == "switching"


def _run(arguments, timeout=10):
    result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{' '.join(arguments[:4])}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def channel_of(frequency_mhz):
    """The channel number of a 2.4, 5 or 6 GHz center frequency."""
    frequency = int(frequency_mhz)
    if frequency == 2484:
        return 14
    if frequency < 3000:
        return (frequency - 2407) // 5
    if frequency >= 5955:
        return (frequency - 5950) // 5
    return (frequency - 5000) // 5


class PodBackhaul:
    """The OpenSync pods' backhaul parents where the room models the backhaul.

    A pod (EMOSA) keeps its backhaul station on the gateway's 5 GHz backhaul BSS,
    the fleet's configured upstream, at a fixed 50 dB, and hears every other native
    AP at the medium's default SNR. Where the room models the backhaul (geometry),
    the pod's station gets the room's links to every native AP (pod_station_keys)
    and the gateway may be out of its reach. So before such a room's RF is applied,
    each pod is moved to its parent in the room's first generation, through the
    controller's Backhaul Steering (SteerWiFiBackhaul(), which EMOSA carries out):
    - a native AP when one is in reach (SNR USABLE_SNR_DB or more on 5 GHz, the
      native backhaul planner's bar): the strongest. The pod's 5 GHz station, on a
      radio of its own, is the higher-capacity link and one hop;
    - else another pod, on 2.4 GHz: the child's 2.4 GHz station on that pod's
      backhaul BSS (emosa-lab spec 8.3, 8.5), the pod with the best path to the
      gateway (its bottleneck, less 3 dB per extra hop, as the native planner scores),
      never one whose own path runs through the child.
    Elsewhere a pod stays where it is: every native AP is within its reach there. A
    wired extender without its HAL guard is never a parent.
    """

    def __init__(self, plan, *, controller="bpibroadband", timeout=90, run=_run, sleep=time.sleep,
                 clock=time.monotonic):
        bindings = plan["bindings"]
        self.pods = {role: binding for role, binding in bindings.items()
                     if binding.get("adapter")
                     and (binding.get("backhaul_station") or binding.get("backhaul_stations"))}
        self.parents = {role: binding["container"] for role, binding in bindings.items()
                        if binding.get("role_type") == "fronthaul_ap" and not binding.get("adapter")
                        and (binding.get("backhaul") != "wired" or binding.get("wired_guard") == "hal")}
        self.bindings = bindings
        self.controller, self.timeout = controller, timeout
        self.run, self.sleep, self.clock = run, sleep, clock
        self.bssids = {}

    def targets(self, world):
        """Each pod's parent in the room at its start: a native AP in reach, else a pod.

        A link's strength is the weaker of its two directions, as the native planner
        takes it. A pod's path is scored as path_quality scores one: its bottleneck,
        less 3 dB per hop past the first (a native parent's own path is the native
        planner's). A pod parent's backhaul BSS is on its 2.4 GHz radio, which the
        child's 2.4 GHz station shares with the child's fronthaul: so only a pod on
        the child's own 2.4 GHz channel can be its parent.
        """
        directed = {}
        for link in world["generations"][0]["links"]:
            if link.get("link_class") == "backhaul":
                for band, value in (link.get("snr_db_by_band") or {}).items():
                    if value is not None:
                        directed[(link["source_role"], link["destination_role"], band)] = value

        def strength(child, parent, band):
            values = [directed.get((child, parent, band)), directed.get((parent, child, band))]
            return None if None in values else min(values)

        # on a tie, the first role by name, as the rooms have always placed them
        targets, paths = {}, {}
        for pod in sorted(self.pods):
            natives = [(value, peer) for peer in sorted(self.parents)
                       if (value := strength(pod, peer, "5")) is not None]
            value, peer = max(natives, key=lambda item: item[0], default=(None, None))
            if value is not None and value >= USABLE_SNR_DB:
                targets[pod], paths[pod] = peer, (value, 1)
        # pods out of every native AP's reach: under the placed pod that gives the best
        # path, the best path placed first (as Dijkstra's widest path), so a parent pod is
        # always placed before its children and none lands under its own child
        while True:
            options = []
            for pod in sorted(set(self.pods) - set(targets)):
                if self._station_of(pod, "2.4") is None:
                    continue
                for parent in sorted(set(targets) & set(self.pods)):
                    value = strength(pod, parent, "2.4")
                    if (value is None or value < USABLE_SNR_DB
                            or self._frequency(pod, "2.4") != self._frequency(parent, "2.4")):
                        continue
                    bottleneck, hops = min(value, paths[parent][0]), paths[parent][1] + 1
                    options.append((bottleneck - 3 * (hops - 1), pod, parent, (bottleneck, hops)))
            if not options:
                break
            _, pod, targets[pod], paths[pod] = max(options, key=lambda option: option[0])
        return {pod: targets[pod] for pod in sorted(targets)}

    def _frequency(self, role, band):
        return (self.bindings[role].get("fronthaul_frequencies_mhz") or {}).get(band)

    def _station_of(self, pod, band):
        """The pod's backhaul station on ``band`` ({interface, station_mac, ...}), or None."""
        binding = self.pods[pod]
        for station in binding.get("backhaul_stations") or []:
            if station.get("band") == band:
                return station
        legacy = binding.get("backhaul_station")
        return legacy if legacy and band == "5" else None

    def _stations(self, pod):
        binding = self.pods[pod]
        stations = list(binding.get("backhaul_stations") or [])
        if binding.get("backhaul_station") and not any(
                s.get("interface") == binding["backhaul_station"].get("interface") for s in stations):
            stations.append(binding["backhaul_station"])
        return stations

    def band_of(self, target):
        """The band a parent's backhaul BSS is on: a pod's on 2.4 GHz, a native AP's on 5."""
        return "2.4" if target in self.pods else "5"

    def channel(self, target):
        """The channel of a parent's backhaul BSS: its fronthaul's on that band (the lab's
        native APs share 5 GHz channel 36)."""
        frequency = self._frequency(target, self.band_of(target))
        return channel_of(frequency) if frequency else 36

    def bssid(self, role):
        """A parent's backhaul BSS: a native AP's 5 GHz one, a pod's 2.4 GHz one."""
        if role not in self.bssids:
            container = self.bindings[role]["container"]
            interface = "b-ap-24" if role in self.pods else "wifi1.1"
            self.bssids[role] = self.run(["lxc", "exec", container, "--", "cat",
                                          f"/sys/class/net/{interface}/address"]).strip().lower()
        return self.bssids[role]

    def parent(self, pod):
        """The BSSID the pod's backhaul is on: whichever of its stations is connected."""
        container = self.pods[pod]["container"]
        for station in self._stations(pod):
            try:
                output = self.run(["lxc", "exec", container, "--", "iw", "dev", station["interface"], "link"])
            except RuntimeError:
                continue  # no such station on this pod's image
            match = re.search(r"Connected to ([0-9a-f:]{17})", output, re.I)
            if match:
                return match.group(1).lower()
        return None

    def chained(self):
        """The pods now under another pod, each with that pod."""
        bssids = {}
        for pod in self.pods:
            try:
                bssids[self.bssid(pod)] = pod
            except RuntimeError:
                continue  # no 2.4 GHz backhaul BSS on this pod's image
        return {pod: bssids[parent] for pod in sorted(self.pods) if (parent := self.parent(pod)) in bssids}

    def device(self, pod):
        """The controller's DataElements index of the pod's agent.

        The controller keeps the pod's backhaul station as a backhaul-STA row of the
        agent's device (unified-wifi-mesh 0216, 0248): its ID names the agent's AL MAC.
        The row is the station's in use, either of the pod's.
        """
        macs = sorted({str(s["station_mac"]).lower() for s in self._stations(pod) if s.get("station_mac")})
        where = " or ".join(f"ID like '%{mac}%'" for mac in macs)
        rows = self.run(["lxc", "exec", self.controller, "--", "mysql", "-N", "-ubpi", "-proot",
                         "OneWifiMesh", "-e", f"select ID from BSSList where {where}"])
        agents = {row.split("@")[1].lower() for row in rows.split() if row.count("@") >= 2}
        if len(agents) != 1:
            raise RuntimeError(f"{pod}: the controller has {len(agents)} agents with stations {macs}")
        agent = agents.pop()
        count = self._value("Device.WiFi.DataElements.Network.DeviceNumberOfEntries")
        for index in range(1, int(count or 0) + 1):
            if (self._value(f"Device.WiFi.DataElements.Network.Device.{index}.ID") or "").lower() == agent:
                return index
        raise RuntimeError(f"{pod}: the controller's data model has no device {agent}")

    def _value(self, name):
        output = self.run(["lxc", "exec", self.controller, "--", "rbuscli", "get", name]).replace("\r", "")
        match = re.search(r"Value\s*:\s*(\S+)", output)
        return match.group(1) if match else None

    def move(self, pod, target):
        """Move ``pod`` to ``target``'s backhaul BSS and wait until its station is there."""
        started = self.clock()
        bssid = self.bssid(target)
        if self.parent(pod) == bssid:
            return {"pod": pod, "parent": target, "bssid": bssid, "moved": False}
        index = self.device(pod)
        self.run(["lxc", "exec", self.controller, "--", "rbuscli", "method_values",
                  f"Device.WiFi.DataElements.Network.Device.{index}.MultiAPDevice.Backhaul.SteerWiFiBackhaul()",
                  "TargetBSS", "string", bssid, "Channel", "int32", str(self.channel(target)),
                  "TimeOut", "int32", "30"], timeout=20)
        while self.clock() - started < self.timeout:
            if self.parent(pod) == bssid:
                return {"pod": pod, "parent": target, "bssid": bssid, "moved": True,
                        "seconds": round(self.clock() - started, 1)}
            self.sleep(2)
        raise RuntimeError(f"{pod} did not move to {target} ({bssid}) within {self.timeout} s")

    def arrange(self, world, models_backhaul=True):
        """Every pod on its parent in ``world``: the pods under native APs first, at once,
        then each pod under a pod once its parent is placed.

        In a room that models the backhaul, the room's parents (targets). Elsewhere a pod
        under another pod goes back to the gateway, its configured upstream: such a room
        does not model the pods' link, and a pod's 5 GHz station keeps the lab's own there
        (a fixed link to the gateway). A pod under a native AP stays where it is.
        """
        targets = self.targets(world) if models_backhaul else {pod: "gateway" for pod in self.chained()}
        if not targets:
            return {"pods": []}
        moved, placed = [], set()
        while len(placed) < len(targets):
            ready = [(pod, parent) for pod, parent in targets.items()
                     if pod not in placed and (parent not in self.pods or parent in placed)]
            if not ready:
                raise RuntimeError(f"pod parents in a loop: {targets}")
            with ThreadPoolExecutor(max_workers=len(ready)) as executor:
                moved += list(executor.map(lambda item: self.move(*item), ready))
            placed |= {pod for pod, _ in ready}
        return {"pods": moved}
