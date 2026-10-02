# External optimizer architecture

[Documents](../README.md)

## Purpose and ownership

The steering optimizer is a completely external component. It runs on the lab
host or LXD VM, outside every BPI container and outside the EasyMesh,
OneWifi, WebUI and wmediumd processes.

For installation, operating commands, input schemas, adapter examples and
policy extension, use the companion [optimizer user and extension
manual](../guides/manual.md).

The optimizer owns all optimization behavior:

- measurement interpretation and freshness rules;
- candidate construction, filtering, ranking and scoring;
- thresholds, margins, hysteresis, dwell and cooldown;
- per-client state and outstanding transactions;
- whether, when and where to steer; and
- decision, action and outcome records.

The BPI EasyMesh implementation supplies protocol facts and mechanisms only:

- topology, association, capability and measurement reports;
- standardized measurement queries and responses;
- Policy Configuration transport for reporting and exclusions;
- Client Steering Request transport, 1905 ACK and steering reports; and
- the OneWifi/HAL path that transmits BTM and reports reassociation.

It supplies no optimizer, target recommendation, candidate score, threshold
evaluation or steering trigger. Agent-local steering must be explicitly disabled
for controller-optimizer experiments so the external optimizer is the only
decision maker.

## Complete architecture

```mermaid
flowchart TB
    classDef external fill:#e8f1ff,stroke:#1e5aa8,stroke-width:2px,color:#102a43
    classDef stimulus fill:#fff3cd,stroke:#9a6700,stroke-width:2px,color:#4d3500
    classDef bpi fill:#eaf7ea,stroke:#287a28,stroke-width:2px,color:#153b15
    classDef kernel fill:#f2e9ff,stroke:#6f42c1,stroke-width:2px,color:#34205f
    classDef client fill:#ffe9ee,stroke:#a61b45,stroke-width:2px,color:#541027
    classDef contract fill:#ffffff,stroke:#4f5b66,stroke-width:1.5px,stroke-dasharray:5 3,color:#20262c
    classDef warning fill:#fff0f0,stroke:#b42318,stroke-width:2px,color:#7a1a12

    subgraph HOST["Linux 7 lab host or LXD VM"]
        direction LR

        subgraph RF["Independent RF stimulus plane"]
            direction TB
            SRC["Scenario source<br/>two-ap crossover"]:::stimulus
            COMP["wmdcfg compiler<br/>frozen physical-radio bindings"]:::stimulus
            RUN["wmdcfg runner<br/>timed atomic generations"]:::stimulus
            SOCK["wmediumd control socket<br/>APPLY / GET / readback / restore"]:::contract
            SRC --> COMP --> RUN --> SOCK
        end

        subgraph EXTOPT["External optimizer process — Python"]
            direction TB
            OBS["Observation adapters<br/>topology + real EasyMesh metrics"]:::external
            NORM["Normalized immutable snapshot<br/>STA, current BSS, candidates,<br/>RCPI, utilization, age"]:::external
            DEC["Pure decision engine<br/>filter + score + state transition"]:::external
            PCFG["Versioned optimizer policy<br/>thresholds, margin, dwell,<br/>hold, timeout, cooldown"]:::external
            STATE[("Per-STA state store<br/>stable / degraded / eligible /<br/>pending / verifying / cooldown")]:::external
            SAFE["Safety gate<br/>health, freshness, exclusions,<br/>one in-flight action"]:::external
            ACT["Action adapter<br/>steer(STA, target BSSID)"]:::external
            VERIFY["Outcome verifier<br/>link + controller model + API + traffic"]:::external
            JOURNAL[("Append-only experiment journal<br/>snapshots, reasons, actions, outcomes")]:::external

            OBS --> NORM --> DEC --> SAFE --> ACT
            PCFG --> DEC
            STATE <--> DEC
            ACT --> STATE
            ACT --> VERIFY --> STATE
            NORM --> JOURNAL
            DEC --> JOURNAL
            ACT --> JOURNAL
            VERIFY --> JOURNAL
        end

        ORACLE["Lab evaluator only<br/>wmediumd truth, client iw link,<br/>traffic and service health"]:::contract
        WMD["wmediumd<br/>one process, all active hwsim radios"]:::stimulus
        SOCK --> WMD
        RUN --> JOURNAL
        WMD -.-> ORACLE
        ORACLE --> JOURNAL
    end

    subgraph KERNEL["Shared Linux 7 kernel"]
        direction LR
        HWSIM["patched mac80211_hwsim<br/>24 radios / 3 channel contexts"]:::kernel
        MAC["cfg80211 + mac80211<br/>nl80211 namespaces"]:::kernel
        HWSIM <--> MAC
    end

    WMD <-->|"generic netlink frames,<br/>frequency and TX status"| HWSIM

    subgraph CONTROLLER["bpibroadband LXD container"]
        direction TB
        HTTP["onewifi_em_cli WebUI/API :8888<br/>live topology and clients only"]:::bpi
        CMD["steer.sh + steer_drv<br/>validated command adapter"]:::bpi
        METRICBR["Raw observation boundary<br/>EasyMesh metric/query results"]:::contract
        CTRL["em_ctrl<br/>EasyMesh controller protocol engine"]:::bpi
        MODEL[("OneWifiMesh model<br/>topology, BSS and STA state")]:::bpi
        C1905["ieee1905_em_ctrl<br/>AL-SAP / IEEE 1905.1"]:::bpi
        CAGENT["colocated em_agent<br/>no optimizer"]:::bpi
        CONEWIFI["OneWifi + HAL<br/>2.4 / 5 / 6 GHz VAPs"]:::bpi

        HTTP <-->|"libemcli command channel"| CTRL
        CMD -->|"steer_sta command"| CTRL
        METRICBR <-->|"read-only metric queries/reports"| CTRL
        CTRL <--> MODEL
        CTRL <--> C1905
        CAGENT <--> C1905
        CAGENT <--> CONEWIFI
    end

    subgraph EXTENDERS["bpiap[-NNN] LXD containers — four accepted"]
        direction TB
        A1905["ieee1905_em_agent"]:::bpi
        AGENT["em_agent<br/>policy receiver and protocol endpoint<br/>no optimizer; local steering mode 0"]:::bpi
        ONEWIFI["OneWifi + HAL<br/>backhaul STA + fronthaul VAPs"]:::bpi
        A1905 <--> AGENT
        AGENT <--> ONEWIFI
    end

    subgraph STATIONS["wlan-client[-NNN] LXD containers — ten accepted"]
        direction TB
        SUPP["WNM-capable wpa_supplicant<br/>802.11v BTM response"]:::client
        DATA["wlan0 traffic and DHCP"]:::client
        SUPP --- DATA
    end

    MAC <-->|"namespaced hwsim wiphy"| CONEWIFI
    MAC <-->|"namespaced hwsim wiphy"| ONEWIFI
    MAC <-->|"namespaced hwsim wiphy"| SUPP

    C1905 <-->|"wireless backhaul<br/>1905 CMDUs and ACKs"| A1905
    ONEWIFI <-->|"802.11 association,<br/>measurements and BTM"| SUPP

    OBS -->|"GET live topology / clients"| HTTP
    OBS -->|"associated STA, AP and<br/>candidate-link measurements"| METRICBR
    ACT -->|"current lab hook:<br/>lxc exec bpibroadband -- steer.sh"| CMD
    CTRL -->|"Client Steering Request"| C1905
    A1905 -->|"RBus raw-frame action"| ONEWIFI
    SUPP -->|"reassociation and BTM response"| ONEWIFI
    A1905 -->|"association notification,<br/>metrics and steering report"| C1905
    MODEL -->|"association/model feedback"| HTTP
    HTTP --> VERIFY
    METRICBR --> VERIFY
    ORACLE -.-> VERIFY

    DEMO["Do not use for decisions:<br/>demonstration-only WebUI endpoints,<br/>Optimize Layout, wmediumd plan or SNR"]:::warning
```

