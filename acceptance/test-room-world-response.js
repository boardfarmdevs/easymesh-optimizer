'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {worldApplyResponse} = require('./room-feature-acceptance.js');

function response(world, method = 'POST', url = 'http://lab/api/demo/world/apply') {
  return {url: () => url, request: () => ({method: () => method, postDataJSON: () => ({world})})};
}

test('world acknowledgement must belong to the requested room, not a late previous apply', () => {
  assert.equal(worldApplyResponse(response('band-ap-counter-roam'), 'band-upgrade-24-5'), false);
  assert.equal(worldApplyResponse(response('band-upgrade-24-5'), 'band-upgrade-24-5'), true);
});

test('default restoration rejects another world and non-apply responses', () => {
  assert.equal(worldApplyResponse(response('band-ap-counter-roam'), 'default'), false);
  assert.equal(worldApplyResponse(response('default'), 'default'), true);
  assert.equal(worldApplyResponse(response('default', 'GET'), 'default'), false);
  assert.equal(worldApplyResponse(response('default', 'POST', 'http://lab/api/demo/playback'), 'default'), false);
});

test('missing or malformed request data cannot acknowledge a world apply', () => {
  assert.equal(worldApplyResponse(response(undefined), 'default'), false);
  const malformed = response('default');
  malformed.request = () => ({method: () => 'POST', postDataJSON: () => { throw new Error('invalid JSON'); }});
  assert.equal(worldApplyResponse(malformed, 'default'), false);
});
