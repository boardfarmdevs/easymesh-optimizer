'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {stopTrace} = require('./controller-render-latency.js');
const {presentationFrames} = require('./controller-presentation-latency.js');
const {distribution} = require('./room-feature-acceptance.js');
const {remoteTimeBounds, startNativeTrace} = require('./native-controller-latency.js');

function installRoomObserver() {
  if (!window.__viewer?.observeFrames) throw new Error('Room profiler requires ?profile=1');
  const records = [];
  const costs = [];
  const errors = [];
  let previous = null;
  let sequence = null;
  let overflow = false;
  let totalCost = 0;
  let callbacks = 0;
  const started = performance.now();
  const equal = (left, right) => JSON.stringify(left) === JSON.stringify(right);
  const observer = ({renderer, nodes, actual, network, frame, clock}) => {
    if (!frame || frame.sequence === sequence) return;
    const begin = performance.now();
    try {
      sequence = frame.sequence;
      const clients = (network?.clients || []).filter(client => client.role && nodes[client.role]);
      const expectedLines = [];
      const gauges = clients.map(client => {
        const node = nodes[client.role];
        const peer = nodes[client.connected_role];
        if (peer) expectedLines.push(peer.grp.position.x, peer.post.position.y * 2 + 0.12, peer.grp.position.z,
          node.grp.position.x, node.post.position.y * 2 + 0.12, node.grp.position.z);
        const measured = Date.parse(client.metric_observed_at || '');
        const reference = clock ? clock.serverMs + Math.max(0, begin - clock.receivedMs) : NaN;
        const fresh = Number.isFinite(measured) && Number.isFinite(reference) && reference - measured <= 20000 && measured - reference <= 5000;
        const level = fresh ? window.EasyMeshSignalMeter.rssiLevel(client.rssi_dbm) : 0;
        const expected = Array.from({length: 10}, (_, index) => window.EasyMeshSignalMeter.segmentColor(index, level));
        const colors = node.gauge?.bars.map(bar => '#' + bar.material.color.getHexString());
        return {role: client.role, mac: client.sta_mac, bssid: client.connected_bssid,
          rssi: client.rssi_dbm, rcpi: client.rcpi, source: client.measurement_source,
          metricObservedAt: client.metric_observed_at, fresh, level, colors, passed: equal(colors, expected) &&
            node.gauge.group.userData.signalLevel === level && node.gauge.bars.every(bar => bar.visible && bar.material.opacity === 1)};
      });
      const segments = actual.geometry.getAttribute('position');
      const lines = Array.from(segments.array).slice(0, actual.geometry.drawRange.count * 3);
      const linesMatch = lines.length === expectedLines.length && lines.every((value, index) => Math.abs(value - expectedLines[index]) < 0.0001);
      const canvas = renderer.domElement;
      const canvases = document.querySelectorAll('canvas');
      const state = gauges.map(gauge => [gauge.mac, gauge.bssid, gauge.rssi, gauge.level]);
      const changed = previous !== null && !equal(previous, state);
      previous = state;
      const record = {id: records.length + 1, sequence, receivedAt: frame.received_at, submittedAt: begin,
        changed, gauges, linesMatch, drawCalls: renderer.info.render.calls,
        canvasCount: canvases.length, contextLost: renderer.getContext().isContextLost(),
        fullscreen: document.fullscreenElement?.contains?.(canvas) || document.fullscreenElement === canvas};
      record.passed = clients.length > 0 && gauges.every(gauge => gauge.passed) && linesMatch &&
        record.drawCalls > 0 && record.canvasCount === 1 && !record.contextLost && record.fullscreen &&
        Number.isFinite(record.receivedAt) && record.submittedAt >= record.receivedAt;
      if (records.length >= 2000) overflow = true;
      else {
        records.push(record);
        record.markedAt = performance.mark('room-latency-submitted:' + record.id).startTime;
      }
    } catch (error) {
      if (errors.length < 20) errors.push(String(error));
    } finally {
      const cost = performance.now() - begin;
      callbacks++;
      totalCost += cost;
      if (costs.length < 2000) costs.push(cost);
      else overflow = true;
    }
  };
  window.__viewer.observeFrames(observer);
  window.__stopRoomRenderLatency = () => {
    window.__viewer.observeFrames(null);
    return {records, errors, overflow, callbacks, costs, totalCost, elapsedMs: performance.now() - started};
  };
}

