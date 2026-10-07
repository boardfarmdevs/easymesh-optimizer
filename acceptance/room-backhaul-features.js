'use strict';

const assert = require('assert').strict;
const fs = require('fs');
const path = require('path');
const os = require('os');
const {execFile} = require('child_process');
const util = require('util');
const {promisify} = util;
const {kernelClientAudit, reauditOwners, foreignDevices} = require('./room-feature-acceptance.js');
const execute = promisify(execFile);
const {hostCommand, guestAuditInstallCommand, startHostMonitor} = require('./room-host-monitor.js');
const {launchBrowser, rendererUsable} = require('./browser-renderer.js');
const rooms = ['backhaul-branch-formation', 'backhaul-parent-handover', 'backhaul-isolation-recovery'];
// Only in the room sets with the wired extender (worlds-wired): run when the catalog has it.
const wiredRooms = ['backhaul-wired-parent'];
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
// A loaded geometry room converges like a catalog room (room-feature-acceptance
// --initial-timeout, 90 s), and its load also re-parents the native backhaul.
const INITIAL_CONVERGENCE_MS = 90000;
// From a star a native branch forms in about 30 s. In this layout extender_1 and
// extender_2 hear extender_3 and extender_4 better than the gateway (20 against 13 dB
// on 5 GHz), so the native tree may start with them hanging off extender_3 and
// extender_4; the midpoint then inverts the whole tree, every extender loses its path
// at once, and re-forming took 62 s (28 Sep). The agents then onboard again and the
// optimizer needs fresh candidates for all ten clients.
const BRANCH_CONVERGENCE_MS = 150000;
// With OpenSync pods (worlds-pods) the load first moves them onto native extenders, and
// every re-parenting of such an extender drops its pod: OneWifi takes an extender's BSSs
// down with its uplink, the pod's OVSDB connection to EMOSA closes, and its agent onboards
// again (about 20 s) once the extender is back. From the default star the first room's
// pods onboarded three times and the last ended 5 s before the 90 s ran out (29 Sep), so a loaded
// room with pods gets the branch window.
const initialConvergenceMs = () =>
  adapterNodes > Object.keys(wiredContainers).length ? BRANCH_CONVERGENCE_MS : INITIAL_CONVERGENCE_MS;
// Recovery can pass before the controller has seen the room's last re-parenting. prpl-0930's
// controller dropped the wired Agent from its model and never took it back once a Wi-Fi Agent
// that had roamed off it reported it gone: an Agent drops a 1905 neighbor 63 s after its last
// discovery message, so up to a minute after recovery passed (30 Sep; prplmesh-lab patch 0033).
// So recovery holds until the extenders are on their start parents and 90 s more, at most
// 150 s, and fails when the mesh misses a node in three samples in a row (one is a blip).
const HOLD_AFTER_START_PARENTS_MS = 90000;
const HOLD_MAX_MS = 150000;
const MESH_LOSS_SAMPLES = 3;

function usage() {
  return 'usage: room-backhaul-features.js --yes-act true --host HOST --vm VM --room-url URL --topology-url URL --output DIRECTORY [--flavor rdk|prpl] [--room ROOM]';
}

function shellQuote(value) {
  return "'" + String(value).replace(/'/g, "'\\\"'\\\"'") + "'";
}

function optionsFrom(argv) {
  if (!argv.length || argv.length % 2 || argv.includes('--help')) throw new Error(usage());
  const options = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index], value = argv[index + 1];
    if (!key.startsWith('--') || !value) throw new Error(usage());
    options[key.slice(2)] = value;
  }
  for (const key of ['yes-act', 'host', 'vm', 'room-url', 'topology-url', 'output']) {
    if (!options[key]) throw new Error(usage());
  }
  return options;
}

function stackProfile(flavor) {
  assert.ok(['rdk', 'prpl'].includes(flavor), 'Unknown stack');
  return flavor === 'rdk' ?
    {containers: {gateway: 'bpibroadband', extender_1: 'bpiap', extender_2: 'bpiap-001', extender_3: 'bpiap-002', extender_4: 'bpiap-003'},
      ap: 'wifi1.1', station: 'wifi1.3', bridge: 'brlan0', gateway: '10.0.0.1', healthNodes: 6} :
    {containers: {gateway: 'prpl-controller', extender_1: 'prpl-agent-01', extender_2: 'prpl-agent-02', extender_3: 'prpl-agent-03', extender_4: 'prpl-agent-04'},
      ap: 'wlan2.1', station: 'wlan3', bridge: 'br-lan', gateway: '192.168.77.1', healthNodes: 5};
}

