'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {spawn, execFile} = require('node:child_process');
const {promisify} = require('node:util');
const {createInterface} = require('node:readline');
const now = () => Number(process.hrtime.bigint()) / 1e6;
const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";

async function nativeCpuSample(args) {
  if (!args['native-stack']) return null;
  if (!['rdk', 'prpl'].includes(args['native-stack']) ||
      ![args.host, args.vm].every(value => typeof value === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(value))) {
    throw new Error('Invalid native trace target');
  }
  const name = args['native-stack'] === 'rdk' ? 'onewifi_em_ctrl' : 'beerocks_controller';
  const source = `import json, os, pathlib, time
matches = []
for binary in pathlib.Path('/proc').glob('[0-9]*/exe'):
    try:
        if binary.resolve().name == '${name}':
            fields = binary.with_name('stat').read_text().rsplit(')', 1)[1].split()
            matches.append({'pid': int(binary.parent.name), 'start': fields[19],
                'cpuTicks': int(fields[11]) + int(fields[12]), 'hz': os.sysconf('SC_CLK_TCK'),
                'monotonicMs': time.monotonic_ns() / 1000000})
    except (OSError, ValueError):
        continue
assert len(matches) == 1, matches
print(json.dumps(matches[0]))`;
  const {stdout} = await promisify(execFile)('ssh', ['--', args.host,
    'lxc exec ' + quote(args.vm) + ' -- python3 -c ' + quote(source)], {timeout: 10000});
  return JSON.parse(stdout);
}

function cpuWindow(samples) {
  const [first, last] = samples;
  const elapsed = (last.at - first.at) / 1000;
  const sameProcesses = first.processes.length === last.processes.length && first.processes.every(process =>
    last.processes.some(other => other.id === process.id && other.type === process.type && other.cpuTime >= process.cpuTime));
  const cpuSeconds = last.processes.reduce((total, process) => total + process.cpuTime, 0) -
    first.processes.reduce((total, process) => total + process.cpuTime, 0);
  const native = first.native && last.native && first.native.pid === last.native.pid &&
    first.native.start === last.native.start && first.native.hz === last.native.hz &&
    last.native.cpuTicks >= first.native.cpuTicks && last.native.monotonicMs > first.native.monotonicMs;
  const nativeElapsed = native ? (last.native.monotonicMs - first.native.monotonicMs) / 1000 : null;
  const nativeTicks = native ? last.native.cpuTicks - first.native.cpuTicks : null;
  return {elapsedSeconds: elapsed, sameBrowserProcesses: sameProcesses,
    browserCpuPercentOneCore: elapsed > 0 && sameProcesses ? 100 * cpuSeconds / elapsed : null,
    sameNativeProcess: Boolean(native), nativeCpuPercentOneCore: native ? {
      lower: 100 * Math.max(0, nativeTicks - 2) / first.native.hz / nativeElapsed,
      upper: 100 * (nativeTicks + 2) / first.native.hz / nativeElapsed} : null};
}

function offsetBounds(samples, localTime, driftPpm = 100) {
  if (!samples.length) throw new Error('Clock calibration missing');
  const lower = Math.max(...samples.map(sample => sample.before - sample.remote -
    Math.abs(localTime - (sample.before + sample.after) / 2) * driftPpm / 1e6));
  const upper = Math.min(...samples.map(sample => sample.after - sample.remote +
    Math.abs(localTime - (sample.before + sample.after) / 2) * driftPpm / 1e6));
  if (![lower, upper].every(Number.isFinite) || lower > upper) throw new Error('Inconsistent monotonic clocks');
  return {lower, upper};
}

async function calibrate(readClock, count = 5, resolutionMs = 0) {
  const samples = [];
  for (let index = 0; index < count; index++) {
    const before = now();
    const remote = Number(await readClock());
    const after = now();
    if (!Number.isFinite(remote)) throw new Error('Invalid clock sample');
    samples.push({before, remote, after, resolutionMs});
  }
  return samples;
}