The absence of a decision edge from wmediumd or the BPI agents to the decision
engine is intentional. wmediumd knows the simulated truth, and the BPI agents
know local protocol state, but neither selects the target for this experiment.

## Interface contracts

| Interface | Producer | Consumer | Contract now |
| --- | --- | --- | --- |
| RF generation | `wmdcfg` | wmediumd | Atomic radio-pair SNR generations with verified restore |
| RF transport | wmediumd | hwsim | Frequency-isolated simulated frame delivery |
| live topology | controller/WebUI | external observer | compact `/api/v1/topology` supplies nodes/client placement; `/api/v1/bsses` supplies controller-owned fronthaul BSSID/device/radio/band/SSID identity |
| live association | controller/WebUI | external observer/verifier | Parent agent and STA MAC in `/api/v1/topology` and `/api/v1/clients` |
| current-link metrics | EasyMesh reports/queries | external observer | Live associated-client RCPI/rates/counters are available from `/api/v1/clients` |
| candidate-link metrics | EasyMesh Unassociated STA Link Metrics | external observer | Active same-band query returns timestamped RCPI mapped from Agent/RUID to exact BSSID; hwsim requires explicit simulator opt-in |
| policy baseline | operator/external setup | controller and agents | Reporting/exclusion configuration; local agent steering set to mode 0 |
| steer action | external actuator | controller | `steer.sh STA TARGET_BSSID` at the current implementation stage |
| protocol action | controller | source agent | EasyMesh Client Steering Request in Mandate mode |
| WLAN action | source agent/OneWifi | client | 802.11v BTM Request from the source VAP |
| outcome | client/agent/controller | external verifier | link, BTM/report, model and API must converge |
| experiment truth | runner/client/health audit | recorder only | Scenario events, client link, traffic and restart counters |

