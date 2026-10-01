'use strict';

const assert = require('node:assert/strict');
const vm = require('node:vm');
const {installMetricObserver, nativeMetricObservations} = require('./controller-metric-latency.js');
const meter = require('../../medium/configurator/worlds/viewer/signal-meter.js');

async function main() {
  let now = 0;
  let rcpi = 120;
  let renderedRcpi = 120;
  let bssid = 'ap';
  let wrongFill = false;
  const frames = [];
  const controller = {apiCall: async () => ({clients: [{mac: 'client', connected_bssid: bssid,
    client_metrics: {rcpi, last_updated: new Date().toISOString()}}]})};
  const original = controller.apiCall;
  const environment = {window: {EasyMeshController: controller, EasyMeshSignalMeter: meter},
    performance: {now: () => now, mark: () => {}}, requestAnimationFrame: callback => frames.push(callback),
    document: {querySelectorAll: () => [{__data__: {sta: {staMAC: 'client', bssid},
      signal: {rcpi: renderedRcpi, available: true}},
    querySelector: () => ({getBoundingClientRect: () => ({left: 1, top: 2, right: 3, bottom: 4})}),
    querySelectorAll: () => Array.from({length: 10}, (_, index) => ({getAttribute: () => wrongFill ? 'wrong' :
      meter.segmentColor(index, meter.rssiLevel(Math.round(renderedRcpi / 2 - 110)))}))}]}};
  vm.runInNewContext('(' + installMetricObserver.toString() + ')()', environment);
  const sample = () => environment.window.__controllerRenderLatency();
  const frame = () => { now += 16; frames.splice(0).forEach(callback => callback()); };
  await controller.apiCall('/clients');
  rcpi = 100;
  await controller.apiCall('/clients');
  frame();
  assert.equal(sample().records.length, 0);
  renderedRcpi = rcpi;
  wrongFill = true;
  frame(); frame();
  assert.equal(sample().records.length, 0);
  wrongFill = false;
  frame(); frame();
  assert.equal(sample().records[0].passed, true);
  assert.equal(sample().records[0].visualChanged, true);
  rcpi = 102;
  await controller.apiCall('/clients');
  renderedRcpi = rcpi;
  frame(); frame();
  assert.equal(sample().records[1].visualChanged, false);
  rcpi = 80;
  await controller.apiCall('/clients');
  rcpi = 60;
  await controller.apiCall('/clients');
  assert.equal(sample().records[2].superseded, true);
  bssid = 'new-ap';
  await controller.apiCall('/clients');
  assert.equal(sample().records[3].associationChanged, true);
  assert.equal(sample().pending.length, 0);
  environment.window.__stopControllerRenderLatency();
  assert.equal(controller.apiCall, original);

  const clocks = {guest: [{before: 0, remote: 0, after: 0}], browser: [{before: 0, remote: 0, after: 0}]};
  const event = (time, rcpi, bssid = 'ap') => ({kind: 'metric', monotonic_ns: time * 1e6,
    sta: 'client', bssid, rcpi, associated: true});
  const record = {mac: 'client', bssid: 'ap', rcpi: 100, fromRcpi: 120, previousRequestedAt: 5,
    requestedAt: 15, receivedAt: 20, presentationObserved: true, decodedToPresentationMs: 10};
  const match = events => nativeMetricObservations([record], events, clocks)[0];
  const result = match([event(10, 100), event(12, 100)]);
  assert.equal(result.nativeObserved, true);
  assert.equal(result.nativeCommit.monotonic_ns, 10000000);
  assert.ok(result.nativeToPresentationMs.lower < 20 && result.nativeToPresentationMs.upper > 20);
  assert.equal(match([]).nativeObserved, false);
  assert.equal(match([event(30, 100)]).nativeObserved, false);
  assert.equal(match([event(10, 100, 'wrong')]).nativeObserved, false);
  assert.equal(match([event(10, 100), event(12, 80)]).nativeObserved, false);
  assert.equal(match([event(10, 100), event(12, 80), event(14, 100)]).nativeMatches, 2);
  assert.equal(match([event(10, 100), {...event(11, 100, 'wrong'), kind: 'commit'}]).nativeObserved, false);
  assert.equal(match([event(10, 100), {...event(11, 100), kind: 'commit', associated: false}]).nativeObserved, false);
  const overlappingRoam = match([event(10, 100), {...event(18, 100, 'new-ap'), kind: 'commit'}]);
  assert.equal(overlappingRoam.nativeObserved, true);
  assert.equal(overlappingRoam.associationRacedRequest, true);
  const overlappingMetric = match([event(10, 100), event(18, 80)]);
  assert.equal(overlappingMetric.nativeObserved, true);
  assert.equal(overlappingMetric.metricRacedRequest, true);
  console.log('PASS: ordinary RCPI changes, real SVG fills, unchanged meters, supersession and unambiguous native ownership');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
