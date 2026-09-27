import copy
from pathlib import Path
from unittest.mock import call, Mock, patch

import pytest

from room_demo.band_profiles import BandProfileManager, ClientBandSettings, validate_profiles
from room_demo.recovery import RecoveryJournal, load_recovery
from wmdcfg.actuator import ActuatorError


STATION = "02:00:00:10:01:00"
CONTAINER = "prpl-client-01"
VALUES = {"freq_list": "2437", "scan_freq": "2437", "key_mgmt": "WPA-PSK", "ieee80211w": "1", "sae_pwe": "0"}
RECORD = {"container": CONTAINER, "sta_mac": STATION, "network_id": "0", "ssid": "private_ssid", "values": VALUES}
PROFILE = {"allowed_bands": ["2.4", "5", "6"], "initial_band": "2.4"}


def manager(tmp_path):
    plan = {"bindings": {
        "client": {"container": CONTAINER, "station_mac": STATION, "radio_permanent_mac": STATION},
        "gateway": {"fronthaul_frequencies_mhz": {"2.4": 2437, "5": 5180, "6": 5975}}}}
    journal = RecoveryJournal(tmp_path / "recovery.json", "test", "inventory")
    journal.prepare("instance", 0, {})
    settings = Mock()
    settings.capture.return_value = copy.deepcopy(RECORD)
    scanner = Mock()
    scanner.capabilities.return_value = {"sta_mac": STATION, "frequencies_mhz": [2437, 5180, 5975],
                                        "key_management": ["WPA-PSK", "SAE"]}
    return BandProfileManager(plan, journal, settings=settings, scanner=scanner), settings, scanner, journal


def test_profiles_are_explicit_and_default_has_none():
    world = {"roles": {"client": "station"}, "generations": [{"present": {"client": True}}]}
    assert validate_profiles(world) == {}
    world["band_steering"] = {"client": PROFILE}
    assert validate_profiles(world) == {"client": PROFILE}
    for invalid in [{"allowed_bands": ["5", "5"], "initial_band": "5"},
                    {"allowed_bands": ["5"], "initial_band": "6"},
                    {**PROFILE, "force_bssid": "anything"}, {"allowed_bands": [5], "initial_band": "5"}]:
        with pytest.raises(ValueError):
            validate_profiles({**world, "band_steering": {"client": invalid}})
    with pytest.raises(ValueError):
        validate_profiles({**world, "roles": {"client": "fronthaul_ap"}})


def test_band_settings_are_journaled_before_mutation_and_restored_on_default(tmp_path):
    profiles, settings, _scanner, journal = manager(tmp_path)

    def initialize(record, values, frequencies):
        assert journal.client_networks()[CONTAINER] == record
        assert frequencies == [2437]
        assert values == {"freq_list": "2437 5180 5975", "scan_freq": "2437 5180 5975",
                          "key_mgmt": "WPA-PSK SAE", "ieee80211w": "1", "sae_pwe": "2"}

    settings.initialize.side_effect = initialize
    with profiles.transition({"client": PROFILE}):
        assert profiles.snapshot()[STATION]["profile"] == PROFILE
    assert load_recovery(journal.path)["client_networks"][CONTAINER] == RECORD
    with profiles.transition({}):
        assert not profiles.active
    settings.restore.assert_called_once_with(RECORD, reconnect=True, wait=True)
    assert journal.client_networks() == {}


@pytest.mark.parametrize("failure", ["setup", "consumer"])
def test_failed_switch_restores_previous_settings_and_journal(tmp_path, failure):
    profiles, settings, _scanner, journal = manager(tmp_path)
    if failure == "setup":
        settings.initialize.side_effect = ActuatorError("setup")
    with pytest.raises(ActuatorError):
        with profiles.transition({"client": PROFILE}):
            raise ActuatorError("consumer")
    settings.restore.assert_called_once_with(RECORD)
    assert profiles.active == {} and journal.client_networks() == {}


def test_failed_rollback_keeps_recoverable_original_settings(tmp_path):
    profiles, settings, _scanner, journal = manager(tmp_path)
    settings.initialize.side_effect = ActuatorError("setup")
    settings.restore.side_effect = ActuatorError("rollback")
    with pytest.raises(ActuatorError, match="rollback failed"):
        with profiles.transition({"client": PROFILE}):
            pass
    assert journal.client_networks()[CONTAINER] == RECORD
    assert load_recovery(journal.path)["state"] == "failed"


@pytest.mark.parametrize("capability", ["frequencies_mhz", "key_management", "sta_mac"])
def test_unsupported_profile_is_rejected_before_any_mutation(tmp_path, capability):
    profiles, settings, scanner, journal = manager(tmp_path)
    scanner.capabilities.return_value[capability] = [] if capability != "sta_mac" else "02:00:00:00:00:00"
    with pytest.raises(ActuatorError):
        with profiles.transition({"client": PROFILE}):
            pass
    settings.initialize.assert_not_called()
    assert journal.client_networks() == {}


def test_crash_recovery_retains_failed_record_and_removes_only_restored_records(tmp_path):
    _profiles, _settings, _scanner, journal = manager(tmp_path)
    journal.preserve_client_network(CONTAINER, RECORD)
    with patch("room_demo.recovery.restore_client_network", side_effect=ActuatorError("offline")):
        with pytest.raises(ActuatorError):
            journal.restore_client_networks()
    assert journal.client_networks()[CONTAINER] == RECORD
    with patch("room_demo.recovery.restore_client_network") as restore:
        journal.restore_client_networks()
    restore.assert_called_once_with(RECORD)
    assert not journal.client_networks()


