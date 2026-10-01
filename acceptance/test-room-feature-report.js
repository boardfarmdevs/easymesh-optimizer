'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {hostSummary, qualificationPassed, summarize} = require('./room-feature-report.js');
const {fronthaulOutages} = require('./room-feature-acceptance.js');
const start = '2026-09-12T00:00:00Z';
const end = '2026-09-12T00:00:10Z';
const rows = [2, 8].map((seconds, index) => ({
  time: '2026-09-12T00:00:0' + seconds + 'Z',
  package_throttle_count: 20 + index,
  package_throttle_total_time_ms: 100 + index * 12,
  sampling_elapsed_ms: 3,
  available_memory_kib: 1048576,
}));
const result = hostSummary(rows, start, end);
assert.equal(result.packageThrottleTimeMs, 12);
assert.equal(result.packageThrottleMeasuredSeconds, 6);
assert.equal(result.packageThrottleWindowPercent, 0.2);
assert.equal(result.samplingElapsedMs.max, 3);
assert.equal(hostSummary(rows.map(row => ({...row, package_throttle_total_time_ms: null})), start, end).packageThrottleTimeMs, null);
assert.equal(hostSummary([rows[0], {...rows[1], package_throttle_total_time_ms: 0}], start, end).packageThrottleTimeMs, null);
assert.equal(hostSummary([rows[0]], start, end).packageThrottleWindowPercent, null);
assert.equal(hostSummary(rows.map(row => ({...row, package_throttle_total_time_ms: 100})), start, end).packageThrottleTimeMs, 0);
console.log('PASS: thermal time uses measured coverage, preserves zero and rejects unsupported/reset counters');

const world = {roles: {extender_4: 'fronthaul_ap'}, duration_ms: 90000, generations: [
  {time_ms: 0, present: {extender_4: true}}, {time_ms: 20000, present: {extender_4: false}},
  {time_ms: 60000, present: {extender_4: true}},
]};
const outageSample = (time, ap) => ({phase: 'playing', playback: {time_ms: time},
  roomAssociations: [{ap}], meshConnected: true, meshViewMatches: true});
assert.deepEqual(fronthaulOutages(world, [outageSample(24000, 'extender_4'), outageSample(25000, 'extender_4'),
  outageSample(26000, 'gateway'), outageSample(60000, 'extender_4')]), [{role: 'extender_4', startMs: 20000,
    endMs: 60000, samples: 2, remainingAssociations: [25000], meshConnected: true}]);
assert.equal(fronthaulOutages(world, [outageSample(25000, 'gateway')])[0].remainingAssociations.length, 0);
assert.equal(fronthaulOutages(world, [outageSample(24000, 'extender_4')])[0].samples, 0);
assert.equal(fronthaulOutages(world, [{...outageSample(25000, 'gateway'), meshViewMatches: false}])[0].meshConnected, false);
const qualified = {tested: 14, passed: 14, errors: [], eventGaps: [], nativeIdentitiesUnchanged: true,
  restoration: {applied: true, convergence: {passed: true}}, host: {samplingComplete: true}};
assert.equal(qualificationPassed(qualified), true);
for (const change of [{tested: 0, passed: 0}, {passed: 13}, {failure: 'failed'}, {errors: ['failed']},
  {eventGaps: ['lost']}, {nativeIdentitiesUnchanged: false}, {restoration: {}}, {host: {samplingComplete: false}}]) {
  assert.equal(qualificationPassed({...qualified, ...change}), false);
}
console.log('PASS: the five-second fronthaul outage gate and qualification failures cannot report success');

const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'band-report-'));
try {
  const write = (name, value) => fs.writeFileSync(path.join(directory, name), JSON.stringify(value));
  const golden = {name: 'band', roles: {client: 'station'}, duration_ms: 1000,
    generations: [{time_ms: 0, present: {client: true}}],
    band_steering: {client: {allowed_bands: ['2.4', '5'], initial_band: '2.4'}},
    band_steering_expectations: [{time_ms: 0, roles: {client: {band: '5', ap: 'gateway'}}}]};
  write('band.world.json', golden);
  write('report.json', {started: start, finished: end, errors: [], eventGaps: [], rooms: [{id: 'band', passed: true,
    bandSteering: {passed: true}, load: {passed: true}, initial: {passed: true}, final: {passed: true},
    playback: {completed: true}, checkpoints: [], scriptCorrect: true, sceneCorrect: true,
    presencePhases: [{topologyVerified: true}], kernel: {passed: true}, errors: []}]});
  write('band-loaded.json', {current: {network: {mesh: {nodes: [{role: 'gateway', device_id: 'device'}]}}}});
  const samples = [0, 1000].map(time => ({monoMs: time, wallTime: start, phase: 'playing', playback: {time_ms: time},
    scriptErrors: [], sceneErrors: [], meshCount: 6, duplicates: false, viewMatchesRoom: true,
    viewMatchesModel: true, meshViewMatches: true, roster: true,
    associations: [{mac: 'aa', bssid: 'bb', owner: 'device', visible: true, label: 'Client'}],
    roomAssociations: [{mac: 'aa', bssid: 'bb', ap: 'gateway'}]}));
  fs.writeFileSync(path.join(directory, 'band-samples.jsonl'), samples.map(sample => JSON.stringify(sample)).join('\n'));
  fs.writeFileSync(path.join(directory, 'events.jsonl'), '');
  const audited = summarize(directory, directory);
  assert.equal(audited.rooms[0].bandSteering.passed, false);
  assert.equal(audited.passed, 0);
  delete golden.band_steering;
  write('band.world.json', golden);
  assert.equal(summarize(directory, directory).passed, 1);
} finally {
  fs.rmSync(directory, {recursive: true, force: true});
}
console.log('PASS: band audit reconstructs native proof from raw events rather than trusting a success badge');
