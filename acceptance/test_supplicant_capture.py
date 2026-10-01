from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import select
import socket
import subprocess
import sys


SCRIPT = Path(__file__).with_name("supplicant-event-capture.py")


def test_capture_owns_attachment_records_event_and_stops_on_eof(tmp_path):
    address = str(tmp_path / "control")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(address)
        server.settimeout(3)

        def answer():
            request, peer = server.recvfrom(1024)
            assert request == b"ATTACH"
            server.sendto(b"OK\n", peer)
            request, peer = server.recvfrom(1024)
            assert request == b"LEVEL 0"
            server.sendto(b"OK\n", peer)
            return peer

        with ThreadPoolExecutor() as executor:
            handshake = executor.submit(answer)
            child = subprocess.Popen([sys.executable, str(SCRIPT), "--socket", address,
                                      "--seconds", "30", "--watch-stdin"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                peer = handshake.result(timeout=3)
                assert select.select([child.stdout], [], [], 3)[0]
                assert json.loads(child.stdout.readline())["kind"] == "ready"
                server.sendto(b"<3>CTRL-EVENT-BSS-TM-RESP status_code=7", peer)
                assert select.select([child.stdout], [], [], 3)[0]
                event = json.loads(child.stdout.readline())
                assert event["event"].endswith("status_code=7")
                assert event["monotonic_ns"] > 0
                child.stdin.close()
                assert child.wait(timeout=3) == 0
                assert json.loads(child.stdout.readline())["kind"] == "end"
                assert server.recvfrom(1024)[0] == b"DETACH"
                assert not child.stderr.read()
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
                child.stdout.close()
                child.stderr.close()
                if not child.stdin.closed:
                    child.stdin.close()


def test_capture_rejects_unbounded_duration():
    result = subprocess.run([sys.executable, str(SCRIPT), "--socket", "unused", "--seconds", "181"],
                            capture_output=True, text=True, timeout=3)
    assert result.returncode != 0
    assert "bounded" in result.stderr
