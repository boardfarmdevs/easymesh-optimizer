# The optimizer's documents

[Repository](../README.md) · [Site](https://vcpe.dev/easymesh-optimizer/)

The [site](https://vcpe.dev/easymesh-optimizer/) explains how the optimizer
works, for a newcomer. These documents go further:

| Document | Kind | What it covers |
| --- | --- | --- |
| [Architecture](concepts/architecture.md) | concept | the optimizer's place outside the stacks, its components and contracts, the decision and feedback sequence, the safety gates, the measurement contract |
| [Manual](guides/manual.md) | guide | operate it (install, live readiness, observe, recommend, act, read results) and extend it (inputs, metrics, algorithms, scenarios); both labs' paths |
| [Native load policy](guides/native-load-policy.md) | guide | the opt-in load-aware policy and counter guard: what they read, how they decide, running them in a room |
| [Scenario suite](reference/scenarios.md) | reference | the pseudo-worlds, traffic and scale axes, the case matrix and each scenario family's capability boundary |
| [Room access](reference/room-access.md) | reference | how a browser reaches a lab's live room: on a LAN, through a tunnel, behind a gateway; the Pages boundary |
| [Band steering](reference/band-steering.md) | reference | the band-steering measurement path, its rooms, required acceptance and qualified results |
| [Pluggable optimizer algorithms](proposals/algorithm-plugins.md) | proposal | an algorithm as a plugin the optimizer loads by name, today's policy as the default: the interface, the package, the offline kit and its checks, deploying to a lab, choosing and evaluating, isolation in stages, the code that changes |
| [The optimizer workbench](proposals/optimizer-workbench.md) | proposal | the later stages of plugins in detail: a sandboxed policy process and its wire protocol, uploads, a registry and campaign queue, the scorecard, the SDK, the Data Elements view (moved here from the RDK lab) |
| [The optimizer as an application](proposals/optimizer-as-an-app.md) | proposal | what stands between today's optimizer and one that runs as a downloadable application inside the apps framework, speaking only Data Elements: the problems found, their fixes and alternatives, the design, the experiments |
