# Band-steering integration and qualification

[Documents](../README.md) · Room acceptance (RDK lab) (in [meta-cmf-bananapi-vcpe](https://vcpe.dev/meta-cmf-bananapi-vcpe/))

## Status and scope

Implemented in both labs. The short qualification of 15 September 2026 passes all three dedicated band
rooms, both new received same-band rooms and six other selected rooms on each
backend, with independent physical-owner and WLAN traffic probes. This is a
targeted eleven-room run, not a full 25-room catalog or long-term soak.

This is the **external lab optimizer**, using native controller steering on RDK
and prplMesh. It does not establish vendor-autonomous band policy or native
end-to-end 802.11k beacon-report support. RDK's beacon-query handler is incomplete;
the current prplMesh lab rejects the tested native beacon request.

## Measurement and decision path

The new opt-in `received-same-band-roam` and
`received-discovery-recovery` rooms reuse this received-scan path without
enabling band preference. A single-band profile explicitly adds
`"measurement_mode": "received_same_band"`; an ordinary single-band profile
still does not opt into scans. Signal thresholds/gain stay with the configured
policy, with a one-second new-scan hold, three-second dwell and two-second age
budget. Missing serving reception fails closed, and unknown/absent candidates
do not acquire HAL-matrix values. Both rooms now pass initial, checkpoint and
final native ownership/traffic and room/topology checks on both backends.
See RF work and measured results (in [easymesh-medium](https://vcpe.dev/easymesh-medium/)).

Only explicitly profiled room clients gain cross-band eligibility. The scanner
reads each client's kernel radio capabilities and supplicant security capabilities.
It requests the actual bound AP frequencies, rather than assuming identical
channels in both labs: the current RDK and prpl 6 GHz channels differ.

Fresh client-received scans provide both serving and candidate RCPI. Consequently
the policy compares AP-to-client observations with the same direction and scan
interval, not native client-to-AP RSSI against a simulated downlink value.
Measurements are labeled `client_nl80211_received_scan`; native topology and
controller metrics retain their own provenance.

Receiver boottime timestamps, association identity before/after acquisition,
world identity, two-second freshness, SSID, band restrictions, SAE and PMF are
checked before using a candidate. Hidden SSIDs are not candidates; missing
observations are unavailable, not fabricated zero-RCPI readings. No room geometry
or ideal wmediumd matrix value is supplied to this policy as a received measurement.

Acquisition is asynchronous, bounded to four clients, and single-flight per
station. Scans yield to world settings and pending native steering so external
scanning does not interrupt authentication. Other clients retain the existing
native candidate path; single-band controls do not need cross-band scans.

The collector uses supplicant `SCAN TYPE=ONLY`, passive scanning and a correlated
completion ID, then reads the kernel scan dump. It cannot select a network as a
side effect. Active probing was rejected after two scanning clients produced a
retry backlog exceeding 28 seconds in the medium, disrupting a pinned control.
Passive acquisition avoids adding probe-request/response traffic.

### Native receive-channel reporting

Passive scans require the paired Linux 7.0 hwsim and wmediumd receive-context
patches. Inferring a radio's channel only from its last transmission cannot
describe a silent scan: the old medium discarded off-serving-band beacons before
the scanning client could receive them. The new kernel reports its actual home
and temporary receive channels; neither room geometry nor desired association
is involved. Kernel receive filtering remains authoritative.

The experimental lab protocol uses command 128 for one-way context notifications
and attribute 255 for snapshots, also carried on ordinary transmitted frames.
These are project-private extensions, not an upstream Linux or EasyMesh ABI.
The fixed 56-byte attribute contains a big-endian 64-bit per-radio sequence,
32-bit version (1), 32-bit count, and ten 32-bit frequency slots. An empty list
means no reception. The daemon rejects malformed snapshots and older sequences,
accepts context updates only from its kernel netlink peer, and leaves TX-learned
VIF ownership separate. Transmit snapshots refresh state if a notification is
missed. Normal channel changes, scans, cancellation, start and stop update the
context; recreated radio pools require their normal paired medium restart.

The collector refuses passive acquisition without the kernel's read-only
`rx_context_reports=Y` parameter. Install both components using the repository
build scripts; the pre-existing signal-only path remains available for older
kernels, but does not qualify these band-steering rooms. A new kernel module
requires stopping the lab and reloading its radio pool, outside any measured run.

The room's band policy prefers a higher permitted band at target RCPI ≥120
(−50 dBm), with at most 16 RCPI signal loss. Below current RCPI 100 (−60 dBm),
a candidate needs at least 4 RCPI improvement. A one-second condition hold must
include another fresh scan; minimum association dwell is three seconds.
Normal five-second post-steer cooldown and bounded failure backoff remain.
These are explicit reference-policy choices, not timing attributed to EasyMesh.

Native BTM remains advisory. A submitted request is not a successful transition:
the destination BSSID and WLAN traffic must independently verify.

The viewer treats a settled band decision as stable and describes convergence
against the AP-and-band policy, not a guarantee of strongest signal. A preferred
higher band may legitimately have a weaker signal. Incomplete band measurements
cannot produce the converged explanation; routine collection does not flash a
waiting badge over a still-valid decision.

### Independent signal and load reporting

The full prplMesh regression exposed a native Agent aggregation defect: when
channel utilization was unavailable, the monitor correctly omitted its AP
Metrics TLV but still produced valid station metrics. The Agent enumerated only
AP Metrics TLVs, silently discarding the independent RSSI and traffic evidence.
The controller retained the association's initial RCPI zero, causing repeated
steering despite a healthy kernel link. Native station queries reproduced this
discrepancy; restarting only the room service did not resolve it.

Patch `0016-agent-preserve-station-metrics-without-channel-load.patch` preserves
the measured station and extended BSS metrics even without an AP Metrics TLV.
It does not invent utilization: unavailable load stays absent, while a genuinely
measured zero remains reportable. This changes the native prplMesh Agent's
telemetry aggregation, not its autonomous band policy. RDK needs no corresponding
Agent change for the qualified rooms.

## Room profiles and recovery

A world can contain up to four initially present station profiles:

```json
"band_steering": {
  "sta_static_01": {
    "allowed_bands": ["5", "6"],
    "initial_band": "5"
  }
}
```

The allowed frequencies, network security, PMF and SAE password-element mode
are captured in the checksummed recovery journal before mutation. The room
initializes the requested starting band, then opens only the permitted bands.
Initialization is scenario setup, not a measured native steering event.
No AP radio, daemon, VM, or container is recreated.

Interactive service startup accepts an otherwise healthy hero on any supported
band: returning from a room may legitimately leave a native client on 2.4 or
6 GHz. Scripted demonstrations still enforce their manifest's specified band.
SSID, roster, RCPI and traffic preflight checks are unchanged.

Leaving the room, normal shutdown, failed switches and crash recovery restore
the saved client settings. RDK's inherited PMF sentinel cannot be written
literally with SET_NETWORK; restoring it requires supplicant reconfiguration,
followed by exact settings readback. Recovery must retain a failed record
rather than declare successful restoration.
During a room switch, leaving band profiles wait for an online client's
completed association on its restored allowed frequencies before releasing
the configuration transaction. Offline roles stay disconnected. This bounded
setup check prevents an asynchronous reconfiguration racing the next policy
decision; it is not a measured native BTM or a longer convergence deadline.

The 0913 provisioned pool has capacity for 100 clients and six logical mesh
roles; the default room activates twenty clients.
The dedicated room currently enables ten clients; other containers stay running
but their WLAN stations are absent from the room.

## Dedicated rooms

| Catalog ID | Intended observations |
|---|---|
| `band-upgrade-24-5` | Start on 2.4 GHz; native upgrade near Agent-1, 2.4 GHz fallback at the edge, no oscillation around the edge, return upgrade. |
| `band-upgrade-5-6` | Start on 5 GHz; SAE/PMF-compatible 6 GHz upgrade, 5 GHz edge fallback, return upgrade. |
| `band-ap-counter-roam` | Two clients exchange sides, change AP and band, then remain stable at opposite APs. |

Each includes a pinned single-band control. World roles are bindings, not SSID
names: `sta_static_02` is not the same client cohort in both stacks.

## Required acceptance

Use the room acceptance runner (RDK lab (meta-cmf-bananapi-vcpe `gen/tests/room-feature-acceptance.js`), prplMesh lab (prplmesh-lab `tests/room-feature-acceptance.js`))
with the three catalog IDs passed as repeated `--world` arguments. Run both
backends independently, preferably concurrently. Preserve failed output
directories and use a new empty output directory for each attempt.

Do not relax the existing room gates: 60-second initial convergence,
45-second checkpoint convergence, 90-second final convergence, and five
continuous seconds of agreement. Retain native process identity, roster,
freshness, topology visibility, playback, RF epoch and traffic checks.

Additional band-room gates independently compare expected band/AP at the initial,
checkpoint and final positions against room observations and rendered/native
topology. Kernel `iw link` and WLAN gateway probes check the physical owner,
frequency and data path. Probes run in the bound client's network namespace,
verify its station MAC and unchanged owner before and after a one-second WLAN
gateway ping. An `iw link` ENOENT race during reassociation is recorded as an
unconverged sample, never a successful link or a reason to abort normal playback.
Other command errors remain fatal. The report correlates band-changing native requests with
successful association-and-traffic verification; an optimizer green badge alone
cannot pass the test.

Also check refused/unsupported candidates, no repeated oscillation, restoration
of original client settings, and continuous usability of both live URLs.
After focused qualification, run all seventeen rooms on both labs without
weakening any old scenario's checks. No new thin tar or box is part of this work.

## Results

| Gate | RDK | prplMesh |
|---|---|---|
| Python suite, receive-context integration and probe recovery | 811 passed; 4 Ruby-dependent VirtualBox skips | 712 passed, including native-aggregation fragment tests |
| Dedicated live rooms | 3/3, repeated with strengthened physical-owner probes | 3/3 after the native Agent fix and clean native restart |
| Full original plus new room regression | 17/17 | 17/17 after native Agent fix and clean native restart |

The evidence is kept outside the repositories. The RDK lab's
`rdk-all-rooms-passive` report passes all original
and new scenarios; `rdk-band-namespace-qualification` repeats
the three dedicated rooms with station-identity and before/after owner checks.
Both preserve native process identities throughout each measured suite and
restore the default twenty clients afterward. The report auditor reconstructs
the required twelve band transitions from native action and verification events;
extra transitions, unverified actions and pinned-control band changes fail.
The corresponding prplMesh repeat is `prpl-band-clean-native`;
its independent auditor also passes, with twelve required band changes verified.
The full prpl result is
`prpl-all-rooms-passive-clean-native`.
Both full runs' `audited-summary.json` report `qualificationPassed=true`, all
seventeen rooms tested and passed, unchanged native identities, no browser errors
or SSE gaps, and successful default-world restoration. RDK's window is
08:18:13–08:52:06 UTC; prpl's is 10:12:53–10:41:33 UTC.
Final real-browser sidebar tests also pass at three viewport sizes, including
truthful band-policy descriptions and stable panel positions during updates.

### Dedicated-room timing

These observations come from the stronger RDK repeat and the clean prpl repeat,
not an intrinsic stack-speed ranking. Steering latency uses the twelve cross-band
actions only, excluding same-band AP moves in the same rooms; acquisition covers
all successful received scans in each suite.

| Observation | RDK | prplMesh |
|---|---|---|
| Required cross-band requests / verified changes | 12 / 12 | 12 / 12 |
| Request → association and WLAN verification, p50 / p95 / max, s | 1.171 / 2.167 / 2.167 | 0.911 / 1.082 / 1.082 |
| Successful received-scan acquisition, p50 / p95 / max, ms | 471 / 516 / 597 | 519 / 563 / 610 |

Acquisition timing includes the actual native passive scan and its collection
overhead. Request-to-verification starts after the policy's dwell, hold and
cooldown checks: it is not time from first physical movement. Five seconds of
continuous whole-room agreement are additionally required by acceptance.
`dedicated-band-performance.json` records the raw-event-derived distributions.

### Preparation and failed attempts

A later prpl attempt also exposed an unusable native AP state: Extender-1's
private 5 GHz interface had an empty kernel management-registration list while
hostapd remained alive. Correctly delivered authentication frames were dropped
as `RX_DROP_U_UNHANDLED_MGMT`, and clients accumulated native BSSID avoidance.
Kernel/netlink traces distinguish this from the native signal aggregation bug
and from a bad RF matrix. The exact registration-removal trigger has not yet
been reproduced.

A clean native lab restart precedes the passing dedicated repeat and full
seventeen-room prpl regression. AP resets
and avoidance-cache recovery are not performed inside measured rooms, and no
optimizer workaround clears those native states to manufacture convergence.
Kernel lifecycle tracing records no further management-registration removal
or purge during these clean suites. This qualifies the measured rooms, not a
root-cause fix for the unreproduced native registration-loss trigger.

Failed runs remain separate evidence, including the first prpl full regression
and its independent band-walk reproduction. They are not passing release evidence.
No thin tar, box, or vendor-autonomous band-policy certification is implied.
