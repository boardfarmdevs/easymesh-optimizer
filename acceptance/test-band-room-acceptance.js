'use strict';

const assert = require('node:assert/strict');
const {bandExpectations, bandSteeringSummary, recordedEventKind, bandNativeErrors} = require('./room-feature-acceptance.js');
const world = {band_steering: {client: {allowed_bands: ['2.4', '5'], initial_band: '2.4'}},
  band_steering_expectations: [{time_ms: 0, roles: {client: {band: '5', ap: 'gateway'}}}]};
const client = {role: 'client', sta_mac: 'aa', band: '5', connected_role: 'gateway'};
const view = {stations: [{mac: 'aa', band: 1}], modelStations: [{mac: 'aa', band: 1}]};
assert.deepEqual(bandExpectations(world, 0, [client], view), []);
assert.notDeepEqual(bandExpectations(world, 0, [{...client, band: '2.4'}], view), []);
assert.notDeepEqual(bandExpectations(world, 0, [{...client, connected_role: 'extender_1'}], view), []);
assert.notDeepEqual(bandExpectations(world, 0, [client], {...view, stations: [{mac: 'aa', band: 3}]}), []);
assert.notDeepEqual(bandExpectations(world, 1000, [{...client, band: '6'}], view), []);
assert.deepEqual(bandExpectations({}, 0, [], {stations: [], modelStations: []}), []);
assert.equal(recordedEventKind('optimizer.band_scan'), true);
const nativeClient = {...client, sta_mac: '02:00:00:10:01:00', connected_bssid: '02:00:00:00:01:00'};
const probe = {link: 'Connected to 02:00:00:00:01:00\n\tfreq: 5180.0', station: nativeClient.sta_mac,
  stable_owner: true, traffic_ok: true};
assert.deepEqual(bandNativeErrors(world, {client: probe}, [nativeClient]), []);
assert.notDeepEqual(bandNativeErrors(world, {}, [nativeClient]), []);
assert.notDeepEqual(bandNativeErrors(world, {client: probe}, []), []);
for (const change of [{traffic_ok: false}, {stable_owner: false}, {station: null}, {station: '02:00:00:10:02:00'},
  {link: 'Not connected.'}, {link: probe.link.replace('5180.0', '2437')}, {link: probe.link.replace('5180.0', '9999')},
  {link: probe.link.replace('00:01:00', '00:02:00')}]) {
  assert.notDeepEqual(bandNativeErrors(world, {client: {...probe, ...change}}, [nativeClient]), []);
}
const action = (identity, from, to, role = 'client') => ({event: {kind: 'optimizer.action', payload: {
  phase: 'requested', action_id: identity, subject_role: role, decision: {current_band: from, target_band: to}}}});
const verified = identity => ({action_id: identity, success: true, traffic_ok: true});
const events = [action('upgrade', '2.4', '5')];
assert.equal(bandSteeringSummary(world, events, [verified('upgrade')]).passed, true);
assert.equal(bandSteeringSummary(world, [], []).passed, false);
assert.equal(bandSteeringSummary(world, events, [{...verified('upgrade'), traffic_ok: false}]).passed, false);
const oscillation = [...events, action('fallback', '5', '2.4'), action('repeated', '2.4', '5')];
assert.equal(bandSteeringSummary(world, oscillation, ['upgrade', 'fallback', 'repeated'].map(verified)).passed, false);
const returning = {...world, band_steering_expectations: [...world.band_steering_expectations,
  {time_ms: 1000, roles: {client: {band: '2.4'}}}, {time_ms: 2000, roles: {client: {band: '5'}}}]};
assert.equal(bandSteeringSummary(returning, events, [verified('upgrade')]).passed, false);
assert.equal(bandSteeringSummary(returning, oscillation, ['upgrade', 'fallback', 'repeated'].map(verified)).passed, true);
const pinned = {...world, band_steering: {...world.band_steering, control: {initial_band: '2.4', allowed_bands: ['2.4']}}};
const receivedOnly = {band_steering: {client: {initial_band: '5', allowed_bands: ['5'], measurement_mode: 'received_same_band'}}};
assert.equal(bandSteeringSummary(receivedOnly, [action('same-band', '5', '5')], [verified('same-band')]).passed, true);
assert.equal(bandSteeringSummary(receivedOnly, [action('same-band', '5', '5')], []).passed, false);
assert.equal(bandSteeringSummary(pinned, events, [verified('upgrade')]).passed, true);
assert.equal(bandSteeringSummary(pinned, [...events, action('control', '2.4', '5', 'control')], ['upgrade', 'control'].map(verified)).passed, false);
assert.equal(bandSteeringSummary(world, [...events, action('foreign', '2.4', '5', 'foreign')], ['upgrade', 'foreign'].map(verified)).passed, false);
console.log('PASS: expected AP/band, native verification of every transition, pinned controls and no oscillation');
