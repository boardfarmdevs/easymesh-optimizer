# EasyMesh external optimizer

This package is the first P1 implementation of the architecture in
[the optimizer architecture](../../doc/easymesh/reference/optimizer-architecture.md). It runs on the
lab host, not in a BPI image.

Implemented now:

- raw endpoint records plus normalized immutable snapshots from `/topology`,
  `/clients`, `/devices` and `/bsses`;
- active same-band candidate RCPI collection through the controller's
  Unassociated STA Link Metrics endpoint, mapped from Agent/RUID to exact
  target BSSID;
- explicit unknown freshness and missing candidate-measurement handling;
- a pure threshold/margin/hold/dwell/cooldown decision engine;
- explicit association-timeout outcome and bounded exponential failure backoff;
- an opt-in band-upgrade baseline that still selects an exact BSSID and applies
  target RCPI, maximum-loss, hold, dwell and cooldown gates;
- a deterministic closed-loop golden-world test double with accept, reject and
  ignore client behavior;
- a recommendation-only pre-association policy with hard time/probe caps and a
  2.4 GHz failsafe cooldown;
- deterministic replay state;
- a hash-chained JSON-lines experiment journal;
- a narrow `gen/steer.sh` actuator and bounded association verifier; and
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
[the optimizer development manual](../../doc/easymesh/experiments/optimizer-development.md). It documents
plain snapshot input, replay sequences, live adapters, new typed metrics, new
algorithms, scenario authoring and acceptance.

For offline algorithm tests only, `simulate` intentionally translates a
verified golden world through a declared receiver-noise/RCPI sensor model into
synthetic EasyMesh-shaped snapshots. Its records use `simulated://` and
`simulated_*` sources and state `live_observer_compatible: false`. This is a
policy test double, not evidence that the controller reported a measurement.

## Install and test

```sh
cd gen/optimizer
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

The provider serializes Agent/radio work and splits each transaction at the
controller's eight-STA limit. The accepted 20-client cycle uses 19
transactions and returns all 80 same-band alternate-BSSID measurements. Three
consecutive no-retry observation cycles, dynamic recommendation, and one
bounded acting crossover passed on the release appliance.

Use `configs/band-upgrade-policy.yaml` to compare conservative 2.4-to-5 and
5-to-6 BSSID upgrades offline. The live Unassociated STA query is same-band;
band inventory alone is never treated as cross-band link quality.

Run a deterministic band-walk with one client ignoring BTM requests:

```sh
python3 -m optimizer.cli simulate \
  --world ../wmediumd/configurator/worlds/golden/home-a-band-walk-small.world.json \
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

See [optimizer scenarios](../../doc/easymesh/experiments/optimizer-scenarios.md) for
the pseudo-home, traffic plan, band-steering and backhaul boundaries.
