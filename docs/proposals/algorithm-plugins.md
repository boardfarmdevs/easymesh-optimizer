# Pluggable optimizer algorithms

[Documents](../README.md)

**Status:** Proposal. Nothing here is implemented, and no code was changed for it.
**Prepared:** 2 October 2026.

**In short:** an optimizer algorithm becomes a small Python package that the optimizer
loads by name. With no name given, the optimizer builds today's policy exactly as it does
now. A plugin is written and checked on a laptop in a minimal Python environment, packed as
a standard wheel, installed on a lab VM without rebuilding anything, chosen for a room run,
and judged by the same rooms, safety gate and records as the default.

This proposal is the overview and the first two stages. The later stages (a sandboxed
process, uploads by outside developers with a queue and reports, a Data Elements view) are
designed in detail in [the optimizer workbench](optimizer-workbench.md), which was moved
here with it.

## 1. The goal and its rules

Optimizer experimentation in the labs: someone writes a new steering algorithm, checks it
in isolation, and evaluates it in the RDK and prplMesh labs without touching the optimizer
or the lab.

1. **The default stays exactly as it is.** With no plugin selected, the same code runs, the
   same decisions come out, and the journals and suite results are byte for byte the same.
2. **A plugin is prepared away from the lab.** It is written, tested against recorded lab
   runs and packed in a minimal Python environment, with nothing but the optimizer package
   installed.
3. **Deploying is not building.** Installing, choosing and removing a plugin needs no lab
   image, no VM rebuild and no change to the optimizer.
4. **One judge for all.** A plugin's decisions pass the same safety gate, are carried out
   and verified by the same machinery, and are scored by the same rule as the default's.
5. **This is the lab's mechanism.** It is not the
   [optimizer as a router application](optimizer-as-an-app.md) (a different packaging for a
   different place), nor the umbrella's "labs as a service" proposal (an optimizer that
   runs on the developer's machine and drives a lab through an API, so no code of theirs
   enters the lab).

## 2. Why it can be small

The optimizer is already shaped like a host for plugins.

| What exists | Where | Why it matters |
| --- | --- | --- |
| The decision is a pure function | `ThresholdPolicy.evaluate(snapshot, prior) -> Evaluation`, `optimizer/policy.py` | no I/O, no clock but the snapshot's; [the manual](../guides/manual.md) already offers it as the input boundary for another team |
| One factory builds the policy | `policy_for(config)`, `optimizer/load_policy.py` | it already chooses between two algorithms |
| Two alternatives are built like plugins | `LoadAwarePolicy` and `BandThresholdPolicy` subclass `ThresholdPolicy` | the pattern a plugin follows is proven in the code |
| Everything a policy sees and returns is plain data | `Snapshot`, `Decision`, `Evaluation`, `PolicyState`: frozen dataclasses with `to_dict` and `from_dict` | a plugin can be fed recorded data, and later run in another process |
| Recorded runs replay exactly | the journal (hash-chained JSON lines) and `em-optimizer replay` | the correctness checks and the proof that the default is unchanged |
| Closed-loop offline runs | `em-optimizer simulate` over a world (`WorldSimulator`), with clients that accept, reject or ignore steering | checking behaviour without a lab |
| The safety gate and verification are not the policy's | the room service: `room_service/steering_safety.py`, the verifier | a plugin cannot bypass them |

**Where the policy is built today:** five places. The command line's live modes, `replay`,
`evaluate` and `simulate` (`optimizer/cli.py`), and the room service's optimizer worker
(`room_service/conductor.py`). All five go through `policy_for`, or build
`ThresholdPolicy` directly.

## 3. What a plugin is

### The interface: `policy-api/1`

A plugin provides one class. It is constructed with the lab's policy configuration and its
own parameters, and offers what the core already calls on the default policy.

| Member | What it is | Called by |
| --- | --- | --- |
| `__init__(config, parameters)` | `config` is the lab's `PolicyConfig`; `parameters` is the plugin's own, validated against its schema | the loader |
| `config` | that `PolicyConfig`, unchanged | the room service, for its freshness limit, timeouts, cooldowns and roster, which it applies whatever the algorithm |
| `requires_candidate_measurement(client, observed_at)` | whether a client's other access points are worth measuring now | the room service, before each collection round |
| `evaluate(snapshot, prior)` | one `Decision` per client and the new `PolicyState`, in an `Evaluation` | every evaluation |
| `api` | the string `"policy-api/1"` | the loader, which refuses any other |

Rules a plugin keeps, checked before it reaches a lab:

- **Pure.** No network, no files outside its package, no subprocesses, no clock but
  `snapshot.observed_at`, no randomness that is not derived from the snapshot.
- **Deterministic.** The same snapshots and prior state give the same evaluations.
- **Bounded.** One evaluation within a time budget (proposed: 250 ms, the room's fastest
  evaluation interval).
- **Well formed.** One decision per client; the action is `steer` or `none`; a steer
  targets a BSSID among that client's candidates in the snapshot; reasons are stable
  `[a-z0-9_]` strings.

**The configuration is the lab's, the parameters are the plugin's.** A plugin reads its own
thresholds from its parameters. The `PolicyConfig` it is given keeps doing what the room
service needs from it: how old a measurement may be, how long a steer may take, the
cooldown after one. So every algorithm is verified, rate-limited and scored by one rule.

**State.** The room service tracks each client's action through `PolicyState` (pending,
cooldown, backoff), and rewrites it when it only recommends or defers an action. A plugin
returns `PolicyState` too. Its own memory goes into one new optional field, `extension` (a
JSON-safe mapping), which is left out of the journal when empty, so the default's journals
do not change.

### Two ways to write one

**Reuse the default's machinery.** The smallest plugin keeps the lab's configuration for the
room service and decides with its own settings. This one is the default with a different
margin and hold, chosen per run:

```python
from dataclasses import replace

from optimizer.policy import PolicyConfig, ThresholdPolicy


class MarginHold(ThresholdPolicy):
    """The default policy with its own steering margin and hold time."""

    api = "policy-api/1"

    def __init__(self, config: PolicyConfig, parameters: dict):
        super().__init__(config)          # the lab's settings: what the room service reads
        self._decide = ThresholdPolicy(replace(
            config,
            minimum_target_gain_rcpi=int(parameters["margin_rcpi"]),
            condition_hold_seconds=float(parameters["hold_seconds"]),
        ))

    def evaluate(self, snapshot, prior=None):
        return self._decide.evaluate(snapshot, prior)
```

It inherits the default's hold, dwell, cooldown and backoff, and `requires_candidate_measurement`
from the lab's settings. `LoadAwarePolicy` and `BandThresholdPolicy` are built the same way
today, by subclassing.

**Write the decision from scratch.** A plugin can implement `evaluate` and
`requires_candidate_measurement` itself, returning `Decision` and `PolicyState` objects. It
must then honour the action lifecycle the room service keeps in `PolicyState`: no new steer
for a client whose phase is `pending`, and none before its `cooldown_until` or
`backoff_until`. The checks test this, and the room's safety gate refuses the rest.

Methods starting with `_` are not part of `policy-api/1`. The package records the optimizer
version it was checked against, and a lab with another version refuses it until it is
checked again.

### Scope of version 1

Version 1 replaces the **client steering policy**. Three things stay built in, and a room
that uses them uses the built-in code for them whatever is selected:

- the band-steering path for clients a band room profiles (`BandThresholdPolicy`);
- the planners: backhaul, channel width, pre-association;
- the counter guard of the load-aware policy, unless the plugin subclasses
  `LoadAwarePolicy`.

## 4. The package

A plugin is a **standard pure-Python wheel**, built with the usual tools, with one entry
point.

```toml
# pyproject.toml of the plugin
[project]
name = "sticky-best-ap"
version = "0.3.0"
requires-python = ">=3.10"
dependencies = []                  # nothing beyond the optimizer, which the lab provides

[project.entry-points."easymesh_optimizer.policies"]
sticky-best-ap = "sticky_best_ap:StickyBestAp"
```

```text
sticky_best_ap-0.3.0-py3-none-any.whl
  sticky_best_ap/__init__.py        the class
  sticky_best_ap/parameters.json    each parameter: type, default, limits, unit, meaning
  sticky_best_ap-0.3.0.dist-info/   name, version, entry point, the file hashes
```

- **Identity:** name, version and the wheel's SHA-256. A version, once installed in a lab,
  never changes.
- **Dependencies:** none at first. The optimizer has none and the lab VM installs nothing
  from the internet. A short list of allowed libraries installed with the lab image is a
  later decision (the workbench proposes numpy first).
- **No compiled code:** the wheel must be `py3-none-any`.
- **Why a wheel:** `python -m build` makes it, `pip` can install it into a test
  environment, it carries its own metadata and file hashes, and it is a zip that the lab can
  read without installing anything.

## 5. Preparing a plugin: the kit

