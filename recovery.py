from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any

from wmdcfg.actuator import ActuatorError, ControlClient
from .client_wifi import reconnect_client


SCHEMA = "easymesh.room-demo.recovery.v1"
INCOMPLETE_STATES = {"prepared", "applying", "active", "restoring", "failed"}


def _digest(document: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(document)
    unsigned.pop("checksum_sha256", None)
    material = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(material).hexdigest()


def load_recovery(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != SCHEMA:
        raise ActuatorError(f"{path}: unsupported recovery schema")
    if document.get("checksum_sha256") != _digest(document):
        raise ActuatorError(f"{path}: recovery checksum mismatch")
    if not isinstance(document.get("baseline"), list):
        raise ActuatorError(f"{path}: recovery baseline is invalid")
    return document


def inventory_identity(inventory: dict[str, Any]) -> str:
    radios = []
    for radio in inventory["radios"]:
        entry = {key: radio.get(key) for key in (
            "container", "kind", "permanent_mac", "tx_mac", "station_mac"
        )}
        if not entry["container"] or not entry["kind"] or not entry["tx_mac"]:
            raise ActuatorError("recovery inventory has an incomplete radio identity")
        entry["interfaces"] = sorted([
            {key: interface.get(key) for key in ("name", "mac", "phy", "type")}
            for interface in radio.get("interfaces", [])
        ], key=lambda interface: (interface["name"] or "", interface["mac"] or ""))
        entry["band_radios"] = {
            band: {key: value.get(key) for key in (
                "phy", "permanent_mac", "tx_mac", "frequency_mhz"
            )}
            for band, value in radio.get("band_radios", {}).items()
        }
        radios.append(entry)
    if not radios or len({radio["container"] for radio in radios}) != len(radios):
        raise ActuatorError("recovery inventory has missing or duplicate containers")
    return _digest({"schema": inventory.get("schema"), "backend": inventory.get("backend"),
                    "radios": sorted(radios, key=lambda radio: radio["container"])})


class RecoveryJournal:
    """Checksummed crash-recovery state written before every RF mutation."""

    def __init__(self, path: Path, run_id: str, inventory_sha256: str,
                 *, inventory_identity_sha256: str | None = None) -> None:
        self.path = path
        self.run_id = run_id
        self.inventory_sha256 = inventory_sha256
        self.inventory_identity_sha256 = inventory_identity_sha256
        self._lock = threading.Lock()
        self._document: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._document is None:
                return {"path": str(self.path), "state": "not-prepared"}
            return {
                "path": str(self.path),
                "state": self._document["state"],
                "checksum_sha256": self._document.get("checksum_sha256"),
                "last_committed_generation": self._document.get(
                    "last_committed_generation"
                ),
                "pending_generation": self._document.get("pending_generation"),
            }

    def _write(self) -> None:
        assert self._document is not None
        self._document["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self._document["checksum_sha256"] = _digest(self._document)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(
            json.dumps(self._document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

    def prepare(
        self,
        instance_id: str,
        generation: int,
        baseline: dict[tuple[str, str, int], tuple[int, bool]],
    ) -> None:
        with self._lock:
            if self.path.exists():
                prior = load_recovery(self.path)
                if prior.get("state") in INCOMPLETE_STATES or prior.get("paused_clients"):
                    raise ActuatorError(
                        f"{self.path}: incomplete room run {prior.get('run_id')} "
                        f"is {prior.get('state')}; run room-demo recover first"
                    )
            rows = [
                {
                    "source": source,
                    "destination": destination,
                    "frequency_mhz": frequency,
                    "value": value,
                    "override": overridden,
                }
                for (source, destination, frequency), (value, overridden)
                in sorted(baseline.items())
            ]
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            self._document = {
                "schema": SCHEMA,
                "run_id": self.run_id,
                "state": "prepared",
                "created_at": now,
                "updated_at": now,
                "medium_instance_id": instance_id,
                "inventory_sha256": self.inventory_sha256,
                "inventory_identity_sha256": self.inventory_identity_sha256,
                "captured_generation": generation,
                "last_committed_generation": generation,
                "pending_generation": None,
                "baseline": rows,
                "error": None,
            }
            self._write()

    def pause_client(self, container: str) -> None:
        with self._lock:
            if self._document is None:
                raise ActuatorError("client pause requires a prepared RF recovery journal")
            clients = self._document.setdefault("paused_clients", [])
            if container not in clients:
                clients.append(container)
            self._write()

    def resume_client(self, container: str) -> None:
        with self._lock:
            if self._document is None:
                return
            clients = self._document.get("paused_clients", [])
            if container in clients:
                clients.remove(container)
            self._write()

    def resume_clients(self) -> None:
        with self._lock:
            clients = list((self._document or {}).get("paused_clients", []))
        for container in clients:
            reconnect_client(container)
            self.resume_client(container)

    def paused_clients(self) -> list[str]:
        with self._lock:
            return list((self._document or {}).get("paused_clients", []))

    def before_apply(self, current_generation: int, pending_generation: int) -> None:
        with self._lock:
            if self._document is None:
                return
            self._document.update({
                "state": "applying",
                "last_committed_generation": current_generation,
                "pending_generation": pending_generation,
                "error": None,
            })
            self._write()

    def committed(self, generation: int) -> None:
        with self._lock:
            if self._document is None:
                return
            self._document.update({
                "state": "active",
                "last_committed_generation": generation,
                "pending_generation": None,
                "error": None,
            })
            self._write()

    def restoring(self) -> None:
        with self._lock:
            if self._document is None:
                return
            self._document["state"] = "restoring"
            self._write()

    def completed(self, generation: int, verified: bool) -> None:
        with self._lock:
            if self._document is None:
                return
            self._document.update({
                "state": "restored" if verified else "failed",
                "last_committed_generation": generation,
                "pending_generation": None,
                "error": None if verified else "baseline readback did not verify",
            })
            self._write()

    def failed(self, error: BaseException | str, *, contaminated: bool = False) -> None:
        with self._lock:
            if self._document is None:
                return
            self._document.update({
                "state": "contaminated" if contaminated else "failed",
                "error": str(error),
            })
            self._write()


def recover_medium(
    path: Path,
    socket_path: str,
    *,
    client_factory=ControlClient,
) -> dict[str, Any]:
    """Restore an interrupted session only against its exact medium instance."""
    document = load_recovery(path)
    if document.get("state") == "restored":
        if document.get("paused_clients"):
            client = client_factory(socket_path)
            try:
                status = client.connect()
                if status.instance_id != document.get("medium_instance_id"):
                    raise ActuatorError("recovery medium instance mismatch before client reconnect")
                if status.generation != document.get("last_committed_generation"):
                    raise ActuatorError("recovery generation mismatch before client reconnect")
            finally:
                client.close()
        _resume_recovered_clients(path, document)
        return {
            "status": "already-restored",
            "run_id": document["run_id"],
            "generation": document["last_committed_generation"],
            "restored_links": len(document["baseline"]),
        }
    if document.get("state") == "contaminated":
        raise ActuatorError(
            f"{path}: ownership was contaminated; automatic recovery is unsafe"
        )
    client = client_factory(socket_path)
    try:
        status = client.connect()
        if status.instance_id != document.get("medium_instance_id"):
            raise ActuatorError(
                "recovery medium instance mismatch: "
                f"expected {document.get('medium_instance_id')}, "
                f"observed {status.instance_id}"
            )
        permitted = {
            int(value)
            for value in (
                document.get("last_committed_generation"),
                document.get("pending_generation"),
            )
            if value is not None
        }
        if status.generation not in permitted:
            raise ActuatorError(
                "recovery generation mismatch: "
                f"record permits {sorted(permitted)}, observed {status.generation}"
            )
        updates = [
            {
                "source": row["source"],
                "destination": row["destination"],
                "frequency_mhz": int(row["frequency_mhz"]),
                "value": int(row["value"]) if row["override"] else 0,
                "override": bool(row["override"]),
            }
            for row in document["baseline"]
        ]
        if len(updates) > status.max_updates:
            raise ActuatorError(
                f"recovery requires {len(updates)} updates; daemon limit is "
                f"{status.max_updates}"
            )
        generation = status.generation + 1
        applied = client.apply_frequency(generation, updates)
        if len(applied) != len(updates):
            raise ActuatorError("recovery apply count mismatch")
        for row in document["baseline"]:
            _, value, overridden = client.get_frequency_link(
                row["source"], row["destination"], int(row["frequency_mhz"])
            )
            if value != int(row["value"]) or overridden != bool(row["override"]):
                raise ActuatorError(
                    "recovery baseline readback mismatch for "
                    f"{row['source']} -> {row['destination']} "
                    f"at {row['frequency_mhz']} MHz"
                )
    finally:
        client.close()
    document.update({
        "state": "restored",
        "last_committed_generation": generation,
        "pending_generation": None,
        "error": None,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    _save_recovered(path, document)
    _resume_recovered_clients(path, document)
    return {
        "status": "restored",
        "run_id": document["run_id"],
        "generation": generation,
        "restored_links": len(updates),
    }


def _save_recovered(path: Path, document: dict[str, Any]) -> None:
    document["checksum_sha256"] = _digest(document)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _resume_recovered_clients(path: Path, document: dict[str, Any]) -> None:
    for container in list(document.get("paused_clients", [])):
        reconnect_client(container)
        document["paused_clients"].remove(container)
        _save_recovered(path, document)
