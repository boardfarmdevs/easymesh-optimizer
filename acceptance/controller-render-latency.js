'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {distribution} = require('./room-feature-acceptance.js');
const {nativeObservations, startNativeTrace, nativeCpuSample, cpuWindow} = require('./native-controller-latency.js');
const {installMetricObserver, nativeMetricObservations} = require('./controller-metric-latency.js');
const {presentationFrames, presentationObservation} = require('./controller-presentation-latency.js');

function installObserver() {
  const controller = window.EasyMeshController;
  const identity = value => String(value || '').toLowerCase();
  const associations = topology => new Map((topology?.nodes || []).flatMap(node =>
    (node.STAList || []).map(station => [identity(station.staMAC), identity(station.bssid)])));
  let previous = null;
  const pending = new Map();
  const records = [];
  let frame = null;
  let responses = 0;
  let sequence = 0;
  let stopped = false;
  const mark = name => { if (performance.mark) performance.mark(name); };
  const finish = (record, fields) => {
    records.push({...record, ...fields});
    if (records.length > 4096) records.shift();
  };
  function inspect() {
    frame = null;
    const elements = [...document.querySelectorAll('#topology-visualization .sta-node')];
    const rendered = new Map(elements.map(element =>
      [identity(element.__data__?.sta?.staMAC), identity(element.__data__?.sta?.bssid)]));
    for (const [mac, record] of pending) {
      const matches = (rendered.get(mac) || null) === record.to;
      if (matches && record.matchedAt != null) {
        finish(record, {decodedResponseToSvgIdentityMs: performance.now() - record.receivedAt, passed: true});
        pending.delete(mac);
      } else if (performance.now() - record.receivedAt > 5000) {
        finish(record, {passed: false, timeout: true});
        pending.delete(mac);
      } else {
        record.matchedAt = matches ? performance.now() : null;
        if (matches) {
          const element = elements.find(item => identity(item.__data__?.sta?.staMAC) === mac);
          const bounds = element?.getBoundingClientRect?.();
          if (bounds) record.bounds = {left: bounds.left, top: bounds.top, right: bounds.right, bottom: bounds.bottom};
          mark('controller-latency-svg:' + record.id);
        }
      }
    }
    if (pending.size) frame = requestAnimationFrame(inspect);
  }
  const original = controller.apiCall;
  controller.apiCall = async function (...args) {
    const requestedAt = performance.now();
    const result = await original.apply(this, args);
    const receivedAt = performance.now();
    if (!stopped && args[0] === '/topology' && result?.nodes?.length) {
      responses++;
      mark('controller-latency-decoded:' + responses);
      const current = associations(result);
      if (previous === null) {
        previous = current;
        return result;
      }
      for (const mac of new Set([...previous.keys(), ...current.keys()])) {
        const from = previous.get(mac) || null;
        const to = current.get(mac) || null;
        if (from === to) continue;
        if (pending.has(mac)) finish(pending.get(mac), {passed: false, superseded: true});
        const record = {id: ++sequence, responseId: responses, mac, from, to, requestedAt,
          receivedAt, wallTime: new Date().toISOString()};
        const element = [...document.querySelectorAll('#topology-visualization .sta-node')]
          .find(item => identity(item.__data__?.sta?.staMAC) === mac);
        const bounds = element?.getBoundingClientRect?.();
        if (bounds) record.bounds = {left: bounds.left, top: bounds.top, right: bounds.right, bottom: bounds.bottom};
        pending.set(mac, record);
      }
      previous = current;
      if (pending.size && frame === null) frame = requestAnimationFrame(inspect);
    }
    return result;
  };
  window.__controllerRenderLatency = () => ({responses, records, pending: [...pending.values()]});
  window.__stopControllerRenderLatency = () => { stopped = true; controller.apiCall = original; };
}

