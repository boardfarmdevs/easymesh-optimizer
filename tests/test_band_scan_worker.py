import pytest

from optimizer.band_scan_worker import coordinated_scan


STATION = "02:00:00:10:01:00"
BSSID = "02:00:00:00:00:00"
STATUS = f"address={STATION}\nbssid={BSSID}\nssid=private_ssid\nwpa_state=COMPLETED\n"


class Connection:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []

    def settimeout(self, remaining):
        assert 0 < remaining <= 5

    def send(self, command):
        self.sent.append(command.decode())

    def recv(self, _size):
        return next(self.replies).encode()


def collect(connection):
    boots = iter([100, 100.2])
    return coordinated_scan(connection, STATION, BSSID, "private_ssid", [2437, 5180],
                            clock=lambda: 0, boottime=lambda: next(boots))


def test_native_scan_only_is_correlated_to_its_completion_and_cannot_select_a_network():
    connection = Connection(["OK", STATUS, "<3>CTRL-EVENT-SCAN-STARTED", "17", "<3>CTRL-EVENT-SCAN-RESULTS id=16",
                             "<3>CTRL-EVENT-SCAN-RESULTS id=17", STATUS])
    result = collect(connection)
    assert result["scan_id"] == 17 and result["completed_boottime"] == 100.2
    assert connection.sent[2] == "SCAN TYPE=ONLY freq=2437,5180 passive=1 only_new=1 use_id=1"
    assert result["mode"] == "passive"
    assert all(not command.startswith(("ROAM", "REASSOCIATE", "SELECT_NETWORK")) for command in connection.sent)


@pytest.mark.parametrize("events", [["FAIL-BUSY"], ["17", "<3>CTRL-EVENT-SCAN-FAILED ret=-16"],
                                   ["17", "<3>CTRL-EVENT-DISCONNECTED"],
                                   ["17", "<3>CTRL-EVENT-SCAN-RESULTS id=17", "wpa_state=DISCONNECTED"]])
def test_native_busy_scan_failure_and_association_changes_are_explicit(events):
    with pytest.raises(RuntimeError):
        collect(Connection(["OK", STATUS, *events]))


def test_namespace_or_station_identity_mismatch_cannot_trigger_a_scan():
    connection = Connection(["OK", STATUS.replace(STATION, BSSID)])
    with pytest.raises(RuntimeError, match="identity changed"):
        collect(connection)
    assert connection.sent == ["ATTACH", "STATUS"]
