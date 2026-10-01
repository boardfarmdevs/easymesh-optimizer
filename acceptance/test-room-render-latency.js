'use strict';

const assert = require('node:assert/strict');
const vm = require('node:vm');
const {installRoomObserver, roomPresentationObservations, nativeRoomObservations} = require('./room-render-latency.js');
const identity = {pid: 10, tid: 11, id2: {local: '0x8'}};
const events = [
  {...identity, name: 'ProxyMain::BeginMainFrame', ph: 'X', ts: 10, dur: 15, args: {begin_frame_id: 20}},
  {...identity, name: 'ProxyMain::BeginMainFrame', ph: 's', id: 7, ts: 10},
  {...identity, name: 'ProxyMain::BeginMainFrame', ph: 'f', id: 7, ts: 40},
  {...identity, name: 'ProxyMain::BeginMainFrame::commit', ph: 'X', ts: 40},
  {...identity, name: 'AnimationFrame', ph: 'b', ts: 5,
    args: {id: '123abc', animation_frame_timing_info: {begin_frame_id: {source_id: 99, sequence_number: 20}}}},
  {...identity, name: 'AnimationFrame', ph: 'e', ts: 25},
  {...identity, name: 'AnimationFrame::Presentation', ph: 'n', ts: 50,
    args: {id: '123abc', begin_frame_id: {source_id: 99, sequence_number: 21}}},
  {...identity, name: 'room-latency-submitted:1', ph: 'R', ts: 20},
  {...identity, name: 'DrawingBuffer::prepareMailbox', ph: 'X', ts: 30, dur: 5},
];
const record = {id: 1, passed: true, receivedAt: 10, submittedAt: 11, markedAt: 11};
const check = traceEvents => roomPresentationObservations([record], {traceEvents})[0];
assert.equal(check(events).presentationObserved, true);
assert.ok(Math.abs(check(events).receivedToPresentationMs - 1.03) < 1e-9);
for (let index = 0; index < events.length; index++) assert.equal(check(events.filter((_, position) => index !== position)).presentationObserved, false);
assert.equal(check([...events, events.at(-1)]).presentationObserved, false);
assert.equal(check([...events, events.at(-2)]).presentationObserved, false);
assert.equal(check([...events, {...events.at(-2), name: 'room-latency-submitted:2', ts: 21}]).superseded, true);
for (const fields of [{pid: 12}, {tid: 12}, {ts: 19}, {ts: 41}, {dur: -1}, {dur: NaN}]) {
  assert.equal(check([...events.slice(0, -1), {...events.at(-1), ...fields}]).presentationObserved, false);
}

let observer;
let now = 100;
const meter = {rssiLevel: () => 2, segmentColor: index => index < 2 ? '#dc2626' : '#d1d5db'};
const marks = [];
const canvas = {getBoundingClientRect: () => ({width: 100, height: 100})};
const window = {__viewer: {observeFrames: value => { observer = value; }}, EasyMeshSignalMeter: meter};
const context = {window, document: {querySelectorAll: () => [canvas], fullscreenElement: canvas},
  performance: {now: () => now, mark: name => { marks.push(name); return {startTime: now}; }}};
vm.runInNewContext('(' + installRoomObserver.toString() + ')()', context);
const bars = Array.from({length: 10}, (_, index) => ({visible: true,
  material: {opacity: 1, color: {getHexString: () => meter.segmentColor(index).slice(1)}}}));
const node = {grp: {position: {x: 1, z: -2}}, post: {position: {y: 0.5}}, gauge: {bars, group: {userData: {signalLevel: 2}}}};
const peer = {grp: {position: {x: 3, z: -4}}, post: {position: {y: 1}}};
const renderer = {domElement: canvas, info: {render: {calls: 5}}, getContext: () => ({isContextLost: () => false})};
const snapshot = {renderer, nodes: {station: node, ap: peer},
  actual: {geometry: {getAttribute: () => ({array: [3, 2.12, -4, 1, 1.12, -2]}), drawRange: {count: 2}}},
  network: {clients: [{role: 'station', sta_mac: 'sta', connected_role: 'ap', connected_bssid: 'bss', rssi_dbm: -80, metric_observed_at: '2026-09-13T00:00:00Z'}]},
  frame: {sequence: 1, received_at: 90}, clock: {serverMs: Date.parse('2026-09-13T00:00:01Z'), receivedMs: 99}};
observer(snapshot);
observer(snapshot);
snapshot.frame = {sequence: 2, received_at: 99};
snapshot.network.clients[0].rssi_dbm = -79;
observer(snapshot);
node.gauge.bars[0].material.opacity = 0;
snapshot.frame.sequence = 3;
observer(snapshot);
now = 110;
const result = window.__stopRoomRenderLatency();
assert.equal(observer, null);
assert.equal(result.records.length, 3);
assert.equal(result.records[0].passed, true);
assert.equal(result.records[0].changed, false);
assert.equal(result.records[1].changed, true);
assert.equal(result.records[2].passed, false);
assert.equal(marks.length, 3);
assert.equal(result.overflow, false);
const clocks = {browser: [{before: 0, after: 0, remote: 0}], guest: [{before: 0, after: 0, remote: 0}]};
const gauge = {mac: 'station', bssid: 'ap', rcpi: 100, rssi: -59, level: 2, source: 'associated_sta_link_metrics', fresh: true, passed: true};
const roomRecords = [{id: 1, receivedAt: 100, gauges: [gauge]},
  {id: 2, receivedAt: 120, passed: true, presentationObserved: true, receivedToPresentationMs: 3,
    gauges: [{...gauge, rcpi: 102}]}];
const metric = {kind: 'metric', sta: 'station', bssid: 'ap', rcpi: 102, monotonic_ns: 110000000};
const join = (events, records = roomRecords) => nativeRoomObservations(records, events, clocks)[0];
assert.equal(join([metric]).nativeObserved, true);
assert.equal(join([metric]).visualChanged, false);
assert.ok(join([metric]).nativeToPresentationMs.lower > 12.9);
assert.equal(join([metric, {...metric, monotonic_ns: 115000000}]).nativeObserved, true);
assert.equal(join([]).nativeObserved, false);
assert.equal(join([{...metric, bssid: 'wrong'}]).nativeObserved, false);
assert.equal(join([{...metric, monotonic_ns: 200000000}]).nativeObserved, false);
assert.equal(join([metric, {...metric, rcpi: 104, monotonic_ns: 112000000}, {...metric, monotonic_ns: 115000000}]).nativeObserved, false);
for (const fields of [{fresh: false}, {passed: false}, {source: 'iw_fallback'}, {rssi: -40}, {rssi: null}]) {
  assert.equal(join([metric], [roomRecords[0], {...roomRecords[1], gauges: [{...gauge, rcpi: 102, ...fields}]}]).nativeObserved, false);
}
for (const rcpi of [0, 221, 102.5, null]) {
  assert.equal(join([{...metric, rcpi}], [roomRecords[0], {...roomRecords[1], gauges: [{...gauge, rcpi}]}]).nativeObserved, false);
}
assert.equal(nativeRoomObservations(roomRecords, [metric], {...clocks, guest: [{before: 0, after: 6, remote: 0}]})[0].nativeObserved, false);
assert.deepEqual(nativeRoomObservations([roomRecords[0], {...roomRecords[1], gauges: []}, roomRecords[1]], [metric], clocks), []);
console.log('PASS: room WebGL checks actual gauges and association geometry, unique mailbox/frame presentation, loss and supersession');