function paintObservations(records, trace) {
  const events = trace.traceEvents || [];
  const marks = new Map(events.filter(event => event.name?.startsWith('controller-latency-'))
    .map(event => [event.name, event]));
  const paints = events.filter(event => event.name === 'Paint' && event.ph === 'X' && event.args?.data?.clip)
    .sort((left, right) => left.ts - right.ts);
  const frames = presentationFrames(trace);
  return records.map(record => {
    const decoded = marks.get('controller-latency-decoded:' + (record.responseId ?? record.id));
    const matched = marks.get('controller-latency-svg:' + record.id);
    if (!record.passed || !decoded || !matched || !record.bounds ||
        !Object.values(record.bounds).every(Number.isFinite) ||
        record.bounds.right <= record.bounds.left || record.bounds.bottom <= record.bounds.top ||
        !Number.isFinite(decoded.ts) || !Number.isFinite(matched.ts) || matched.ts < decoded.ts ||
        decoded.pid !== matched.pid || decoded.tid !== matched.tid) return {...record, paintObserved: false};
    const paint = paints.find(event => {
      if (!Number.isFinite(event.ts) || !Number.isFinite(event.dur) || event.dur < 0 ||
          event.pid !== matched.pid || event.tid !== matched.tid || event.ts < matched.ts ||
          event.ts - matched.ts > 500000) return false;
      const clip = event.args.data.clip;
      if (!Array.isArray(clip) || clip.length !== 8 || !clip.every(Number.isFinite)) return false;
      const horizontal = clip.filter((value, index) => index % 2 === 0);
      const vertical = clip.filter((value, index) => index % 2 === 1);
      return Math.min(...horizontal) <= record.bounds.left && Math.max(...horizontal) >= record.bounds.right &&
        Math.min(...vertical) <= record.bounds.top && Math.max(...vertical) >= record.bounds.bottom;
    });
    if (!paint) return {...record, paintObserved: false};
    const decodedToPaintMs = (paint.ts + (paint.dur || 0) - decoded.ts) / 1000;
    return presentationObservation({...record, paintObserved: true, decodedToPaintMs,
      requestToPaintMs: record.receivedAt - record.requestedAt + decodedToPaintMs,
      paint: {timestampUs: paint.ts, durationUs: paint.dur || 0, clip: paint.args.data.clip,
        nodeId: paint.args.data.nodeId, frame: paint.args.data.frame, pid: paint.pid, tid: paint.tid}}, frames);
  });
}

async function stopTrace(session, timeoutMs = 30000, maximumBytes = 512 * 1024 * 1024) {
  let timeout;
  let finished;
  const completed = new Promise((resolve, reject) => {
    finished = resolve;
    session.once('Tracing.tracingComplete', finished);
    timeout = setTimeout(() => reject(new Error('Chrome trace completion timed out')), timeoutMs);
  });
  let completion;
  try {
    [, completion] = await Promise.all([session.send('Tracing.end'), completed]);
  } finally {
    clearTimeout(timeout);
    session.removeListener('Tracing.tracingComplete', finished);
  }
  const {stream, dataLossOccurred} = completion;
  if (!stream) throw new Error('Chrome trace stream missing');
  const chunks = [];
  let bytes = 0;
  try {
    while (true) {
      const result = await session.send('IO.read', {handle: stream, size: 1024 * 1024});
      const chunk = Buffer.from(result.data, result.base64Encoded ? 'base64' : 'utf8');
      bytes += chunk.length;
      if (bytes > maximumBytes) throw new Error('Chrome trace export exceeded byte budget');
      chunks.push(chunk);
      if (result.eof) break;
    }
  } finally { await session.send('IO.close', {handle: stream}); }
  return {trace: JSON.parse(Buffer.concat(chunks).toString('utf8')), dataLossOccurred: dataLossOccurred ?? null};
}

