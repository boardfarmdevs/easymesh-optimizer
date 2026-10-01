from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading


class ControllerNamespaceChanged(RuntimeError):
    pass


class LxdUbusTransport:
    """Run native ubus with pinned container mount/root descriptors, not an LXD exec session."""

    def __init__(self, controller):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*", controller):
            raise ValueError("invalid local controller name")
        self.controller = controller
        self.namespace = os.geteuid() == 0 and bool(shutil.which("nsenter"))
        self.name = "controller-mount-namespace" if self.namespace else "lxd-exec"
        self._identity = None
        self._lock = threading.Lock()

    @staticmethod
    def _stamp(process):
        root = Path(f"/proc/{process}")
        return root.stat().st_ino, root.joinpath("stat").read_text().rpartition(")")[2].split()[19]

    def _discover(self):
        with self._lock:
            if self._identity is None:
                state = json.loads(subprocess.run(
                    ["lxc", "query", f"/1.0/instances/{self.controller}/state"],
                    check=True, text=True, capture_output=True, timeout=10).stdout)
                process = state.get("pid")
                if state.get("status") != "Running" or type(process) is not int or process <= 1:
                    raise ControllerNamespaceChanged("controller is not running")
                self._identity = process, self._stamp(process)
            return self._identity

    @contextmanager
    def _pinned_namespace(self):
        process, stamp = self._discover()
        with ExitStack() as handles:
            def open_handle(path, flags):
                descriptor = os.open(path, flags)
                handles.callback(os.close, descriptor)
                return descriptor

            try:
                if self._stamp(process) != stamp:
                    raise ControllerNamespaceChanged("controller changed; restart candidate observation")
                mount = open_handle(f"/proc/{process}/ns/mnt", os.O_RDONLY)
                root = open_handle(f"/proc/{process}/root", os.O_PATH | os.O_DIRECTORY)
                if self._stamp(process) != stamp:
                    raise ControllerNamespaceChanged("controller changed while pinning namespace")
            except FileNotFoundError as error:
                raise ControllerNamespaceChanged("controller exited; restart candidate observation") from error
            yield mount, root

    def __call__(self, obj, method, payload):
        arguments = ["ubus", "-t", "5", "call", obj, method,
                     json.dumps(payload, separators=(",", ":"))]
        if not self.namespace:
            return subprocess.run(["lxc", "exec", self.controller, "--", *arguments],
                                  check=True, text=True, capture_output=True, timeout=12)
        with self._pinned_namespace() as (mount, root):
            return subprocess.run(
                ["nsenter", f"--mount=/proc/self/fd/{mount}", f"--root=/proc/self/fd/{root}",
                 "--wd=/", "--", "/usr/bin/" + arguments[0], *arguments[1:]],
                pass_fds=(mount, root), check=True, text=True, capture_output=True, timeout=12)