function roomPresentationObservations(records, trace) {
  const events = trace.traceEvents || [];
  const frames = presentationFrames(trace);
  const marks = events.filter(event => event.name?.startsWith('room-latency-submitted:'));
  const mailboxes = events.filter(event => event.name === 'DrawingBuffer::prepareMailbox' && event.ph === 'X');
  return records.map(record => {
    const unmatched = {...record, presentationObserved: false};
    const submitted = marks.filter(event => event.name === 'room-latency-submitted:' + record.id);
    if (!record.passed || submitted.length !== 1 || !Number.isFinite(submitted[0].ts) ||
        !Number.isFinite(record.markedAt) || record.markedAt < record.submittedAt) return unmatched;
    const mark = submitted[0];
    const matches = frames.filter(frame => frame.mainFrame && frame.presentation &&
      frame.start.pid === mark.pid && frame.start.tid === mark.tid && frame.start.ts <= mark.ts && frame.end.ts >= mark.ts);
    if (matches.length !== 1) return unmatched;
    const frame = matches[0];
    const transfers = mailboxes.filter(event => event.pid === mark.pid && event.tid === mark.tid &&
      Number.isFinite(event.ts) && Number.isFinite(event.dur) && event.dur >= 0 && event.ts >= mark.ts &&
      event.ts + event.dur <= frame.mainFrame.ts + frame.mainFrame.dur);
    if (transfers.length !== 1 || frame.presentation.ts < transfers[0].ts + transfers[0].dur) return unmatched;
    if (marks.some(event => event !== mark && event.pid === mark.pid && event.tid === mark.tid &&
      event.ts > mark.ts && event.ts <= transfers[0].ts)) return {...unmatched, superseded: true};
    const submittedToPresentationMs = (frame.presentation.ts - mark.ts) / 1000 + record.markedAt - record.submittedAt;
    const receivedToPresentationMs = record.submittedAt - record.receivedAt + submittedToPresentationMs;
    return {...record, presentationObserved: true, submittedToPresentationMs,
      receivedToPresentationMs,
      receivedToPresentationBoundsMs: {lower: Math.max(0, receivedToPresentationMs - 0.2), upper: receivedToPresentationMs + 0.2},
      presentation: {timestampUs: frame.presentation.ts, traceId: frame.start.args.id,
        beginFrame: frame.presentation.args.begin_frame_id, pid: mark.pid, tid: mark.tid},
      mailbox: {timestampUs: transfers[0].ts, durationUs: transfers[0].dur}};
  });
}

