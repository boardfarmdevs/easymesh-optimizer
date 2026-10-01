'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {distribution, eventPerformance, viewAgreement, fronthaulOutages, bandSteeringSummary} = require('./room-feature-acceptance.js');

function readJsonLines(filename) {
  return fs.readFileSync(filename, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
}

function hostSummary(rows, started, finished, monitorError = null) {
  const start = Date.parse(started), end = Date.parse(finished);
  const samples = rows.filter(row => Date.parse(row.time) >= start && Date.parse(row.time) <= end)
    .sort((left, right) => Date.parse(left.time) - Date.parse(right.time));
  const times = [start, ...samples.map(row => Date.parse(row.time)), end];
  const gaps = times.slice(1).map((value, index) => (value - times[index]) / 1000);
  const counters = samples.filter(row => row.package_throttle_count != null)
    .map(row => Number(row.package_throttle_count)).filter(Number.isFinite);
  const throttleSamples = samples.filter(row => row.package_throttle_total_time_ms != null &&
    Number.isFinite(Number(row.package_throttle_total_time_ms)));
  const throttleTimes = throttleSamples.map(row => Number(row.package_throttle_total_time_ms));
  const monotonicThrottle = throttleTimes.length >= 2 &&
    throttleTimes.every((value, index) => value >= 0 && (index === 0 || value >= throttleTimes[index - 1]));
  const throttleTimeMs = monotonicThrottle ? throttleTimes.at(-1) - throttleTimes[0] : null;
  const throttleIntervalMs = monotonicThrottle
    ? Date.parse(throttleSamples.at(-1).time) - Date.parse(throttleSamples[0].time) : null;
  return {samples: samples.length, samplingComplete: !monitorError && samples.length >= 2 && gaps.every(gap => gap >= 0 && gap <= 30),
    monitorError,
    maximumSamplingGapSeconds: Math.max(...gaps),
    cpuBusyPercent: distribution(samples.map(row => row.cpu_busy_percent)),
    maximumSensorCelsius: distribution(samples.map(row => Math.max(...Object.values(row.temperatures_celsius || {})))),
    minimumAvailableMemoryGiB: samples.length ? Math.min(...samples.map(row => row.available_memory_kib)) / 1048576 : null,
    packageThrottleDelta: counters.length >= 2 ? counters.at(-1) - counters[0] : null,
    packageThrottleTimeMs: throttleTimeMs,
    packageThrottleMeasuredSeconds: throttleIntervalMs == null ? null : throttleIntervalMs / 1000,
    packageThrottleWindowPercent: throttleTimeMs == null || throttleIntervalMs <= 0 ? null : throttleTimeMs / throttleIntervalMs * 100,
    samplingElapsedMs: distribution(samples.map(row => row.sampling_elapsed_ms))};
}

function summarize(directory, worlds = path.join(directory, '..', 'goldens')) {
  const report = JSON.parse(fs.readFileSync(path.join(directory, 'report.json')));
  const events = readJsonLines(path.join(directory, 'events.jsonl'));
  const gates = ['roster', 'viewMatchesRoom', 'viewMatchesModel', 'healthy', 'epochMatches', 'complete',
    'sameBandBest', 'metricsFresh', 'meshConnected', 'meshViewMatches', 'policyConverged', 'strongestApConverged'];
  const rooms = report.rooms.map(room => {
    const samples = readJsonLines(path.join(directory, room.id + '-samples.jsonl'));
    const golden = JSON.parse(fs.readFileSync(path.join(worlds, room.id + '.world.json')));
    const snapshotPath = path.join(directory, room.id + '-loaded.json');
    const snapshot = fs.existsSync(snapshotPath) ? JSON.parse(fs.readFileSync(snapshotPath)) : null;
    const devices = Object.fromEntries((snapshot?.current.network.mesh.nodes || []).map(node => [node.role, String(node.device_id)]));
    const owners = samples.map(sample => {
      const associations = new Map((sample.roomAssociations || []).map(client => [client.mac.toLowerCase(), client]));
      return {time: sample.wallTime, phase: sample.phase, errors: sample.associations.filter(client => {
        const native = associations.get(client.mac.toLowerCase());
        return native && native.bssid === client.bssid && devices[native.ap] !== client.owner;
      }).map(client => client.mac)};
    }).filter(sample => sample.errors.length);
    const phases = Object.fromEntries([...new Set(samples.map(sample => sample.phase))].map(phase => {
      const selected = samples.filter(sample => sample.phase === phase);
      return [phase, {samples: selected.length,
        failedGates: Object.fromEntries(gates.map(gate => [gate, selected.filter(sample => !sample[gate]).length])),
        scriptErrors: selected.filter(sample => sample.scriptErrors.length).length,
        sceneErrors: selected.filter(sample => sample.sceneErrors.length).length,
        metricFaults: [...new Set(selected.map(sample => sample.unavailable).filter(Boolean))],
        firstConverged: selected.find(sample => sample.converged)?.playback.time_ms ?? null,
      }];
    }));
    const outages = fronthaulOutages(golden, samples);
    let directional = null;
    const auditPath = path.join(directory, '..', 'asymmetric-rf-audit.json');
    if (room.id === 'home-a-asymmetric-link' && fs.existsSync(auditPath)) {
      const audit = JSON.parse(fs.readFileSync(auditPath));
      const frame = golden.generations.findLast(item => item.time_ms <= audit.playback.time_ms) || golden.generations[0];
      const mismatches = audit.links.filter(row => {
        const down = frame.links.find(link => link.source_role === row.ap && link.destination_role === 'sta_mobile_01').snr_db_by_band[row.band];
        const up = frame.links.find(link => link.destination_role === row.ap && link.source_role === 'sta_mobile_01').snr_db_by_band[row.band];
        return row.uplink_minus_downlink_db !== up - down;
      });
      const generationStable = new Set(audit.links.flatMap(row => [row.up[0], row.down[0]])).size === 1;
      directional = {passed: audit.world === golden.name && audit.epoch_stable && generationStable && mismatches.length === 0,
        timeMs: audit.playback.time_ms, generationStable, checked: audit.links.length, mismatches};
    }
    const directionalRequired = room.id === 'home-a-asymmetric-link';
    const relevant = events.filter(record => record.room === room.id);
    const bandSteering = golden.band_steering ? bandSteeringSummary(golden, relevant,
      relevant.filter(record => record.event.kind === 'optimizer.verification').map(record => record.event.payload)) : null;
    const playing = samples.filter(sample => sample.phase === 'playing');
    const agreement = viewAgreement(playing);
    const viewCorrect = agreement.passed && playing.every(sample => !sample.duplicates && sample.meshCount === 6 && sample.associations.every(client => client.visible && client.label));
    const basePassed = room.load?.passed && room.initial?.passed && room.final?.passed && room.playback?.completed &&
      room.checkpoints.every(checkpoint => checkpoint.passed) && room.scriptCorrect && room.sceneCorrect && viewCorrect &&
      room.presencePhases.every(entry => entry.topologyVerified) && room.kernel?.passed && !room.errors.length &&
      !report.errors.some(error => error.room === room.id);
    return {id: room.id, passed: Boolean(basePassed) && owners.length === 0 && bandSteering?.passed !== false && (!directionalRequired || directional?.passed === true) &&
      outages.every(outage => outage.samples > 0 && !outage.remainingAssociations.length && outage.meshConnected),
      directionalRequired, directional, bandSteering, fronthaulOutages: outages,
      loadAckMs: room.load?.acknowledgementMs, initial: room.initial, final: room.final,
      playback: room.playback, checkpoints: room.checkpoints, phases, ownerErrors: owners,
      scriptCorrect: room.scriptCorrect, sceneCorrect: room.sceneCorrect, viewCorrect,
      presencePhases: room.presencePhases, kernel: room.kernel, errors: room.errors,
      performance: {...room.performance, viewAgreement: agreement, viewMismatchSeconds: agreement.observedBadSpanSeconds,
        ...eventPerformance(events.filter(record => record.room === room.id))}};
  });
  const monitorPath = fs.existsSync(path.join(directory, 'host-monitor.jsonl'))
    ? path.join(directory, 'host-monitor.jsonl') : path.join(directory, '..', 'host-monitor.jsonl');
  const monitor = fs.existsSync(monitorPath) ? readJsonLines(monitorPath) : [];
  return {flavor: report.flavor, started: report.started, finished: report.finished,
    convergenceCriterion: report.convergenceCriterion || 'absolute-strongest-ap',
    failure: report.failure, errors: report.errors, eventGaps: report.eventGaps,
    catalogCount: report.catalog?.length, tested: rooms.length, passed: rooms.filter(room => room.passed).length,
    nativeIdentitiesUnchanged: report.nativeIdentitiesUnchanged, restoration: report.restoration,
    performance: eventPerformance(events.filter(record => rooms.some(room => room.id === record.room))), rooms,
    host: hostSummary(monitor, report.started, report.finished, report.hostMonitor?.error || null)};
}

function qualificationPassed(report) {
  return report.tested > 0 && report.tested === report.passed && !report.failure &&
    report.errors.length === 0 && report.eventGaps.length === 0 &&
    report.nativeIdentitiesUnchanged === true && report.restoration?.applied === true &&
    report.restoration?.convergence?.passed === true && report.host?.samplingComplete === true;
}

module.exports = {summarize, hostSummary, qualificationPassed};
if (require.main === module) {
  const report = summarize(process.argv[2], process.argv[3]);
  report.qualificationPassed = qualificationPassed(report);
  process.stdout.write(JSON.stringify(report, null, 2) + '\n');
  process.exitCode = report.qualificationPassed ? 0 : 1;
}
