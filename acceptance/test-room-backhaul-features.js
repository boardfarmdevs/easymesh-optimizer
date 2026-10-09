'use strict';

const assert = require('assert').strict;
const fs = require('fs');
const path = require('path');
const harness = fs.readFileSync(path.join(__dirname, 'room-backhaul-features.js'), 'utf8');
assert.match(harness, /browserHost: os\.hostname\(\), labHost: options\.host, vm: options\.vm/);
assert.match(harness, /hostMonitor = await startHostMonitor\(options\.host, directory, \{processes: true\}\);\s*report\.before = await identity\(\)/);
assert.match(harness, /report\.hostMonitor = await hostMonitor\.stop\(\)/);
assert.match(harness, /if \(report\.hostMonitor\.error\) report\.errors\.push/);
assert.ok(harness.includes("document.fullscreenElement ? '#fullscreenPlay' : '#play'"));
assert.match(harness, /async function load\(id\)[\s\S]*?fullscreenElement[\s\S]*?#roomFullscreen[\s\S]*?changed = true/);
assert.match(harness, /await roomPage\.waitForFunction\(\(\) => !document\.fullscreenElement\);\s*changed = true/);
assert.match(harness, /await roomPage\.waitForFunction\(\(\) => document\.fullscreenElement\?\.id === 'roomView'\);\s*return result/);
const {interfaceState, summarizeNative, stackProfile, ready, parentPaths, nativeCycles} = require('./room-backhaul-features.js');
assert.deepEqual(nativeCycles({first: 'root', second: 'first', isolated: null}), []);
assert.deepEqual(nativeCycles({first: 'second', second: 'first'}), [['first', 'second']]);
assert.deepEqual(nativeCycles({first: 'second', second: 'third', third: 'first'}), [['first', 'second', 'third']]);
assert.deepEqual(nativeCycles({first: 'first'}), [['first']]);
assert.match(harness, /if \(flavor === 'prpl'\) assert\.deepEqual\(currentRoom\.nativeOutcome\.observedCycles, \[\]/);
assert.ok(harness.includes('/tmp/backhaul-native-probe.py'));
assert.equal(stackProfile('rdk').gateway, '10.0.0.1');
assert.equal(stackProfile('prpl').gateway, '192.168.77.1');
assert.equal(stackProfile('prpl').healthNodes, 5);
assert.equal(stackProfile('prpl').containers.extender_3, 'prpl-agent-03');
assert.throws(() => stackProfile('unknown'));
const healthy = {native: {
  nodes: Object.fromEntries(['gateway', 'extender_1', 'extender_2', 'extender_3', 'extender_4']
    .map(role => [role, {pingOk: true, fronthaulAps: 6, apOperating: true}])),
  parents: {extender_1: 'gateway', extender_2: 'gateway', extender_3: 'extender_1', extender_4: 'extender_2'}},
  health: {healthy: true, topology_nodes: 5, api_active: 10},
  optimizer: {fleet: {converged: true}},
  topology: {nodes: Array(6).fill({}), stations: Array.from({length: 10}, (_, index) => ({mac: String(index)}))}};
assert.equal(ready(healthy, 5), true);
const diagnostics = require('./room-backhaul-features.js').convergenceDiagnostics(healthy);
assert.equal(diagnostics.healthy, true);
assert.equal(diagnostics.activeClients, 10);
assert.equal(diagnostics.fleet.converged, true);
assert.equal(diagnostics.nodes.extender_4.apOperating, true);
assert.equal(diagnostics.nodes.extender_4.pingOk, true);
assert.equal(Object.keys(diagnostics.nodes).length, 5);
assert.equal(ready(healthy, 6), false);
assert.equal(ready({...healthy, native: {...healthy.native, nodes: {}}}, 5), false);
assert.equal(ready({...healthy, native: {...healthy.native, parents: {...healthy.native.parents, extender_4: null}}}, 5), false);
assert.equal(ready({...healthy, optimizer: {fleet: {converged: false}}}, 5), false);
assert.deepEqual(parentPaths(healthy.native.parents).extender_3, ['extender_3', 'extender_1', 'gateway']);
for (const parents of [
  {...healthy.native.parents, extender_1: 'extender_1'},
  {...healthy.native.parents, extender_1: 'extender_3', extender_2: 'extender_4'},
  {extender_1: 'extender_2', extender_2: 'extender_3', extender_3: 'extender_1', extender_4: 'extender_1'},
  {...healthy.native.parents, extender_1: 'unknown'},
]) {
  assert.equal(ready({...healthy, native: {...healthy.native, parents}}, 5), false,
    'Connected parent observations and cached successful pings cannot qualify a disconnected or cyclic path');
}
assert.deepEqual(parentPaths({extender_1: 'extender_3', extender_3: 'extender_1'}).extender_1,
  ['extender_1', 'extender_3', 'extender_1']);
assert.deepEqual(parentPaths({extender_1: null}).extender_1, ['extender_1', null]);
assert.deepEqual(parentPaths({}), {});
assert.equal(ready({...healthy, native: {...healthy.native,
  parents: {extender_1: 'gateway', extender_2: 'extender_1', extender_3: 'extender_2', extender_4: 'extender_3'}}}, 5), true);
const inactive = 'Interface wifi1.1\n addr 02:00:00:00:01:01\n type AP\n' +
  'Connected to 02:00:00:00:02:02 (on wifi1.3)\n SSID: mesh_backhaul\n freq: 5180\nPROBE_EXIT=0\n';
assert.equal(interfaceState(inactive).apOperating, false, 'STA SSID does not prove its backhaul AP is running');
assert.equal(interfaceState(inactive + '\nFRONTHAUL_APS=6\n').fronthaulAps, 6);
assert.ok(Number.isNaN(interfaceState(inactive).fronthaulAps), 'Missing AP observation must not imply readiness');
assert.equal(interfaceState(inactive).parentBssid, '02:00:00:00:02:02');
assert.equal(interfaceState(inactive).pingOk, true);
const active = inactive.replace(' type AP', ' ssid mesh_backhaul\n channel 36 (5180 MHz), width: 20 MHz\n type AP');
assert.equal(interfaceState(active).apOperating, true);
assert.equal(interfaceState('Not connected.\nPROBE_EXIT=1\n').parentBssid, null);
assert.equal(interfaceState('Not connected.\nPROBE_EXIT=1\n').pingOk, false);
const observed = [{native: {parents: {extender_3: 'extender_1', extender_4: 'extender_2'}, nodes: {extender_4: {pingOk: true}}}}];
assert.equal(summarizeNative(observed, 'backhaul-branch-formation').branchObserved, true);
assert.equal(summarizeNative(observed, 'backhaul-parent-handover').lowerRelayObserved, false);
assert.equal(summarizeNative(observed, 'backhaul-wired-parent').wiredParentObserved, false);
assert.equal(summarizeNative([{native: {parents: {extender_3: 'extender_5'}, nodes: {}}}], 'backhaul-wired-parent').wiredParentObserved, true);
assert.equal(summarizeNative([{native: {parents: {}, nodes: {extender_4: {pingOk: null}}}}],
  'backhaul-isolation-recovery').upstreamOutageObserved, false, 'Missing observations are not successful isolation');
assert.equal(summarizeNative([{native: {parents: {extender_4: null}, nodes: {extender_4: {pingOk: false}}}}],
  'backhaul-isolation-recovery').upstreamOutageObserved, true);
assert.equal(summarizeNative([{native: {parents: {extender_4: 'gateway'}, nodes: {extender_4: {pingOk: false}}}}],
  'backhaul-isolation-recovery').upstreamOutageObserved, false, 'One lost ping is not proof of backhaul isolation');
const {meshComplete, meshLoss} = require('./room-backhaul-features.js');
assert.equal(meshComplete(healthy, 5), true);
const dropped = {...healthy, health: {...healthy.health, topology_nodes: 4}, topology: {...healthy.topology, nodes: Array(5).fill({})}};
assert.equal(meshComplete(dropped, 5), false);
assert.equal(meshComplete({...healthy, topology: {...healthy.topology, nodes: Array(5).fill({})}}, 5), false,
  'The topology view alone missing a node is a loss');
assert.equal(meshLoss([healthy, dropped, healthy, dropped, dropped, healthy], 5), -1, 'Blips of one or two samples are not a loss');
assert.equal(meshLoss([healthy, dropped, dropped, dropped, healthy], 5), 3);
assert.equal(meshLoss([], 5), -1);
assert.match(harness, /report\.startParents = \(await sample\('start', true\)\)\.native\.parents;\s*for \(const id of selectedRooms\)/);
assert.match(harness, /if \(report\.recoveryPassed\) \{\s*const hold = [\s\S]*?meshLoss\(entries, profile\.healthNodes\) !== -1\) \{\s*report\.recoveryPassed = false;/);
assert.match(harness, /util\.isDeepStrictEqual\(entry\.native\.parents, report\.startParents\)[\s\S]*?until = Math\.min\(limit, Date\.now\(\) \+ HOLD_AFTER_START_PARENTS_MS\)/);
const {podState, podChain, podsUnchained} = require('./room-backhaul-features.js');
const chainedPod = podState('AP=02:00:00:00:22:24\nSTATION=bhaul-sta-50 \nSTATION=bhaul-sta-24 Connected to 02:00:00:00:21:24 (on bhaul-sta-24)\n');
assert.deepEqual(chainedPod, {apBssid: '02:00:00:00:22:24', station: 'bhaul-sta-24', parentBssid: '02:00:00:00:21:24',
  connectedStations: 1});
assert.deepEqual(podState('AP=\nSTATION=bhaul-sta-50 Not connected.\nSTATION=bhaul-sta-24 \n'),
  {apBssid: null, station: null, parentBssid: null, connectedStations: 0});
const chain = {pods: {pod_1: {parent: 'extender_1', station: 'bhaul-sta-50'}, pod_2: {parent: 'pod_1', station: 'bhaul-sta-24'}},
  mesh: {backhaul_edges: [{child_role: 'pod_1', parent_role: 'extender_1'}, {child_role: 'pod_2', parent_role: 'pod_1'}]}};
assert.equal(podChain(chain), true);
assert.equal(podChain({...chain, mesh: {backhaul_edges: [{child_role: 'pod_2', parent_role: 'gateway'}]}}), false,
  'The controller must model pod_2 under pod_1 too');
assert.equal(podChain({...chain, pods: {...chain.pods, pod_2: {parent: 'pod_1', station: 'bhaul-sta-50'}}}), false);
assert.equal(podChain({...chain, pods: {...chain.pods, pod_1: {parent: 'pod_2'}}}), false, 'pod_1 must be under a native AP');
assert.equal(podsUnchained(chain), false);
assert.equal(podsUnchained({pods: {pod_1: {parent: 'extender_1'}, pod_2: {parent: 'gateway'}}}), true);
assert.equal(podsUnchained({pods: {pod_1: {parent: 'extender_1'}, pod_2: {parent: null}}}), false, 'A pod without a parent is not recovered');
assert.equal(podsUnchained({}), true, 'A lab without pods');
assert.match(harness, /if \(ready\(recovery, profile\.healthNodes, 20\) && podsUnchained\(recovery\)\)/);
console.log('PASS: backhaul AP readiness, native parents, traffic, missing-observation classification, the recovery hold and the pod chain');
