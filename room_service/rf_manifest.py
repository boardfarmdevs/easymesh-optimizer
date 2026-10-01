from __future__ import annotations

import hashlib
from pathlib import Path
import platform

from wmdcfg.rf_audit import git_command, read_text

from .lab import ROOM_STACKS


def annotate_manifest(manifest, root, stack, owner, policy):
    root = Path(root)
    folders = ROOM_STACKS["rdk" if stack == "rdk" else "prplmesh"].patch_folders
    series = {}
    for folder in folders:
        checksum = hashlib.sha256()
        files = sorted((root / folder).glob("*.patch"))
        try:
            for path in files:
                checksum.update(path.name.encode() + b"\0" + path.read_bytes())
            series[folder] = {"patches": len(files), "sha256": checksum.hexdigest() if files else None}
        except OSError as error:
            series[folder] = {"patches": len(files), "sha256": None, "error": str(error)}
    commit = git_command(root, "rev-parse", "HEAD")
    changes = git_command(root, "status", "--porcelain", "--untracked-files=normal")
    runtime = "/run/meta-cmf-wmediumd" if stack == "rdk" else "/run/prpl-wmediumd"
    return {**manifest, "inventory": {
        "stack": stack, "decision_owner": owner, "policy": str(policy),
        "source_commit": commit["stdout"].strip() if commit["returncode"] == 0 else None,
        "source_dirty": bool(changes["stdout"]) if changes["returncode"] == 0 else None,
        "kernel_release": platform.release(),
        "hwsim_srcversion": read_text(Path("/sys/module/mac80211_hwsim/srcversion")),
        "receive_context_reporting": read_text(Path("/sys/module/mac80211_hwsim/parameters/rx_context_reports")),
        "medium_binary_manifest": read_text(Path(runtime) / "wmediumd-binary.sha256"),
        "patch_series": series,
        "native_hal_identity": "not inventoried here; use the independent rf-audit before qualification",
        "loaded_binary_source_match": "not established by repository hashes",
    }}