function nativeRoomObservations(records, events, clocks) {
  const transitions = [];
  const latest = new Map();
  for (const event of events.filter(event => ['commit', 'metric'].includes(event.kind))
    .sort((left, right) => left.monotonic_ns - right.monotonic_ns)) {
    const previous = latest.get(event.sta);
    if (event.kind === 'commit') {
      if (!event.associated || previous?.bssid !== event.bssid) latest.delete(event.sta);
    } else {
      if (!previous || previous.bssid !== event.bssid || previous.rcpi !== event.rcpi) transitions.push(event);
      latest.set(event.sta, event);
    }
  }
  const previous = new Map();
  const matchedThrough = new Map();
  const observations = [];
  for (const record of records) {
    const current = new Set();
    for (const gauge of record.gauges) {
      current.add(gauge.mac);
      const prior = previous.get(gauge.mac);
      previous.set(gauge.mac, gauge);
      if (!prior || prior.bssid !== gauge.bssid || prior.rcpi === gauge.rcpi) continue;
      const result = {frameId: record.id, sequence: record.sequence, mac: gauge.mac, bssid: gauge.bssid,
        rcpi: gauge.rcpi, fromRcpi: prior.rcpi, receivedAt: record.receivedAt,
        visualChanged: gauge.level !== prior.level, source: gauge.source,
        nativeObserved: false, presentationObserved: record.presentationObserved};
      const decoded = remoteTimeBounds(clocks.browser, record.receivedAt);
      const matches = transitions.filter(event => {
        const committed = remoteTimeBounds(clocks.guest, event.monotonic_ns / 1e6);
        return event.sta === gauge.mac && event.bssid === gauge.bssid && event.rcpi === gauge.rcpi &&
          event.monotonic_ns > (matchedThrough.get(gauge.mac) ?? -Infinity) &&
          committed.lower <= decoded.upper && decoded.lower - committed.upper <= 30000;
      });
      result.nativeMatches = matches.length;
      const nativeSource = ['associated_sta_link_metrics', 'prplmesh_associated_sta_link_metrics'].includes(gauge.source);
      const signalMatches = Number.isFinite(gauge.rssi) && Math.abs(gauge.rssi - (gauge.rcpi / 2 - 110)) <= 0.5;
      if (matches.length === 1 && nativeSource && signalMatches && gauge.fresh && gauge.passed &&
          record.passed && record.presentationObserved && Number.isInteger(gauge.rcpi) &&
          gauge.rcpi > 0 && gauge.rcpi <= 220) {
        const commit = matches[0];
        const committed = remoteTimeBounds(clocks.guest, commit.monotonic_ns / 1e6);
        const presentation = remoteTimeBounds(clocks.browser, record.receivedAt + record.receivedToPresentationMs);
        matchedThrough.set(gauge.mac, commit.monotonic_ns);
        result.nativeCommit = commit;
        result.nativeToDecodedMs = {lower: decoded.lower - committed.upper, upper: decoded.upper - committed.lower};
        result.nativeToPresentationMs = {lower: presentation.lower - committed.upper, upper: presentation.upper - committed.lower};
        result.clockUncertaintyMs = result.nativeToPresentationMs.upper - result.nativeToPresentationMs.lower;
        result.nativeObserved = result.nativeToDecodedMs.upper >= 0 && result.clockUncertaintyMs <= 5;
      }
      observations.push(result);
    }
    for (const mac of previous.keys()) if (!current.has(mac)) previous.delete(mac);
  }
  return observations;
}