def test_network_writer_checks_identity_readback_and_handles_unset_frequency():
    settings = ClientBandSettings()
    record = {**RECORD, "values": {**VALUES, "freq_list": None, "scan_freq": None}}
    settings.capture = Mock(side_effect=[RECORD, copy.deepcopy(record)])
    settings.control = Mock(return_value="OK")
    settings.write(record, record["values"])
    assert settings.control.call_args_list[0].args == (CONTAINER, "set_network", "0", "freq_list", "")
    settings.capture = Mock(return_value={**record, "sta_mac": "02:00:00:00:00:00"})
    with pytest.raises(ActuatorError, match="identity changed"):
        settings.write(record, record["values"])


def test_successful_mutation_with_wrong_readback_is_not_accepted():
    settings = ClientBandSettings()
    settings.capture = Mock(return_value=RECORD)
    settings.control = Mock(return_value="OK")
    with pytest.raises(ActuatorError, match="readback mismatch"):
        settings.write(RECORD, {**VALUES, "freq_list": "5180"})


def test_leaving_profile_keeps_unavailable_client_disconnected(tmp_path):
    profiles, settings, _scanner, journal = manager(tmp_path)
    with profiles.transition({"client": PROFILE}):
        pass
    with profiles.transition({}, present_roles=set()):
        assert not profiles.active
    settings.restore.assert_called_once_with(RECORD, reconnect=False, wait=False)
    assert not journal.client_networks()


def test_restore_waits_for_completed_association_on_restored_frequency():
    settings = ClientBandSettings(clock=Mock(return_value=0), sleep=Mock())
    settings.write = Mock()
    settings._ok = Mock()
    settings.control = Mock(side_effect=[
        "wpa_state=SCANNING",
        "wpa_state=COMPLETED\nssid=private_ssid\nfreq=5975",
        "wpa_state=COMPLETED\nssid=private_ssid\nfreq=2437",
    ])
    settings.restore(RECORD, wait=True)
    settings._ok.assert_called_once_with(CONTAINER, "reassociate")
    assert settings.sleep.call_count == 2
    settings.write.assert_called_once_with(RECORD, VALUES)


def test_initial_association_selects_from_a_scan_of_the_new_rf():
    settings = ClientBandSettings(clock=Mock(return_value=0), sleep=Mock())
    settings.write = Mock()
    settings._ok = Mock()
    in_use = "bssid=02:00:00:f9:bd:e4\nfreq=5180"
    settings.control = Mock(side_effect=[
        in_use, "FAIL-BUSY", "OK", in_use, in_use + "\nbssid=02:00:00:02:34:fc\nfreq=2437",
        "wpa_state=COMPLETED\nssid=private_ssid\nfreq=2437",
    ])
    settings.initialize(RECORD, VALUES, [2437])
    # results scanned under the previous room's RF are dropped, and the client selects
    # from a passive scan of the new RF, which hears every AP in range
    assert settings._ok.call_args_list == [call(CONTAINER, "bss_flush", "0"), call(CONTAINER, "reassociate")]
    scans = [c for c in settings.control.call_args_list if c.args[1] == "scan"]
    assert scans == [call(CONTAINER, "scan", "TYPE=ONLY", "freq=2437", "passive=1")] * 2
    assert settings.control.call_count == 6


def test_initial_passive_scan_rejection_fails_the_band_setup():
    settings = ClientBandSettings(clock=Mock(return_value=0), sleep=Mock())
    settings.write = Mock()
    settings._ok = Mock()
    settings.control = Mock(side_effect=["", "FAIL"])
    with pytest.raises(ActuatorError, match="passive band scan failed"):
        settings.initialize(RECORD, VALUES, [2437])


def test_restore_does_not_reassociate_an_unavailable_client():
    settings = ClientBandSettings()
    settings.write = Mock()
    settings._ok = Mock()
    settings.control = Mock()
    settings.restore(RECORD, reconnect=False)
    settings._ok.assert_called_once_with(CONTAINER, "disconnect")
    settings.control.assert_not_called()


def test_restore_fails_when_association_does_not_finish():
    settings = ClientBandSettings(clock=Mock(side_effect=[0, 0, 13]), sleep=Mock())
    settings.write = Mock()
    settings._ok = Mock()
    settings.control = Mock(return_value="wpa_state=SCANNING")
    with pytest.raises(ActuatorError, match="restored band association timed out"):
        settings.restore(RECORD, wait=True)


def test_unrestricted_restore_requires_a_real_frequency_and_correct_ssid():
    settings = ClientBandSettings(clock=Mock(return_value=0), sleep=Mock())
    settings.write = Mock()
    settings._ok = Mock()
    settings.control = Mock(side_effect=[
        "wpa_state=COMPLETED\nssid=private_ssid\nfreq=0",
        "wpa_state=COMPLETED\nssid=other\nfreq=5180",
        "wpa_state=COMPLETED\nssid=private_ssid\nfreq=5180",
    ])
    settings.restore({**RECORD, "values": {**VALUES, "freq_list": None, "scan_freq": None}}, wait=True)
    assert settings.sleep.call_count == 2

def test_same_band_reception_requires_explicit_single_band_mode():
    world = {"roles": {"client": "station"}, "generations": [{"present": {"client": True}}]}
    profile = {"allowed_bands": ["5"], "initial_band": "5", "measurement_mode": "received_same_band"}
    assert validate_profiles({**world, "band_steering": {"client": profile}}) == {"client": profile}
    for invalid in [{**profile, "measurement_mode": "oracle"}, {**PROFILE, "measurement_mode": "received_same_band"}]:
        with pytest.raises(ValueError):
            validate_profiles({**world, "band_steering": {"client": invalid}})
