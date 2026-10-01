# Optimizer scenario and experiment suite

[Optimizer README](../README.md)

## Objective

The suite creates a reproducible pseudo-world in which RF, traffic and policy
are independent axes. It is preparation for policy research and P4 scale
acceptance, not an ML algorithm and not evidence that an unavailable control
has been implemented.

```text
P Agent layouts x M mobility/RF worlds = reusable golden RF sequences

golden RF sequence x N traffic profiles x K policies x S seeds
                              |
                              v
                  deterministic case matrix
                              |
              +---------------+----------------+
              |                                |
        runnable today                 blocked with exact
        on accepted scale               missing capabilities
```

The RF values and traffic schedule are evaluator truth. The optimizer may
observe only measurements reported through EasyMesh. It must not read the
world file, wmediumd matrix or intended path as a candidate score.

There are two deliberately separate execution boundaries:

- live/capture/recommend/act use `ControllerObserver` and consume only
  controller-reported facts;
- offline `simulate` uses a declared sensor model to turn a verified golden
  world into `simulated_*` EasyMesh-shaped facts for deterministic policy and
  closed-loop tests.

Synthetic snapshots are marked `simulated://`, explicitly say they are not
live-observer compatible, and cannot promote a live capability. They let
cross-band policy and accept/reject/ignore response handling be developed
before a cross-band measurement provider is complete without disguising
evaluator truth as controller evidence. Same-band hwsim candidate collection
is now live and is covered separately by the controller provider.

### Pre-association preference is bounded influence

The recommendation-only `PreAssociationPolicy` expresses the intended
behavior without pretending that EasyMesh supplies a portable probe-blocking
primitive. It suppresses a 2.4 GHz probe response only when prior knowledge
says the client supports 5 or 6 GHz, for at most three seconds and three probes
by default. A preferred-band probe is answered immediately. Reaching either
limit forces a 2.4 GHz response and a 30-second cooldown, so preference cannot
become denial of service. The current capability remains blocked because
OneWifi/hwsim has no accepted per-probe observation/action adapter.

## Checked-in pseudo-worlds

Sources are under `gen/medium/configurator/worlds/`; generated timelines are
under `worlds/golden/`.

| World | Agents / clients | Purpose |
| --- | ---: | --- |
| stationary | 5 / 10 | passive small-profile control |
| slow walk, layout A | 5 / 20 | ten fixed and ten moving clients crossing several cells |
| slow walk, layout B | 5 / 20 | repeat the identical paths with different Agent placement |
| border hover | 5 / 12 | repeated cell/wall-threshold crossings and ping-pong pressure |
| flash crowd | 5 / 20 | ten clients appear together from 20–50 seconds |
| disappear/reappear | 5 / 12 | per-client liveness and stale-state cleanup |
| fast transit | 5 / 12 | candidate opportunity shorter than a costly roam |
| asymmetric link | 5 / 11 | station-originated SNR is deliberately weaker than AP-originated SNR |
| extender loss/recovery | 5 / 10 | one Agent becomes fully attenuated and later returns |
| small band walk | 5 / 10 | move all ten accepted client roles without increasing lab scale |

The two layouts and ten worlds are small enough to review and deterministic
enough to reuse. They contain complete directed station/Agent fronthaul and
Agent/Agent backhaul values for 2.4, 5 and 6 GHz at each tick. They do not
change meaning after a client roams.

Walls add a fixed 5 dB for each proper straight-line crossing. This deliberately
creates abrupt measurement changes without pretending to model diffraction,
reflection, material or antenna orientation accurately. Seeded shadowing is
available, but the first goldens use zero random shadowing so geometry and wall
effects remain directly explainable.

## Traffic axis

Traffic is defined separately in
`scenarios/traffic-profiles.json`:

| Profile | Driver | Status |
| --- | --- | --- |
| idle keepalive | none | available |
| latency probe | `ping` every 200 ms | available |
| constant load | `iperf3` | specified, not accepted |
| staggered bursty applications | `iperf3` | specified, not accepted |
| synchronized traffic flash crowd | `iperf3` | specified, not accepted |

`traffic-plan` expands a selected matrix case into container-bound command
arrays. It never embeds shell fragments and records its own hash. Execution and
capacity acceptance of the `iperf3` driver remain a separate task, so those
cases stay blocked instead of silently becoming ping tests.

## Build and inspect the matrix

