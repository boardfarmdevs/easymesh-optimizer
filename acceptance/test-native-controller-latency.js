'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {offsetBounds, remoteTimeBounds, nativeObservations, cpuWindow, startNativeTrace} = require('./native-controller-latency.js');
assert.deepEqual(offsetBounds([{before: 9, remote: 5, after: 11}], 10, 0), {lower: 4, upper: 6});
assert.throws(() => offsetBounds([], 10), /missing/);
assert.throws(() => offsetBounds([{before: 10, remote: 1, after: 12}, {before: 20, remote: 1, after: 22}], 10, 0), /Inconsistent/);
const clocks = {reference: 0, guest: [{before: 0, remote: 0, after: 0}], browser: [{before: 0, remote: 0, after: 0}]};
const event = (timestamp, bssid) => ({kind: 'commit', monotonic_ns: timestamp * 1e6, sta: 'client', bssid, associated: true});
const record = (receivedAt, from, to) => ({mac: 'client', from, to, requestedAt: receivedAt - 5,
  receivedAt, paintObserved: true, decodedToPaintMs: 10});
const result = nativeObservations([record(20, 'old', 'first'), record(40, 'first', 'second'), record(60, 'second', 'first')],
  [event(10, 'first'), event(30, 'second'), event(50, 'first')], clocks);
assert.ok(result.every(row => row.nativeObserved));
assert.equal(result[2].nativeCommit.monotonic_ns, 50000000);
assert.ok(result[2].nativeToPaintMs.lower < 20 && result[2].nativeToPaintMs.upper > 20);
const drift = remoteTimeBounds([{before: 0, remote: 0, after: 0}, {before: 1000, remote: 1000, after: 1000}], 500);
assert.ok(drift.upper - drift.lower > .09);
assert.ok(drift.lower < 500 && drift.upper > 500);
assert.deepEqual(remoteTimeBounds([{before: 0, remote: 0, after: 0}], 50, 0), {lower: 50, upper: 50});
assert.deepEqual(remoteTimeBounds([{before: 0, remote: 0, after: 0, resolutionMs: .1}], 50, 0), {lower: 49.8, upper: 50.2});
assert.throws(() => remoteTimeBounds([{before: 1, remote: 0, after: 0, resolutionMs: .1}], 50), /Invalid clock sample/);
const recurrence = nativeObservations([record(20, null, 'first'), record(40, 'first', 'second'), record(60, 'second', 'first')],
  [event(10, 'first'), event(30, 'second'), event(50, 'first')], clocks);
assert.equal(recurrence[2].nativeObserved, true);
assert.equal(recurrence[2].nativeCommit.monotonic_ns, 50000000);
assert.equal(nativeObservations([record(20, 'old', 'missing')], [], clocks)[0].nativeObserved, false);
assert.equal(nativeObservations([record(20, null, 'first')], [event(10, 'first')], clocks)[0].nativeScope, 'addition-or-removal-not-qualified');
assert.equal(nativeObservations([record(60, 'old', 'first')], [event(10, 'first'), event(30, 'second'), event(50, 'first')], clocks)[0].nativeMatches, 2);
console.log('PASS: bounded clock mapping, missing/ambiguous commits, native owner recurrence and scope');
const first = {at: 1000, processes: [{id: 10, type: 'renderer', cpuTime: 1}],
  native: {pid: 20, start: 'abc', hz: 100, cpuTicks: 100, monotonicMs: 1000}};
const last = {at: 11000, processes: [{id: 10, type: 'renderer', cpuTime: 3}],
  native: {...first.native, cpuTicks: 200, monotonicMs: 11000}};
assert.equal(cpuWindow([first, last]).browserCpuPercentOneCore, 20);
assert.deepEqual(cpuWindow([first, last]).nativeCpuPercentOneCore, {lower: 9.8, upper: 10.2});
assert.equal(cpuWindow([first, {...last, native: {...last.native, start: 'restarted'}}]).nativeCpuPercentOneCore, null);
assert.equal(cpuWindow([first, {...last, processes: []}]).browserCpuPercentOneCore, null);
console.log('PASS: observer CPU windows retain process identity and scheduler-tick uncertainty');

async function testStartupDiagnostics() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'native-trace-diagnostics-'));
  const previousPath = process.env.PATH;
  try {
    process.env.PATH = directory;
    const scenarios = [
      {name: 'unsupported', stderr: 'ValueError: unsupported native binary SHA256: 737ab07f89fec1aabcc861bd90ddafda931be2e3e1a108cc7352ea2c3ecd994c; qualify a new probe profile', code: 1},
      {name: 'bcc', stderr: 'ModuleNotFoundError: No module named bcc', code: 1},
      {name: 'attach', stderr: 'Failed to attach BPF to uprobe', code: 1},
      {name: 'connection', stderr: 'ssh: connect to host unavailable: Connection refused', code: 255},
      {name: 'empty', stderr: '', code: 0},
      {name: 'signal', stderr: 'interrupted receiver', code: null, signal: 'SIGTERM'}
    ];
    for (const scenario of scenarios) {
      const output = path.join(directory, scenario.name);
      fs.mkdirSync(output);
      fs.writeFileSync(path.join(directory, 'ssh'), '#!' + process.execPath + '\n' +
        `process.stderr.write(${JSON.stringify(scenario.stderr)}, () => {` +
        (scenario.signal ? `process.kill(process.pid, '${scenario.signal}');` : `process.exit(${scenario.code});`) + '});\n', {mode: 0o755});
      let browserReads = 0;
      await assert.rejects(startNativeTrace({'native-stack': 'prpl', host: 'fakehost', vm: 'fakevm', seconds: 5}, output,
        {evaluate: async () => { browserReads++; return 0; }}), error => {
        assert.ok(error.message.includes(scenario.stderr || '(empty)'));
        assert.match(error.message, /prpl on fakehost\/fakevm/);
        assert.ok(error.message.includes(path.join(output, 'native-stderr.txt')));
        assert.equal(error.code, scenario.code);
        assert.equal(error.signal, scenario.signal || null);
        assert.equal(error.stderr, scenario.stderr);
        assert.equal(fs.readFileSync(path.join(output, 'native-stderr.txt'), 'utf8'), scenario.stderr);
        assert.equal(browserReads, 0);
        return true;
      });
    }
    fs.unlinkSync(path.join(directory, 'ssh'));
    const output = path.join(directory, 'spawn');
    fs.mkdirSync(output);
    await assert.rejects(startNativeTrace({'native-stack': 'rdk', host: 'fakehost', vm: 'fakevm', seconds: 5}, output, {}),
      /spawn ssh ENOENT/);
    assert.equal(fs.readFileSync(path.join(output, 'native-stderr.txt'), 'utf8'), '');
    console.log('PASS: startup errors preserve remote stderr, target, exit code, signal and evidence without clock waits');
  } finally {
    if (previousPath === undefined) delete process.env.PATH;
    else process.env.PATH = previousPath;
    fs.rmSync(directory, {recursive: true, force: true});
  }
}

testStartupDiagnostics().catch(error => { console.error(error); process.exitCode = 1; });