### Observation boundary

`/api/v1/topology`, `/api/v1/devices`, `/api/v1/clients` and `/api/v1/bsses`
derive identity, association placement and target BSS identity from the current
controller tree. WebSocket initial state uses the same live inventory.
`/api/v1/clients` joins the controller's detailed associated-STA report by MAC
and exposes real RCPI, derived dBm, rate, counters, association uptime and the
report receipt timestamp. `/api/v1/bsses` deliberately exposes identity rather
than invented quality. The observer actively POSTs
`/api/v1/unassoc_sta_query`, groups clients per Agent radio, and maps each
timestamped response RUID to an exact BSSID. Inventory entries with no response
remain unknown and cannot trigger an action.

The hwsim provider measures at the simulated-radio/HAL boundary and labels the
response simulated. The optimizer accepts it only with an explicit lab-only
flag. The query listens on the channel where a STA currently transmits, so it
provides same-band candidates. Live cross-band policy still needs
Beacon/Probe/capability observations. Performance, interference and other
demonstration endpoints remain non-authoritative and must not be optimizer
inputs.

Native controller code remains an interface adapter: it correlates commands
and exposes measurements, but contains no optimizer state, ranking, target
selection or policy algorithm.

Direct MariaDB and `iw` reads are useful acceptance oracles. They must not
become the durable optimizer API because they bypass the EasyMesh observation
contract being evaluated.

### Action boundary

The current action hook is deliberately narrow:

```text
external actuator
  -> lxc exec bpibroadband -- /usr/bin/steer.sh STA TARGET_BSSID
  -> steer_drv / libemcli
  -> em_ctrl Client Steering Request
```

This path is already proven at scale. The actuator must resolve the current
source and validate the target immediately before execution. A later
authenticated steering API may replace `lxc exec`, but it must preserve the
same transaction and verification semantics.

## External optimizer components

The Python package is this repository, mounted in each lab (`gen/optimizer` in
the RDK lab, `optimizer` in the prplMesh lab); the main files:

```text
easymesh-optimizer/
|-- pyproject.toml
|-- configs/
|   |-- threshold-policy.yaml
|   `-- band-upgrade-policy.yaml
|-- scenarios/
|   |-- capabilities-current.json
|   |-- traffic-profiles.json
|   |-- scenario-catalog.json
|   `-- generated/home-suite.matrix.json
|-- optimizer/
|   |-- cli.py
|   |-- stacks.py        the stacks' differences
|   |-- model.py
|   |-- observer.py      RDK: em_cli API
|   |-- candidates.py    RDK: Unassociated STA queries
|   |-- prplmesh.py      prplMesh: topology adapter and NBAPI
|   |-- policy.py
|   |-- state.py
|   |-- actuator.py
|   |-- verifier.py
|   |-- experiments.py
|   |-- traffic.py
|   |-- simulator.py
|   |-- planners.py
|   |-- preassociation.py
|   `-- recorder.py
`-- tests/
```

Its execution modes are:

| Mode | Behavior |
| --- | --- |
| `observe` | Collect and record real snapshots; never recommend or steer |
| `recommend` | Run the complete state machine and record decisions; never act |
| `act` | Issue one validated action and verify the complete outcome |
| `evaluate` | Validate and evaluate one team-supplied plain Snapshot v1; never act |
| `replay` | Re-evaluate a hash-chained chronological snapshot journal |
| `simulate` | Run synthetic EasyMesh-shaped telemetry from a verified golden world; never a live claim |
| `backhaul-plan` | Build a recommendation-only weighted loop-free backhaul tree |
| `width-plan` | Produce explained recommendation-only channel-width choices |

The decision core should be a pure function over a snapshot, versioned policy
and prior client state. Network I/O belongs in adapters, not in scoring logic.
This makes recorded inputs replayable in unit tests.

## External policy model

The optimizer policy is its own versioned configuration, not an EasyMesh Policy
Configuration TLV and not a WebUI conservative/balanced/aggressive preset. The
following values illustrate the schema; they are not accepted tuning defaults.

```yaml
policy_version: 1
decision_interval_seconds: 1
current_rcpi_below: 100
minimum_target_gain_rcpi: 16
condition_hold_seconds: 5
minimum_dwell_seconds: 20
steer_timeout_seconds: 10
post_steer_cooldown_seconds: 30
failure_backoff_seconds: 60
maximum_failure_backoff_seconds: 600
maximum_in_flight_per_sta: 1
reject_stale_metrics_after_seconds: 3
```

Configured wmediumd SNR is never substituted for observed RCPI. The two are
different quantities and their relationship must be measured through the
actual Wi-Fi/EasyMesh reporting path.

EasyMesh policy configuration remains useful only to establish the experiment
environment:

- agent-local steering mode `0` so no BPI process makes a steering decision;
- empty local and BTM exclusion lists unless the experiment tests exclusions;
- reporting intervals and inclusion settings needed to obtain raw metrics; and
- no reliance on the misleading WebUI `Add Station Entry` label: per-radio
  settings are keyed by RUID, not client MAC.

## Decision and feedback sequence

```mermaid
sequenceDiagram
    autonumber
    participant R as wmdcfg runner
    participant W as wmediumd
    participant N as hwsim / Wi-Fi network
    participant E as BPI EasyMesh controller + agents
    participant O as External observer
    participant P as External policy engine
    participant A as External actuator
    participant V as External verifier
    participant J as Experiment journal

    Note over E,P: Agent-local steering is disabled and no BPI component selects a target
    R->>W: Apply next atomic SNR generation
    W->>N: Change simulated frame delivery conditions
    N->>E: Real association and link measurements
    E-->>O: Topology, AP metrics and STA link metrics
    O->>O: Normalize identities, timestamps and freshness
    O->>P: Immutable observation snapshot
    P->>P: Update hold/dwell/hysteresis state
    P-->>J: Snapshot, candidates, scores and state transition

    alt Not eligible or data stale
        P-->>J: No action with explicit reason
    else Exactly one target is eligible
        P->>A: Decision(STA, source, target, reason, policy hash)
        A->>E: steer.sh STA TARGET_BSSID
        E->>E: EasyMesh Steering Request and matching 1905 ACK
        E->>N: OneWifi transmits BTM from source VAP
        N-->>E: Reassociation and BTM response/report
        A->>V: Start bounded verification transaction
        E-->>V: Controller model and API association update
        N-->>V: Client-link and traffic test oracle
        V-->>P: Passed, rejected or timed out
        V-->>J: Per-plane evidence and convergence latency
        P->>P: Enter cooldown or failure backoff
    end

    R->>W: Restore captured medium baseline
    W-->>R: Readback confirms restoration
    R-->>J: Final scenario and restoration result
```