```sh
cd gen/medium/configurator
worlds/build-goldens.sh --check
python3 -m pytest -q

cd ../../optimizer
python3 -m optimizer.cli matrix \
  --spec scenarios/home-suite.json \
  --output scenarios/generated/home-suite.matrix.json
python3 -m pytest -q

jq '.summary, .coverage' scenarios/generated/home-suite.matrix.json
jq -r '.cases[] | select(.status == "blocked") |
  [.id, (.missing_capabilities | join(","))] | @tsv' \
  scenarios/generated/home-suite.matrix.json
```

The current matrix contains 148 cases: 56 capability-runnable and 92 blocked.
It applies
both the ordinary weak-link threshold baseline and the opt-in band-upgrade
baseline to the same RF/traffic cases. The count is not a quality score. It
shows that the Cartesian core and specialized scenario
families exist while preserving the present lab boundary. Accepting the
five-Agent/20-client mixed profile promotes cases whose other declared
requirements are already available. It does not mean that all 56 combinations
have run live. Band-steering cases remain blocked on the distinct cross-band
capability.

Compile the stationary latency plan using the supplied legacy-named fixture
after checking its bindings against the target lab:

```sh
case_id='cartesian-home--home-five-agent--stationary--latency-probe--threshold-policy--seed-1701'
python3 -m optimizer.cli traffic-plan \
  --matrix scenarios/generated/home-suite.matrix.json \
  --case "$case_id" \
  --bindings scenarios/rev130-small-bindings.json \
  --output /tmp/stationary-latency.traffic.json
jq '{status, duration_ms, events: (.events | length), plan_sha256}' \
  /tmp/stationary-latency.traffic.json
```

The role binding is explicit: gateway, four extenders, ten fixed private
clients and ten mobile IoT clients map to the fixture's container names. The
bindings also name the lab's traffic target (`traffic_target`, where the
clients' traffic goes); a profile's schedule may name its own.
The `rev130` filename is not a current host address or deployment qualification.
A larger world fails plan compilation until its additional roles have real
containers and a profile is accepted.

## Capability boundaries by scenario family

| Family | Present foundation | Required before a valid live claim |
| --- | --- | --- |
| ordinary client steering | atomic RF, current RCPI, same-band candidate RCPI with receipt time, BTM action and association verifier | scenario-specific recommend/act acceptance and longer stability evidence |
| band steering | per-band worlds, frequency-qualified RF control, BSSID inventory and band-aware observation/policy schemas | fresh exact-BSSID cross-band measurements plus capability filtering |
| pre-association steering | bounded decision state machine and failsafe semantics | probe-response observation/control adapter |
| BTM `NoDisconnect` | action and outcome model | clients that deterministically accept, reject or ignore BTM |
| load steering | traffic schedules | accepted `iperf3` driver and timestamped AP/BSS load metrics |
| backhaul topology | directed Agent/Agent values in every world | backhaul metric observer and safe topology/band action adapter |
| channel width | location and traffic axes | representative 20/40/80/160 MHz hwsim operation and action adapter |
| scale | generated 5/20 RF worlds | reproducible container manifests and accepted resource envelope |

### Band steering is a BSSID decision