async function main(argv) {
  const args = Object.fromEntries(Array.from({length: argv.length / 2}, (_, index) =>
    [argv[index * 2].replace(/^--/, ''), argv[index * 2 + 1]]));
  if (!args.url || !args.output) throw new Error('Required: --url URL --output NEW_DIRECTORY [--seconds 60]');
  const seconds = Number(args.seconds || 60);
  if (!Number.isFinite(seconds) || seconds < 5 || seconds > 180) throw new Error('Duration must be 5–180 seconds');
  const overheadScope = args.scope === 'overhead';
  const metricScope = args.scope === 'metrics' || overheadScope;
  if (args.scope && !['metrics', 'associations', 'overhead'].includes(args.scope)) throw new Error('Scope must be metrics, associations or overhead');
  if (overheadScope && (!args['native-stack'] || !args['room-url'])) throw new Error('Overhead scope requires --native-stack and --room-url');
  const labState = async () => {
    const response = await fetch(new URL('api/demo/interactions', args['room-url']), {signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error('Lab preflight HTTP ' + response.status);
    const state = await response.json();
    if (state.playback?.status !== 'paused' || state.playback.time_ms !== 0 || state.lease?.held ||
        state.recording?.active || state.expected_online_clients !== 20 || state.fault) throw new Error('Overhead scope requires idle default lab');
    return {epoch: state.environment_epoch, roles: state.roles, playback: state.playback};
  };
  fs.mkdirSync(args.output);
  const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const environment = {...process.env};
  delete environment.DISPLAY;
  const browser = await chromium.launch({headless: true, env: environment,
    executablePath: process.env.CHROMIUM_PATH,
    args: ['--no-sandbox', '--ozone-platform=headless', '--disable-background-timer-throttling']});
  let nativeTrace;
  try {
    if (browser.version() !== '139.0.7258.5') throw new Error('Unsupported Chromium version; qualify its presentation trace profile first');
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    const observedEndpoint = url => /\/api\/v1\/(clients|topology)(?:\?|$)/.test(new URL(url).pathname);
    page.on('response', response => {
      if (observedEndpoint(response.url()) && response.status() >= 400) errors.push('HTTP ' + response.status() + ': ' + response.url());
    });
    page.on('requestfailed', request => {
      if (observedEndpoint(request.url())) errors.push('Request failed: ' + request.url() + ': ' + request.failure()?.errorText);
    });
    await page.goto(args.url);
    await page.locator('[data-tab="topology"]').click();
    await page.waitForSelector('.sta-node');
    await page.locator('#topologyFullscreen').click();
    const session = await browser.newBrowserCDPSession();
    if (overheadScope) {
      await session.send('Tracing.start', {traceConfig: {includedCategories: ['blink.user_timing'],
        traceBufferSizeInKb: 512}, transferMode: 'ReturnAsStream'});
      const warmup = await stopTrace(session);
      if (warmup.dataLossOccurred !== false) throw new Error('Chrome tracing-service warmup was incomplete');
    }
    const pageSession = await page.context().newCDPSession(page);
    await pageSession.send('Performance.enable');
    const processes = await session.send('SystemInfo.getProcessInfo');
    for (const process of processes.processInfo) {
      execFileSync('renice', ['19', '-p', String(process.id)], {stdio: 'ignore'});
    }
    const usage = async () => ({native: await nativeCpuSample(args), at: performance.now(), processes: (await session.send('SystemInfo.getProcessInfo')).processInfo,
      metrics: (await pageSession.send('Performance.getMetrics')).metrics});
    const baseline = [];
    const labBefore = overheadScope ? await labState() : null;
    const baselineSeconds = overheadScope ? 20 : metricScope ? 10 : 0;
    baseline.push(await usage());
    if (baselineSeconds) await page.waitForTimeout(baselineSeconds * 1000);
    baseline.push(await usage());
    await session.send('Tracing.start', {traceConfig: {includedCategories: ['devtools.timeline', 'blink.user_timing', 'benchmark'],
      recordMode: 'recordContinuously', traceBufferSizeInKb: 131072}, transferMode: 'ReturnAsStream'});
    if (args['native-stack']) nativeTrace = await startNativeTrace(args, args.output, page);
    await page.evaluate(metricScope ? installMetricObserver : installObserver);
    const tracedStart = await usage();
    await page.waitForTimeout(seconds * 1000);
    await page.evaluate(() => window.__stopControllerRenderLatency());
    await page.waitForFunction(() => !window.__controllerRenderLatency().pending.length, null, {timeout: 6000});
    const observations = await page.evaluate(() => window.__controllerRenderLatency());
    const tracedEnd = await usage();
    const completedTrace = nativeTrace;
    nativeTrace = null;
    const native = completedTrace ? await completedTrace.stop() : null;
    const captured = await stopTrace(session);
    const after = [await usage()];
    if (baselineSeconds) await page.waitForTimeout(baselineSeconds * 1000);
    after.push(await usage());
    await page.screenshot({path: path.join(args.output, 'topology.png')});
    fs.writeFileSync(path.join(args.output, 'trace.json'), JSON.stringify(captured.trace));
    observations.records = paintObservations(observations.records, captured.trace);
    if (native) observations.records = (metricScope ? nativeMetricObservations : nativeObservations)(observations.records, native.events, native.clocks);
    const result = {url: args.url, seconds, errors, ...observations,
      traceDataLoss: captured.dataLossOccurred, browserVersion: browser.version(),
      overhead: {baselineBefore: baseline, traced: [tracedStart, tracedEnd], baselineAfter: after,
        windows: {before: cpuWindow(baseline), traced: cpuWindow([tracedStart, tracedEnd]), after: cpuWindow(after)},
        callbackMs: distribution(observations.observer?.costs || []),
        callbackMsUpper: distribution((observations.observer?.costs || []).map(cost => cost + 0.2)),
        callbackTimeFraction: observations.observer ? observations.observer.costTotalMs / observations.observer.elapsedMs : null,
        callbackTimeFractionUpper: observations.observer ?
          (observations.observer.costTotalMs + observations.observer.callbacks * 0.2) / (observations.observer.elapsedMs - 0.2) : null},
      scope: 'HTTP decode to matching SVG and covering Paint, joined by renderer/thread/animation-frame identity to Chrome presentation feedback. Headless compositor, not physical display or animation completion. Unchanged signal-meter levels do not require a new frame.',
      latencyMs: distribution(observations.records.map(record => record.decodedResponseToSvgIdentityMs).filter(Number.isFinite)),
      decodedToPresentationMs: distribution(observations.records.filter(record => record.presentationObserved).map(record => record.decodedToPresentationMs)),
      decodedToPaintMs: distribution(observations.records.filter(record => record.paintObserved).map(record => record.decodedToPaintMs)),
      requestToPaintMs: distribution(observations.records.filter(record => record.paintObserved).map(record => record.requestToPaintMs))};
    result.passed = !errors.length && observations.records.length > 0 &&
      observations.records.every(record => record.passed && (record.visualChanged === false || record.presentationObserved)) &&
      !observations.pending.length && captured.dataLossOccurred === false;
    if (metricScope) result.passed &&= !observations.overflow && observations.records.some(record => record.presentationObserved) &&
      result.overhead.callbackTimeFractionUpper <= 0.01 && result.overhead.callbackMsUpper.p95 <= 2;
    if (native) {
      const moves = metricScope ? observations.records : observations.records.filter(record => record.from && record.to);
      result.native = {identity: native.identity, capture: native.capture, movements: moves.length,
        inFlightSnapshotRaces: moves.filter(record => record.associationRacedRequest || record.metricRacedRequest).length,
        matched: moves.filter(record => record.nativeObserved).length,
        nativeToPaintLowerMs: distribution(moves.filter(record => record.nativeToPaintMs).map(record => record.nativeToPaintMs.lower)),
        nativeToPaintUpperMs: distribution(moves.filter(record => record.nativeToPaintMs).map(record => record.nativeToPaintMs.upper)),
        nativeToDecodedLowerMs: distribution(moves.filter(record => record.nativeObserved).map(record => record.nativeToDecodedMs.lower)),
        nativeToDecodedUpperMs: distribution(moves.filter(record => record.nativeObserved).map(record => record.nativeToDecodedMs.upper)),
        nativeToPresentationLowerMs: distribution(moves.filter(record => record.nativeToPresentationMs).map(record => record.nativeToPresentationMs.lower)),
        nativeToPresentationUpperMs: distribution(moves.filter(record => record.nativeToPresentationMs).map(record => record.nativeToPresentationMs.upper)),
        maximumClockUncertaintyMs: Math.max(0, ...moves.filter(record => record.nativeObserved).map(record => record.clockUncertaintyMs))};
      result.scope = 'Qualified native ' + (metricScope ? 'RCPI store' : 'association commit') + ' to HTTP decode and covering Paint/frame presentation feedback. Monotonic clock brackets include 100 ppm drift. Physical display, completed animation and other native counters are not qualified.';
      result.passed &&= moves.length > 0 && moves.every(record => record.nativeObserved) &&
        result.native.maximumClockUncertaintyMs <= 5;
    }
    if (overheadScope) {
      const labAfter = await labState();
      result.overhead.labBefore = labBefore;
      result.overhead.labAfter = labAfter;
      const windows = result.overhead.windows;
      const identityWindow = cpuWindow([baseline[0], after[1]]);
      const complete = identityWindow.sameBrowserProcesses && identityWindow.sameNativeProcess &&
        Object.values(windows).every(window => window.sameBrowserProcesses && window.sameNativeProcess);
      const nativeUpper = complete ? Math.max(0, windows.traced.nativeCpuPercentOneCore.upper -
        Math.min(windows.before.nativeCpuPercentOneCore.lower, windows.after.nativeCpuPercentOneCore.lower)) : null;
      const browserUpper = complete ? Math.max(0, windows.traced.browserCpuPercentOneCore -
        Math.min(windows.before.browserCpuPercentOneCore, windows.after.browserCpuPercentOneCore)) : null;
      result.overhead.incrementalNativeCpuPercentagePointsUpper = nativeUpper;
      result.overhead.incrementalBrowserCpuPercentagePointsUpper = browserUpper;
      result.scope = 'Controlled stationary before/traced/after overhead envelope, not metric or presentation latency qualification. Includes native scheduler-tick uncertainty, not a universal or statistical bound. Requires an unchanged paused lab and no associations or RCPI changes.';
      result.passed = complete && JSON.stringify(labBefore) === JSON.stringify(labAfter) &&
        !errors.length && captured.dataLossOccurred === false && !observations.overflow &&
        observations.records.length === 0 && observations.pending.length === 0 &&
        native.events.some(event => event.kind === 'metric') && !native.events.some(event => event.kind === 'commit') &&
        result.overhead.callbackTimeFractionUpper <= 0.01 && result.overhead.callbackMsUpper.p95 <= 2 &&
        nativeUpper <= 5 && browserUpper <= 10;
    }
    fs.writeFileSync(path.join(args.output, 'report.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
    process.exitCode = result.passed ? 0 : 1;
  } finally {
    try { if (nativeTrace) await nativeTrace.stop(); }
    finally { await browser.close(); }
  }
}

module.exports = {installObserver, paintObservations, stopTrace};
if (require.main === module) main(process.argv.slice(2)).catch(error => { console.error(error); process.exitCode = 2; });