function remoteTimeBounds(samples, remoteTime, driftPpm = 100) {
  if (!samples.length) throw new Error('Clock calibration missing');
  const fraction = driftPpm / 1e6;
  if (!Number.isFinite(remoteTime) || !Number.isFinite(fraction) || fraction < 0 || fraction >= 1) {
    throw new Error('Invalid clock mapping');
  }
  const intervals = samples.map(sample => {
    const delta = remoteTime - sample.remote;
    const radius = 2 * (sample.resolutionMs ?? 0);
    if (![sample.before, sample.after, sample.remote, radius].every(Number.isFinite) ||
        sample.before > sample.after || radius < 0) throw new Error('Invalid clock sample');
    const endpoints = [(delta - radius) / (1 - fraction), (delta - radius) / (1 + fraction),
      (delta + radius) / (1 - fraction), (delta + radius) / (1 + fraction)];
    return {lower: sample.before + Math.min(...endpoints), upper: sample.after + Math.max(...endpoints)};
  });
  const lower = Math.max(...intervals.map(interval => interval.lower));
  const upper = Math.min(...intervals.map(interval => interval.upper));
  if (![lower, upper].every(Number.isFinite) || lower > upper) throw new Error('Inconsistent monotonic clocks');
  return {lower, upper};
}

function nativeObservations(records, events, clocks) {
  const transitions = [];
  const owners = new Map();
  for (const event of events.filter(event => event.kind === 'commit')
    .sort((left, right) => left.monotonic_ns - right.monotonic_ns)) {
    if (!event.associated) continue;
    const previous = owners.get(event.sta);
    if (!previous || previous.bssid !== event.bssid) transitions.push(event);
    owners.set(event.sta, event);
  }
  const used = new Set();
  const matchedThrough = new Map();
  return records.map(record => {
    if (!record.from || !record.to) return {...record, nativeScope: 'addition-or-removal-not-qualified'};
    const decoded = remoteTimeBounds(clocks.browser, record.receivedAt);
    const matches = transitions.filter(event => {
      const committed = event.monotonic_ns / 1e6;
      const guest = remoteTimeBounds(clocks.guest, committed);
      return event.sta === record.mac && event.bssid === record.to && !used.has(event) &&
        event.monotonic_ns > (matchedThrough.get(record.mac) ?? -Infinity) &&
        guest.lower <= decoded.upper &&
        decoded.lower - guest.upper <= 30000;
    });
    if (matches.length !== 1) return {...record, nativeObserved: false, nativeMatches: matches.length};
    const commit = matches[0];
    used.add(commit);
    matchedThrough.set(record.mac, commit.monotonic_ns);
    const committed = commit.monotonic_ns / 1e6;
    const guest = remoteTimeBounds(clocks.guest, committed);
    const delay = browserTime => {
      const browser = remoteTimeBounds(clocks.browser, browserTime);
      return {lower: browser.lower - guest.upper, upper: browser.upper - guest.lower};
    };
    const decodedDelay = delay(record.receivedAt);
    const paintDelay = record.paintObserved ? delay(record.receivedAt + record.decodedToPaintMs) : null;
    const presentationDelay = record.presentationObserved ? delay(record.receivedAt + record.decodedToPresentationMs) : null;
    return {...record, nativeObserved: decodedDelay.upper >= 0 && Boolean(paintDelay),
      nativeCommit: commit, nativeToRequestMs: delay(record.requestedAt),
      nativeToDecodedMs: decodedDelay, nativeToPaintMs: paintDelay,
      nativeToPresentationMs: presentationDelay,
      clockUncertaintyMs: paintDelay ? paintDelay.upper - paintDelay.lower : null};
  });
}

