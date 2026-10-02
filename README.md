# easymesh-optimizer: the steering optimizer of the EasyMesh labs

<!-- labs block: the same in every repository of the EasyMesh labs, but for the Site line -->
**Site:** <https://vcpe.dev/easymesh-optimizer/>
The [EasyMesh labs](https://mesh.vcpe.dev/) serve three
goals: EasyMesh optimizer development
([easymesh-optimizer](https://vcpe.dev/easymesh-optimizer/)) in a rich
virtual lab, on both stacks
([RDK EasyMesh](https://vcpe.dev/meta-cmf-bananapi-vcpe/),
[prplMesh](https://vcpe.dev/prplmesh-lab/)); unchanged OpenSync
pods as EasyMesh agents under a local controller, without the OpenSync cloud
([EMOSA](https://vcpe.dev/emosa-lab/), with the
[OpenSync lab](https://vcpe.dev/opensync-lab/)'s pods); and
EasyMesh on physical hardware
([Protocol lab](https://vcpe.dev/easymesh-lab/)). Two core
components carry them: the RF medium
([easymesh-medium](https://vcpe.dev/easymesh-medium/)) and EMOSA's
OVSDB ⇄ EasyMesh conversion. The rest is infrastructure, tools (the
[room builder](https://vcpe.dev/easymesh-room-builder/)) and learning
around them.
<!-- /labs block -->

The external steering optimizer both virtual labs run: it observes an EasyMesh
controller, normalizes immutable snapshots, applies an explainable policy, records a
hash-chained journal and, when told to, issues one bounded BTM steer and verifies the
outcome. It runs in the lab VM next to the controller, not in a device image. The
policy, planners, verifier and recorder are shared; each controller stack has one
adapter, named in [optimizer/stacks.py](optimizer/stacks.py):

| Stack | Lab | Observer and candidates | Steering |
| --- | --- | --- | --- |
| `rdk` | [RDK EasyMesh](https://vcpe.dev/meta-cmf-bananapi-vcpe/) (`gen/optimizer`) | em_cli API on `127.0.0.1:8888`: [observer.py](optimizer/observer.py), [candidates.py](optimizer/candidates.py) | the lab's `gen/steer.sh` |
| `prplmesh` | [prplMesh](https://vcpe.dev/prplmesh-lab/) (`optimizer`) | topology adapter on `127.0.0.1:8092` and NBAPI over ubus: [prplmesh.py](optimizer/prplmesh.py), [lxd_ubus.py](optimizer/lxd_ubus.py) | the lab's `scripts/steer-client.sh` |

Each lab pins this repository as a submodule next to the medium, which it also pins
(`medium`): the scenarios read the medium's golden worlds as `../medium`, and the stack
is the lab's (`--backend`, else `OPTIMIZER_STACK`, else found from the lab's steering
script).

## Components

| Part | What it is |
| --- | --- |
| [optimizer/](optimizer) | the policy and its stack adapters: observation, candidate measurement, the decision engine, the planners (backhaul, channel width, pre-association), the actuator and verifier, the journal, replay and simulation; the `em-optimizer` command |
| [room_service/](room_service) | the room service: plays the medium's rooms through wmediumd while a room runs (the interactive room on port 8891), keeps the clients and their bands, serves the live view and drives the optimizer against the controller; what differs per stack is in [room_service/lab.py](room_service/lab.py) |
| [acceptance/](acceptance) | the live acceptance tools both labs' suites run against a lab VM (`--flavor rdk\|prpl` or `--stack rdk\|prpl`): the room catalog in a browser, the backhaul rooms, default readiness, the RF property, access, load, counter and traffic acceptance, the guest audits and host monitors; the latency tools (controller to topology page, room to room viewer, the native controller's own share), run by hand |
| [configs/](configs) | the policies: threshold (the default), band upgrade, load-aware and the counter guard |
| [scenarios/](scenarios) | the experiment suite: the scenario catalog, traffic and scale profiles, each lab's role bindings, the generated case matrix, example inputs |
| [tests/](tests) | the optimizer's tests; the room service's in [tests/room](tests/room) |
| [site/](site) | the explainer site: how the optimizer works |

Each lab starts the room service with its own launcher, manifests and role bindings
(`rooms/room-service`, `rooms/manifests`, `rooms/bindings`, next to this checkout);
the tests of a lab's own rooms and its suite runners stay in the lab.

## Getting started

The tests read the medium's golden worlds from `../medium`, as in the labs. Check the
medium out next to this repository at the commit the labs pin (the
[CI](.github/workflows/checks.yml) does the same):

```sh
git clone git@github.com:boardfarmdevs/easymesh-optimizer.git optimizer
git clone git@github.com:boardfarmdevs/easymesh-medium.git medium
cd optimizer
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e '.[test]'
pytest                                   # the optimizer, the room service, the acceptance tools
em-optimizer evaluate --input scenarios/examples/normalized-snapshot.json \
  --policy configs/threshold-policy.yaml --output /tmp/em-evaluation.json
```

In a lab VM, observe first, then recommend, then act explicitly; the
[manual](docs/guides/manual.md) has the safe progression.

## Documentation

The [site](https://vcpe.dev/easymesh-optimizer/) explains how the
optimizer observes, decides, steers and verifies. The documents are indexed in
[docs/README.md](docs/README.md): the architecture, the manual (operate and extend it),
the opt-in load policy, the scenario suite, the band-steering qualification, and the
proposals: algorithms as plugins (and the workbench for their later stages), and the
optimizer as an application on Data Elements.
