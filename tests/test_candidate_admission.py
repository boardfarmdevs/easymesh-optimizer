from io import BytesIO
import json
import urllib.error

import pytest

from optimizer.candidates import (
    CandidateMetricsBusy, CandidateMetricsError, CandidateSnapshotSuperseded,
    ControllerCandidateProvider, _default_request,
)
from .test_candidates import client, inventory, bsses, response


def test_only_explicit_unsubmitted_native_busy_is_readmitted():
    calls = []
    def request(_url, payload):
        calls.append(payload)
        if len(calls) == 1:
            raise CandidateMetricsBusy("Error_Prev_Cmd_In_Progress")
        return response()
    provider = ControllerCandidateProvider("http://controller", requester=request,
        allow_simulated=True, busy_wait_seconds=1)
    assert len(list(provider((client(),), (inventory(),), bsses(), "2026-08-21T20:00:01.000Z"))) == 1
    assert len(calls) == 2
    assert provider.last_raw[0]["native_admission_busy"] is True
    assert provider.last_raw[0]["error"] == "Error_Prev_Cmd_In_Progress"


def test_timeout_is_not_treated_as_safe_native_admission_retry():
    calls = []
    def request(*args):
        calls.append(args)
        raise CandidateMetricsError("HTTP 504")
    provider = ControllerCandidateProvider("http://controller", requester=request,
        allow_simulated=True, busy_wait_seconds=1)
    with pytest.raises(CandidateMetricsError, match="HTTP 504"):
        list(provider((client(),), (inventory(),), bsses(), "2026-08-21T20:00:01.000Z"))
    assert len(calls) == 1


def test_busy_admission_respects_superseding_world():
    valid = [True]
    def request(*args):
        valid[0] = False
        raise CandidateMetricsBusy("busy")
    provider = ControllerCandidateProvider("http://controller", requester=request,
        allow_simulated=True, busy_wait_seconds=1, generation_guard=lambda: valid[0])
    with pytest.raises(CandidateSnapshotSuperseded):
        list(provider((client(),), (inventory(),), bsses(), "2026-08-21T20:00:01.000Z"))


@pytest.mark.parametrize('reason', ['Error_Prev_Cmd_In_Progress', 'Error_Not_Ready'])
def test_transport_classifies_only_explicit_busy_rejection(monkeypatch, reason):
    error = urllib.error.HTTPError("http://controller", 503, "busy", {},
        BytesIO(json.dumps({"message": "native candidate command not accepted: " + reason}).encode()))
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(CandidateMetricsBusy):
        _default_request("http://controller", {})


@pytest.mark.parametrize('status, detail', [
    (503, {'message': 'native response omitted required measurements', 'native_completed': True}),
    (503, {'message': 'service unavailable'}),
    (504, {'message': 'Error_Not_Ready'}),
])
def test_other_errors_do_not_become_safe_admission_retries(monkeypatch, status, detail):
    error = urllib.error.HTTPError('http://controller', status, 'unavailable', {},
        BytesIO(json.dumps(detail).encode()))
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr('urllib.request.urlopen', fail)
    with pytest.raises(CandidateMetricsError) as caught:
        _default_request('http://controller', {})
    assert not isinstance(caught.value, CandidateMetricsBusy)