The checked-in world calculates different 2.4, 5 and 6 GHz reach. Patch `0012`
extends wmediumd's live key to `(source radio, destination radio, frequency
MHz)` while retaining the old pair value as fallback. Atomic apply, get, dump,
clear and exact restore are implemented. `world-export --band all` resolves
2.4/5/6 GHz through each Agent's actual live fronthaul frequency and emits all
three conditions in one scenario.

Rev130 proved the frequency boundary on an associated 5180 MHz link: a 25 dB
override moved signal/RCPI from -41 dBm/138 to -66 dBm/88 while a different
2437 MHz override existed for the same pair. Clearing both directions returned
to the original 50 dB pair fallback and -41 dBm/138 without packet loss.

The ten-client small band walk then exercised all three bands together for 30
ticks. The 60-second plan completed in 60.8 seconds with at most 65.3 ms apply
lateness, restored all 300 touched frequency keys and left the 5/15/50
controller model and all 10 WLAN associations complete. This accepts the RF
stimulus path at the current small profile; it does not claim an optimizer made
band decisions.

The optimizer ranks target BSSIDs using reported candidate measurements. It
does not simply choose the label “5 GHz” or “6 GHz.” Same-band measurements
are accepted; a 2.4-to-5 or 5-to-6 case remains blocked until exact cross-band
measurement and client-capability requirements exist.

`band-upgrade-policy.yaml` is the first conservative, explainable baseline. It
normalizes the EasyMesh band enumeration (0=2.4, 1=5, 3=6 GHz), keeps the exact
source and target BSSID in every score/decision, and considers only a higher
band whose fresh measured RCPI is at least 120 and no more than 8 RCPI (4 dB)
below the current link. The same hold, dwell, pending timeout and cooldown gates
still apply. It deliberately defaults off in `threshold-policy.yaml`.

Those numbers are hypotheses for comparison, not EasyMesh-standard policy
primitives and not a claim that a higher band is always better. Candidate
inventory with an unknown RCPI cannot trigger the policy. The live band cases
therefore remain blocked by cross-band measurement and capability filtering
even though deterministic 2.4/5/6 GHz RF stimulus and trustworthy receipt
times now work.

The offline closed-loop runner already uses the same policy core against the
small band-walk golden. It assigns deterministic synthetic device, BSSID and
STA identities, converts directed station-to-Agent SNR through an explicit
noise-floor/RCPI sensor model, applies accepted actions to association state,
and leaves rejected or ignored actions unchanged. This tests policy logic and
failure backoff; it does not clear the live cross-band-measurement blocker.

Patch `0068` adds the read-only `/api/v1/bsses` projection from the controller's
serialized `get_sta` tree. Patch `0077` also joins the topology display to that
authoritative per-Agent radio inventory instead of recursively borrowing a
descendant's BSS list. Rev130 returns exactly 30 fronthaul identities: private
and IoT BSSs on five devices across bands 0, 1 and 3. The observer normalizes
those as 2.4, 5 and 6 GHz and produces 14 same-SSID target identities for each
associated private client. Same-band entries receive active RCPI; cross-band
inventory remains unknown and cannot trigger a band steer.

### Backhaul topology is a slower, separate loop

Backhaul selection should be represented as a weighted graph whose vertices
are Agents and whose possible edges have band, measured signal/SNR, rates,
retry cost, utilization and stability. A slow optimizer can compare connected,
loop-free trees while penalizing churn and weak upstream bottlenecks. Five or
6 GHz preference and 2.4 GHz failsafe are policy hypotheses, not hard-coded
facts.

The current configurator computes those potential edge values but does not
apply them: `.wmd` export keeps `protect backhaul`. This prevents a client
steering scenario from accidentally partitioning the mesh before a safe
backhaul transaction and verifier exist.

`backhaul-plan` now provides the first recommendation-only baseline. It accepts
fresh observed alternatives for every undirected edge and band, scores SNR,
PHY rate, utilization and retries, applies configurable band bonuses, and uses
a maximum-spanning-tree selection to guarantee a loop-free connected result.
It is not limited to 5 GHz: 6 GHz has a small default preference, 5 GHz remains
preferred over 2.4 GHz, and penalized 2.4 GHz remains a connectivity failsafe.
The actual measurements and transactional topology action remain unavailable,
so this does not promote the live backhaul capability.

### Channel width and field behavior

The current hwsim lab operates at an effective 20 MHz width. Location-specific
40/80/160 MHz policies may be designed and replayed, but throughput claims are
blocked until those widths are represented and measured. Likewise, BTM
`NoDisconnect` is a normal outcome rather than a protocol failure. The
threshold policy now turns an expired pending steer into an explicit
`association_timeout`, applies a 60-second exponentially increasing backoff
(capped at 600 seconds), and preserves the failure count in replay state. Live
deterministic testing remains blocked until clients can be assigned
accept/reject/ignore behavior; the policy already has safe timeout semantics.

`width-plan` is the first recommendation-only width baseline. It can recommend
40 MHz on 2.4 GHz only for a clean low-neighbor location, cap 5 GHz near radar
or high congestion, and prefer 160 MHz on sufficiently clean 6 GHz. Every
result names its reason. The inputs are explicit radio-environment
observations; the command does not mutate OneWifi and cannot support a
throughput claim until representative widths, traffic and a verifier exist.

## Acceptance progression

1. Keep the current 5-Agent/20-client small profile green.
2. Add a traffic executor and accept latency, constant-load and burst evidence.
3. **Complete for same-band:** retain real receipt time and exact target-BSSID
   candidate measurements; add a separate cross-band provider.
4. **Crossover complete:** continue threshold recommend testing against
   stationary, border and fast-transit cases.
5. **One bounded action complete:** repeat it under cooldown, rejection and
   longer-soak conditions before increasing `--max-actions`.
6. **Bounded 50-client cold reconstruction and 105-radio fan-out complete:**
   complete the 5/20 churn and 50-client duration gates before routine medium-
   profile use or a full 100-client stress attempt.
7. **Frequency-qualified RF complete:** keep its apply/readback/restore
   regression mandatory; cross-band measurements still gate a band-steering
   policy claim.
8. Add backhaul and width action adapters as separate safety domains.

Every promotion updates `capabilities-current.json`; matrix regeneration then
moves only genuinely supported cases from `blocked` to `runnable`.
