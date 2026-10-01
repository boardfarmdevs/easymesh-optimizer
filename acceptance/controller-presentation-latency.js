'use strict';

function presentationFrames(trace) {
  const opened = new Map();
  const frames = [];
  const presentations = new Map();
  const collapsed = [];
  const renderStarts = new Map();
  const renders = [];
  const events = trace.traceEvents || [];
  const startsByTime = new Map();
  const endsById = new Map();
  const commits = new Set();
  const position = event => [event.pid, event.tid, event.ts].join(':');
  const append = (index, key, event) => index.set(key, [...(index.get(key) || []), event]);
  for (const event of events) {
    if (event.name === 'ProxyMain::BeginMainFrame' && event.ph === 's') append(startsByTime, position(event), event);
    if (event.name === 'ProxyMain::BeginMainFrame' && event.ph === 'f') append(endsById, event.id, event);
    if (event.name === 'ProxyMain::BeginMainFrame::commit' && event.ph === 'X') commits.add(position(event));
  }
  const mainFrames = events.filter(event => event.name === 'ProxyMain::BeginMainFrame' && event.ph === 'X')
    .flatMap(event => {
      const starts = startsByTime.get(position(event)) || [];
      if (starts.length !== 1 || !Number.isSafeInteger(starts[0].id)) return [];
      const ends = (endsById.get(starts[0].id) || []).filter(flow =>
        flow.pid === event.pid && flow.tid === event.tid && flow.ts > event.ts);
      if (ends.length !== 1 || !commits.has(position(ends[0]))) return [];
      return [{...event, dur: ends[0].ts - event.ts}];
    });
  const key = event => [event.pid, event.tid, event.id2?.local].join(':');
  for (const event of [...(trace.traceEvents || [])].sort((left, right) => left.ts - right.ts)) {
    if (!Number.isFinite(event.ts) || !event.id2?.local) continue;
    if (event.name === 'AnimationFrame' && event.ph === 'b') {
      opened.set(key(event), opened.has(key(event)) ? null : event);
    } else if (event.name === 'AnimationFrame' && event.ph === 'e') {
      const start = opened.get(key(event));
      opened.delete(key(event));
      if (start?.args?.id && event.ts > start.ts) frames.push({start, end: event});
    } else if (event.name === 'AnimationFrame' && event.ph === 'n' && event.args?.id) {
      collapsed.push(event);
    } else if (event.name === 'AnimationFrame::Render' && event.ph === 'b') {
      renderStarts.set(key(event), renderStarts.has(key(event)) ? null : event);
    } else if (event.name === 'AnimationFrame::Render' && event.ph === 'e') {
      const start = renderStarts.get(key(event));
      renderStarts.delete(key(event));
      if (start && event.ts > start.ts) renders.push({start, end: event});
    } else if (event.name === 'AnimationFrame::Presentation' && event.ph === 'n' && event.args?.id) {
      const identity = key(event) + ':' + event.args.id;
      const matches = presentations.get(identity) || [];
      matches.push(event);
      presentations.set(identity, matches);
    }
  }
  for (const start of collapsed) {
    const info = start.args.animation_frame_timing_info;
    const hosts = mainFrames.filter(event => event.pid === start.pid && event.tid === start.tid &&
      event.args?.begin_frame_id === info?.begin_frame_id?.sequence_number);
    if (hosts.length !== 1 || !Number.isSafeInteger(info.duration_ms) || info.duration_ms < 0) continue;
    const host = hosts[0];
    const intervals = renders.filter(render => key(render.start) === key(start) &&
      render.start.ts >= host.ts && render.end.ts <= host.ts + host.dur &&
      Math.floor((render.end.ts - start.ts) / 1000) === info.duration_ms);
    if (intervals.length === 1) frames.push({start, end: intervals[0].end, collapsed: true});
  }
  return frames.map(frame => {
    const matches = presentations.get(key(frame.start) + ':' + frame.start.args.id) || [];
    const original = frame.start.args.animation_frame_timing_info?.begin_frame_id;
    const presentation = matches.length === 1 ? matches[0] : null;
    const presented = presentation?.args.begin_frame_id;
    const valid = original?.sequence_number > 0 && original?.source_id > 0 &&
      presented?.sequence_number > 0 && presented?.source_id === original.source_id &&
      presentation.ts >= frame.end.ts && presentation.ts - frame.end.ts <= 1000000;
    const hosts = mainFrames.filter(event => event.pid === frame.start.pid && event.tid === frame.start.tid &&
      event.ts <= frame.end.ts && event.ts + event.dur >= frame.end.ts &&
      event.args?.begin_frame_id === original?.sequence_number);
    return {...frame, mainFrame: hosts.length === 1 ? hosts[0] : null, presentation: valid ? presentation : null};
  });
}

function presentationObservation(record, frames) {
  if (!record.paintObserved) return {...record, presentationObserved: false};
  const paint = record.paint;
  const matches = frames.filter(frame => frame.start.pid === paint.pid && frame.start.tid === paint.tid &&
    frame.mainFrame && frame.end.ts <= paint.timestampUs &&
    frame.mainFrame.ts + frame.mainFrame.dur >= paint.timestampUs + paint.durationUs);
  if (matches.length !== 1 || !matches[0].presentation) return {...record, presentationObserved: false};
  const frame = matches[0];
  if (frame.presentation.ts < paint.timestampUs + paint.durationUs) return {...record, presentationObserved: false};
  const paintToPresentationMs = (frame.presentation.ts - paint.timestampUs - paint.durationUs) / 1000;
  return {...record, presentationObserved: true, paintToPresentationMs,
    decodedToPresentationMs: record.decodedToPaintMs + paintToPresentationMs,
    presentation: {timestampUs: frame.presentation.ts, traceId: frame.start.args.id,
      beginFrame: frame.presentation.args.begin_frame_id, pid: frame.start.pid, tid: frame.start.tid}};
}

module.exports = {presentationFrames, presentationObservation};
