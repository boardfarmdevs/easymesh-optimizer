import importlib.util
import json
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location('room_feature_rf_audit', Path(__file__).with_name('room-feature-rf-audit.py'))
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def snapshot(epoch):
    return {'selected_world':'asymmetric', 'environment_epoch':epoch, 'daemon':{'instance_id':'native'},
            'playback':{'time_ms':30000, 'status':'playing'}, 'roles':{'sta_mobile_01':{'present':True}}}


def links(generation):
    return [{'ap':role, 'band':band, 'frequency_mhz':frequency, 'down':[generation,54,True],
             'up':[generation,47,True], 'uplink_minus_downlink_db':-7}
            for role in ['gateway','extender_1','extender_2','extender_3','extender_4']
            for band,frequency in [('2.4',2437),('5',5180),('6',5955)]]


def collect(pairs, matrices=None, run_ids=None, clock=None):
    requests = []
    current_ids = iter(run_ids or ['same-run'] * (2 * len(pairs)))
    interactions = iter(value for pair in pairs for value in pair)
    matrices = iter(matrices or [links(219+ordinal) for ordinal in range(len(pairs))])
    reads = []

    def request(suffix):
        requests.append(suffix)
        return {'run_id':next(current_ids)} if suffix == 'current' else next(interactions)

    def native(current):
        reads.append(current)
        return next(matrices), 'native'

    result = AUDIT.collect(request, native, **({'clock':clock} if clock else {}))
    return result, requests, reads


def test_first_coherent_attempt_preserves_original_output():
    result, requests, reads = collect([(snapshot(184),snapshot(184))])
    assert result['coherent'] is result['epoch_stable'] is True
    assert result['epoch'] == result['epoch_after'] == 184
    assert result['links'] == links(219)
    assert len(reads) == 1 and requests == ['current','interactions','interactions','current']


def test_proven_epoch_race_requeries_every_native_direction_and_retains_failure():
    result, requests, reads = collect([(snapshot(184),snapshot(185)),(snapshot(185),snapshot(185))])
    assert result['coherent'] is True and result['epoch'] == result['epoch_after'] == 185
    assert result['attempts'][0]['epoch_stable'] is False
    assert result['attempts'][0]['generation_stable'] is True
    assert result['attempts'][0]['before']['environment_epoch'] == 184
    assert result['attempts'][0]['after']['environment_epoch'] == 185
    assert result['attempts'][0]['links'] == links(219)
    assert result['links'] == links(220)
    assert len(reads) == 2 and len(requests) == 8


def test_persistent_epoch_race_fails_with_all_attempts(capsys):
    result, requests, reads = collect([(snapshot(epoch),snapshot(epoch+1)) for epoch in range(3)])
    assert result['coherent'] is False and len(reads) == 3
    assert 'epoch_stable' not in result and 'links' not in result
    assert AUDIT.emit(result) == 1
    captured = capsys.readouterr()
    assert captured.out == '' and len(json.loads(captured.err)['attempts']) == 3


def test_mixed_native_generations_retry_instead_of_combining_rows():
    mixed = links(219)
    mixed[-1]['up'][0] = 220
    result, requests, reads = collect([(snapshot(184),snapshot(184))] * 2, [mixed,links(220)])
    assert result['coherent'] is True and len(reads) == 2
    assert result['attempts'][0]['epoch_stable'] is True
    assert result['attempts'][0]['generation_stable'] is False
    assert result['links'] == links(220)


def test_persistent_generation_conflict_never_selects_a_sample():
    mixed = links(219)
    mixed[-1]['up'][0] = 220
    result, requests, reads = collect([(snapshot(184),snapshot(184))] * 3, [mixed] * 3)
    assert not result['coherent'] and len(reads) == 3
    assert all(attempt['epoch_stable'] and not attempt['generation_stable'] for attempt in result['attempts'])


@pytest.mark.parametrize('generation', [None,True,'219',-1,2 ** 64])
def test_invalid_native_generation_fails_closed(generation):
    result, requests, reads = collect([(snapshot(184),snapshot(184))], [links(generation)])
    assert not result['coherent'] and len(reads) == 1
    assert 'invalid native generation' in result['attempts'][0]['error']


