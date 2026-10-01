'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {HostMonitor, hostCommand, guestAuditInstallCommand} = require('./room-host-monitor.js');
const {hostSummary} = require('./room-feature-report.js');

async function test() {
  assert.deepEqual(hostCommand('local', 'printf test'), ['bash', ['-c', 'printf test']]);
  assert.deepEqual(hostCommand('user@host', 'true'), ['ssh', ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '--', 'user@host', 'true']]);
  assert.throws(() => hostCommand('-oProxyCommand=bad', 'true'), /Invalid SSH host/);
  const installation = guestAuditInstallCommand('local', 'fixture');
  assert.equal(installation[0], 'bash');
  assert.match(installation[1][1], /set -euo pipefail;/);
  for (const name of ['room-feature-guest-audit.py', 'room-feature-rf-audit.py', 'backhaul-native-probe.py'])
    assert.ok(installation[1][1].includes('install -m 0644 /dev/stdin /tmp/' + name));
  assert.throws(() => guestAuditInstallCommand('local', '-x'), /Invalid VM/);
  assert.throws(() => guestAuditInstallCommand('local', 'vm; false'), /Invalid VM/);
  const sample = {cpu_busy_percent: 10, available_memory_kib: 1048576, temperatures_celsius: {cpu: 60}};
  const summary = hostSummary([
    {...sample, time: '2026-09-12T00:00:00Z', cpu_busy_percent: 99, package_throttle_count: 10},
    {...sample, time: '2026-09-12T00:00:31Z', package_throttle_count: 20},
    {...sample, time: '2026-09-12T00:00:33Z', package_throttle_count: 22},
  ], '2026-09-12T00:00:30Z', '2026-09-12T00:00:34Z');
  assert.equal(summary.samples, 2);
  assert.equal(summary.samplingComplete, true);
  assert.equal(summary.cpuBusyPercent.max, 10);
  assert.equal(summary.packageThrottleDelta, 2);
  const broken = hostSummary([
    {...sample, time: '2026-09-12T00:00:31Z'},
    {...sample, time: '2026-09-12T00:00:33Z'},
  ], '2026-09-12T00:00:30Z', '2026-09-12T00:00:34Z', 'sampler exited unexpectedly');
  assert.equal(broken.samplingComplete, false);
  assert.equal(broken.packageThrottleDelta, null);
  assert.equal(broken.monitorError, 'sampler exited unexpectedly');
  const missing = hostSummary([], '2026-09-12T00:00:00Z', '2026-09-12T00:01:00Z');
  assert.equal(missing.samplingComplete, false);
  assert.equal(missing.packageThrottleDelta, null);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'room-host-monitor-'));
  try {
    const filename = path.join(directory, 'samples.jsonl');
    const child = spawn('python3', ['-u', path.join(__dirname, 'room-feature-host-monitor.py'),
      '--interval', '.1', '--duration', '30', '--watch-stdin']);
    const monitor = new HostMonitor(child, filename);
    await monitor.ready;
    assert.ok(monitor.samples >= 2);
    assert.deepEqual(await monitor.stop(), {samples: monitor.samples, error: null, stopped: true});
    const samples = fs.readFileSync(filename, 'utf8').trim().split('\n').map(line => JSON.parse(line));
    assert.equal(samples.length, monitor.samples);
    const failed = new HostMonitor(spawn('python3', ['-c', 'raise SystemExit(3)']), path.join(directory, 'failed.jsonl'));
    await assert.rejects(failed.ready, /sampler/);
    assert.match((await failed.stop()).error, /unexpectedly/);
    console.log('PASS: host sampler readiness, retained samples, owned shutdown and early failure');
  } finally { fs.rmSync(directory, {recursive: true, force: true}); }
}
test().catch(error => { console.error(error); process.exitCode = 1; });
