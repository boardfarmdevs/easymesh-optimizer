import pytest

from optimizer import stacks


def test_the_mounting_lab_names_its_stack(tmp_path, monkeypatch):
    monkeypatch.delenv("OPTIMIZER_STACK", raising=False)
    assert stacks.detect(tmp_path) is None
    with pytest.raises(LookupError, match="OPTIMIZER_STACK"):
        stacks.stack(lab=tmp_path)
    (tmp_path / "steer.sh").write_text("")
    assert stacks.stack(lab=tmp_path).controller == "bpibroadband"
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "steer-client.sh").write_text("")
    assert stacks.detect(tmp_path) is None
    (tmp_path / "steer.sh").unlink()
    prplmesh = stacks.stack(lab=tmp_path)
    assert (prplmesh.controller, prplmesh.base_url, prplmesh.byte_counter_unit_bytes) == (
        "prpl-controller", "http://127.0.0.1:8092", 1024)
    assert stacks.steer_script(prplmesh, tmp_path) == str(tmp_path / "scripts" / "steer-client.sh")


def test_a_named_stack_wins_over_the_environment_and_the_lab(tmp_path, monkeypatch):
    (tmp_path / "steer.sh").write_text("")
    monkeypatch.setenv("OPTIMIZER_STACK", "prplmesh")
    assert stacks.stack(lab=tmp_path).name == "prplmesh"
    assert stacks.stack("rdk", lab=tmp_path).name == "rdk"
    with pytest.raises(LookupError, match="unknown optimizer stack"):
        stacks.stack("openwrt", lab=tmp_path)