function parentPaths(parents) {
  return Object.fromEntries(Object.keys(parents).sort().map(role => {
    const chain = [];
    let current = role;
    while (current && current !== 'gateway' && !chain.includes(current)) {
      chain.push(current);
      current = parents[current];
    }
    chain.push(current || null);
    return [role, chain];
  }));
}

// AP roles beyond the lab's five (OpenSync pods through the EMOSA adapter, an extender on
// a wired backhaul), from the room's catalog; each adds a topology and a health node.
let adapterNodes = 0;
// The APs on a wired backhaul (role -> container), from the room's catalog: read like the
// lab's own nodes; each is a child of the gateway over its LAN port, never over Wi-Fi.
let wiredContainers = {};

// Every mesh node in the controller's model: the room's health and the topology view.
function meshComplete(entry, healthNodes) {
  return entry.health?.topology_nodes === healthNodes + adapterNodes &&
    entry.topology?.nodes.length === 6 + adapterNodes;
}

// The index of the sample that completes the first run of MESH_LOSS_SAMPLES samples without
// every mesh node, or -1.
function meshLoss(entries, healthNodes) {
  let run = 0;
  return entries.findIndex(entry => (run = meshComplete(entry, healthNodes) ? 0 : run + 1) >= MESH_LOSS_SAMPLES);
}

function ready(entry, healthNodes, clients = 10) {
  const wired = Object.keys(wiredContainers);
  return Object.keys(entry.native.nodes).length === 5 + wired.length &&
    Object.keys(entry.native.parents).length === 4 + wired.length &&
    wired.every(role => entry.native.nodes[role]?.wiredUplink === true && entry.native.nodes[role].fronthaulAps === 6) &&
    Object.values(entry.native.parents).every(Boolean) &&
    Object.values(parentPaths(entry.native.parents)).every(chain => chain.at(-1) === 'gateway') &&
    // A wired extender's readiness is its fronthaul, its LAN port in the bridge and no Wi-Fi
    // parent; its own backhaul BSS and LAN address come from its unit (gen/wired-extender.sh),
    // and a Wi-Fi extender taking it as its parent is the wired room's own check.
    Object.entries(entry.native.nodes).filter(([role]) => !wired.includes(role))
      .every(([, node]) => node.pingOk && node.fronthaulAps === 6 && node.apOperating) &&
    entry.health?.healthy && meshComplete(entry, healthNodes) && entry.health.api_active === clients &&
    entry.optimizer?.fleet?.converged === true &&
    new Set(entry.topology.stations.map(station => station.mac)).size === clients;
}

function nativeCycles(parents) {
  const cycles = new Map();
  for (const start of Object.keys(parents)) {
    const visited = new Map();
    const path = [];
    let current = start;
    while (current && Object.hasOwn(parents, current)) {
      if (visited.has(current)) {
        const cycle = path.slice(visited.get(current));
        const key = [...cycle].sort().join(',');
        if (!cycles.has(key)) cycles.set(key, cycle);
        break;
      }
      visited.set(current, path.length);
      path.push(current);
      current = parents[current];
    }
  }
  return [...cycles.values()];
}

function convergenceDiagnostics(entry) {
  return {healthy: entry.health?.healthy, activeClients: entry.health?.api_active,
    topologyNodes: entry.topology?.nodes.length, topologyClients: entry.topology?.stations.length,
    fleet: entry.optimizer?.fleet,
    nodes: Object.fromEntries(Object.entries(entry.native.nodes).map(([role, node]) => [role,
      {apOperating: node.apOperating, fronthaulAps: node.fronthaulAps,
        parentBssid: node.parentBssid, pingOk: node.pingOk, error: node.error}]))};
}

function interfaceState(raw) {
  const apInfo = raw.split(/Connected to|Not connected/)[0];
  return {apBssid: apInfo.match(/\baddr ([0-9a-f:]{17})/i)?.[1]?.toLowerCase(),
    apOperating: /\bssid mesh_backhaul\b/.test(apInfo) && /\bchannel 36 \(5180 MHz\)/.test(apInfo),
    fronthaulAps: Number(raw.match(/FRONTHAUL_APS=(\d+)/)?.[1] ?? NaN),
    eth1Master: raw.match(/ETH1_MASTER=(\S*)/)?.[1] || null,
    parentBssid: raw.match(/Connected to ([0-9a-f:]{17})/i)?.[1]?.toLowerCase() || null,
    pingOk: /PROBE_EXIT=0\b/.test(raw)};
}