The optimizer package itself is the kit: no dependencies, Python 3.10 or later. Installed in
a fresh virtual environment, it brings the types, the default policy as an example, replay
and simulation. Four new commands are proposed:

| Command | What it does |
| --- | --- |
| `em-optimizer plugin new NAME` | a package to start from: `pyproject.toml` with the entry point, a policy class subclassing the default, `parameters.json`, tests |
| `em-optimizer plugin check PATH` | the correctness checks below, in a clean environment; writes a check report |
| `em-optimizer plugin pack PATH` | builds the wheel and places the check report beside it |
| `em-optimizer plugin compare WHEEL --journal J` | the plugin's decisions next to the default's on the same recorded run: where and why they differ |

**Inputs for checking, without a lab:**

- **Sample journals:** recorded room runs of both labs, published as release files of this
  repository and refreshed with the labs. Replaying one gives the plugin the snapshots a
  real controller produced.
- **Synthetic worlds:** a few of the medium's compiled worlds, for `simulate`, with clients
  that accept, reject or ignore steering. Marked synthetic, never presented as lab results.

**The checks, in order:**

| Check | Passes when |
| --- | --- |
| Package | a pure-Python wheel; one entry point in `easymesh_optimizer.policies`; no dependencies outside the allowed list |
| Load | it imports with `python -I` in an environment holding only the optimizer and the plugin; the class declares `policy-api/1` |
| Parameters | the defaults and the limits in `parameters.json` are valid; the class accepts the defaults |
| Contract | every evaluation of every sample journal is well formed (section 3) |
| Purity | an audit hook (Python's `sys.addaudithook`) records no socket, subprocess or file access outside the package during the replays |
| Determinism | two replays of each sample journal give identical journals, byte for byte |
| State | the state survives `to_dict` and `from_dict` at every step |
| Time | the slowest evaluation, and the 95th percentile, within the budget |
| Robustness | snapshots with no candidates, stale metrics, an incomplete roster and an empty mesh give `none` decisions, not errors |
| Lifecycle | no steer for a client that is pending, cooling down or backing off, on the sample journals and on crafted states |
| Simulation | each synthetic world runs to the end, with the counts of steers, refusals and moves reported |

The check report (JSON) names the optimizer version, the plugin's identity, the inputs'
hashes and every result. It travels with the wheel. A lab repeats the checks on install; the
report shows that the author ran them first.

## 6. Deploying a plugin

### Where it goes

Each lab VM keeps its plugins in one directory, outside the optimizer's checkout:

```text
/var/lib/easymesh-optimizer/plugins/
  index.json                                   what is installed: identity, check results, when
  sticky-best-ap/0.3.0/
    sticky_best_ap-0.3.0-py3-none-any.whl      as received; its hash is its identity
    site/                                      the wheel unpacked, read-only
    check-report.json                          the author's
    lab-check.json                             this lab's, against this lab's own recorded runs
```

The room service runs from the optimizer's checkout today (each lab's launcher puts it on
the path). When a plugin is selected, the loader adds that one plugin's `site/` to the path
and resolves its entry point. Plugins that are not selected are never imported.

### How it gets there

