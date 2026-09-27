import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock, patch

import pytest

from room_demo.band_profiles import ClientBandSettings
from wmdcfg.actuator import ActuatorError


CONTAINER = "wlan-client-001"
STATION = "02:00:00:00:03:00"
VALUES = {"freq_list": "2437", "scan_freq": "2437", "key_mgmt": "WPA-PSK", "ieee80211w": "1", "sae_pwe": "0"}


def settings_with_native_commands(state=None):
    state = {"pid": 1234, "status": "Running"} if state is None else state

    def command(*arguments, **options):
        if arguments[:2] == ("lxc", "query"):
            assert options == {"timeout": 5}
            return json.dumps(state)
        assert arguments[:15] == ("nsenter", "--target", "1234", "--user", "--mount", "--net", "--pid", "--root", "--wd",
                                  "--", "/usr/bin/env", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "wpa_cli", "-i", "wlan0")
        operation, *parameters = arguments[15:]
        if operation == "status":
            return f"address={STATION}\nwpa_state=COMPLETED\nssid=private_ssid\nfreq=2437"
        if operation == "list_networks":
            return "network id / ssid / bssid / flags\n0\tprivate_ssid\tany\t[CURRENT]\n"
        if operation in {"get", "get_network"}:
            return VALUES[parameters[-1]]
        return "OK"

    with patch("room_demo.band_profiles._command", side_effect=command) as native:
        settings = ClientBandSettings()
    settings._start_ticks = Mock(return_value=100)
    settings._user_namespace_arguments = Mock(return_value=("--user",))
    return settings, native


@pytest.mark.parametrize("shared,expected", [(True, ()), (False, ("--user",))])
def test_only_distinct_user_namespace_is_entered(shared, expected):
    with patch("room_demo.band_profiles.Path.samefile", return_value=shared) as samefile:
        assert ClientBandSettings._user_namespace_arguments(1234) == expected
    samefile.assert_called_once_with("/proc/self/ns/user")


def test_shared_user_namespace_keeps_mount_network_pid_and_root_entry():
    settings, native = settings_with_native_commands()
    settings._user_namespace_arguments.return_value = ()
    native.side_effect = [json.dumps({"pid": 1234, "status": "Running"}), "OK"]
    assert settings.control(CONTAINER, "status") == "OK"
    assert native.call_args.args[:9] == (
        "nsenter", "--target", "1234", "--mount", "--net", "--pid", "--root", "--wd", "--")
    assert "--user" not in native.call_args.args


@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError()])
def test_unreadable_user_namespace_fails_before_execution(error):
    settings, native = settings_with_native_commands()
    settings._user_namespace_arguments = ClientBandSettings._user_namespace_arguments
    with patch("room_demo.band_profiles.Path.samefile", side_effect=error):
        with pytest.raises(ActuatorError, match="user namespace is unavailable"):
            settings.control(CONTAINER, "status")
    assert native.call_count == 1
    assert not hasattr(settings._sessions, "current")


def test_capture_discovers_only_selected_instance_once_and_uses_native_cli():
    settings, native = settings_with_native_commands()
    record = settings.capture(CONTAINER, STATION)
    assert record["values"] == VALUES
    assert native.call_count == 8
    assert native.call_args_list[0].args == ("lxc", "query", f"/1.0/instances/{CONTAINER}/state")
    assert all(call.args[0] == "nsenter" for call in native.call_args_list[1:])
    assert not hasattr(settings._sessions, "current")


