#!/usr/bin/env python3
import json
from pathlib import Path
import signal
import sys
import time
import urllib.request


MAX_ATTEMPTS = 3
SNAPSHOT_SECONDS = 10


def request(suffix):
    with urllib.request.urlopen('http://127.0.0.1:8891/api/demo/' + suffix, timeout=2) as response:
        return json.load(response)


def native_links(flavor, current, client_factory):
    evidence = Path('/home/easymesh/easymesh-evidence/room-runs' if flavor == 'rdk' else '/var/lib/prplmesh-lab/room-runs') / current['run_id']
    bindings = json.loads((evidence / 'bindings.json').read_text())['roles']
    inventory = json.loads((evidence / 'inventory.json').read_text())['radios']
    radios = {radio['container']: radio for radio in inventory}
    station = radios[bindings['sta_mobile_01']]['tx_mac']
    rows = []
    socket_path = '/run/wmediumd-control.sock' if flavor == 'rdk' else '/run/prpl-wmediumd/control.sock'
    with client_factory(socket_path) as client:
        client.socket.settimeout(2)
        for role in ['gateway', 'extender_1', 'extender_2', 'extender_3', 'extender_4']:
            for band, radio in radios[bindings[role]]['band_radios'].items():
                frequency = radio['frequency_mhz']
                down = client.get_frequency_link(radio['tx_mac'], station, frequency)
                up = client.get_frequency_link(station, radio['tx_mac'], frequency)
                rows.append({'ap': role, 'band': band, 'frequency_mhz': frequency,
                             'down': down, 'up': up, 'uplink_minus_downlink_db': up[1] - down[1]})
        return rows, client.instance_id


def collect(request_fn, links_fn, clock=time.monotonic):
    attempts = []
    deadline = clock() + SNAPSHOT_SECONDS
    selected = None
    for ordinal in range(MAX_ATTEMPTS):
        attempt = {'attempt': ordinal + 1, 'started_monotonic': clock(), 'coherent': False}
        attempts.append(attempt)
        try:
            if clock() >= deadline:
                raise TimeoutError('RF snapshot budget exhausted')
            current = request_fn('current')
            before = request_fn('interactions')
            attempt.update(run_id=current['run_id'], before=before)
            rows, daemon_id = links_fn(current)
            attempt.update(links=rows, native_daemon_instance_id=daemon_id)
            after = request_fn('interactions')
            current_after = request_fn('current')
            attempt.update(after=after, after_run_id=current_after['run_id'])
            epochs_valid = all(type(snapshot['environment_epoch']) is int and snapshot['environment_epoch'] >= 0
                               for snapshot in (before, after))
            epoch_stable = epochs_valid and before['environment_epoch'] == after['environment_epoch']
            identity_stable = (current['run_id'] == current_after['run_id']
                               and before['selected_world'] == after['selected_world']
                               and daemon_id is not None
                               and before['daemon']['instance_id'] == after['daemon']['instance_id'] == daemon_id)
            generations = {value[0] for row in rows for value in (row['up'], row['down'])}
            if not all(type(generation) is int and 0 <= generation < 2 ** 64 for generation in generations):
                raise ValueError('RF snapshot contains an invalid native generation')
            generation_stable = (len(rows) == 15 and len({(row['ap'], row['band']) for row in rows}) == 15
                                 and len(generations) == 1)
            attempt.update(epoch_stable=epoch_stable, identity_stable=identity_stable,
                           generation_stable=generation_stable, finished_monotonic=clock())
            if clock() >= deadline:
                raise TimeoutError('RF snapshot budget exhausted')
            attempt['coherent'] = epoch_stable and identity_stable and generation_stable
            if attempt['coherent']:
                selected = attempt
                break
            if not identity_stable or not epochs_valid:
                raise ValueError('RF snapshot run/world/daemon identity or epoch is invalid')
        except Exception as error:
            attempt.update(error=f'{type(error).__name__}: {error}', finished_monotonic=clock())
            break
    result = {'coherent': selected is not None, 'attempts': attempts, 'maximum_attempts': MAX_ATTEMPTS,
              'snapshot_budget_seconds': SNAPSHOT_SECONDS}
    if selected is not None:
        before = selected['before']
        result.update(world=before['selected_world'], playback=before['playback'], epoch=before['environment_epoch'],
                      epoch_after=selected['after']['environment_epoch'], epoch_stable=selected['epoch_stable'],
                      roles=before['roles'], links=selected['links'])
    return result


def emit(result):
    print(json.dumps(result, indent=2), file=sys.stdout if result['coherent'] else sys.stderr)
    return 0 if result['coherent'] else 1


def main(flavor):
    if flavor not in ('rdk', 'prpl'):
        raise ValueError('RF audit requires rdk or prpl')
    root = Path('/home/easymesh/git/meta-cmf-bananapi-vcpe' if flavor == 'rdk' else '/opt/prplmesh-lab')
    configurator = root / ('gen/medium/configurator' if flavor == 'rdk' else 'medium/configurator')
    sys.path.insert(0, str(configurator))
    from wmdcfg.actuator import ControlClient

    def expired(signum, frame):
        raise TimeoutError('RF audit exceeded its 20-second process bound')

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(20)
    try:
        return emit(collect(request, lambda current: native_links(flavor, current, ControlClient)))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