The feedback loop belongs to the external optimizer: it observes the resulting
association and closes its own transaction. It never asks wmediumd whether the
steer should have worked.

## Health and safety gates

The optimizer code inhibits an action unless all of these are true:

- expected five non-controller mesh-device records and 20/20 active clients
  (ten private and ten IoT) are present;
- the STA has one current association and a fresh current-link measurement;
- the target BSSID exists, is not the source and is eligible for the STA;
- candidate measurements are fresh and exceed the configured margin;
- minimum dwell and condition-hold periods are satisfied;
- no steering transaction, failure backoff or cooldown exists for the STA; and
- the operator supplied the explicit act confirmation.

The live test harness must additionally require the complete `5/15/50`
controller model, healthy wmediumd, no controller/agent/OneWifi restart, a
writable journal and complete post-action traffic/model agreement. These are
acceptance gates around the optimizer; the policy does not read wmediumd or
service-manager state.

Missing, contradictory or stale data results in `NO_ACTION`, never an inferred
value or optimistic steer.

## Measurement contract

The package implements immutable Snapshot v1, controller and candidate
adapters per stack, a pure threshold/margin/hold/dwell/cooldown state machine,
plain-JSON evaluation, deterministic replay, a hash-chained journal, the narrow
actuator (the lab's steering script, `gen/steer.sh` in the RDK lab) and bounded
association verifier.
It imports no wmediumd state in its live observation or decision path.

Associated and candidate metrics carry controller receipt times. Candidate
requests are serialized by Agent radio and the controller retires a completed
command synchronously when its correlated response is retained, so an
immediate next request is admitted. Collection failure aborts the complete
snapshot; optional non-acting soak behavior records and skips that cycle rather
than evaluating partial candidates.

### Measurement capability and exposure matrix

| Fact or mechanism | Standard/native implementation | Controller state | External exposure now | Use / limit |
| --- | --- | --- | --- | --- |
| current association | topology notifications, Associated Clients repair | current STA owner and BSSID | `/clients` | use now |
| associated-link RCPI/rates/counters | periodic AP Metrics Response and Associated STA Link Metrics | persisted with report receipt time | `/clients` with receipt time | use now with freshness gate |
| AP/BSS utilization | Modeled hwsim survey → HAL → OneWifi → native AP/Radio Metrics; periodic and utilization-crossing reports | native report fields and receipt time | read-only native load collector; POST `/api/v1/ap_metrics_query` submits `{AlMac, BSSIDs}` | HTTP 202 means submitted, not measured; native replies preserve query MID and BSSID selection; RF qualification (in [meta-cmf-bananapi-vcpe](https://vcpe.dev/meta-cmf-bananapi-vcpe/)) |
| candidate BSS identity | device/radio/BSS model | BSSList contains target identities | `/bsses` returns 30 fronthaul identities across five devices and three bands | use now; unknown quality remains unknown |
| Unassociated STA Link Metrics | Profile-3 CMDU, OneWifi method/event and hwsim provider | controller correlates MID, Agent/RUID, STA, opclass, channel, RCPI and receipt time | POST `/api/v1/unassoc_sta_query` waits for and returns correlated measurements | use now for same-band hwsim with explicit simulator opt-in |
| Beacon Metrics | query/response handlers and agent RBus beacon-report path exist | raw measurement-report elements are copied into the STA model | no external query/result API and no decoded candidate RCPI | evaluate after unassociated metrics; decode only required fields |
| client capability/BTM support | client capability query/report and reassociation parsing | capability data exists in STA model | not normalized for optimizer filtering | add read-only capability flags |
| steering | Client Steering Request, BTM, ACK/report | proven controller/agent transaction | `gen/steer.sh` | use through narrow actuator |

The next measurement slice is cross-band evidence and client capability, not a
shortcut through scenario truth. Beacon/probe observations must name exact
BSSID/band, source and receipt time before live band-upgrade action is enabled.

### Unassociated-metrics boundary

The implementation separates radio selection/state, RBus method/event,
payload-shape, hwsim-provider, result-correlation and HTTP semantic defects.
The layer repairs each boundary without moving policy into the BPI:

1. controller orchestration selects the requested Agent radio/opclass and
   admits operational radio states;
2. the Agent invokes OneWifi's real per-VAP `GetNaSta` method and consumes
   `Device.WiFi.EM.NaStaResponse`;
3. producer and parser share the flat STA-list contract;
4. hwsim obtains read-only candidate quality at the simulated-radio medium
   boundary and labels the result simulated;
5. controller and Agent correlate message IDs and retain RCPI with receipt
   time; and
6. HTTP waits for a semantic result and returns provider, simulated flag and
   measurements rather than command submission alone.

Multi-Agent collection exposed one additional lifecycle race: the first result
was externally visible before its active orchestrator command was retired. An
immediate second query was silently treated as already in progress and HTTP
returned 504. The controller now completes that command when the correlated
response is retained. This is a generic command-lifecycle correction, not an
optimizer retry workaround.

Scenario preparation is also implemented. Two 2D Agent layouts, ten mobility/
presence worlds and five separate traffic profiles expand into a deterministic
148-case matrix across two policy baselines. The current capability file marks
56 cases capability-runnable and 92 blocked with exact missing mechanisms.
Frequency-qualified RF, metric receipt time and same-band candidate collection
are accepted, as is the five-Agent/20-client mixed profile. Cross-band
evidence, load traffic, controlled BTM response, backhaul actions, channel
width and the 50/100-client profiles must not be inferred from configuration
alone. See [optimizer scenarios](../reference/scenarios.md).

### Test ladder

The optimizer tests intentionally progress in layers:

1. model/MAC/timestamp validation and serialization;
2. observer normalization and the rule that API serialization time is not
   measurement time;
3. missing, stale, threshold, margin, dwell, hold, pending and cooldown policy
   transitions;
4. actuator source/target validation and verifier convergence;
5. hash-chain integrity and byte-deterministic replay; and
6. the existing `two-ap-crossover.wmd` and isolated five-AP phase contracts
   combined with measured-shaped EasyMesh streams, requiring exactly one
   recommendation; and
7. both 2.4-to-5 and 5-to-6 BSSID decisions against the ten-client small band
   world without reading its simulated SNR as policy input.

The crossover test reads the scenario only for phase/timing coordination. Its
RCPI inputs are explicitly labelled Associated STA Link Metrics and Beacon
Metrics observations; it never calculates them from the scenario SNR.

## Acceptance criteria

Require these gates for each new optimizer qualification:

- recorded policy/scenario hashes and frozen radio bindings;
- real measurement changes preceding the decision;
- exactly one explained steer for the selected STA;
- no unintended agent-local steering;
- client link, BTM/report, controller database and API agreement;
- bounded traffic impact and no service restarts;
- cooldown preventing an immediate return steer;
- verified wmediumd restoration; and
- a complete append-only record sufficient to replay the decision offline.

The room displays optimizer state, but the browser is not the decision engine.
`Optimize Layout`, canned metrics and generic steering presets are not policy
decisions. See room coordination (in [meta-cmf-bananapi-vcpe](https://vcpe.dev/meta-cmf-bananapi-vcpe/)) for live authority.
