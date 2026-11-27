"""DNS rebinding in app/netsafe.py (security review): the connection must go to the address that was checked.

Like test_vapid.py this imports a module of the app directly: rebinding needs a DNS server that changes its answer,
which only a fake resolver (socket.getaddrinfo patched inside this process) can give in a test. netsafe.py only
imports config.py from the app.
"""
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import config  # noqa: E402
import netsafe  # noqa: E402

PUBLIC = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.215.14", 80))
LOOPBACK = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80))


class RebindingTest(unittest.TestCase):
    def setUp(self):
        self._allow = config.ALLOW_PRIVATE_URLS
        config.ALLOW_PRIVATE_URLS = False

    def tearDown(self):
        config.ALLOW_PRIVATE_URLS = self._allow

    def test_second_answer_to_loopback_is_blocked(self):
        answers = [[PUBLIC], [LOOPBACK]]  # first lookup public (the check), every later one 127.0.0.1
        connected = []

        def fake_getaddrinfo(*a, **k):
            return answers.pop(0) if len(answers) > 1 else answers[0]

        def fake_connect(self, addr):
            connected.append(addr)
            raise ConnectionRefusedError("test: no connection")

        with mock.patch("socket.getaddrinfo", fake_getaddrinfo), mock.patch("socket.socket.connect", fake_connect):
            with self.assertRaises(netsafe.Blocked):
                netsafe.open_url("http://rebind.example/recipe", timeout=2)
        self.assertNotIn(("127.0.0.1", 80), connected)

    def test_connects_to_the_checked_address(self):
        connected = []

        def fake_connect(self, addr):
            connected.append(addr)
            raise ConnectionRefusedError("test: no connection")

        with mock.patch("socket.getaddrinfo", lambda *a, **k: [PUBLIC]), mock.patch("socket.socket.connect", fake_connect):
            with self.assertRaises(OSError):
                netsafe.open_url("http://public.example/recipe", timeout=2)
        self.assertEqual(connected, [("93.184.215.14", 80)])

    def test_private_addresses_refused(self):
        for addr in ("10.0.0.1", "192.168.2.64", "169.254.169.254", "::1", "fd00::1", "::ffff:127.0.0.1"):
            fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
            info = [(fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, 80))]
            with mock.patch("socket.getaddrinfo", lambda *a, **k: info):
                with self.assertRaises(netsafe.Blocked, msg=addr):
                    netsafe.check("http://x.example/")


if __name__ == "__main__":
    unittest.main()
