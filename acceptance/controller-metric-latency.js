'use strict';

const {remoteTimeBounds} = require('./native-controller-latency.js');

function installMetricObserver() {
  const controller = window.EasyMeshController;
  const meter = window.EasyMeshSignalMeter;
  const normalize = value => String(value || '').toLowerCase();
  const records = [];
  const pending = new Map();
  const costs = [];
  let previous = new Map();
  let frame = null;
  let sequence = 0;
  let responses = 0;
  let overflow = false;
  let costTotalMs = 0;
  let callbacks = 0;
  let stopped = false;
  const startedAt = performance.now();
  const mark = name => performance.mark(name);
  const cost = started => {
    const elapsed = performance.now() - started;
    costTotalMs += elapsed;
    callbacks++;
    if (costs.length < 16384) costs.push(elapsed);
    else overflow = true;
  };
  const finish = (record, fields) => {
    if (records.length < 4096) records.push({...record, ...fields});
    else overflow = true;
  };
  function inspect() {
    const started = performance.now();
    frame = null;
    const elements = new Map([...document.querySelectorAll('#topology-visualization .sta-node')]
      .map(element => [normalize(element.__data__?.sta?.staMAC), element]));
    for (const [mac, record] of pending) {
      const element = elements.get(mac);
      const data = element?.__data__;
      const segments = [...(element?.querySelectorAll('rect.sta-signal-segment') || [])];
      const matches = normalize(data?.sta?.bssid) === record.bssid && data?.signal?.available &&
        data.signal.rcpi === record.rcpi && segments.length === 10 && segments.every((segment, index) =>
          segment.getAttribute('fill') === meter.segmentColor(index, record.level));
      if (matches && record.matchedAt != null) {
        finish(record, {passed: true, decodedResponseToSvgIdentityMs: performance.now() - record.receivedAt});
        pending.delete(mac);
      } else if (performance.now() - record.receivedAt > 5000) {
        finish(record, {passed: false, timeout: true});
        pending.delete(mac);
      } else {
        record.matchedAt = matches ? performance.now() : null;
        if (matches && record.visualChanged) {
          const bounds = element.querySelector('.sta-signal-bars').getBoundingClientRect();
          record.bounds = {left: bounds.left, top: bounds.top, right: bounds.right, bottom: bounds.bottom};
          mark('controller-latency-svg:' + record.id);
        }
      }
    }
    if (pending.size) frame = requestAnimationFrame(inspect);
    cost(started);
  }
  const original = controller.apiCall;
  controller.apiCall = async function (...args) {
    const requestedAt = performance.now();
    const result = await original.apply(this, args);
    if (stopped || args[0] !== '/clients' || !Array.isArray(result?.clients)) return result;
    const receivedAt = performance.now();
    mark('controller-latency-decoded:' + ++responses);
    const current = new Map();
    for (const client of result.clients) {
      const mac = normalize(client.mac);
      const metrics = client.client_metrics || {};
      const rcpi = metrics.rcpi;
      const bssid = normalize(client.connected_bssid);
      const age = Date.now() - Date.parse(metrics.last_updated);
      if (!mac || !bssid || !Number.isInteger(rcpi) || rcpi <= 0 || rcpi > 220 ||
          !Number.isFinite(age) || age < -5000 || age > 20000) continue;
      const level = meter.rssiLevel(Math.round(rcpi / 2 - 110));
      const prior = previous.get(mac);
      const stableSinceRequestedAt = prior?.bssid === bssid && prior?.rcpi === rcpi ? prior.stableSinceRequestedAt : requestedAt;
      const sample = {mac, bssid, rcpi, level, requestedAt, receivedAt, stableSinceRequestedAt};
      current.set(mac, sample);
      if (!prior || prior.bssid !== bssid || prior.rcpi === rcpi) continue;
      if (pending.has(mac)) finish(pending.get(mac), {passed: false, superseded: true});
      pending.delete(mac);
      const record = {...sample, id: ++sequence, responseId: responses, kind: 'metric',
        fromRcpi: prior.rcpi, previousRequestedAt: prior.stableSinceRequestedAt, metricObservedAt: metrics.last_updated,
        visualChanged: prior.level !== level};
      if (!record.visualChanged) record.visualScope = 'unchanged-meter-no-presentation-required';
      pending.set(mac, record);
    }
    for (const [mac, record] of pending) {
      if (!current.has(mac) || current.get(mac).bssid !== record.bssid) {
        finish(record, {passed: false, associationChanged: true});
        pending.delete(mac);
      }
    }
    previous = current;
    if (pending.size && frame === null) frame = requestAnimationFrame(inspect);
    cost(receivedAt);
    return result;
  };
  window.__controllerRenderLatency = () => ({responses, records, pending: [...pending.values()], overflow,
    observer: {costs, costTotalMs, callbacks, elapsedMs: performance.now() - startedAt}});
  window.__stopControllerRenderLatency = () => { stopped = true; controller.apiCall = original; };
}

