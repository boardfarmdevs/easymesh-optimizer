from __future__ import annotations

from datetime import datetime, timedelta, timezone

from optimizer.model import (
    CandidateObservation,
    ClientObservation,
    MeshHealth,
    Snapshot,
    format_time,
)


STA = "02:00:00:00:03:00"
SOURCE = "02:00:00:aa:aa:01"
TARGET = "02:00:00:bb:bb:01"
START = datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc)


def snapshot(
    seconds: int,
    *,
    current_rcpi: int | None = 90,
    target_rcpi: int | None = 130,
    metric_age: int | None = 0,
    target_age: int | None = 0,
    source: str = SOURCE,
    current_band: str | None = "5",
    target_band: str | None = "5",
    association_uptime: int = 120,
    devices: int = 5,
    clients: int = 10,
) -> Snapshot:
    now = START + timedelta(seconds=seconds)
    current_at = None if metric_age is None else format_time(now - timedelta(seconds=metric_age))
    candidate_at = None if target_age is None else format_time(now - timedelta(seconds=target_age))
    client = ClientObservation(
        sta_mac=STA,
        connected_device_id="02:00:00:00:09:20",
        connected_device_name="Extender-1",
        connected_bssid=source,
        rcpi=current_rcpi,
        association_uptime_seconds=association_uptime,
        metric_observed_at=current_at,
        measurement_source="associated_sta_link_metrics",
        band=current_band,
    )
    candidate = CandidateObservation(
        sta_mac=STA,
        bssid=TARGET,
        device_id="02:00:00:00:08:20",
        device_name="Extender-2",
        rcpi=target_rcpi,
        metric_observed_at=candidate_at,
        measurement_source="beacon_metrics_response",
        band=target_band,
    )
    return Snapshot(
        schema_version=1,
        sequence=seconds,
        observed_at=format_time(now),
        controller_url="recorded://controller",
        health=MeshHealth(devices=devices, clients=clients, radios=15, bsses=50),
        clients=(client,),
        candidates=(candidate,),
    )
