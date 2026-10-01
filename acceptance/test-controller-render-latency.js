'use strict';
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const {installObserver, paintObservations, stopTrace} = require('./controller-render-latency.js');

async function main() {
  let now = 0;
  let bssid = 'first';
  let rendered = 'first';
  const frames = [];
  const controller = {apiCall: async () => ({nodes: [{STAList: bssid ? [{staMAC: 'client', bssid}] : []}]})};
  const environment = {window: {EasyMeshController: controller},
    performance: {now: () => now}, requestAnimationFrame: callback => frames.push(callback),
    document: {querySelectorAll: () => rendered ? [{__data__: {sta: {staMAC: 'client', bssid: rendered}}}] : []}};
  vm.runInNewContext('(' + installObserver.toString() + ')()', environment);
  const sample = () => environment.window.__controllerRenderLatency();
  const frame = () => { now += 16; frames.splice(0).forEach(callback => callback()); };
  await controller.apiCall('/topology');
  assert.equal(sample().records.length, 0);
  bssid = 'second';
  await controller.apiCall('/topology');
  frame();
  assert.equal(sample().records.length, 0);
  rendered = 'second';
  frame();
  assert.equal(sample().records.length, 0);
  frame();
  assert.equal(sample().records[0].decodedResponseToSvgIdentityMs, 48);
  assert.equal(sample().records[0].passed, true);
  bssid = null;
  await controller.apiCall('/topology');
  rendered = null;
  frame(); frame();
  assert.equal(sample().records[1].to, null);
  assert.equal(sample().records[1].passed, true);
  bssid = 'third';
  await controller.apiCall('/topology');
  bssid = 'fourth';
  await controller.apiCall('/topology');
  assert.equal(sample().records[2].superseded, true);
  now += 5001;
  frame();
  assert.equal(sample().records[3].timeout, true);
  assert.equal(sample().pending.length, 0);
  assert.equal(sample().responses, 5);
  const record = {id: 1, passed: true, requestedAt: 10, receivedAt: 30,
    bounds: {left: 10, top: 20, right: 100, bottom: 200}};
  const marks = [
    {name: 'controller-latency-decoded:1', pid: 10, tid: 11, ts: 10000},
    {name: 'controller-latency-svg:1', pid: 10, tid: 11, ts: 20000},
  ];
  const paint = {name: 'Paint', ph: 'X', pid: 10, tid: 11, ts: 25000, dur: 500,
    args: {data: {clip: [0, 0, 300, 0, 300, 400, 0, 400]}}};
  const observe = extra => paintObservations([record], {traceEvents: [...marks, ...extra]})[0];
  assert.equal(observe([paint]).decodedToPaintMs, 15.5);
  assert.equal(observe([paint]).requestToPaintMs, 35.5);
  assert.equal(paintObservations([{...record, responseId: 9}], {traceEvents: [
    {...marks[0], name: 'controller-latency-decoded:9'}, marks[1], paint,
  ]})[0].decodedToPaintMs, 15.5);
  assert.equal(observe([]).paintObserved, false);
  assert.equal(observe([{...paint, pid: 12}]).paintObserved, false);
  assert.equal(observe([{...paint, tid: 12}]).paintObserved, false);
  assert.equal(observe([{...paint, ts: 15000}]).paintObserved, false);
  assert.equal(observe([{...paint, ts: 600000}]).paintObserved, false);
  assert.equal(observe([{...paint, ts: NaN}]).paintObserved, false);
  assert.equal(observe([{...paint, dur: -1}]).paintObserved, false);
  assert.equal(observe([{...paint, args: {data: {clip: [0, 0, 5, 0, 5, 5, 0, 5]}}}]).paintObserved, false);
  assert.equal(paintObservations([{...record, passed: false}], {traceEvents: [...marks, paint]})[0].paintObserved, false);
  const session = new EventEmitter();
  let closed = false;
  session.send = async method => {
    if (method === 'Tracing.end') session.emit('Tracing.tracingComplete', {stream: 'trace', dataLossOccurred: true});
    if (method === 'IO.read') return {data: Buffer.from('{"traceEvents":[]}').toString('base64'), base64Encoded: true, eof: true};
    if (method === 'IO.close') closed = true;
  };
  assert.equal((await stopTrace(session)).dataLossOccurred, true);
  assert.equal(closed, true);
  await assert.rejects(stopTrace(session, 50, 1), /byte budget/);
  assert.equal(closed, true);
  assert.equal(session.listenerCount('Tracing.tracingComplete'), 0);
  session.send = async () => {};
  await assert.rejects(stopTrace(session, 1), /timed out/);
  assert.equal(session.listenerCount('Tracing.tracingComplete'), 0);
  session.send = async () => { throw new Error('Disconnected'); };
  await assert.rejects(stopTrace(session, 50), /Disconnected/);
  assert.equal(session.listenerCount('Tracing.tracingComplete'), 0);
  console.log('PASS: render timing waits for two matching frames, tracks removals, supersession and timeouts');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
