'use strict';
// The browser the room harnesses drive: on the host's GPU when it has a usable one (Vulkan
// through ANGLE), in software (SwiftShader) otherwise. Software WebGL for the room's 3D view
// and the topology page took most of a 16-thread lab host in a traffic room (Chromium near
// ten cores, the load near 20), so a screenshot of the topology page waited past 45 s
// (rdk-1004's traffic-quieter-ap, 5 October); on the GPU the same room peaked under one core.

const RENDERER_ARGUMENTS = {
  swiftshader: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
  vulkan: ['--use-angle=vulkan', '--disable-software-rasterizer'],
};
const RENDERERS = ['auto', ...Object.keys(RENDERER_ARGUMENTS)];

function softwareRenderer(actual) {
  return !actual || /swiftshader|llvmpipe|software/i.test(actual);
}

async function webglRenderer(browser) {
  const page = await browser.newPage();
  try {
    return await page.evaluate(() => {
      const context = document.createElement('canvas').getContext('webgl');
      const extension = context?.getExtension('WEBGL_debug_renderer_info');
      return extension ? context.getParameter(extension.UNMASKED_RENDERER_WEBGL) : null;
    });
  } finally {
    await page.close();
  }
}

// launchBrowser(chromium, {renderer, env, executablePath, args}): the browser and what it
// renders with ({requested, chosen, actual}). auto: Vulkan when WebGL then reports a hardware
// renderer, else SwiftShader; vulkan or swiftshader: that one, checked by the caller.
async function launchBrowser(chromium, {renderer = 'auto', env, executablePath, args = []} = {}) {
  if (!RENDERERS.includes(renderer)) throw new Error('Unsupported --renderer ' + renderer + ' (' + RENDERERS.join(', ') + ')');
  const launch = choice => chromium.launch({headless: true, env, executablePath,
    args: ['--no-sandbox', '--ozone-platform=headless', '--use-gl=angle', ...RENDERER_ARGUMENTS[choice], ...args]});
  if (renderer === 'auto') {
    let browser = null;
    try {
      browser = await launch('vulkan');
      const actual = await webglRenderer(browser);
      if (!softwareRenderer(actual)) return {browser, renderer: {requested: 'auto', chosen: 'vulkan', actual}};
    } catch (error) {
      // no usable GPU (no device, no permission, no driver): software below
    }
    if (browser) await browser.close().catch(() => {});
    const software = await launch('swiftshader');
    return {browser: software, renderer: {requested: 'auto', chosen: 'swiftshader', actual: await webglRenderer(software)}};
  }
  const browser = await launch(renderer);
  return {browser, renderer: {requested: renderer, chosen: renderer, actual: await webglRenderer(browser)}};
}

// The renderer a run can use: WebGL at all, and hardware when Vulkan was chosen.
function rendererUsable(renderer) {
  return Boolean(renderer.actual) && (renderer.chosen !== 'vulkan' || !softwareRenderer(renderer.actual));
}

module.exports = {launchBrowser, rendererUsable, softwareRenderer, RENDERERS};