| Way | Who | How |
| --- | --- | --- |
| On the lab host | operators | copy the wheel to the host, then `em-optimizer plugin install WHEEL --lab VM`: checks the hash and the report, pushes it into the VM, runs the lab check, updates the index |
| Offline | anyone with the wheel | the same, from a USB stick or a file share; nothing needs the internet |
| Remote, today | operators | the same over SSH to the lab host |
| Remote, later | developers with a gateway account | an upload through [easymesh-remote](https://vcpe.dev/easymesh-remote/)'s gateway, behind its login and reservation (the workbench's registry, stage 4) |

`em-optimizer plugin list` and `remove` complete it. A version that a recorded run used is
kept, so the run can be replayed.

## 7. Choosing and evaluating

| Where | How it is chosen | When absent |
| --- | --- | --- |
| A room run | `"algorithm": "sticky-best-ap@0.3.0"` and optional `"algorithm_parameters"` in the room manifest's `optimizer` section | the default |
| The command line | `--algorithm NAME[@VERSION]` and `--algorithm-parameters FILE` on the live modes, `replay`, `evaluate` and `simulate` | the default |
| The room viewer | later: a selector, for the person holding the lab | the default |

- **The journal says which algorithm decided.** A run with a plugin begins with one more
  record: name, version, wheel hash and the parameters' digest. A run without one writes no
  such record, so its journal is unchanged.
- **The same rooms judge it.** The labs' room catalogs, with their pass criteria and the
  room service's convergence summary, run as they do for the default. The two results side
  by side are the evaluation.
- **A failing plugin cannot take the room down.** If loading fails, the room service stops
  without an optimizer and says so. An exception during an evaluation is recorded and that
  step counts as no decision. Three in a row stop the plugin for the run.

## 8. Trust and isolation, in stages

A plugin loaded into the room service runs with the room service's rights. It could read
what the optimizer must not know (the world's positions and links) or reach the lab's
controls. The checks catch accidents, not intent. So isolation grows with who writes the
plugins.

| Stage | Who writes plugins | Where a plugin runs | What changes in the core |
| --- | --- | --- | --- |
| 1 | the team | in-process, in the kit and in `replay`, `evaluate`, `simulate` | the loader and the factory (section 9) |
| 2 | the team and partners under agreement | in-process, in the labs' room service | the room manifest's key and the journal record |
| 3 | partners | its own sandboxed process (a transient systemd unit with no network), behind a proxy object with the same interface | the proxy and the runner; the room service is unchanged because the proxy looks like a policy |
| 4 | outside developers | as stage 3, uploaded through the gateway, with a registry, a queue and reports | the [workbench](optimizer-workbench.md)'s registry, campaign queue and scorecard, and its cleaner contract (`step`, `measure`, feedback) |
| 5 | anyone writing against a standard | as stage 4, seeing the controller as Data Elements | the workbench's contract version 2 |

Stage 3 keeps `policy-api/1`: the runner in the sandbox loads the same wheel, and every call
crosses as one JSON line (the snapshot and prior state in, the evaluation out). State
already travels with every call, so the room service's rewriting of it keeps working.

## 9. What changes in the code

A proposal; nothing is changed yet.

| Change | Size | Where |
| --- | --- | --- |
| The loader: read the plugin directory, check identity, resolve the entry point, construct | one new module | `optimizer/plugins.py` |
| One factory for every caller: `make_policy(config, algorithm=None, parameters=None)`, which returns `policy_for(config)` when `algorithm` is `None` | a few lines | `optimizer/load_policy.py` |
| The five construction points call it | one line each | `optimizer/cli.py`, `room_service/conductor.py` |
| The options and the manifest key | a few lines | the same files |
| The journal's identity record, for plugins only | a few lines | `optimizer/recorder.py` and its callers |
| `PolicyState.extension`, omitted when empty | a few lines | `optimizer/state.py` |
| The kit: `new`, `check`, `pack`, `compare`, `install`, `list`, `remove` | new modules | `optimizer/plugin_kit.py` and the command line |

Everything else stays as it is: the default policies, the room service's measurement,
safety, actuation and verification, the stacks' adapters, the scenarios and the suites.

## 10. Proving the default is unchanged

Each of these must hold before a stage is done, and after it:

1. **The tests** of this repository pass unchanged.
2. **Replay:** every sample journal, replayed with no algorithm selected, gives the same
   journal as before the change, byte for byte.
3. **The seam is neutral:** the default policy, packed as a plugin and loaded through the
   plugin path, gives identical journals to the direct path.
4. **The labs:** both labs' room catalogs pass as they do now, with no plugin selected.

## 11. Experiments

| | Question | How |
| --- | --- | --- |
| E1 | Does a plugin that only subclasses the default reproduce it exactly? | the neutral-seam test (section 10, item 3) on the sample journals. Already seen while writing this: the same settings written as `5.0` instead of `5` give identical decisions and state but a different policy hash, so the test compares decisions and state |
| E2 | Are the sample journals enough to judge a plugin before the lab? | write two plugins (a margin variant, a load variant), compare their offline checks with their room results |
| E3 | How long do real evaluations take? | time the default on recorded 50-client runs; set the budget from it |
| E4 | Does the audit hook catch what it should, cheaply? | a plugin that opens a socket, one that reads a world file, one that starts a process |
| E5 | What does a crash cost a room? | a plugin that raises, one that hangs, one that returns nonsense, in a live room |

## 12. Open questions

1. **First users:** the team only (stages 1 and 2), or partners soon (stage 3 first)?
2. **Libraries:** none at first, or a short list (numpy) installed with the lab images?
3. **The parameters' place:** a separate file beside the room manifest, or inside it?
4. **Band rooms:** should version 1 let a plugin take over the band-profiled clients too?
5. **Distribution of the kit:** this repository's releases, or a package index?
6. **Who may install** on a lab VM before the gateway's uploads exist: operators only?