async function startNativeTrace(args, directory, page) {
  if (!['rdk', 'prpl'].includes(args['native-stack']) || !args.host || !args.vm) {
    throw new Error('Native tracing requires --native-stack rdk|prpl --host HOST --vm VM');
  }
  if (![args.host, args.vm].every(value => /^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(value))) {
    throw new Error('Invalid native trace host or VM');
  }
  const seconds = Number(args.seconds || 60);
  if (seconds > 150) throw new Error('Native/browser capture is bounded to 150 seconds');
  const prefix = 'lxc exec ' + quote(args.vm) + ' -- python3 -u -c ';
  const clocks = {guest: [], browser: []};
  const source = fs.readFileSync(path.join(__dirname, 'native-controller-trace.py'), 'utf8');
  const child = spawn('ssh', ['--', args.host, prefix + quote(source) + ' --stack ' + args['native-stack'] +
    ' --seconds ' + Math.ceil(seconds + 25) + ' --watch-stdin' + (['metrics', 'overhead'].includes(args.scope) ? ' --metrics' : '')]);
  const events = [];
  const clockRequests = new Map();
  let clockSequence = 0;
  const readGuestClock = () => new Promise((resolve, reject) => {
    const request = 'clock:' + ++clockSequence;
    const timeout = setTimeout(() => { clockRequests.delete(request); reject(new Error('Native clock reply timed out')); }, 2000);
    clockRequests.set(request, value => { clearTimeout(timeout); resolve(value); });
    child.stdin.write(request + '\n');
  });
  let stderr = '';
  let failure = null;
  let closed = false;
  let exitStatus = null;
  const filename = path.join(directory, 'native-events.jsonl');
  const stderrFilename = path.join(directory, 'native-stderr.txt');
  const diagnosticError = error => {
    const status = exitStatus ? ` (code=${exitStatus.code}, signal=${exitStatus.signal})` : '';
    const result = new Error(`${error.message}${status}\nNative trace ${args['native-stack']} on ${args.host}/${args.vm}` +
      `\nNative stderr saved to ${stderrFilename}:\n${stderr.trim() || '(empty)'}`, {cause: error});
    result.stderr = stderr;
    result.code = exitStatus?.code;
    result.signal = exitStatus?.signal;
    return result;
  };
  const descriptor = fs.openSync(filename, 'wx');
  const lines = createInterface({input: child.stdout});
  let readyResolve;
  let readyReject;
  const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  const exited = new Promise(resolve => child.once('close', (code, signal) => {
    closed = true;
    exitStatus = {code, signal};
    fs.closeSync(descriptor);
    fs.writeFileSync(stderrFilename, stderr);
    if (code !== 0 || signal) failure ||= new Error('Native trace exited ' + (signal || code));
    if (!events.some(event => event.kind === 'ready')) readyReject(failure || new Error('Native trace never became ready'));
    resolve();
  }));
  lines.on('line', line => {
    try {
      const event = JSON.parse(line);
      fs.writeSync(descriptor, line + '\n');
      events.push(event);
      if (event.kind === 'clock') {
        clockRequests.get(event.request)?.(event.monotonic_ms);
        clockRequests.delete(event.request);
      }
      if (events.length > 100010) throw new Error('Native trace exceeded event budget');
      if (event.kind === 'ready') readyResolve();
    } catch (error) { failure = error; readyReject(error); child.stdin.end(); }
  });
  child.on('error', error => { failure = error; readyReject(error); });
  child.stdin.on('error', error => { failure ||= error; });
  child.stderr.on('data', chunk => { stderr += chunk; });
  const timer = setTimeout(() => readyReject(new Error('Native trace startup timed out')), 15000);
  let calibrationTimer;
  let calibrationInFlight = Promise.resolve();
  let stopping = false;
  const collectClocks = async () => {
    clocks.guest.push(...await calibrate(readGuestClock));
    clocks.browser.push(...await calibrate(() => page.evaluate(() => performance.now()), 5, 0.1));
  };
  const scheduleCalibration = () => {
    calibrationTimer = setTimeout(() => {
      calibrationInFlight = collectClocks().catch(error => { failure ||= error; }).finally(() => {
        if (!stopping) scheduleCalibration();
      });
    }, 10000);
  };
  const stop = async () => {
    stopping = true;
    clearTimeout(calibrationTimer);
    await calibrationInFlight;
    if (!closed && events.some(event => event.kind === 'ready')) {
      try {
        await collectClocks();
      } catch (error) { failure ||= error; }
    }
    child.stdin.end();
    const timeout = setTimeout(() => { failure ||= new Error('Native trace shutdown timed out'); child.kill(); }, 10000);
    try { await exited; } finally { clearTimeout(timeout); }
    clocks.reference = now();
    fs.writeFileSync(path.join(directory, 'native-clocks.json'), JSON.stringify(clocks, null, 2) + '\n');
    fs.writeFileSync(stderrFilename, stderr);
    const end = events.find(event => event.kind === 'end');
    if (failure) throw diagnosticError(failure);
    if (stderr.trim() || !end || end.lost !== 0 || end.records !== end.emitted ||
        end.records !== events.filter(event => ['commit', 'metric'].includes(event.kind)).length ||
        events.some(event => event.kind === 'metric' && (!event.associated ||
          !event.sta || !event.bssid || [event.sta, event.bssid].includes('00:00:00:00:00:00')))) {
      throw diagnosticError(new Error('Native trace incomplete or reported diagnostics'));
    }
    return {events, clocks, identity: events.find(event => event.kind === 'identity'), capture: end};
  };
  try {
    await ready;
    await collectClocks();
    scheduleCalibration();
  }
  catch (error) { await stop().catch(() => {}); throw diagnosticError(error); }
  finally { clearTimeout(timer); }
  return {stop};
}

module.exports = {offsetBounds, remoteTimeBounds, calibrate, nativeObservations, startNativeTrace, nativeCpuSample, cpuWindow};