async function main(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 2) args[argv[index].replace(/^--/, '')] = argv[index + 1];
  const seconds = Number(args.seconds || 60);
  if (!args.url || !args.output || !Number.isFinite(seconds) || seconds < 5 || seconds > 150) throw new Error('Supply --url, new --output and --seconds 5..150');
  fs.mkdirSync(args.output);
  const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const environment = {...process.env};
  delete environment.DISPLAY;
  const browser = await chromium.launch({headless: true, env: environment, executablePath: process.env.CHROMIUM_PATH,
    args: ['--no-sandbox', '--ozone-platform=headless', '--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader',
      '--disable-background-timer-throttling']});
  let native = null;
  try {
    if (browser.version() !== '139.0.7258.5') throw new Error('Unsupported Chromium presentation profile');
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const errors = [];
    const reportError = error => { if (errors.length < 50) errors.push(String(error)); };
    page.on('pageerror', reportError);
    page.on('response', response => { if (response.url().includes('/api/demo/') && response.status() >= 400) reportError('Room API HTTP ' + response.status()); });
    page.on('requestfailed', request => { if (request.url().includes('/api/demo/')) reportError('Room API request failed: ' + request.failure()?.errorText); });
    const pageSession = await page.context().newCDPSession(page);
    await pageSession.send('Network.enable');
    let lastSequence = null;
    let streamMessages = 0;
    pageSession.on('Network.eventSourceMessageReceived', message => {
      try {
        if (message.eventName === 'reset') throw new Error('Room SSE reset during profiling');
        const event = JSON.parse(message.data);
        if (!Number.isSafeInteger(event.sequence) || lastSequence !== null && event.sequence !== lastSequence + 1) throw new Error('Room SSE sequence gap');
        lastSequence = event.sequence;
        streamMessages++;
      } catch (error) { reportError(error); }
    });
    const url = new URL(args.url);
    url.searchParams.set('profile', '1');
    await page.goto(url.href);
    await page.waitForFunction(() => window.__viewer?.observeFrames && window.__viewer.frameTimings.length > 1);
    await page.locator('#roomFullscreen').click();
    const session = await browser.newBrowserCDPSession();
    for (const process of (await session.send('SystemInfo.getProcessInfo')).processInfo) {
      execFileSync('renice', ['19', '-p', String(process.id)], {stdio: 'ignore'});
      if (process.type.toLowerCase() === 'gpu') execFileSync('taskset', ['-apc', '0-1', String(process.id)], {stdio: 'ignore'});
    }
    if (args['native-stack']) native = await startNativeTrace({...args, scope: 'metrics'}, args.output, page);
    await session.send('Tracing.start', {traceConfig: {includedCategories: ['devtools.timeline', 'blink.user_timing', 'benchmark', 'blink'],
      recordMode: 'recordContinuously', traceBufferSizeInKb: 131072}, transferMode: 'ReturnAsStream'});
    await page.evaluate(installRoomObserver);
    await page.waitForTimeout(seconds * 1000);
    const observations = await page.evaluate(() => window.__stopRoomRenderLatency());
    await page.waitForTimeout(1000);
    const receiver = native;
    native = null;
    const nativeCapture = receiver ? await receiver.stop() : null;
    const captured = await stopTrace(session);
    fs.writeFileSync(path.join(args.output, 'trace.json'), JSON.stringify(captured.trace));
    const records = roomPresentationObservations(observations.records, captured.trace);
    const nativeRecords = nativeCapture ? nativeRoomObservations(records, nativeCapture.events, nativeCapture.clocks) : null;
    const costUpper = distribution(observations.costs.map(cost => cost + 0.2));
    const callbackTimeFractionUpper = (observations.totalCost + observations.callbacks * 0.2) / (observations.elapsedMs - 0.2);
    const result = {...observations, records, errors: [...errors, ...observations.errors], browserVersion: browser.version(),
      traceDataLoss: captured.dataLossOccurred, callbackMsUpper: costUpper, callbackTimeFractionUpper,
      streamMessages,
      receivedToPresentationMs: distribution(records.filter(record => record.presentationObserved).map(record => record.receivedToPresentationMs)),
      receivedToPresentationLowerMs: distribution(records.filter(record => record.presentationObserved).map(record => record.receivedToPresentationBoundsMs.lower)),
      receivedToPresentationUpperMs: distribution(records.filter(record => record.presentationObserved).map(record => record.receivedToPresentationBoundsMs.upper)),
      changedFrames: records.filter(record => record.changed).length,
      native: nativeCapture ? {identity: nativeCapture.identity, capture: nativeCapture.capture, records: nativeRecords,
        decodedLowerMs: distribution(nativeRecords.filter(record => record.nativeObserved).map(record => record.nativeToDecodedMs.lower)),
        decodedUpperMs: distribution(nativeRecords.filter(record => record.nativeObserved).map(record => record.nativeToDecodedMs.upper)),
        presentationLowerMs: distribution(nativeRecords.filter(record => record.nativeObserved).map(record => record.nativeToPresentationMs.lower)),
        presentationUpperMs: distribution(nativeRecords.filter(record => record.nativeObserved).map(record => record.nativeToPresentationMs.upper)),
        scope: 'Native controller serving-RCPI store through room SSE decode, checked client materials/geometry and exact WebGL frame presentation. Not RF generation, over-air reception, pixel readback or physical scanout.'} : null,
      scope: 'Decoded room network snapshot to checked client gauge materials/association geometry and actual WebGL draws, single-canvas mailbox preparation, exact Chromium frame presentation feedback. Not native RF-to-room latency, physical scanout, pixel readback or animation completion.'};
    result.passed = !result.errors.length && !result.overflow && streamMessages > 0 && records.length > 0 && result.changedFrames > 0 &&
      records.every(record => record.passed && record.presentationObserved) && captured.dataLossOccurred === false &&
      costUpper.p95 <= 2 && callbackTimeFractionUpper <= 0.01;
    if (nativeRecords) result.passed &&= nativeRecords.length > 0 && nativeRecords.every(record => record.nativeObserved);
    await page.screenshot({path: path.join(args.output, 'room.png')});
    fs.writeFileSync(path.join(args.output, 'report.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify({passed: result.passed, frames: records.length, changedFrames: result.changedFrames,
      receivedToPresentationMs: result.receivedToPresentationMs, errors: result.errors}));
    process.exitCode = result.passed ? 0 : 1;
  } finally {
    if (native) await native.stop().catch(error => console.error(error));
    await browser.close();
  }
}

module.exports = {installRoomObserver, roomPresentationObservations, nativeRoomObservations};
if (require.main === module) main(process.argv.slice(2)).catch(error => { console.error(error); process.exitCode = 1; });
