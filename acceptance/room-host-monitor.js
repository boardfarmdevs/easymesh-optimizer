'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');

function hostCommand(host, command) {
  if (host === 'local') return ['bash', ['-c', command]];
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.@-]*$/.test(host)) throw new Error('Invalid SSH host');
  return ['ssh', ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '--', host, command]];
}

function guestAuditInstallCommand(host, vm) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(vm)) throw new Error('Invalid VM name');
  const commands = ['room-feature-guest-audit.py', 'room-feature-rf-audit.py', 'backhaul-native-probe.py'].map(name => {
    const encoded = fs.readFileSync(path.join(__dirname, name)).toString('base64');
    return 'printf %s ' + encoded + ' | base64 -d | lxc exec --mode non-interactive ' + vm +
      ' -- install -m 0644 /dev/stdin /tmp/' + name;
  });
  return hostCommand(host, 'set -euo pipefail; ' + commands.join('; '));
}

class HostMonitor {
  constructor(child, filename) {
    this.child = child;
    this.samples = 0;
    this.error = null;
    this.stopping = false;
    const descriptor = fs.openSync(filename, 'wx');
    this.ready = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Host sampler did not produce two samples')), 10000);
      const lines = createInterface({input: child.stdout});
      lines.on('line', line => {
        try {
          const sample = JSON.parse(line);
          if (!Number.isFinite(Date.parse(sample.time))) throw new Error('Invalid host sample timestamp');
          fs.writeSync(descriptor, line + '\n');
          this.samples++;
          if (this.samples >= 2) { clearTimeout(timeout); resolve(); }
        } catch (error) { this.error = error.message; clearTimeout(timeout); reject(error); }
      });
      child.once('error', error => { this.error = error.message; clearTimeout(timeout); reject(error); });
      this.exited = new Promise(done => child.once('close', code => {
        fs.closeSync(descriptor);
        clearTimeout(timeout);
        if (!this.stopping || code !== 0) this.error ||= 'Host sampler exited unexpectedly: ' + code;
        if (this.samples < 2) reject(new Error(this.error || 'Host sampler ended before readiness'));
        done();
      }));
    });
    child.stderr.on('data', chunk => { this.error = String(chunk).trim(); });
    child.stdin.on('error', error => { this.error ||= error.message; });
  }

  async stop() {
    this.stopping = true;
    this.child.stdin.end();
    const timeout = setTimeout(() => {
      this.error ||= 'Host sampler did not stop after stdin closed';
      this.child.kill('SIGKILL');
    }, 5000);
    try { await this.exited; } finally { clearTimeout(timeout); }
    return {samples: this.samples, error: this.error, stopped: true};
  }
}

async function startHostMonitor(host, directory, options = {}) {
  const interval = options.interval ?? 2;
  const duration = options.duration ?? 7200;
  const processes = options.processes ?? false;
  if (!Number.isFinite(interval) || interval < 0.1 || interval > 10 ||
      !Number.isFinite(duration) || duration < interval || duration > 7200 || typeof processes !== 'boolean') {
    throw new Error('Invalid host sampler options');
  }
  const filename = path.join(directory, 'host-monitor.jsonl');
  if (fs.existsSync(filename)) throw new Error('Refusing to overwrite host samples');
  const source = fs.readFileSync(path.join(__dirname, 'room-feature-host-monitor.py'), 'utf8');
  const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
  const child = spawn(...hostCommand(host, 'python3 -u -c ' + quote(source) +
    ' --interval ' + interval + ' --duration ' + duration + ' --watch-stdin' + (processes ? ' --processes' : '')));
  const monitor = new HostMonitor(child, filename);
  try { await monitor.ready; return monitor; }
  catch (error) { await monitor.stop(); throw error; }
}

module.exports = {HostMonitor, startHostMonitor, hostCommand, guestAuditInstallCommand};
