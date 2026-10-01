'use strict';
const assert = require('node:assert/strict');
const {trafficExperimentSummary, recordedEventKind} = require('./room-feature-acceptance.js');
const phase = {role: 'client', packets_per_second: 10, payload_bytes: 1200};
const world = {golden_sha256: 'world', traffic_experiment: {phases: [phase]}};
const result = {world_sha256: 'world', key: ['world', 1, 0], role: 'client',
  source: 'bound_client_wlan0_icmp_echo', state: 'completed', requested_packets: 50,
  transmitted_packets: 49, received_echo_replies: 48};
const event = payload => ({event: {kind: 'traffic.experiment', payload: {world_sha256: 'world', ...payload}}});
const records = [event({...result, state: 'running', requested_packets_per_second: 10, payload_bytes: 1200}),
  event({state: 'completed', history: [result]}), event({state: 'off', history: [result]})];
assert.equal(recordedEventKind('traffic.experiment'), true);
assert.equal(trafficExperimentSummary(world, records).passed, true);
assert.equal(trafficExperimentSummary(world, []).passed, false);
assert.equal(trafficExperimentSummary(world, records.slice(0, 2)).passed, false);
for (const change of [{received_echo_replies: 0}, {transmitted_packets: null},
  {requested_packets: 40}, {state: 'failed'}, {role: 'other'}, {world_sha256: 'old'}]) {
  assert.equal(trafficExperimentSummary(world, [records[0], event({state: 'off', history: [{...result, ...change}]})]).passed, false);
}
const udpPhase = {role: 'client', mode: 'udp', offered_mbps: 8, payload_bytes: 1200};
const udpWorld = {...world, traffic_experiment: {phases: [udpPhase]}};
const udpResult = {world_sha256: 'world', key: ['world', 1, 0], role: 'client', mode: 'udp',
  source: 'bound_client_wlan0_udp_iperf3', state: 'completed', requested_offered_mbps: 8, payload_bytes: 1200,
  source_interface: 'wlan0', port: 55204,
  sender: {status: 'complete', returncode: 0, source: 'iperf3_sender_json', bytes: 12000, packets: 10,
    seconds: 1, bits_per_second: 96000},
  receiver: {status: 'complete', returncode: 0, source: 'iperf3_receiver_json', bytes: 10800, packets: 10,
    seconds: 1.1, goodput_bits_per_second: 78545, lost_packets: 1, loss_percent: 10}};
const udpStart = event({...udpResult, state: 'running'});
const udpRecords = [udpStart, event({state: 'off', history: [udpResult]})];
assert.equal(trafficExperimentSummary(udpWorld, udpRecords).passed, true);
assert.equal(trafficExperimentSummary(udpWorld, []).phases[0].result.state, 'missing');
assert.equal(trafficExperimentSummary(udpWorld, []).passed, false);
for (const change of [{state: 'cancelled'}, {mode: 'icmp'}, {source_interface: 'eth0'}, {port: 5201},
  {key: ['world', 2, 0]}, {requested_offered_mbps: 9}, {sender: undefined}, {receiver: undefined},
  {sender: {...udpResult.sender, status: 'partial'}}, {receiver: {...udpResult.receiver, status: 'missing'}},
  {receiver: {...udpResult.receiver, goodput_bits_per_second: null}},
  {receiver: {...udpResult.receiver, goodput_bits_per_second: NaN}},
  {receiver: {...udpResult.receiver, returncode: 1}}, {receiver: {...udpResult.receiver, bytes: 0}},
  {receiver: {...udpResult.receiver, seconds: 0}}, {receiver: {...udpResult.receiver, source: 'configured_scalar'}},
  {receiver: {...udpResult.receiver, lost_packets: null}}, {receiver: {...udpResult.receiver, loss_percent: 20}}]) {
  assert.equal(trafficExperimentSummary(udpWorld, [udpStart,
    event({state: 'off', history: [{...udpResult, ...change}]})]).passed, false, JSON.stringify(change));
}
assert.equal(trafficExperimentSummary(udpWorld, [...udpRecords, udpRecords[0]]).passed, false);
const zeroLoss = {...udpResult, receiver: {...udpResult.receiver, lost_packets: 0, loss_percent: 0}};
assert.equal(trafficExperimentSummary(udpWorld, [udpStart, event({state: 'off', history: [zeroLoss]})]).passed, true);
const boundary = {...udpResult, sender: {...udpResult.sender, bytes: 10800},
  receiver: {...udpResult.receiver, bytes: 12000, lost_packets: 0, loss_percent: 0}};
const boundaryReport = trafficExperimentSummary(udpWorld, [udpStart, event({state: 'off', history: [boundary]})]);
assert.equal(boundaryReport.passed, true);
assert.equal(boundaryReport.phases[0].accounting.state, 'one-datagram-accounting-boundary');
assert.equal(boundaryReport.phases[0].result.sender.bytes, 10800);
assert.equal(boundaryReport.phases[0].result.receiver.bytes, 12000);
for (const change of [
  {sender: {...boundary.sender, bytes: 9600}},
  {sender: {...boundary.sender, bytes: 10801}},
  {receiver: {...boundary.receiver, packets: 11}},
  {receiver: {...boundary.receiver, lost_packets: 1, loss_percent: 10}},
  {receiver: {...boundary.receiver, bytes: 13200}}
]) {
  assert.equal(trafficExperimentSummary(udpWorld, [udpStart,
    event({state: 'off', history: [{...boundary, ...change}]})]).passed, false, JSON.stringify(change));
}
console.log('PASS: ICMP compatibility and measured UDP endpoints, loss, identity and off-state required');