function summarizeNative(samples, room) {
  const parents = samples.map(sample => sample.native?.parents || {});
  const isolated = samples.some(sample => sample.native?.parents.extender_4 === null && sample.native?.nodes.extender_4?.pingOk === false);
  return room === 'backhaul-branch-formation'
    ? {branchObserved: parents.some(value => value.extender_3 === 'extender_1' && value.extender_4 === 'extender_2')}
    : room === 'backhaul-parent-handover'
      ? {lowerRelayObserved: parents.some(value => value.extender_3 === 'extender_2')}
      : room === 'backhaul-wired-parent'
        ? {wiredParentObserved: parents.some(value => value.extender_3 === 'extender_5')}
        : {upstreamOutageObserved: isolated};
}

async function run(options) {
  for (const key of ['host', 'vm']) assert.match(options[key], /^[a-zA-Z0-9_.-]+$/);
  assert.equal(options['yes-act'], 'true', 'Explicit --yes-act true is required; these rooms change live RF and can interrupt service');
  assert.match(options['room-url'], /^https?:\/\/.+/, usage());
  assert.match(options['topology-url'], /^https?:\/\/.+/, usage());
  let selectedRooms = options.room ? [options.room] : [...rooms, ...wiredRooms];
  const flavor = options.flavor || 'rdk';
  const profile = stackProfile(flavor);
  const containers = profile.containers;
  assert.ok(selectedRooms.every(id => rooms.includes(id) || wiredRooms.includes(id)), 'Unknown geometry room');
  const directory = path.resolve(options.output);
  assert.ok(!fs.existsSync(directory), 'Use a new output directory');
  fs.mkdirSync(directory, {recursive: true});
  const save = (name, value) => fs.writeFileSync(path.join(directory, name), JSON.stringify(value, null, 2) + '\n');
  const report = {flavor, browserHost: os.hostname(), labHost: options.host, vm: options.vm,
    started: new Date().toISOString(), scope: 'Geometry-room playback, native parent/traffic convergence and restoration; bounded, not a soak',
    rooms: [], errors: [], featureChecksPassed: false, recoveryPassed: false};
  await execute(...guestAuditInstallCommand(options.host, options.vm), {timeout: 30000, maxBuffer: 1048576});
  // devices the room does not own (the VM's foreign-devices file): out of the topology samples
  const foreign = await foreignDevices({...options, flavor});
  report.foreignDevices = foreign;
  const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
  const environment = {...process.env};
  delete environment.DISPLAY;
  // auto (the default): the host's GPU when it has a usable one, else software (browser-renderer.js)
  const {browser, renderer: rendering} = await launchBrowser(chromium, {renderer: options.renderer || 'auto',
    env: environment, executablePath: process.env.CHROMIUM_PATH,
    args: ['--disable-background-timer-throttling', '--disable-renderer-backgrounding']});
  const context = await browser.newContext({viewport: {width: 1280, height: 900}});
  context.setDefaultTimeout(45000);
  const roomPage = await context.newPage();
  const topologyPage = await context.newPage();
  const base = options['room-url'].replace(/\/$/, '');
  let leaseToken = null;
  let changed = false;
  let baseline = null;
  let currentRoom = null;
  let bindings = {};
  let hostMonitor = null;
  const request = async endpoint => {
    const response = await context.request.get(base + endpoint, {timeout: 15000});
    if (!response.ok()) throw new Error(endpoint + ': HTTP ' + response.status());
    return response.json();
  };
  roomPage.on('request', request => {
    if (request.method() === 'POST' && request.url().endsWith('/api/demo/world/apply')) {
      leaseToken = request.postDataJSON()?.token || leaseToken;
    }
  });
  for (const page of [roomPage, topologyPage]) page.on('pageerror', error => report.errors.push(error.message));

  async function identity() {
    const result = await execute(...hostCommand(options.host, 'lxc exec ' + options.vm +
      ' -- python3 /tmp/room-feature-guest-audit.py identity ' + flavor), {timeout: 45000, maxBuffer: 1048576});
    return JSON.parse(result.stdout);
  }

  async function native() {
    if (flavor === 'prpl') {
      const result = await execute(...hostCommand(options.host, 'lxc exec ' + options.vm +
        ' -- python3 /tmp/backhaul-native-probe.py'), {timeout: 25000, maxBuffer: 1048576});
      const nodes = Object.fromEntries(Object.entries(JSON.parse(result.stdout)).map(([role, value]) =>
        [role, {...value, ...interfaceState(value.raw)}]));
      const owners = Object.fromEntries(Object.entries(nodes).map(([role, value]) => [value.apBssid, role]));
      // a wired Agent's parent is the gateway over its LAN port, and only while it has no
      // Wi-Fi parent (that would be a second path into the LAN)
      for (const role of Object.keys(wiredContainers)) {
        const node = nodes[role];
        if (node) node.wiredUplink = node.eth1Master === profile.bridge && !node.parentBssid && node.pingOk === true;
      }
      const parents = Object.fromEntries(Object.entries(nodes).filter(([role]) => role !== 'gateway')
        .map(([role, value]) => [role, role in wiredContainers ? (value.wiredUplink ? 'gateway' : null)
          : owners[value.parentBssid] || null]));
      return {nodes, parents, paths: parentPaths(parents)};
    }
    const readings = await Promise.all(Object.entries({...containers, ...wiredContainers}).map(async ([role, container]) => {
      const script = 'iw dev wifi1.1 info; iw dev wifi1.3 link 2>/dev/null; ping -I brlan0 -q -c 1 -W 1 10.0.0.1 >/dev/null 2>&1; printf "\\nPROBE_EXIT=%s\\n" "$?"; printf "FRONTHAUL_APS=%s\\n" "$(iw dev | grep -Ec \"ssid (private_ssid|iot_ssid)$\")"; printf "ETH1_MASTER=%s\\n" "$(basename \"$(readlink /sys/class/net/eth1/master)\")"';
      try {
        const result = await execute(...hostCommand(options.host,
          'lxc exec ' + options.vm + ' -- lxc exec ' + container + " -- sh -c '" + script.replace('wifi1.1', profile.ap).replace('wifi1.3', profile.station).replace('brlan0', profile.bridge).replace('10.0.0.1', profile.gateway) + "'"),
        {timeout: 12000, maxBuffer: 65536});
        return [role, {...interfaceState(result.stdout), raw: result.stdout}];
      } catch (error) { return [role, {error: error.message, pingOk: null}]; }
    }));
    const nodes = Object.fromEntries(readings);
    const owners = Object.fromEntries(readings.filter(([, value]) => value.apBssid).map(([role, value]) => [value.apBssid, role]));
    // a wired AP's parent is the gateway over its LAN port, and only while no station of it
    // has a Wi-Fi parent (that would be a second path into the LAN)
    for (const role of Object.keys(wiredContainers)) {
      const node = nodes[role];
      if (node) node.wiredUplink = node.eth1Master === profile.bridge && !node.parentBssid;
    }
    const parents = Object.fromEntries(readings.filter(([role]) => role !== 'gateway')
      .map(([role, value]) => [role, role in wiredContainers ? (value.wiredUplink ? 'gateway' : null)
        : owners[value.parentBssid] || null]));
    return {nodes, parents, paths: parentPaths(parents)};
  }

  async function auditClientsOnce(observation) {
    const mapping = Object.fromEntries(Object.entries(bindings).map(([role, client]) => [role, client.container]));
    assert.ok(Object.values(mapping).every(value => /^(?:prpl-client-[0-9]{2,3}|wlan-client(?:-[0-9]{3})?)$/.test(value)));
    const wanted = Object.entries(observation.interactions.roles)
      .filter(([role, value]) => value.present && bindings[role]).map(([role]) => bindings[role].sta_mac);
    const model = observation.clients.map(client => ({mac: client.sta_mac, bssid: client.connected_bssid}));
    const response = await execute(...hostCommand(options.host, 'lxc exec ' + options.vm +
      " -- python3 /tmp/room-feature-guest-audit.py links '" + JSON.stringify(mapping) + "'"),
      {timeout: 45000, maxBuffer: 1048576});
    return kernelClientAudit(bindings, wanted, model, JSON.parse(response.stdout));
  }

  async function auditClients(observation) {
    // a client moved between the model sample and the links is audited again (reauditOwners)
    const result = await reauditOwners(await auditClientsOnce(observation),
      async () => auditClientsOnce(await sample('owner-reaudit')));
    assert.equal(result.passed, true, JSON.stringify(result.errors));
    return result;
  }

  async function sample(label, includeNative = false) {
    const [state, interactions, topology, visible] = await Promise.all([
      request('/api/demo/current'), request('/api/demo/interactions'),
      topologyPage.evaluate(foreign => {
        const instance = window.EasyMeshController;
        const own = id => !foreign.includes(String(id ?? '').toLowerCase());
        return {nodes: (instance?.topology?.nodes || []).filter(node => own(node.id)).map(node => ({id: node.id, name: node.name})),
          edges: (instance?.topology?.edges || []).filter(edge => own(edge.from) && own(edge.to))
            .map(edge => ({from: String(edge.from), to: String(edge.to)})),
          stations: [...document.querySelectorAll('#topology-visualization .sta-node')]
            .filter(element => own(element.__data__?.nodeRef?.id)).map(element => ({
              mac: element.__data__?.sta?.staMAC, bssid: element.__data__?.sta?.bssid}))};
      }, foreign),
      roomPage.evaluate(() => ({policy: document.getElementById('backhaulPolicyTitle').textContent,
        clock: document.getElementById('tnow').textContent, meta: document.getElementById('worldmeta').textContent,
        optimizer: document.getElementById('optimizerStatus').textContent})),
    ]);
    const result = {at: new Date().toISOString(), label, interactions, visible, topology,
      health: state.health, mesh: state.network?.mesh, clients: state.network?.clients, optimizer: state.optimizer,
      native: includeNative ? await native() : undefined};
    if (includeNative) result.nativeCycles = nativeCycles(result.native.parents);
    if (currentRoom) currentRoom.samples.push(result);
    return result;
  }

  async function load(id) {
    await roomPage.bringToFront();
    if (await roomPage.evaluate(() => Boolean(document.fullscreenElement))) await roomPage.locator('#roomFullscreen').click();
    await roomPage.waitForFunction(() => !document.fullscreenElement);
    changed = true;
    const [response] = await Promise.all([
      // up to 150 s: before a geometry room the OpenSync pods move to their parents in it
      roomPage.waitForResponse(response => response.url().endsWith('/api/demo/world/apply') && response.request().method() === 'POST', {timeout: 150000}),
      id === 'default' ? roomPage.locator('#defaultWorld').click() : roomPage.locator('#world').selectOption(id),
    ]);
    const result = await response.json();
    assert.ok(response.ok(), JSON.stringify(result));
    assert.equal(result.backhaul_rf_verified, true);
    await roomPage.waitForFunction(() => !document.getElementById('world').disabled, null, {timeout: 60000});
    await roomPage.locator('#roomFullscreen').click();
    await roomPage.waitForFunction(() => document.fullscreenElement?.id === 'roomView');
    return result;
  }

  async function playTo(time) {
    await roomPage.bringToFront();
    const button = await roomPage.evaluate(() => document.fullscreenElement ? '#fullscreenPlay' : '#play');
    await roomPage.locator(button).click();
    const deadline = Date.now() + 35000;
    while (Date.now() < deadline) {
      const state = await request('/api/demo/interactions');
      if (state.playback.time_ms === time && state.playback.status !== 'playing') return state;
      assert.ok(!state.fault, 'Room fault: ' + state.fault);
      await delay(500);
    }
    throw new Error('Playback did not reach ' + time + ' within the short-test deadline');
  }

  try {
    hostMonitor = await startHostMonitor(options.host, directory, {processes: true});
    report.before = await identity();
    report.renderer = rendering;
    assert.ok(rendererUsable(report.renderer), 'Requested renderer unavailable: ' + JSON.stringify(report.renderer));
    const before = await request('/api/demo/current');
    baseline = await request('/api/demo/interactions');
    assert.equal(before.health.healthy, true, 'Start with a healthy default lab');
    assert.equal(before.health.api_active, 20);
    assert.equal(baseline.backhaul_policy, 'fixed-startup-mesh');
    assert.equal(baseline.lease.held, false, 'Do not take another operator’s lease');
    save('baseline.json', {health: before.health, mesh: before.network.mesh, backhaul: baseline.backhaul_links, daemon: baseline.daemon});
    const catalog = await request('/api/demo/worlds');
    adapterNodes = Math.max(0, (catalog.mesh_devices ?? 5) - 5);
    wiredContainers = catalog.wired_bindings || {};
    for (const container of Object.values(wiredContainers)) assert.match(String(container), /^[a-zA-Z0-9_.-]+$/);
    bindings = catalog.client_bindings;
    assert.equal(Object.keys(bindings).length, 100);
    for (const id of rooms) assert.equal(catalog.worlds.find(entry => entry.id === id)?.backhaul_rf, 'geometry');
    if (!options.room) selectedRooms = selectedRooms.filter(id => rooms.includes(id) ||
      catalog.worlds.some(entry => entry.id === id && entry.backhaul_rf === 'geometry'));
    await roomPage.goto(base + '/');
    await roomPage.waitForFunction(() => window.__viewer && !document.getElementById('world').disabled, null, {timeout: 60000});
    await topologyPage.goto(options['topology-url']);
    await topologyPage.locator('[data-tab="topology"]').click();
    await topologyPage.waitForSelector('.sta-node', {timeout: 30000});
    await topologyPage.locator('#topologyFullscreen').click();
    await roomPage.bringToFront();
    await roomPage.locator('#roomFullscreen').click();
    await roomPage.waitForFunction(() => document.fullscreenElement?.id === 'roomView');
    report.startParents = (await sample('start', true)).native.parents;
    for (const id of selectedRooms) {
      currentRoom = {id, samples: []};
      report.rooms.push(currentRoom);
      console.log(id + ': loading geometry RF');
      currentRoom.load = await load(id);
      let loaded = await sample('loaded', true);
      {
        const deadline = Date.now() + initialConvergenceMs();
        while (!ready(loaded, profile.healthNodes) && Date.now() < deadline) {
          await delay(1000);
          loaded = await sample('initial-client-convergence', true);
        }
        currentRoom.initialReadiness = convergenceDiagnostics(loaded);
        assert.equal(ready(loaded, profile.healthNodes), true,
          'The loaded ten-client room must converge before testing its movement: ' +
          JSON.stringify(currentRoom.initialReadiness));
        currentRoom.initialConvergenceVerified = true;
        currentRoom.initialKernel = await auditClients(loaded);
        currentRoom.initialParents = loaded.native.parents;
      }
      if (id === 'backhaul-isolation-recovery') {
        const deadline = Date.now() + 15000;
        while (loaded.native.nodes.extender_4.pingOk !== true && Date.now() < deadline) {
          await delay(1000);
          loaded = await sample('initial-reachability', true);
        }
        assert.equal(loaded.native.nodes.extender_4.pingOk, true, 'Isolation requires a verified working upstream link before movement');
        currentRoom.initialUpstreamVerified = true;
      }
      assert.ok(Object.values(loaded.native.nodes).every(node => !node.error), 'Missing out-of-band native observations');
      // (a wired extender's own backhaul BSS waits for RDK to act on its Ethernet uplink, plan 1.2)
      assert.ok(Object.entries(loaded.native.nodes).every(([role, node]) =>
        (node.apOperating || role in wiredContainers) && node.fronthaulAps === 6),
        'Geometry rooms require operating backhaul and client-facing APs, not only configured BSS records');
      assert.equal(loaded.interactions.backhaul_policy, 'modeled');
      assert.equal(loaded.interactions.backhaul_authority, 'native');
      assert.equal(loaded.interactions.expected_online_clients, 10);
      assert.match(loaded.visible.policy, /Geometry-driven backhaul · native parent selection/);
      assert.equal(loaded.interactions.daemon.instance_id, baseline.daemon.instance_id);
      currentRoom.midpoint = await playTo(12000);
      assert.notDeepEqual(currentRoom.midpoint.backhaul_links, loaded.interactions.backhaul_links);
      for (let index = 0; index < 3; index++) {
        const observation = await sample('midpoint-' + index, true);
        assert.ok(Object.values(observation.native.nodes).every(node => !node.error), 'Missing native midpoint observations');
        if (index < 2) await delay(1500);
      }
      if (id === 'backhaul-isolation-recovery') {
        const links = currentRoom.midpoint.backhaul_links.filter(link => [link.source_role, link.destination_role].includes('extender_4'));
        assert.ok(links.length > 0 && links.every(link => link.snr_db === -20), 'Isolation must affect real mesh RF');
        assert.equal(currentRoom.midpoint.roles.extender_4.present, true, 'This must not be a fronthaul-disable shortcut');
      }
      currentRoom.nativeOutcome = summarizeNative(currentRoom.samples.filter(entry => entry.label.startsWith('midpoint')), id);
      if (id === 'backhaul-branch-formation') {
        const deadline = Date.now() + BRANCH_CONVERGENCE_MS;
        let observation = currentRoom.samples.at(-1);
        const converged = entry => summarizeNative([entry], id).branchObserved && ready(entry, profile.healthNodes) &&
          entry.mesh?.backhaul_edges?.some(edge => edge.child_role === 'extender_3' && edge.parent_role === 'extender_1') &&
          entry.mesh?.backhaul_edges?.some(edge => edge.child_role === 'extender_4' && edge.parent_role === 'extender_2');
        while (!converged(observation) && Date.now() < deadline) {
          await delay(1000);
          observation = await sample('midpoint-branch-convergence', true);
        }
        currentRoom.nativeOutcome = {...summarizeNative([observation], id), convergenceVerified: converged(observation)};
        assert.equal(currentRoom.nativeOutcome.convergenceVerified, true,
          'Branch must recover native uplinks, traffic, both topology views and all ten client decisions; parent paths: ' +
          JSON.stringify(observation.native.paths));
      }
      if (id !== 'backhaul-branch-formation') {
        const deadline = Date.now() + 60000;
        let observation = currentRoom.samples.at(-1);
        const verified = entry => id === 'backhaul-parent-handover' ?
          summarizeNative([entry], id).lowerRelayObserved && ready(entry, profile.healthNodes) &&
            entry.mesh?.backhaul_edges?.some(edge => edge.child_role === 'extender_3' && edge.parent_role === 'extender_2') :
          id === 'backhaul-wired-parent' ?
            // extender_3 on the wired extender's backhaul BSS, natively and in the controller's model
            summarizeNative([entry], id).wiredParentObserved && ready(entry, profile.healthNodes) &&
              entry.mesh?.backhaul_edges?.some(edge => edge.child_role === 'extender_3' && edge.parent_role === 'extender_5') :
          summarizeNative([entry], id).upstreamOutageObserved && entry.native.nodes.extender_4.apOperating &&
            entry.native.nodes.extender_4.fronthaulAps === 6;
        while (!verified(observation) && Date.now() < deadline) {
          await delay(1000);
          observation = await sample('midpoint-native-convergence', true);
        }
        currentRoom.nativeOutcome = {...summarizeNative([observation], id), convergenceVerified: verified(observation)};
        currentRoom.nativeOutcome.nodes = convergenceDiagnostics(observation).nodes;
        assert.equal(currentRoom.nativeOutcome.convergenceVerified, true,
          'Native handover/isolation was not verified: ' + JSON.stringify(currentRoom.nativeOutcome));
      }
      currentRoom.relayApOperating = Object.fromEntries(Object.entries(currentRoom.samples.at(-1).native.nodes)
        .filter(([role]) => role !== 'gateway').map(([role, value]) => [role, value.apOperating]));
      await roomPage.bringToFront();
      await roomPage.screenshot({path: path.join(directory, id + '-room.png')});
      await topologyPage.bringToFront();
      await topologyPage.screenshot({path: path.join(directory, id + '-topology.png')});
      currentRoom.finish = await playTo(24000);
      assert.deepEqual(currentRoom.finish.backhaul_links, loaded.interactions.backhaul_links);
      currentRoom.returnObservation = await sample('returned', true);
      {
        const deadline = Date.now() + 60000;
        const returned = entry => ready(entry, profile.healthNodes) &&
          (id !== 'backhaul-parent-handover' || (entry.native.parents.extender_3 === 'extender_1' &&
            entry.mesh?.backhaul_edges?.some(edge => edge.child_role === 'extender_3' && edge.parent_role === 'extender_1')));
        while (!returned(currentRoom.returnObservation) && Date.now() < deadline) {
          await delay(1000);
          currentRoom.returnObservation = await sample('return-recovery', true);
        }
        currentRoom.nativeOutcome.returnRecoveryObserved = returned(currentRoom.returnObservation);
        if (id === 'backhaul-parent-handover')
          currentRoom.nativeOutcome.upperRelayRestored = currentRoom.returnObservation.native.parents.extender_3 === 'extender_1';
        assert.equal(currentRoom.nativeOutcome.returnRecoveryObserved, true, 'Ten-client native recovery must converge after return');
        currentRoom.returnKernel = await auditClients(currentRoom.returnObservation);
      }
      // A native backhaul cycle in any sample: recorded on both stacks, a failure on
      // prplMesh's (where the check was qualified)
      currentRoom.nativeOutcome.observedCycles = currentRoom.samples.flatMap(entry =>
        (entry.nativeCycles || []).map(cycle => ({at: entry.at, label: entry.label, cycle})));
      if (flavor === 'prpl') assert.deepEqual(currentRoom.nativeOutcome.observedCycles, [], 'Native backhaul cycles must not occur');
      currentRoom.featureChecksPassed = true;
      currentRoom.status = 'passed';
      save(id + '.json', currentRoom);
      console.log(JSON.stringify({room: id, featureChecksPassed: true, native: currentRoom.nativeOutcome}));
    }
    report.featureChecksPassed = report.rooms.every(entry => entry.featureChecksPassed) && report.errors.length === 0;
  } catch (error) {
    report.failure = error.stack;
    if (currentRoom && !currentRoom.featureChecksPassed) {
      currentRoom.status = 'failed';
      currentRoom.failureReasons = [error.message];
    }
    for (const id of selectedRooms.filter(id => !report.rooms.some(room => room.id === id))) {
      report.rooms.push({id, status: 'blocked', featureChecksPassed: false,
        failureReasons: ['Earlier geometry check failed; no further RF mutation attempted']});
    }
    console.error(error.message);
  } finally {
    if (changed && baseline) {
      try {
        console.log('Restoring default RF and checking native recovery (at most 60 s, then a hold of at most 150 s)');
        await load('default');
        currentRoom = null;
        const restored = await request('/api/demo/interactions');
        assert.equal(restored.backhaul_policy, 'fixed-startup-mesh');
        assert.deepEqual(restored.backhaul_links, baseline.backhaul_links);
        const deadline = Date.now() + 60000;
        while (Date.now() < deadline) {
          const recovery = await sample('default-recovery', true);
          report.recovery = recovery;
          if (ready(recovery, profile.healthNodes, 20)) {
            report.recoveryPassed = true;
            break;
          }
          await delay(1500);
        }
        if (!report.recoveryPassed) report.recoveryFailure = 'Fixed RF restored, but native recovery not verified within 60 seconds; no forced parent changes or native restarts attempted';
        if (report.recoveryPassed) {
          const hold = {started: new Date().toISOString(), startParentsAt: null, samples: []};
          const entries = [];
          const limit = Date.now() + HOLD_MAX_MS;
          let until = limit;
          while (Date.now() < until) {
            await delay(1500);
            const entry = await sample('recovery-hold', true);
            entries.push(entry);
            hold.samples.push({at: entry.at, parents: entry.native.parents, meshComplete: meshComplete(entry, profile.healthNodes),
              topologyNodes: entry.topology?.nodes.length, healthNodes: entry.health?.topology_nodes});
            if (meshLoss(entries, profile.healthNodes) !== -1) {
              report.recoveryPassed = false;
              report.recoveryFailure = 'The mesh lost a node after recovery (' + MESH_LOSS_SAMPLES +
                ' samples in a row): ' + JSON.stringify(convergenceDiagnostics(entry));
              break;
            }
            if (!hold.startParentsAt && util.isDeepStrictEqual(entry.native.parents, report.startParents)) {
              hold.startParentsAt = entry.at;
              until = Math.min(limit, Date.now() + HOLD_AFTER_START_PARENTS_MS);
            }
          }
          hold.finished = new Date().toISOString();
          report.recoveryHold = hold;
        }
      } catch (error) { report.recoveryFailure = error.stack; }
    }
    if (leaseToken) {
      try { await context.request.delete(base + '/api/demo/interactions/lease', {data: {token: leaseToken, command_id: 'backhaul-short-release-' + Date.now()}}); }
      catch (error) { report.errors.push('Lease release: ' + error.message); }
    }
    report.finished = new Date().toISOString();
    try {
      report.after = await identity();
      assert.deepEqual(report.after, report.before, 'Native process identities changed');
      report.nativeIdentitiesUnchanged = true;
    } catch (error) { report.errors.push(error.message); report.nativeIdentitiesUnchanged = false; }
    if (hostMonitor) {
      try {
        report.hostMonitor = await hostMonitor.stop();
        if (report.hostMonitor.error) report.errors.push('Host monitor: ' + report.hostMonitor.error);
      } catch (error) { report.errors.push('Host monitor: ' + error.message); }
    }
    report.featureChecksPassed = report.featureChecksPassed && report.errors.length === 0;
    save('report.json', report);
    await browser.close();
  }
  return report;
}

module.exports = {summarizeNative, interfaceState, stackProfile, ready, meshComplete, meshLoss, parentPaths, nativeCycles,
  convergenceDiagnostics};
if (require.main === module) {
  let options;
  try { options = optionsFrom(process.argv.slice(2)); }
  catch (error) { console.error(error.message); process.exit(2); }
  run(options).then(report => {
    console.log(JSON.stringify({featureChecksPassed: report.featureChecksPassed, recoveryPassed: report.recoveryPassed, output: options.output}));
    process.exitCode = report.featureChecksPassed && report.recoveryPassed ? 0 : 1;
  }).catch(error => { console.error(error); process.exitCode = 2; });
}
