#!/usr/bin/env python3
import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def request(suffix):
    with urllib.request.urlopen('http://127.0.0.1:8891/api/demo/' + suffix, timeout=10) as response:
        return json.load(response)


def link(item):
    role, container = item
    result = subprocess.run(['lxc', 'exec', container, '--', 'iw', 'dev', 'wlan0', 'link'],
                            capture_output=True, text=True, timeout=10)
    return role, {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}


deadline = time.monotonic() + 360
started = False
bindings = None
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
    while time.monotonic() < deadline:
        state = request('interactions')
        if 'disappear-reappear' not in str(state['selected_world']) or state['playback']['status'] != 'playing':
            if started:
                break
            time.sleep(1)
            continue
        current = request('current')
        if bindings is None:
            base = '/home/easymesh/easymesh-evidence/room-runs' if sys.argv[1] == 'rdk' else '/var/lib/prplmesh-lab/room-runs'
            roles = json.loads((Path(base) / current['run_id'] / 'bindings.json').read_text())['roles']
            bindings = {role: roles[role] for role in ['sta_mobile_01', 'sta_mobile_02']}
        started = True
        links = dict(workers.map(link, bindings.items()))
        after = request('interactions')
        print(json.dumps({'time_ms': state['playback']['time_ms'], 'epoch': state['environment_epoch'],
                          'epoch_stable': state['environment_epoch'] == after['environment_epoch'],
                          'expected_present': {role: state['roles'][role]['present'] for role in bindings},
                          'links': links, 'controller_clients': [client for client in current['network']['clients'] if client['role'] in bindings]}), flush=True)
        time.sleep(1)
if not started:
    raise SystemExit('No matching playback observed within the audit deadline')
