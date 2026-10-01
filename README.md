# EasyMesh optimizer

<!-- labs block: the same in every repository of the EasyMesh labs, but for the Site line -->
**Site:** none of its own; the labs' is <https://boardfarmdevs.github.io/easymesh-labs/>.
The [EasyMesh labs](https://boardfarmdevs.github.io/easymesh-labs/) serve three
goals: EasyMesh optimizer development
([easymesh-optimizer](https://github.com/boardfarmdevs/easymesh-optimizer)) in a rich
virtual lab, on both stacks
([RDK EasyMesh](https://boardfarmdevs.github.io/meta-cmf-bananapi-vcpe/),
[prplMesh](https://boardfarmdevs.github.io/prplmesh-lab/)); unchanged OpenSync
pods as EasyMesh agents under a local controller, without the OpenSync cloud
([EMOSA](https://boardfarmdevs.github.io/emosa-lab/), with the
[OpenSync lab](https://boardfarmdevs.github.io/opensync-lab/)'s pods); and
EasyMesh on physical hardware
([Protocol lab](https://boardfarmdevs.github.io/easymesh-lab/)). Two core
components carry them: the RF medium
([easymesh-medium](https://github.com/boardfarmdevs/easymesh-medium)) and EMOSA's
OVSDB ⇄ EasyMesh conversion. The rest is infrastructure and learning around them.
<!-- /labs block -->

The external steering optimizer of the EasyMesh labs: it observes an EasyMesh
controller, normalizes immutable snapshots, applies an explainable policy,
records a hash-chained journal and, when told to, issues one bounded BTM steer
and verifies the outcome. It runs in the lab VM, next to the controller, not in
a device image. The policy, planners, verifier and recorder are shared; what
differs per controller stack is one adapter each, named in
[`optimizer/stacks.py`](optimizer/stacks.py):

| Stack | Lab | Observer and candidates | Steering |
| --- | --- | --- | --- |
| `rdk` | [meta-cmf-bananapi-vcpe](https://github.com/boardfarmdevs/meta-cmf-bananapi-vcpe) (`gen/optimizer`) | em_cli API on `127.0.0.1:8888`: [`observer.py`](optimizer/observer.py), [`candidates.py`](optimizer/candidates.py) | the lab's `gen/steer.sh` |
| `prplmesh` | [prplmesh-lab](https://github.com/boardfarmdevs/prplmesh-lab) (`optimizer`) | topology adapter on `127.0.0.1:8092` and NBAPI over ubus: [`prplmesh.py`](optimizer/prplmesh.py), [`lxd_ubus.py`](optimizer/lxd_ubus.py) | the lab's `scripts/steer-client.sh` |

Each lab pins this repository as a submodule, next to the medium it also pins
(`medium`, [easymesh-medium](https://github.com/boardfarmdevs/easymesh-medium)):
the scenarios read the medium's golden worlds as `../medium`, and the stack is
the lab's (`--backend`, else `OPTIMIZER_STACK`, else found from the lab's
steering script).

Two packages: `optimizer`, the policy and its adapters, and `room_service`, the
room service that runs it live while a room plays (the interactive room on port
8891: it plays the medium's rooms through wmediumd, keeps the clients and their
bands, serves the live view and drives the optimizer against the controller).
Each lab starts the room service with its own launcher, manifests and role
bindings (`rooms/room-service`, `rooms/manifests`, `rooms/bindings`, next to this
checkout); what the room service does differently per stack is in
[`room_service/lab.py`](room_service/lab.py). Its tests are in `tests/room`; the tests
of a lab's own rooms stay in the lab.

[`acceptance/`](acceptance) holds the live acceptance tools both labs' suites run against a
lab VM (with `--flavor rdk|prpl` or `--stack rdk|prpl`): the room catalog in a browser
(`room-feature-acceptance.js`), the geometry backhaul rooms (`room-backhaul-features.js`),
default readiness (`room-final-readiness.py`), the RF property and access rooms, the
load-aware, counter-guard, native-counter and traffic acceptance, and the guest audits
and host monitors they install; each with its unit tests. The labs' suite runners stay in
the labs and call them from here. The labs' room manuals: the
[RDK lab's](https://github.com/boardfarmdevs/meta-cmf-bananapi-vcpe/blob/main/doc/easymesh/room-service/README.md),
[prplMesh's](https://github.com/boardfarmdevs/prplmesh-lab/blob/main/docs/room-service/README.md).

The manuals: [architecture](docs/architecture.md),
[development](docs/development.md) (operate and extend it),
[scenarios](docs/scenarios.md) (the experiment matrix) and
[band steering](docs/band-steering.md).

Implemented now:

- raw endpoint records plus normalized immutable snapshots from `/topology`,
  `/clients`, `/devices` and `/bsses`;
- active same-band candidate RCPI collection through the controller's
  Unassociated STA Link Metrics endpoint, mapped from Agent/RUID to exact
  target BSSID;
- explicit unknown freshness and missing candidate-measurement handling;
- a pure threshold/margin/hold/dwell/cooldown decision engine;
- explicit association-timeout outcome and bounded exponential failure backoff;
- prompt failed-attempt reporting when native ownership moves from the observed
  source to a different AP than requested; absence or an unproven cached owner
  still uses the original timeout, and target success still requires traffic;
- an opt-in band-upgrade baseline that still selects an exact BSSID and applies
  target RCPI, maximum-loss, hold, dwell and cooldown gates;
- a deterministic closed-loop golden-world test double with accept, reject and
  ignore client behavior;
- a recommendation-only pre-association policy with hard time/probe caps and a
  2.4 GHz failsafe cooldown;
- deterministic replay state;
- a hash-chained JSON-lines experiment journal;
- a narrow actuator (the lab's steering script) and bounded association verifier; and
- unit, adapter, replay, isolated five-AP crossover and existing configurator
  scenario tests.

The scenario preparation layer also expands ten checked-in golden RF worlds,
five independent traffic profiles, policy configurations and seeds into a
hash-verified case matrix. Missing lab abilities remain explicit per-case
blockers. The live observer never reads simulated RF truth.

The live controller supplies the associated-report receipt time and an active
same-band candidate query with per-result receipt time. In the hwsim lab the
candidate provider is explicitly identified as simulated-radio infrastructure,
so it requires `--allow-simulated-candidates`. A physical deployment must
report its operating channel and must not use that opt-in. Cross-band decisions
still require Beacon/Probe/capability observations; candidate inventory alone
is never treated as link quality. wmediumd SNR is never accepted as an
optimizer observation.

The complete operator and extension contract is in
[the optimizer development manual](docs/development.md). It documents
plain snapshot input, replay sequences, live adapters, new typed metrics, new
algorithms, scenario authoring and acceptance.

For offline algorithm tests only, `simulate` intentionally translates a
verified golden world through a declared receiver-noise/RCPI sensor model into
synthetic EasyMesh-shaped snapshots. Its records use `simulated://` and
`simulated_*` sources and state `live_observer_compatible: false`. This is a
policy test double, not evidence that the controller reported a measurement.

## Opt-in Native Load Policy

`configs/load-counter-guard-policy.yaml` additionally requires fresh native
retry, TX-failure and RX-drop rates before load balancing; missing counters
abstain and rates over explicit limits veto balancing. Signal rescue remains
unchanged. See [RF coverage](https://github.com/boardfarmdevs/easymesh-medium/blob/main/docs/reference/rf-property-coverage.md)
for the limits, decision reasons, demonstration rooms and focused validation.
The checked-in `gen/demo/manifests/native-counter-guard-room-profile.json`
operates `rf-asymmetric-ack` with this guard; select it with
`gen/rooms/room-service interactive --mode recommend --profiling --manifest ...`
after stopping the existing room service. The bounded native counter acceptance
can replay real report windows through the shared guard without inventing a
load target. Clear/pressure/recovery qualifies only that veto, not a load move.

Default operation remains signal-only. Select `configs/load-aware-policy.yaml`
for an experiment; live mode requires root in the VM and
`--candidate-provider controller`. Its owned native IEEE 1905 receiver closes
on exit. Default operation starts no additional collector.

From the repository root, copy the room manifest, change only `policy` to
`gen/optimizer/configs/load-aware-policy.yaml`, and add
`--manifest /absolute/temporary-manifest.json` to the normal interactive
command. Stop the existing room service first; never run two actuating
optimizers. Restart the unchanged service to restore signal-only operation.

Configure a same-band, different-channel fronthaul AP first. Do not retune
the active backhaul radio. Band-directed profiles can restrict `freq_list`:
explicitly permit the new channel before BTM; a scan alone does
not grant eligibility. Verify kernel **and native controller** channel state.
RDK local retunes need an operating-channel report; an agent refresh is test
preconditioning, never a timed-steer repair. After a refresh, replay the native
metrics-reporting policy and verify fresh reports: agent timer state is volatile.
Global `Device.WiFi.ApplyRadioSettings` can also apply stale channel settings
on unrelated radios. Snapshot all three **live** channels, preserve them during
the test and verify their exact restoration, including 6 GHz.
The policy does not reconfigure
radios or client capabilities automatically.

Schema-2 snapshots carry native AP utilization (0–255), station count,
packet-counter activity, receipt timestamps and provider epoch. The survey
bridge supplies only provenance/liveness, not load values or SNR inputs.
Packets/second are activity, not offered demand or calibrated capacity;
backhaul hops are a conservative cost guard, not a bandwidth estimate.

Transport provenance is `ieee1905-ethernet` on RDK. The shared collector uses
prpl's native broker for its colocated and remote APs, preserving native
publication timestamps instead of re-timestamping cached data. Disconnected,
stale or unsupported receivers fail closed. A bounded read-only check is
`PYTHONPATH=gen/optimizer python3 gen/tests/native-load-acceptance.py --stack rdk --output /tmp/native-load-new`
inside the VM; it checks all BSS loads, station counts, client activity,
advancing timestamps and receiver cleanup.

Strong links balance from sustained high load to a fresh quieter channel
with viable RF and no extra wireless hop. RDK's independent five-second
native reports use a ten-second hold, with five-second freshness/skew bounds;
both reports must advance. prpl's profile uses one-second skew/five-second
hold. These opt-in parameters do not change native reporting or default room
gates. One active client moves at a time, then settles with
cooldown; weak links retain signal protection. Missing, stale, skewed,
synthetic or epoch-mismatched observations cannot become zero load. Decisions
record explicit reasons and evidence. Native BTM may still be rejected by
the client. Reception-backed candidates, demand/capacity estimation and
general channel selection remain separate work.

## Install and test

The tests read the medium's golden worlds from `../medium`, as in the labs. On
its own, check the medium out next to this repository at the commit the labs pin
(the CI does the same, `.github/workflows/checks.yml`):

```sh
git clone git@github.com:boardfarmdevs/easymesh-optimizer.git optimizer
git clone git@github.com:boardfarmdevs/easymesh-medium.git medium
cd optimizer
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
pytest
```

Capture live read-only snapshots:

```sh
em-optimizer observe \
  --base-url http://127.0.0.1:8888 \
  --count 30 --interval 1 \
  --journal /tmp/em-observe.jsonl
```

Run deterministic replay after a journal contains normalized snapshots with
trustworthy candidate facts:

```sh
em-optimizer replay \
  --input /tmp/em-capture.jsonl \
  --policy configs/threshold-policy.yaml \
  --journal /tmp/em-replay.jsonl
```

Evaluate a team-supplied plain JSON snapshot without live I/O or action:

```sh
em-optimizer evaluate \
  --input scenarios/examples/normalized-snapshot.json \
  --policy configs/threshold-policy.yaml \
  --output /tmp/em-evaluation.json \
  --state-out /tmp/em-state.json
```

Collect live same-band candidate measurements and make recommendations:

```sh
em-optimizer recommend \
  --base-url http://127.0.0.1:8888 \
  --candidate-provider controller \
  --allow-simulated-candidates \
  --policy configs/threshold-policy.yaml \
  --count 10 --interval 1 \
  --journal /tmp/em-recommend.jsonl
```

The RDK provider serializes Agent/radio work and splits each transaction at the
controller's per-query limit (64 stations since ccsp-one-wifi 0040, eight before). The accepted 20-client cycle uses 19
transactions and returns all 80 same-band alternate-BSSID measurements. Three
consecutive no-retry observation cycles, dynamic recommendation, and one
bounded acting crossover passed on the release appliance.

Use `configs/band-upgrade-policy.yaml` to compare conservative 2.4-to-5 and
5-to-6 BSSID upgrades offline. The live Unassociated STA query is same-band;
band inventory alone is never treated as cross-band link quality.

Run a deterministic band-walk with one client ignoring BTM requests:

```sh
python3 -m optimizer.cli simulate \
  --world ../medium/configurator/worlds/golden/home-a-band-walk-small.world.json \
  --policy configs/band-upgrade-policy.yaml \
  --initial-band 2.4 \
  --client-behavior sta_static_01=ignore \
  --output /tmp/home-band-sim.json
jq '{truth_boundary, summary}' /tmp/home-band-sim.json
```

The same command and inputs produce the same simulation hash. Do not use this
output as a live result claim.

Generate recommendation-only backhaul and channel-width plans from example
observation documents:

```sh
python3 -m optimizer.cli backhaul-plan \
  --input scenarios/examples/backhaul-observations.json \
  --output /tmp/backhaul-plan.json
python3 -m optimizer.cli width-plan \
  --input scenarios/examples/radio-environment.json \
  --output /tmp/width-plan.json
```

The backhaul baseline scores fresh undirected edge/band alternatives using
SNR, PHY rate, utilization, retries and configurable band bonuses, then returns
a maximum-utility loop-free spanning tree. It supports 2.4, 5 and 6 GHz;
2.4 GHz has a default penalty but remains available when needed for
connectivity. The width baseline covers 20/40/80/160 MHz and explains clean
2.4 GHz, radar-risk, congestion and clean-6 GHz recommendations. Neither
command changes a radio or backhaul link.

`PreAssociationPolicy` is also available to test probe-response preference
logic. It suppresses a known multiband client's 2.4 GHz response only within a
bounded window and probe count, immediately permits 5/6 GHz probes, and then
forces a 2.4 GHz response plus cooldown. There is deliberately no live probe
control adapter yet.

`act` requires both a policy-produced recommendation and the explicit
`--yes-act` flag. It defaults to `--max-actions 1` and exits after that action
attempt and its bounded verification. A failed candidate collection cycle is
never evaluated or acted upon. Recommend mode records an emitted choice as a
recommendation, not a pending action, and suppresses the unchanged choice on
later cycles with `recommendation_unchanged`.

Build and inspect the scenario matrix:

```sh
python3 -m optimizer.cli matrix \
  --spec scenarios/home-suite.json \
  --output scenarios/generated/home-suite.matrix.json
jq '.summary' scenarios/generated/home-suite.matrix.json
```

See [optimizer scenarios](docs/scenarios.md) for
the pseudo-home, traffic plan, band-steering and backhaul boundaries.

## prplMesh

The live prplMesh path is:

```text
NBAPI topology + associated RCPI
          +
Unassociated STA Link Metrics candidate RCPI
          -> normalized snapshot -> policy -> recommend
                                      |
                                      +-> explicit act -> BTMRequest -> verify
```

Root-in-VM candidate collection runs the same native `ubus` calls through
descriptor-pinned controller mount/root namespaces. LXD discovers the controller
once, not once per registration/query. Four registration workers remain bounded;
only discovery is locked. Native publication timestamps, freshness and RPC
deadlines are unchanged. Transactions record the transport and elapsed time.
Non-root hosts retain `lxc exec`. Namespace failures never silently fall back;
restart observation after a controller restart to discard its registration cache.

On compatible native builds, `_describe` advertises the optional boolean
`AddUnassociatedStation.defer_query`. Collection registers a cohort without
issuing redundant fleet-wide queries for every addition, then uses the existing
explicit `UpdateUnassociatedStationsStats` and waits for fresh native reports.
Older controllers retain their original behavior; no unsupported option is sent.
A streaming round asks for one band's clients at a time, as the prplMesh lab was
qualified.

Collect live same-band candidates and recommend without changing the mesh:

```sh
python3 -m optimizer.cli recommend \
  --backend prplmesh \
  --candidate-provider controller --allow-simulated-candidates \
  --policy configs/threshold-policy.yaml \
  --count 10 --interval 1 --journal /tmp/prpl-recommend.jsonl
```

`--candidate-timeout` bounds one complete candidate transaction (30 seconds by
default) and `--expected-clients` overrides the policy's client count for a
larger lab profile. The load-aware policy as qualified in this lab is
`configs/load-aware-policy-prplmesh.yaml`.