def test_native_cli_has_container_paths_when_merged_usr_service_path_omits_sbin(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin")
    settings, native = settings_with_native_commands()
    assert settings.capture(CONTAINER, STATION)["values"] == VALUES
    assert all(call.args[10:13] == ("/usr/bin/env", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "wpa_cli")
               for call in native.call_args_list[1:])


def test_write_reuses_namespace_for_identity_readback_and_discards_it_afterwards():
    settings, native = settings_with_native_commands()
    record = settings.capture(CONTAINER, STATION)
    native.reset_mock()
    settings.write(record, VALUES)
    assert sum(call.args[:2] == ("lxc", "query") for call in native.call_args_list) == 1
    assert sum(call.args[-1] == "status" for call in native.call_args_list) == 2
    settings.capture(CONTAINER, STATION)
    assert sum(call.args[:2] == ("lxc", "query") for call in native.call_args_list) == 2


def test_initialize_keeps_both_writes_and_association_poll_in_one_namespace_scope():
    settings, native = settings_with_native_commands()
    record = settings.capture(CONTAINER, STATION)
    native.reset_mock()
    settings.initialize(record, VALUES, {2437})
    assert sum(call.args[:2] == ("lxc", "query") for call in native.call_args_list) == 1
    assert sum(call.args[-1] == "reassociate" for call in native.call_args_list) == 1
    assert sum(call.args[-1] == "status" for call in native.call_args_list) == 5


@pytest.mark.parametrize("state", [{"status": "Stopped", "pid": 1234}, {"status": "Running", "pid": True},
                                  {"status": "Running", "pid": 1}, {"status": "Running", "pid": "1234"}, {}])
def test_invalid_namespace_never_executes_client_command(state):
    settings, native = settings_with_native_commands(state)
    with pytest.raises(ActuatorError, match="namespace is unavailable"):
        settings.control(CONTAINER, "status")
    assert native.call_count == 1
    assert not hasattr(settings._sessions, "current")


@pytest.mark.parametrize("ticks,commands", [([100, 101], 1), ([100, 100, 101], 2)])
def test_pid_reuse_is_rejected_before_execution_or_before_accepting_response(ticks, commands):
    settings, native = settings_with_native_commands()
    settings._start_ticks.side_effect = ticks
    with pytest.raises(ActuatorError, match="namespace identity changed"):
        settings.control(CONTAINER, "status")
    assert native.call_count == commands
    assert not hasattr(settings._sessions, "current")


def test_failed_command_clears_scope_and_next_command_discovers_again():
    settings, native = settings_with_native_commands()
    native.side_effect = [json.dumps({"pid": 1234, "status": "Running"}), RuntimeError("native failure"),
                          json.dumps({"pid": 1234, "status": "Running"}), "OK"]
    with pytest.raises(RuntimeError, match="native failure"):
        settings.control(CONTAINER, "status")
    assert settings.control(CONTAINER, "reconnect") == "OK"
    assert native.call_args_list[2].args[:2] == ("lxc", "query")


def test_scope_cannot_be_reused_for_a_different_client():
    settings, native = settings_with_native_commands()
    with settings._session(CONTAINER):
        with pytest.raises(ActuatorError, match="namespace changed within a transaction"):
            settings.control("wlan-client-002", "status")
    native.assert_not_called()


def test_parallel_clients_have_separate_scopes_without_serializing_discovery():
    settings, native = settings_with_native_commands()
    barrier = Barrier(4, timeout=2)

    def command(*arguments, **options):
        if arguments[:2] == ("lxc", "query"):
            barrier.wait()
            return json.dumps({"pid": 1000 + int(arguments[2].split("/")[-2][-3:]), "status": "Running"})
        return arguments[2]

    native.side_effect = command
    containers = [f"wlan-client-{index:03}" for index in range(1, 5)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        result = list(executor.map(lambda container: settings.control(container, "status"), containers))
    assert result == [str(index) for index in range(1001, 1005)]


def test_injected_command_interface_and_container_validation_are_preserved():
    command = Mock(return_value="OK\n")
    settings = ClientBandSettings(command=command)
    assert settings.control(CONTAINER, "set_network", "0", "freq_list", "") == "OK"
    command.assert_called_once_with("lxc", "exec", CONTAINER, "--", "wpa_cli", "-i", "wlan0",
                                    "set_network", "0", "freq_list", "")
    with pytest.raises(ValueError):
        settings.control("agent-1", "status")
    assert command.call_count == 1


def test_start_ticks_handles_process_names_with_spaces_and_parentheses():
    fields = ["S"] + ["0"] * 18 + ["123456"]
    with patch("room_demo.band_profiles.Path.read_text", return_value="1234 (nested ) name) " + " ".join(fields)):
        assert ClientBandSettings._start_ticks(1234) == 123456


@pytest.mark.parametrize("value", ["1234 (init) Z " + "0 " * 19, "truncated", "1234 (init) S"])
def test_invalid_or_exited_process_is_not_reused(value):
    with patch("room_demo.band_profiles.Path.read_text", return_value=value):
        with pytest.raises(ActuatorError, match="no longer available"):
            ClientBandSettings._start_ticks(1234)