@pytest.mark.parametrize('change', ['world','daemon','run','missing-epoch','boolean-epoch'])
def test_identity_change_or_invalid_epoch_is_fatal_not_retried(change):
    before, after = snapshot(184), snapshot(184)
    run_ids = None
    if change == 'world':
        after['selected_world'] = 'other'
    elif change == 'daemon':
        after['daemon']['instance_id'] = 'restarted'
    elif change == 'run':
        run_ids = ['old','new']
    elif change == 'missing-epoch':
        del after['environment_epoch']
    else:
        before['environment_epoch'] = after['environment_epoch'] = True
    result, requests, reads = collect([(before,after)], run_ids=run_ids)
    assert not result['coherent'] and len(reads) == 1
    assert result['attempts'][0]['error']


def test_native_errors_are_not_hidden_by_retries():
    def failed(current):
        raise OSError('native socket disconnected')

    result = AUDIT.collect(lambda suffix: {'run_id':'same'} if suffix == 'current' else snapshot(184), failed)
    assert result['coherent'] is False and len(result['attempts']) == 1
    assert result['attempts'][0]['error'] == 'OSError: native socket disconnected'


def test_budget_exhaustion_fails_closed_without_more_reads():
    times = iter([0,0,0,11,11,11])
    result, requests, reads = collect([(snapshot(184),snapshot(184))], clock=lambda:next(times))
    assert not result['coherent'] and len(reads) == 1
    assert 'budget exhausted' in result['attempts'][0]['error']


def test_budget_exhausted_before_query_does_not_query():
    times = iter([0,10,10,10])
    result, requests, reads = collect([], clock=lambda:next(times))
    assert not result['coherent'] and requests == reads == []
    assert 'budget exhausted' in result['attempts'][0]['error']


def test_exact_deadline_is_not_accepted_as_a_fresh_snapshot():
    times = iter([0,0,0,10,10,10])
    result, requests, reads = collect([(snapshot(184),snapshot(184))], clock=lambda:next(times))
    assert not result['coherent'] and len(reads) == 1
    assert 'budget exhausted' in result['attempts'][0]['error']


def test_http_is_get_only_with_a_bounded_timeout(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *arguments):
            return None

        def read(self):
            return '{}'

    def opened(url, timeout):
        calls.append((url,timeout))
        return Response()

    monkeypatch.setattr(AUDIT.urllib.request, 'urlopen', opened)
    assert AUDIT.request('interactions') == {}
    assert calls == [('http://127.0.0.1:8891/api/demo/interactions',2)]


@pytest.mark.parametrize('flavor,socket_path', [('prpl','/run/prpl-wmediumd/control.sock'),
                                            ('rdk','/run/wmediumd-control.sock')])
def test_native_transport_only_reads_all_fifteen_pairs(monkeypatch, flavor, socket_path):
    roles = ['gateway','extender_1','extender_2','extender_3','extender_4']
    bindings = {role:role for role in roles} | {'sta_mobile_01':'client'}
    inventory = [{'container':'client','tx_mac':'client-radio'}]
    for role in roles:
        inventory.append({'container':role, 'band_radios':{
            band:{'frequency_mhz':frequency,'tx_mac':role+'-'+band}
            for band,frequency in [('2.4',2437),('5',5180),('6',5955)]}})

    def read(path):
        assert path.parent.name == 'actual-run'
        return json.dumps({'roles':bindings} if path.name == 'bindings.json' else {'radios':inventory})

    calls = []
    timeouts = []

    class Client:
        instance_id = 'native'

        def __init__(self, path):
            assert path == socket_path
            self.socket = self

        def __enter__(self):
            return self

        def __exit__(self, *arguments):
            return None

        def settimeout(self, value):
            timeouts.append(value)

        def get_frequency_link(self, source, destination, frequency):
            calls.append((source,destination,frequency))
            return (219,47 if source == 'client-radio' else 54,True)

    monkeypatch.setattr(AUDIT.Path, 'read_text', read)
    rows, daemon_id = AUDIT.native_links(flavor, {'run_id':'actual-run'}, Client)
    assert json.loads(json.dumps(rows)) == links(219) and daemon_id == 'native'
    assert len(calls) == 30 and timeouts == [2]
    for down, up in zip(calls[::2],calls[1::2]):
        assert down == (up[1],up[0],up[2]) and down[1] == 'client-radio'
