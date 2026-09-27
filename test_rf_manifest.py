from unittest.mock import patch

from room_demo.rf_manifest import annotate_manifest


def test_source_inventory_is_diagnostic_and_does_not_qualify_runtime(tmp_path):
    folder = tmp_path / "patches" / "hwsim"
    folder.mkdir(parents=True)
    (folder / "one.patch").write_text("first patch")
    contract = {"qualification": {"physical_capacity": False}, "wire": {"state": "missing"}}
    def git(_root, *arguments):
        return {"returncode": 0, "stdout": "commit-id\n" if arguments[0] == "rev-parse" else " M modified\n"}
    with patch("room_demo.rf_manifest.git_command", side_effect=git), \
         patch("room_demo.rf_manifest.read_text", return_value=None):
        report = annotate_manifest(contract, tmp_path, "prplmesh", "external-act", "policy.yaml")
    assert report["qualification"] == {"physical_capacity": False}
    assert report["inventory"]["source_dirty"] is True
    assert report["inventory"]["source_commit"] == "commit-id"
    assert report["inventory"]["patch_series"]["patches/hwsim"]["patches"] == 1
    assert len(report["inventory"]["patch_series"]["patches/hwsim"]["sha256"]) == 64
    assert report["inventory"]["medium_binary_manifest"] is None
    assert report["inventory"]["loaded_binary_source_match"].startswith("not established")
    assert "inventory" not in contract


def test_missing_git_inventory_does_not_claim_a_clean_or_identified_source(tmp_path):
    with patch("room_demo.rf_manifest.git_command", return_value={"returncode": None, "stdout": ""}), \
         patch("room_demo.rf_manifest.read_text", return_value=None):
        report = annotate_manifest({}, tmp_path, "rdk", "external-recommend", "policy.yaml")
    assert report["inventory"]["source_commit"] is None
    assert report["inventory"]["source_dirty"] is None
    assert all(row["sha256"] is None for row in report["inventory"]["patch_series"].values())
