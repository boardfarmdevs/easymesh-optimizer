'use strict';
// browser-renderer.js with a stand-in for Playwright's chromium: auto takes the GPU when WebGL
// reports a hardware renderer, else software; an explicit renderer is launched as asked.
const assert = require('assert');
const {launchBrowser, rendererUsable, softwareRenderer} = require('./browser-renderer.js');

function fakeChromium(rendererFor) {
  const launches = [];
  return {
    launches,
    async launch({args}) {
      const angle = args.find(a => a.startsWith('--use-angle=')).split('=')[1];
      launches.push(angle);
      const actual = rendererFor(angle);
      if (actual instanceof Error) throw actual;
      const browser = {
        closed: false,
        async newPage() { return {async evaluate() { return actual; }, async close() {}}; },
        async close() { browser.closed = true; },
      };
      return browser;
    },
  };
}

(async () => {
  const intel = 'ANGLE (Intel, Vulkan 1.3.255 (Intel(R) Graphics (ADL GT2)), Intel open-source Mesa driver)';
  const swift = 'ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)';

  // a GPU: auto keeps the Vulkan browser
  let chromium = fakeChromium(angle => angle === 'vulkan' ? intel : swift);
  let result = await launchBrowser(chromium, {});
  assert.deepEqual(chromium.launches, ['vulkan']);
  assert.deepEqual(result.renderer, {requested: 'auto', chosen: 'vulkan', actual: intel});
  assert.ok(rendererUsable(result.renderer));

  // Vulkan only in software (llvmpipe): auto closes it and takes SwiftShader
  chromium = fakeChromium(angle => angle === 'vulkan' ? 'ANGLE (Mesa, llvmpipe (LLVM 17.0.6, 256 bits))' : swift);
  result = await launchBrowser(chromium, {renderer: 'auto'});
  assert.deepEqual(chromium.launches, ['vulkan', 'swiftshader']);
  assert.equal(result.renderer.chosen, 'swiftshader');
  assert.ok(rendererUsable(result.renderer));

  // no GPU at all (the launch fails): software
  chromium = fakeChromium(angle => angle === 'vulkan' ? new Error('no Vulkan device') : swift);
  result = await launchBrowser(chromium, {});
  assert.equal(result.renderer.chosen, 'swiftshader');

  // asked for explicitly: launched as asked, and Vulkan in software is not usable
  chromium = fakeChromium(() => swift);
  result = await launchBrowser(chromium, {renderer: 'vulkan'});
  assert.deepEqual(chromium.launches, ['vulkan']);
  assert.ok(!rendererUsable(result.renderer));
  result = await launchBrowser(fakeChromium(() => swift), {renderer: 'swiftshader'});
  assert.ok(rendererUsable(result.renderer));
  result = await launchBrowser(fakeChromium(() => null), {renderer: 'swiftshader'});
  assert.ok(!rendererUsable(result.renderer), 'no WebGL at all is never usable');

  await assert.rejects(launchBrowser(fakeChromium(() => swift), {renderer: 'opengl'}), /Unsupported --renderer/);
  assert.ok(softwareRenderer(null) && softwareRenderer(swift) && !softwareRenderer(intel));
  console.log('PASS browser-renderer: auto takes the GPU when WebGL reports hardware, else software');
})().catch(error => { console.error(error); process.exit(1); });
