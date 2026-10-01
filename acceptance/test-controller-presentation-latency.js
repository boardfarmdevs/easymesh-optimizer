'use strict';

const assert = require('node:assert/strict');
const {presentationFrames, presentationObservation} = require('./controller-presentation-latency.js');
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
];
const record = {paintObserved: true, decodedToPaintMs: 1,
  paint: {pid: 10, tid: 11, timestampUs: 30, durationUs: 5}};
const check = input => presentationObservation(record, presentationFrames({traceEvents: input}));
assert.equal(check(events).presentationObserved, true);
assert.equal(check(events).decodedToPresentationMs, 1.015);
for (let index = 0; index < events.length; index++) {
  assert.equal(check(events.filter((_, position) => position !== index)).presentationObserved, false);
}
assert.equal(check([...events, events.at(-1)]).presentationObserved, false);
assert.equal(check([...events, events[1]]).presentationObserved, false);
for (const fields of [{pid: 12}, {tid: 12}, {ts: 34}, {ts: 1000030}, {ts: NaN},
  {args: {id: 'wrong', begin_frame_id: {source_id: 99, sequence_number: 21}}},
  {args: {id: '123abc', begin_frame_id: {source_id: 0, sequence_number: 0}}}]) {
  assert.equal(check([...events.slice(0, -1), {...events.at(-1), ...fields}]).presentationObserved, false);
}
assert.equal(presentationObservation({...record, paint: {...record.paint, timestampUs: 41}},
  presentationFrames({traceEvents: events})).presentationObserved, false);
assert.equal(presentationObservation({...record, paintObserved: false}, []).presentationObserved, false);
const collapsed = events.filter(event => !(event.name === 'AnimationFrame' && event.ph === 'e')).map(event =>
  event.name === 'AnimationFrame' && event.ph === 'b' ? {...event, ph: 'n', args: {...event.args,
    animation_frame_timing_info: {...event.args.animation_frame_timing_info, duration_ms: 0}}} : event);
collapsed.push({...identity, name: 'AnimationFrame::Render', ph: 'b', ts: 12},
  {...identity, name: 'AnimationFrame::Render', ph: 'e', ts: 25});
assert.equal(check(collapsed).presentationObserved, true);
assert.equal(check(collapsed.slice(0, -1)).presentationObserved, false);
assert.equal(check([...collapsed, collapsed.at(-2)]).presentationObserved, false);
const wrongDuration = collapsed.map(event => event.name === 'AnimationFrame' ? {...event,
  args: {...event.args, animation_frame_timing_info: {...event.args.animation_frame_timing_info, duration_ms: 1}}} : event);
assert.equal(check(wrongDuration).presentationObserved, false);
console.log('PASS: Paint joins exact main-frame/commit flow and presentation identity; missing, duplicate and wrong frames fail closed');