function nativeMetricObservations(records, events, clocks) {
  const ordered = events.filter(event => event.kind === 'metric').sort((left, right) => left.monotonic_ns - right.monotonic_ns);
  return records.map(record => {
    const decoded = remoteTimeBounds(clocks.browser, record.receivedAt);
    const requested = remoteTimeBounds(clocks.browser, record.requestedAt);
    const prior = remoteTimeBounds(clocks.browser, record.previousRequestedAt);
    const samples = ordered.filter(event => event.sta === record.mac && event.bssid === record.bssid &&
      remoteTimeBounds(clocks.guest, event.monotonic_ns / 1e6).lower <= decoded.upper &&
      remoteTimeBounds(clocks.guest, event.monotonic_ns / 1e6).upper >= prior.lower);
    let previous = record.fromRcpi;
    const matches = samples.filter(event => {
      const changed = event.rcpi !== previous;
      previous = event.rcpi;
      return changed && event.rcpi === record.rcpi;
    });
    const crossings = events.filter(event => event.kind === 'commit' && event.sta === record.mac &&
      (!event.associated || event.bssid !== record.bssid) &&
      remoteTimeBounds(clocks.guest, event.monotonic_ns / 1e6).lower <= decoded.upper &&
      remoteTimeBounds(clocks.guest, event.monotonic_ns / 1e6).upper >= prior.lower);
    const associationChanged = crossings.some(event => remoteTimeBounds(clocks.guest, event.monotonic_ns / 1e6).lower <= requested.upper);
    const following = matches.length === 1 ? samples.filter(event => event.monotonic_ns > matches[0].monotonic_ns && event.rcpi !== record.rcpi) : [];
    const metricChangedBeforeRequest = following.some(event => remoteTimeBounds(clocks.guest, event.monotonic_ns / 1e6).lower <= requested.upper);
    if (matches.length !== 1 || associationChanged || metricChangedBeforeRequest) {
      return {...record, nativeObserved: false, nativeMatches: matches.length, associationChanged};
    }
    const committed = remoteTimeBounds(clocks.guest, matches[0].monotonic_ns / 1e6);
    const delay = browserTime => {
      const browser = remoteTimeBounds(clocks.browser, browserTime);
      return {lower: browser.lower - committed.upper, upper: browser.upper - committed.lower};
    };
    const decodedDelay = delay(record.receivedAt);
    const presentationDelay = record.presentationObserved ? delay(record.receivedAt + record.decodedToPresentationMs) : null;
    return {...record, nativeObserved: decodedDelay.upper >= 0, nativeCommit: matches[0],
      associationChanged: false, associationRacedRequest: crossings.length > 0, metricRacedRequest: following.length > 0,
      nativeToRequestMs: delay(record.requestedAt), nativeToDecodedMs: decodedDelay,
      nativeToPresentationMs: presentationDelay,
      clockUncertaintyMs: (presentationDelay || decodedDelay).upper - (presentationDelay || decodedDelay).lower};
  });
}

module.exports = {installMetricObserver, nativeMetricObservations};
