import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "native_controller_trace", Path(__file__).with_name("native-controller-trace.py"))
trace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trace)
CURRENT = {
    "rdk": "4372013d624b22127ea727e365fc4bde582ad34b56f055088cd5703a21301252",
    "prpl": "737ab07f89fec1aabcc861bd90ddafda931be2e3e1a108cc7352ea2c3ecd994c",
}


class FakeProbe:
    def __init__(self, fail_attach=False):
        self.opened = False
        self.cleaned = False
        self.attached = 0
        self.fail_attach = fail_attach
        self.attachments = []
        self.detachments = []

    def __getitem__(self, key):
        return self if key == "events" else [SimpleNamespace(value=0)]

    def open_perf_buffer(self, *_args, **_kwargs):
        self.opened = True

    def attach_uprobe(self, **arguments):
        if not self.opened:
            raise AssertionError("probe attached before its event consumer")
        if self.fail_attach:
            raise RuntimeError("attachment failed")
        self.attached += 1
        self.attachments.append(arguments)

    attach_uretprobe = attach_uprobe

    def detach_uprobe(self, **arguments):
        self.attached -= 1
        self.detachments.append(arguments)

    detach_uretprobe = detach_uprobe

    def perf_buffer_poll(self, **_kwargs):
        pass

    def cleanup(self):
        self.cleaned = True


class NativeTraceTest(unittest.TestCase):
    def run_trace(self, stack, probe, metrics=False, digest=None):
        with contextlib.ExitStack() as contexts:
            contexts.enter_context(patch.dict("sys.modules", {
                "bcc": SimpleNamespace(BPF=lambda **_kwargs: probe)}))
            contexts.enter_context(patch.object(trace.os, "geteuid", return_value=0))
            contexts.enter_context(patch.object(trace, "controller", return_value=("binary", digest or CURRENT[stack])))
            contexts.enter_context(patch.object(trace.signal, "signal"))
            contexts.enter_context(patch.object(trace.time, "monotonic", side_effect=[0, 0, 10, 10]))
            self.output = io.StringIO()
            contexts.enter_context(contextlib.redirect_stdout(self.output))
            return trace.main(["--stack", stack, "--seconds", "5"] + (["--metrics"] if metrics else []))

    def test_consumer_precedes_both_native_profiles(self):
        for stack in ("rdk", "prpl"):
            with self.subTest(stack=stack):
                probe = FakeProbe()
                self.assertEqual(self.run_trace(stack, probe), 0)
                self.assertTrue(probe.cleaned)
                self.assertEqual(probe.attached, 0)

    def test_every_digest_selects_its_own_attachments_and_identity(self):
        for stack, profiles in trace.PROFILES.items():
            for digest, profile in profiles.items():
                with self.subTest(stack=stack, digest=digest):
                    probe = FakeProbe()
                    self.run_trace(stack, probe, metrics=True, digest=digest)
                    expected = ([{"name": "binary", "addr": address, "fn_name": "commit"}
                                 for address in profile["association_addresses"]] if stack == "rdk" else
                                [{"name": "binary", "sym": trace.PRPL_ASSOCIATION, "fn_name": name}
                                 for name in ("enter", "commit")])
                    expected.append({"name": "binary", "addr": profile["metric_address"], "fn_name": "metric"})
                    self.assertEqual(probe.attachments, expected)
                    self.assertEqual(probe.detachments, [
                        {key: value for key, value in arguments.items() if key != "fn_name"}
                        for arguments in expected])
                    identity = json.loads(self.output.getvalue().splitlines()[0])
                    self.assertEqual(identity["sha256"], digest)
                    self.assertEqual(identity["profile"]["id"], profile["id"])

    def test_unknown_and_cross_stack_binaries_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "unsupported native binary SHA256:.*qualify"):
            trace.qualify_binary("rdk", b"unqualified")
        with patch.object(trace.hashlib, "sha256", return_value=SimpleNamespace(hexdigest=lambda: CURRENT["prpl"])):
            with self.assertRaisesRegex(ValueError, "unsupported native binary"):
                trace.qualify_binary("rdk", b"wrong stack")

    def test_digest_alone_cannot_enable_current_instruction_profile(self):
        for stack, digest in CURRENT.items():
            with self.subTest(stack=stack):
                with patch.object(trace.hashlib, "sha256", return_value=SimpleNamespace(hexdigest=lambda: digest)):
                    with self.assertRaisesRegex(ValueError, "instruction mismatch"):
                        trace.qualify_binary(stack, b"different instructions")

    def test_current_boundaries_and_layouts_remain_distinct_from_legacy(self):
        rdk = trace.PROFILES["rdk"][CURRENT["rdk"]]
        prpl = trace.PROFILES["prpl"][CURRENT["prpl"]]
        self.assertEqual(rdk["association_addresses"], (0xdf99a, 0xdf9db))
        self.assertEqual(rdk["metric_address"], 0x89b90)
        self.assertEqual(prpl["metric_address"], 0x274651)
        self.assertIn("#define RCPI_OFFSET 212", trace.program("rdk", rdk))
        self.assertIn("#define FRAME_OFFSET 120", trace.program("rdk", rdk))
        self.assertIn("#define RCPI_OFFSET 288", trace.program("prpl", prpl))
        self.assertIn("#define BSS_OFFSET 888", trace.program("prpl", prpl))
        legacy = trace.PROFILES["prpl"]["5f66442074ee3fd51b7a172dad4fa7996fcb94d19db1c7a01dc2ab5ffe74673d"]
        self.assertIn("#define RCPI_OFFSET 280", trace.program("prpl", legacy))
        self.assertIn("#define BSS_OFFSET 880", trace.program("prpl", legacy))

    def test_rejected_binary_never_initializes_bpf(self):
        with patch.object(trace.os, "geteuid", return_value=0), \
                patch.object(trace, "controller", side_effect=ValueError("unsupported native binary")), \
                patch.dict("sys.modules", {"bcc": SimpleNamespace(BPF=None)}):
            with self.assertRaisesRegex(ValueError, "unsupported native binary"):
                trace.main(["--stack", "rdk", "--seconds", "5"])

    def test_controller_requires_one_exact_process(self):
        for amount in (0, 2):
            matches = [SimpleNamespace(resolve=lambda: Path("/usr/bin/onewifi_em_ctrl"))
                       for _index in range(amount)]
            with self.subTest(amount=amount), patch.object(trace.Path, "glob", return_value=matches):
                with self.assertRaisesRegex(ValueError, "requires exactly one.*found " + str(amount)):
                    trace.controller("rdk")

    def test_failed_attachment_cleans_up(self):
        probe = FakeProbe(fail_attach=True)
        with self.assertRaisesRegex(RuntimeError, "attachment failed"):
            self.run_trace("rdk", probe)
        self.assertTrue(probe.cleaned)

    def test_metric_profiles_detach_with_association_probes(self):
        for stack in ("rdk", "prpl"):
            with self.subTest(stack=stack):
                probe = FakeProbe()
                self.assertEqual(self.run_trace(stack, probe, metrics=True), 0)
                self.assertTrue(probe.cleaned)
                self.assertEqual(probe.attached, 0)
