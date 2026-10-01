'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {argumentsFrom, expectedFrame, evaluate, distribution, eventPerformance, viewAgreement, recordedEventKind, kernelClientAudit, qualificationFailures} = require('./room-feature-acceptance.js');
const harnessSource = fs.readFileSync(path.join(__dirname, 'room-feature-acceptance.js'), 'utf8');
assert.match(harnessSource, /movingCapture = screenshot\('moving'\)\.catch/);
assert.doesNotMatch(harnessSource, /await screenshot\('moving'\)/);
assert.match(harnessSource, /finally \{\s*await movingCapture;/);
assert.ok(harnessSource.includes("error === 'stale_revision'"));
assert.match(harnessSource, /guestAuditInstallCommand\(args\.host, args\.vm\)/);
assert.match(harnessSource, /response\.status\(\) === 409 && \(await response\.json\(\)\)\.error === 'stale_revision'/);
assert.match(harnessSource, /await room\.waitForFunction\(\(\) => document\.fullscreenElement\?\.id === 'roomView'\);\s*phase = 'playing';\s*await clickPlay\(\);/);
const requiredArguments = ['--yes-act', '--flavor', 'prpl', '--host', 'lab-host', '--vm', 'lab-vm',
  '--room-url', 'http://room/', '--topology-url', 'http://topology/', '--worlds', '/worlds', '--output', '/evidence'];
assert.equal(argumentsFrom(requiredArguments)['fail-fast'], undefined);
const failFastArguments = argumentsFrom([...requiredArguments, '--fail-fast', '--world', 'first', '--world', 'second']);
assert.equal(failFastArguments['fail-fast'], true);
assert.deepEqual(failFastArguments.world, ['first', 'second']);
assert.match(harnessSource, /save\('report.json', report\);\s*}\s*if \(args\['fail-fast'\] && !activeRoom.passed\) \{\s*report.abortedAfterRoom = activeRoom.id;\s*break;/);
assert.ok(harnessSource.includes('context.setDefaultTimeout(45000)'));
assert.ok(harnessSource.includes("const renderer = args.renderer || 'swiftshader'"));
assert.ok(harnessSource.includes("['--use-angle=vulkan', '--disable-software-rasterizer']"));
assert.ok(harnessSource.includes("save('renderer.json', report.renderer)"));
assert.match(harnessSource, /renderer === 'vulkan' &&[\s\S]*?swiftshader\|llvmpipe\|software/);
assert.match(harnessSource, /async function loadWorld\(id\) \{\s*await room\.bringToFront\(\);/);
assert.match(harnessSource, /async function exitRoomFullscreen\(\)[\s\S]*?await room\.waitForFunction\(\(\) => !document\.fullscreenElement\);/);
assert.match(harnessSource, /phase = 'restore'; activeRoom = null;\s*try \{\s*await exitRoomFullscreen\(\);/);
const screenshotSource = harnessSource.slice(harnessSource.indexOf('  async function screenshot(label)'),
  harnessSource.indexOf('  async function loadWorld(id)'));
assert.match(screenshotSource, /await room\.bringToFront\(\);\s*await room\.screenshot/);
assert.match(screenshotSource, /await topology\.bringToFront\(\);\s*await topology\.screenshot/);
assert.doesNotMatch(screenshotSource, /Promise\.all/);
assert.ok(screenshotSource.lastIndexOf('await room.bringToFront()') > screenshotSource.indexOf('await topology.screenshot'));
assert.ok(harnessSource.includes("report.browserNetwork = 'lab-origins-only'"));
assert.ok(harnessSource.includes("route.abort('blockedbyclient')"));
assert.match(harnessSource, /activeRoom\.final = await settle\([^;]+;\s*activeRoom\.kernel = await auditKernel\(activeRoom\.final\.final, world\);\s*await movingCapture;\s*await screenshot\('final'\);/);
assert.ok(harnessSource.includes('modelAgeAtStartMs: started - sampleResult.monoMs'));
const kernelBindings = {active: {sta_mac: '02:00:00:10:04:00'}, dormant: {sta_mac: '02:00:00:20:32:00'}};
const kernelWanted = ['02:00:00:10:04:00'];
const kernelModel = [{mac: kernelWanted[0], bssid: '02:00:00:00:08:00'}];
const kernelLinks = {active: 'Connected to 02:00:00:00:08:00 (on wlan0)\n\tfreq: 5975', dormant: 'Not connected.'};
const nativeCheck = (links = kernelLinks, model = kernelModel, wanted = kernelWanted) =>
  kernelClientAudit(kernelBindings, wanted, model, links);
assert.equal(nativeCheck().passed, true);
assert.equal(nativeCheck().onlineCount, 1);
assert.equal(nativeCheck().offlineCount, 1);
assert.equal(nativeCheck({...kernelLinks, active: 'Connected to 02:00:00:00:02:00 (on wlan0)'}).passed, false);
assert.equal(nativeCheck(kernelLinks, []).passed, false);
assert.equal(nativeCheck({...kernelLinks, active: 'Not connected.'}).passed, false);
assert.equal(nativeCheck({...kernelLinks, dormant: kernelLinks.active}).passed, false);
assert.equal(nativeCheck({active: kernelLinks.active}).passed, false);
assert.equal(nativeCheck(kernelLinks, kernelModel, [...kernelWanted, '02:00:00:00:ff:00']).passed, false);
const now = Date.now();
const timestamp = new Date(now).toISOString();
const world = {roles: {client: 'station'}, generations: [
  {time_ms: 0, positions: {client: [0, 0]}, present: {client: true}},
  {time_ms: 10000, positions: {client: [10, 0]}, present: {client: true}},
  {time_ms: 20000, positions: {client: [20, 0]}, present: {client: false}},
]};
assert.deepEqual(expectedFrame(world, 5000).client.position, [5, 0]);
assert.deepEqual(expectedFrame(world, 15000).client.position, [10, 0]);
assert.equal(expectedFrame(world, 20000).client.present, false);
const bindings = {client: {sta_mac: 'aa'}};
const parents = ['ext1', 'ext2', 'ext3', 'ext4'];
const current = {environment_epoch: 1, health: {healthy: true, expected_online_clients: 1},
  network: {clients: [{sta_mac: 'aa', connected_bssid: 'bb', ssid: 'private', rcpi: 100, metric_observed_at: timestamp}],
    mesh: {nodes: ['gateway', ...parents].map(role => ({role, device_id: role})),
      backhaul_edges: parents.map(role => ({child_role: role, parent_role: 'gateway'}))}},
  optimizer: {environment_epoch: 1, evaluated_at: timestamp,
    fleet: {converged: true, measurement_complete: true, clients_checked: 1, clients_evaluated: 1, clients_with_stronger_ap: 0},
    client_decisions: [{sta_mac: 'aa', source_bssid: 'bb', current_rcpi: 100, current_band: '5', scores: [{band: '5', gain_rcpi: -2}]}]}};
const interactions = {environment_epoch: 1, playback: {time_ms: 0}, roles: {client: {position: [0, 0], present: true}}};
const station = {mac: 'aa', bssid: 'bb', ssid: 'private'};
const view = {meshCount: 6, stations: [station], modelStations: [station], edges: parents.map(role => ({to: role, from: 'gateway'}))};
const check = (snapshot = current, state = interactions, rendered = view) => evaluate(snapshot, state, rendered, world, bindings, now);
assert.equal(check().converged, true);
assert.equal(check().strongestApConverged, true);
const marginHeld = {...current, optimizer: {...current.optimizer,
  fleet: {...current.optimizer.fleet, clients_with_stronger_ap: 1},
  client_decisions: [{...current.optimizer.client_decisions[0], reason: 'insufficient_gain',
    scores: [{band: '5', gain_rcpi: 2}]}]}};
assert.equal(check(marginHeld).converged, true);
assert.equal(check(marginHeld).policyConverged, true);
assert.equal(check(marginHeld).strongestApConverged, false);
assert.equal(check(marginHeld).strongerClientGaps[0].maximum_gain_rcpi, 2);
assert.equal(check({...marginHeld, optimizer: {...marginHeld.optimizer,
  fleet: {...marginHeld.optimizer.fleet, converged: false}}}).converged, false);
assert.equal(check({...current, optimizer: {...current.optimizer,
  client_decisions: [{...current.optimizer.client_decisions[0], sta_mac: 'wrong'}]}}).converged, false);
assert.equal(check(current, {...interactions, fault: 'medium restarted'}).converged, false);
assert.equal(check(current, interactions, {...view, stations: [station, station]}).converged, false);
assert.equal(check(current, interactions, {...view, stations: [{...station, bssid: 'wrong'}]}).viewMatchesRoom, false);
assert.equal(check(current, {...interactions, environment_epoch: 2}).converged, false);
assert.equal(check({...current, optimizer: {...current.optimizer, evaluated_at: new Date(now - 31000).toISOString()}}).converged, false);
assert.equal(check({...current, optimizer: {...current.optimizer, fleet: {...current.optimizer.fleet, measurement_complete: false}}}).converged, false);
assert.equal(check({...current, network: {...current.network, clients: [{...current.network.clients[0], rcpi: 0}]}}).converged, false);
assert.equal(check({...current, network: {...current.network, mesh: {...current.network.mesh,
  backhaul_edges: current.network.mesh.backhaul_edges.map(edge => ({...edge, parent_role: edge.child_role}))}}}).meshConnected, false);
// A pod world: the four extenders must reach the gateway; a pod has a backhaul edge only
// while on Wi-Fi backhaul, and then it must reach the gateway too.
const podWorld = {...world, roles: {client: 'station', gateway: 'fronthaul_ap', extender_1: 'fronthaul_ap',
  extender_2: 'fronthaul_ap', extender_3: 'fronthaul_ap', extender_4: 'fronthaul_ap', pod_1: 'fronthaul_ap', pod_2: 'fronthaul_ap'}};
const podMesh = edges => evaluate({...current, network: {...current.network, mesh: {...current.network.mesh,
  backhaul_edges: edges.map(([child_role, parent_role]) => ({child_role, parent_role}))}}},
  interactions, view, podWorld, bindings, now).meshConnected;
const extenderEdges = [1, 2, 3, 4].map(n => [`extender_${n}`, 'gateway']);
assert.equal(podMesh(extenderEdges), true);  // pods on their wired path
assert.equal(podMesh([...extenderEdges, ['pod_1', 'gateway'], ['pod_2', 'extender_1']]), true);  // on Wi-Fi
assert.equal(podMesh([...extenderEdges, ['pod_1', 'nowhere']]), false);
assert.equal(podMesh(extenderEdges.slice(1)), false);
// a wired extender: an Ethernet child of the gateway, never a Wi-Fi child; a Wi-Fi
// extender may hang off it
const wiredWorld = {...podWorld, roles: {...podWorld.roles, extender_5: 'fronthaul_ap'}, wired_backhaul: ['extender_5']};
const toGateway = [['extender_5', 'gateway']];
const wiredMesh = (edges, wiredEdges = toGateway) => evaluate({...current, network: {...current.network, mesh: {...current.network.mesh,
  backhaul_edges: edges.map(([child_role, parent_role]) => ({child_role, parent_role})),
  wired_edges: wiredEdges.map(([child_role, parent_role]) => ({child_role, parent_role, media_type: 'Ethernet'}))}}},
  interactions, view, wiredWorld, bindings, now).meshConnected;
assert.equal(wiredMesh(extenderEdges), true);
assert.equal(wiredMesh(extenderEdges.slice(1)), false);
assert.equal(wiredMesh(extenderEdges, []), false);  // not seen as wired at all
assert.equal(wiredMesh([...extenderEdges, ['extender_5', 'gateway']]), false);  // a Wi-Fi child too: a second path
assert.equal(wiredMesh([['extender_1', 'gateway'], ['extender_2', 'extender_5'], ['extender_3', 'extender_2'],
  ['extender_4', 'gateway']]), true);  // a branch off the wired extender
assert.equal(wiredMesh([['extender_1', 'gateway'], ['extender_2', 'extender_5'], ['extender_3', 'extender_2'],
  ['extender_4', 'gateway']], []), false);
assert.equal(check(current, {...interactions, roles: {client: {position: [5, 0], present: true}}}).converged, false);
assert.equal(check(current, interactions, {...view, edges: []}).meshViewMatches, false);
// ap_expectations: a named client on its AP at the final settle or a checkpoint
{
  const {apExpectations} = require('./room-feature-acceptance.js');
  const settledOn = pairs => ({final: {roomAssociations: pairs.map(([role, ap]) => ({role, ap}))}});
  const world = {ap_expectations: [{at: 'final', roles: {sta_mobile_01: 'extender_5'}},
    {at: 30000, roles: {sta_mobile_01: 'gateway'}}]};
  const result = {final: settledOn([['sta_mobile_01', 'extender_5']]),
    checkpoints: [{timeMs: 30000, ...settledOn([['sta_mobile_01', 'extender_2']])}]};
  const checked = apExpectations(world, result);
  assert.deepEqual(checked.map(item => item.passed), [true, false]);
  assert.deepEqual(checked[1].misses, [{role: 'sta_mobile_01', expected: 'gateway', observed: 'extender_2'}]);
  assert.deepEqual(apExpectations({}, result), []);
  assert.equal(apExpectations(world, {checkpoints: []})[0].settled, false);
}
assert.deepEqual(distribution([1, 2, 4, 3]), {count: 4, p50: 2, p95: 4, max: 4});
assert.deepEqual(distribution([]), {count: 0, p50: null, p95: null, max: null});
const measured = eventPerformance([
  {event: {kind: 'optimizer.action', payload: {phase: 'requested'}}},
  {event: {kind: 'optimizer.action', payload: {phase: 'submitted'}}},
  {event: {kind: 'optimizer.collection', payload: {phase: 'completed', transactions: [{operation: 'register'}]}}},
  {event: {kind: 'optimizer.collection', payload: {phase: 'published', publication_wait_ms: 12}}},
]);
assert.equal(measured.submittedActions, 1);
assert.equal(measured.candidateTransactionMs.count, 0);
assert.equal(measured.candidatePublicationWaitMs.p50, 12);
const actionTimings = eventPerformance([
  {event: {kind: 'optimizer.action', recorded_at: '2026-09-09T00:00:00Z', payload: {phase: 'requested', decision: {sta_mac: 'aa', target_bssid: 'bb'}}}},
  {event: {kind: 'optimizer.action', recorded_at: '2026-09-09T00:00:02Z', payload: {phase: 'submitted', decision: {sta_mac: 'aa', target_bssid: 'bb'}}}},
  {event: {kind: 'optimizer.verification', recorded_at: '2026-09-09T00:00:03Z', payload: {success: true, subject_mac: 'aa', target_bssid: 'bb'}}},
]);
assert.equal(actionTimings.submissionSeconds.p50, 2);
assert.equal(actionTimings.requestToVerificationSeconds.p50, 3);
const overlapping = eventPerformance([
  {event: {kind: 'optimizer.action', recorded_at: '2026-09-09T00:00:00Z', payload: {phase: 'requested', action_id: 'first'}}},
  {event: {kind: 'optimizer.action', recorded_at: '2026-09-09T00:00:01Z', payload: {phase: 'requested', action_id: 'second'}}},
  {event: {kind: 'optimizer.verification', recorded_at: '2026-09-09T00:00:02Z', payload: {success: true, action_id: 'first'}}},
  {event: {kind: 'optimizer.collection', payload: {phase: 'completed', transactions: [
    {operation: 'timestamp_resolution_wait', elapsed_ms: 750},
    {error: 'HTTP 503 Error_Prev_Cmd_In_Progress', native_admission_busy: true, elapsed_ms: 100},
    {error: 'HTTP 504', elapsed_ms: 8000},
  ]}}},
]);
assert.equal(overlapping.requestToVerificationSeconds.p50, 2);
assert.equal(overlapping.collectionOperationMs.timestamp_resolution_wait.p50, 750);
assert.equal(overlapping.nativeBusyRejections, 1);
assert.equal(overlapping.nativeBusyReadmissions, 1);
assert.equal(overlapping.nativeResponseTimeouts, 1);
const incomplete = eventPerformance([
  {event: {kind: 'optimizer.collection', payload: {phase: 'completed', unavailable: 'prplMesh candidate metrics incomplete after 30s'}}},
  {event: {kind: 'optimizer.collection', payload: {phase: 'completed', unavailable: 'collection_superseded'}}},
]);
assert.equal(incomplete.candidateUnavailableCollections, 1);
assert.equal(incomplete.candidateCancelledCollections, 1);
assert.equal(recordedEventKind('optimizer.verification.discarded'), true);
assert.equal(recordedEventKind('optimizer.verification'), true);
assert.equal(recordedEventKind('network.snapshot'), false);
const discarded = eventPerformance([
  {event: {kind: 'optimizer.action', payload: {phase: 'submitted', action_id: 'old-world'}}},
  {event: {kind: 'optimizer.action', payload: {phase: 'submitted', action_id: 'unmatched'}}},
  {event: {kind: 'optimizer.verification.discarded', payload: {action_id: 'old-world', success: false}}},
]);
assert.equal(discarded.discardedVerifications, 1);
assert.equal(discarded.unmatchedSubmittedActions, 1);
assert.equal(discarded.failedVerifications, 0);
assert.equal(discarded.verifiedActions, 0);
const matching = {viewMatchesRoom: true, viewMatchesModel: true, duplicates: false};
assert.equal(viewAgreement([{...matching, monoMs: 0, viewMatchesRoom: false}, {...matching, monoMs: 6000}]).passed, true);
assert.equal(viewAgreement(Array.from({length: 7}, (_, index) => ({...matching, monoMs: index * 1000, viewMatchesRoom: false}))).passed, false);
const qualification = {id: 'fixture', load: {passed: true}, initial: {passed: true},
  final: {passed: true}, playback: {completed: true}, checkpoints: [], scriptCorrect: true,
  sceneCorrect: true, viewCorrect: true, presencePhases: [], fronthaulOutages: [],
  kernel: {passed: true}, errors: []};
assert.deepEqual(qualificationFailures(qualification), []);
assert.deepEqual(qualificationFailures({...qualification, trafficExperiment: {passed: false}}), ['trafficExperiment']);
assert.deepEqual(qualificationFailures({...qualification,
  fronthaulOutages: [{samples: 4, remainingAssociations: [9000], meshConnected: true}]}), ['fronthaulOutage']);
assert.deepEqual(qualificationFailures(qualification, [{room: 'fixture'}]), ['browserErrors']);
assert.deepEqual(qualificationFailures({...qualification, initial: undefined, final: {passed: false},
  errors: ['failure']}), ['initialConvergence', 'finalConvergence', 'roomErrors']);
console.log('PASS: golden interpolation, absence, duplicates, ownership, epochs, freshness, coverage, mesh, script and percentile checks');

(async () => {
  const {reauditOwners} = require('./room-feature-acceptance.js');
  const now = () => 0;
  const owner = {passed: false, errors: [{role: 'sta_static_06', reason: 'native_owner_mismatch', expected: 'a', actual: 'b'}]};
  const agreed = {passed: true, errors: []};
  const noPause = async () => {};
  assert.deepEqual(await reauditOwners(agreed, async () => { throw new Error('no re-audit'); }, 3, 0, noPause), agreed);
  let calls = 0;
  const caught = await reauditOwners(owner, async () => { calls++; return agreed; }, 3, 0, noPause);
  assert.equal(caught.passed, true);
  assert.equal(caught.ownerReaudits, 1);
  assert.deepEqual(caught.firstOwnerErrors, owner.errors, 'the first mismatch stays in the report');
  assert.equal(calls, 1);
  calls = 0;
  const stuck = await reauditOwners(owner, async () => { calls++; return owner; }, 3, 0, noPause);
  assert.equal(stuck.passed, false, 'a model that stays wrong still fails');
  assert.equal(calls, 2);
  assert.equal(stuck.ownerAgreementMs, null);
  const other = {passed: false, errors: [{reason: 'offline_client_connected_or_unobserved'}]};
  assert.deepEqual(await reauditOwners(other, async () => agreed, 3, 0, noPause), other, 'only owner mismatches are audited again');
  const mixed = {passed: false, errors: [...owner.errors, ...other.errors]};
  assert.deepEqual(await reauditOwners(mixed, async () => agreed, 3, 0, noPause), mixed);
  void now;
  console.log('PASS: owner mismatches between a model sample and later links are audited again, boundedly');
})().catch(error => { console.error(error); process.exitCode = 1; });
