"""End-to-end test: fake Cerebrum (TCP) -> bridge -> pty -> fake SC-40.

The fake SC-40 deliberately sends each reply in two pieces with a gap, like a
USB serial adapter does. With frame mode on, the TCP client must receive every
reply in one piece. POSIX only (uses a pseudo-terminal)."""

import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from utah_eth2rs422 import rcp1
from utah_eth2rs422.bridge import Bridge, EventLog
from utah_eth2rs422.config import Config


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeSC40(threading.Thread):
    """Answers ESC L ooo CR like the real SC-40, split into two writes."""

    def __init__(self, master_fd: int, gap: float = 0.015):
        super().__init__(daemon=True)
        self.fd = master_fd
        self.gap = gap
        self.matrix = {o: o for o in range(144)}
        self.stop = threading.Event()

    def report(self, out: int) -> bytes:
        a = bytes([rcp1.STX]) + b"A@" + f"{self.matrix[out]:03d}{out:03d}".encode()
        b = bytes([rcp1.STX]) + b"NO31?" + f"{out:03d}".encode()
        return (a + bytes([rcp1.checksum(a), rcp1.CR]) +
                b + bytes([rcp1.checksum(b), rcp1.CR, rcp1.US]))

    def run(self):
        buf = b""
        while not self.stop.is_set():
            try:
                buf += os.read(self.fd, 64)
            except OSError:
                return
            while True:
                if buf.startswith(b"\x1bL") and len(buf) >= 6:
                    out = int(buf[2:5])
                    buf = buf[6:]
                    rep = self.report(out)
                    os.write(self.fd, rep[:7])
                    time.sleep(self.gap)
                    os.write(self.fd, rep[7:])
                elif buf.startswith(b"\x01") and len(buf) >= 10:
                    m = buf[:10]
                    buf = buf[10:]
                    self.matrix[int(m[6:9])] = int(m[3:6])
                elif buf and buf[0] not in (0x1B, 0x01):
                    buf = buf[1:]
                else:
                    break


@unittest.skipIf(sys.platform.startswith("win"), "needs a POSIX pty")
class TestBridge(unittest.TestCase):
    def setUp(self):
        import tty
        self.master, slave = os.openpty()
        tty.setraw(self.master)
        tty.setraw(slave)
        self.slave_name = os.ttyname(slave)
        self.sc40 = FakeSC40(self.master)
        self.sc40.start()
        self.port = free_port()
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(serial_port=self.slave_name, listen_host="127.0.0.1",
                     listen_port=self.port, ui_port=free_port(),
                     log_to_file=True, log_dir=self.tmp.name)
        self.bridge = Bridge(cfg, EventLog())
        self.bridge.start()
        for _ in range(50):
            if self.bridge.serial_ok and self.bridge.server_ok:
                break
            time.sleep(0.05)

    def tearDown(self):
        self.bridge.stop()
        self.sc40.stop.set()
        self.tmp.cleanup()

    def connect(self):
        c = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return c

    def test_replies_arrive_whole(self):
        c = self.connect()
        for out in range(20):
            c.sendall(b"\x1bL" + f"{out:03d}".encode() + b"\r")
            first = c.recv(4096)  # must be the complete report in one piece
            self.assertTrue(rcp1.report_complete(first), f"fragmented reply: {first!r}")
            self.assertEqual(rcp1.parse_report(first)[0]["dst"], out)
        c.close()
        st = self.bridge.status()
        self.assertEqual(st["stats"]["polls"], 20)
        self.assertEqual(st["stats"]["replies"], 20)
        self.assertGreater(st["stats"]["fragments_joined"], 0)

    def test_take_is_forwarded_and_logged(self):
        c = self.connect()
        c.sendall(b"\x01A@004009\r")
        time.sleep(0.2)
        c.sendall(b"\x1bL009\r")
        rep = c.recv(4096)
        self.assertEqual(rcp1.parse_report(rep)[0]["src"], 4)
        kinds = [e["kind"] for e in self.bridge.events.since(0)]
        self.assertIn("take", kinds)
        logs = list(Path(self.tmp.name).glob("eth2rs422_*.log"))
        self.assertTrue(logs and "TAKE src 005 -> dst 010" in logs[0].read_text())
        c.close()

    def test_new_client_replaces_old(self):
        a = self.connect()
        time.sleep(0.2)
        b = self.connect()
        time.sleep(0.4)
        b.sendall(b"\x1bL001\r")
        self.assertTrue(rcp1.report_complete(b.recv(4096)))
        self.assertTrue(self.bridge.status()["client"].startswith("127.0.0.1:"))
        a.close()
        b.close()

    def test_router_test_query(self):
        self.assertEqual(self.bridge.test_query(5), "")
        for _ in range(40):
            if self.bridge.test_result:
                break
            time.sleep(0.05)
        self.assertTrue(self.bridge.test_result["ok"])
        self.assertIn("dst 006 <- src 006", self.bridge.test_result["text"])


if __name__ == "__main__":
    unittest.main()
