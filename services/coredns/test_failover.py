"""Local integration test: COREDNS=/path/to/coredns python3 services/coredns/test_failover.py."""

import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest


class Upstream:
    def __init__(self, address):
        self.address = address
        self.mode = "answer"
        self.queries = []
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(0.1)
        self.port = self.socket.getsockname()[1]
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stopped.is_set():
            try:
                request, peer = self.socket.recvfrom(4096)
            except socket.timeout:
                continue
            qtype = struct.unpack("!H", request[-4:-2])[0]
            self.queries.append(qtype)
            mode = self.mode
            if mode == "drop":
                continue
            question = request[12:]
            flags, answer = 0x8180, b""
            if mode == "servfail":
                flags |= 2
            elif qtype == 1:
                answer = struct.pack("!HHHIH", 0xC00C, 1, 1, 1, 4) + socket.inet_aton(self.address)
            response = request[:2] + struct.pack("!HHHHH", flags, 1, bool(answer), 0, 0) + question + answer
            self.socket.sendto(response, peer)

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)
        self.socket.close()


@unittest.skipUnless(os.environ.get("COREDNS"), "COREDNS binary required")
class FailoverTests(unittest.TestCase):
    def test_failover_and_automatic_priority_recovery(self):
        upstreams = [Upstream("192.0.2." + str(i)) for i in (1, 2, 3)]
        process = None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                config = Path(tmp, "Corefile")
                destinations = " ".join("127.0.0.1:" + str(u.port) for u in upstreams)
                template = Path(__file__).with_name("Corefile.template").read_text()
                config.write_text(template.replace(
                    ".:53 {", f".:{port} {{\n    bind 127.0.0.1"
                ).replace("${AZ_FORWARD_HOSTS}", destinations).replace(
                    "prefer_udp", "prefer_udp\n        read_timeout 200ms"
                ))
                process = subprocess.Popen(
                    [os.environ["COREDNS"], "-conf", str(config)],
                )

                def query():
                    request = struct.pack("!HHHHHH", 42, 0x100, 1, 0, 0, 0)
                    request += b"\x07example\x03com\x00\x00\x01\x00\x01"
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                        client.settimeout(2)
                        client.sendto(request, ("127.0.0.1", port))
                        response, _ = client.recvfrom(4096)
                    return socket.inet_ntoa(response[-4:])

                def eventually(address):
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        try:
                            if query() == address:
                                return
                        except (socket.timeout, ConnectionRefusedError):
                            pass
                        self.assertIsNone(process.poll(), "CoreDNS exited before answering")
                        time.sleep(0.05)
                    self.fail("Did not switch to " + address)

                eventually("192.0.2.1")
                upstreams[0].mode = "drop"
                eventually("192.0.2.2")
                deadline = time.monotonic() + 6
                # A third probe means two previous probes have timed out.
                while upstreams[0].queries.count(2) < 3 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreaterEqual(upstreams[0].queries.count(2), 3)
                time.sleep(0.1)
                before = upstreams[0].queries.count(1)
                for _ in range(3):
                    self.assertEqual(query(), "192.0.2.2")
                self.assertEqual(upstreams[0].queries.count(1), before)

                upstreams[0].mode = "answer"
                eventually("192.0.2.1")

                # SERVFAIL is a domain-level failover, not a network failure.
                upstreams[0].mode = "servfail"
                before = upstreams[0].queries.count(1)
                for _ in range(3):
                    self.assertEqual(query(), "192.0.2.2")
                self.assertEqual(upstreams[0].queries.count(1), before + 3)
                upstreams[1].mode = "servfail"
                self.assertEqual(query(), "192.0.2.3")
                upstreams[0].mode = "answer"
                self.assertEqual(query(), "192.0.2.1")
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for upstream in upstreams:
                upstream.close()


if __name__ == "__main__":
    unittest.main()
