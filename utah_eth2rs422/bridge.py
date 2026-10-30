"""The bridge: one TCP client (Cerebrum) <-> one serial port (Utah SC-40).

Why frame mode exists
---------------------
Cerebrum's Utah RCP-1 driver drops the connection when a router reply reaches
it split across several reads (as happens with USB serial adapters, or when a
bridge forwards bytes the instant they arrive). In frame mode the bridge holds
router bytes until a complete RCP-1 report has arrived (terminated by US, 0x1F)
and forwards it to Cerebrum in one piece. Partial data is still flushed after a
short idle gap or a hard maximum so nothing can stall.
"""

from __future__ import annotations

import collections
import queue
import datetime as _dt
import socket
import sys
import threading
import time
from pathlib import Path

import serial  # pyserial

from . import rcp1
from .config import Config

PARITY = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
STOPBITS = {1: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2: serial.STOPBITS_TWO}
UNSOLICITED_AFTER = 0.5  # s since last request before a reply counts as unsolicited


class EventLog:
    """Ring buffer of events for the UI, plus optional daily log files."""

    def __init__(self, maxlen: int = 3000):
        self._buf: collections.deque = collections.deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()
        self._file = None
        self._file_day = None
        self._dir: Path | None = None

    def configure_file(self, enabled: bool, directory: Path, keep_days: int) -> None:
        with self._lock:
            self._close_file()
            self._dir = directory if enabled else None
        if enabled:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                cutoff = time.time() - keep_days * 86400
                for p in directory.glob("eth2rs422_*.log"):
                    if p.stat().st_mtime < cutoff:
                        p.unlink(missing_ok=True)
            except OSError:
                pass

    def _close_file(self):
        if self._file:
            try:
                self._file.close()
            except OSError:
                pass
        self._file = None
        self._file_day = None

    def add(self, kind: str, text: str, data: bytes | None = None) -> None:
        now = time.time()
        with self._lock:
            self._seq += 1
            ev = {
                "seq": self._seq,
                "t": now,
                "kind": kind,
                "text": text,
                "hex": rcp1.hexdump(data) if data else "",
            }
            self._buf.append(ev)
            if self._dir is not None:
                self._write_file(ev)

    def _write_file(self, ev: dict) -> None:
        day = _dt.date.fromtimestamp(ev["t"]).isoformat()
        try:
            if self._file is None or self._file_day != day:
                self._close_file()
                self._file = open(self._dir / f"eth2rs422_{day}.log", "a", encoding="utf-8")
                self._file_day = day
            stamp = _dt.datetime.fromtimestamp(ev["t"]).strftime("%H:%M:%S.%f")[:-3]
            line = f"{stamp} {ev['kind']:<11} {ev['text']}"
            if ev["hex"]:
                line += f"   [{ev['hex']}]"
            self._file.write(line + "\n")
            self._file.flush()
        except OSError:
            self._dir = None  # stop trying; UI still shows events

    def close_file(self) -> None:
        with self._lock:
            self._close_file()

    def since(self, seq: int, limit: int = 500) -> list[dict]:
        with self._lock:
            items = [e for e in self._buf if e["seq"] > seq]
        return items[-limit:]

    @property
    def last_seq(self) -> int:
        return self._seq

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


class Bridge:
    def __init__(self, cfg: Config, events: EventLog | None = None):
        self.cfg = cfg
        self.events = events or EventLog()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._ser: serial.Serial | None = None
        self._ser_lock = threading.Lock()
        self._srv: socket.socket | None = None
        self._client: socket.socket | None = None
        self._client_addr = ""
        self._client_lock = threading.Lock()
        self.running = False
        self.serial_ok = False
        self.server_ok = False
        self.serial_error = ""
        self.server_error = ""
        self._last_request = 0.0
        self._pending_since = 0.0
        self._txq: queue.Queue = queue.Queue()
        self._idle = threading.Event()   # set = router not busy answering
        self._idle.set()
        self._test_pending = 0.0
        self.test_result: dict | None = None
        self.listen_only = False
        self._sync_wait: threading.Event | None = None
        self._sync_reply: bytes = b""
        self.take_test: dict | None = None
        self._refused_logged = 0.0
        self.started_at: float | None = None
        self.reset_stats()

    # ------------------------------------------------------------------ stats
    def reset_stats(self) -> None:
        self.stats = {
            "polls": 0, "takes": 0, "replies": 0, "unsolicited": 0,
            "bad_checksum": 0, "fragments_joined": 0, "dropped_no_serial": 0,
            "dropped_no_client": 0, "client_connects": 0, "serial_errors": 0,
            "bytes_to_router": 0, "bytes_from_router": 0, "reply_timeouts": 0, "partial_reports": 0,
            "latency_ms": None, "latency_avg_ms": None,
        }
        self._poll_times: collections.deque = collections.deque(maxlen=600)
        self.last_take = ""

    def polls_per_sec(self) -> float:
        now = time.time()
        recent = [t for t in self._poll_times if now - t <= 5.0]
        return round(len(recent) / 5.0, 1)

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.running = True
        self.started_at = time.time()
        self.events.configure_file(self.cfg.log_to_file, self.cfg.resolved_log_dir(),
                                   self.cfg.log_keep_days)
        c = self.cfg
        self.events.add("info", f"Bridge started: tcp {c.listen_host}:{c.listen_port} <-> "
                                f"{c.serial_port} {c.baudrate} {c.bytesize}{c.parity}"
                                f"{c.stopbits:g}, frame mode {'on' if c.frame_mode else 'off'}, "
                                f"pacing {'on' if c.pace_requests else 'off'}")
        self._txq = queue.Queue()
        self._idle.set()
        self._threads = [
            threading.Thread(target=self._serial_loop, name="serial", daemon=True),
            threading.Thread(target=self._server_loop, name="server", daemon=True),
            threading.Thread(target=self._writer_loop, name="writer", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        self._drop_client("bridge stopped")
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=3)
        self._close_serial()
        self.running = False
        self.serial_ok = self.server_ok = False
        self.events.add("info", "Bridge stopped")
        self.events.close_file()

    # ----------------------------------------------------------------- serial
    def _open_serial(self) -> serial.Serial:
        c = self.cfg
        return serial.Serial(
            c.serial_port, baudrate=c.baudrate, bytesize=c.bytesize,
            parity=PARITY[c.parity], stopbits=STOPBITS[c.stopbits],
            timeout=0.05, write_timeout=1, xonxoff=False, rtscts=False, dsrdtr=False)

    def _close_serial(self) -> None:
        with self._ser_lock:
            if self._ser:
                try:
                    self._ser.close()
                except (OSError, serial.SerialException):
                    pass
            self._ser = None
        self.serial_ok = False

    def _serial_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ser = self._open_serial()
            except (OSError, serial.SerialException, ValueError) as e:
                msg = str(e)
                if msg != self.serial_error:
                    self.events.add("error", f"Cannot open {self.cfg.serial_port}: {msg}")
                self.serial_error = msg
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 5.0)
                continue
            with self._ser_lock:
                self._ser = ser
            self.serial_ok = True
            self.serial_error = ""
            backoff = 1.0
            self.events.add("info", f"Serial {self.cfg.serial_port} open")
            try:
                while not self._stop.is_set():
                    data = self._read_frame(ser) if self.cfg.frame_mode else self._read_any(ser)
                    if data:
                        self._from_router(data)
            except (OSError, serial.SerialException) as e:
                if not self._stop.is_set():
                    self.stats["serial_errors"] += 1
                    self.serial_error = str(e)
                    self.events.add("error", f"Serial error on {self.cfg.serial_port}: {e}")
            finally:
                self._close_serial()
            if not self._stop.is_set():
                self._stop.wait(1.0)

    def _read_any(self, ser: serial.Serial) -> bytes:
        first = ser.read(1)
        if not first:
            return b""
        n = ser.in_waiting
        return first + (ser.read(n) if n else b"")

    def _read_frame(self, ser: serial.Serial) -> bytes:
        first = ser.read(1)
        if not first:
            return b""
        buf = bytearray(first)
        start = last = time.monotonic()
        idle = self.cfg.frame_idle_ms / 1000.0
        hard = self.cfg.frame_max_ms / 1000.0
        reads = 1
        while not self._stop.is_set():
            if rcp1.report_complete(buf):
                break
            n = ser.in_waiting
            if n:
                buf += ser.read(n)
                reads += 1
                last = time.monotonic()
                continue
            now = time.monotonic()
            if now - last > idle or now - start > hard:
                break
            time.sleep(0.001)
        if reads > 2:
            self.stats["fragments_joined"] += 1
        return bytes(buf)

    def _to_router(self, data: bytes) -> bool:
        with self._ser_lock:
            ser = self._ser
            if ser is None:
                return False
            try:
                ser.write(data)
            except (OSError, serial.SerialException) as e:
                self.events.add("error", f"Serial write failed: {e}")
                return False
        self.stats["bytes_to_router"] += len(data)
        return True

    def _from_router(self, data: bytes) -> None:
        now = time.time()
        self._idle.set()
        self.stats["bytes_from_router"] += len(data)
        one = self.cfg.one_based

        w = self._sync_wait
        if w is not None and not w.is_set():
            self._sync_reply = data
            w.set()
            return

        if self._test_pending and now - self._test_pending < 2.0:
            self._test_pending = 0.0
            desc = rcp1.describe_report(data, one)
            self.test_result = {"ok": True, "t": now, "text": desc, "hex": rcp1.hexdump(data)}
            self.events.add("test", f"Test reply: {desc}", data)
            return

        if self.cfg.frame_mode and rcp1.report_is_partial(data):
            self.stats["partial_reports"] = self.stats.get("partial_reports", 0) + 1
            self.events.add("warn", "Router paused mid-reply; forwarded a partial report "
                                    f"({rcp1.describe_report(data, one)})", data)
        elif rcp1.report_has_bad_checksum(data):
            self.stats["bad_checksum"] += 1
            self.events.add("warn", f"Bad checksum from router: {rcp1.describe_report(data, one)}", data)

        unsolicited = now - self._last_request > UNSOLICITED_AFTER
        if self._pending_since:
            lat = (now - self._pending_since) * 1000.0
            self._pending_since = 0.0
            self.stats["latency_ms"] = round(lat, 1)
            avg = self.stats["latency_avg_ms"]
            self.stats["latency_avg_ms"] = round(lat if avg is None else avg * 0.95 + lat * 0.05, 1)

        if unsolicited:
            self.stats["unsolicited"] += 1
            self.events.add("unsolicited", f"Router report: {rcp1.describe_report(data, one)}", data)
        else:
            self.stats["replies"] += 1
            if self.cfg.log_detail == "all":
                self.events.add("reply", rcp1.describe_report(data, one), data)

        with self._client_lock:
            client = self._client
        if client is None:
            self.stats["dropped_no_client"] += 1
            return
        try:
            client.sendall(data)
        except OSError as e:
            self._drop_client(f"send failed: {e}")

    # ----------------------------------------------------------------- network
    def _server_loop(self) -> None:
        while not self._stop.is_set():
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # On Windows SO_REUSEADDR lets two programs share a port (connections
                # then go to the wrong one), so only use it elsewhere. Windows' default
                # bind already tolerates TIME_WAIT and fails cleanly if the port is taken.
                if not sys.platform.startswith("win"):
                    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((self.cfg.listen_host, self.cfg.listen_port))
                srv.listen(2)
                srv.settimeout(0.5)
            except OSError as e:
                msg = str(e)
                if msg != self.server_error:
                    self.events.add("error", f"Cannot listen on {self.cfg.listen_host}:"
                                             f"{self.cfg.listen_port}: {msg} "
                                             f"(port in use by another program?)")
                self.server_error = msg
                self._stop.wait(3.0)
                continue
            self._srv = srv
            self.server_ok = True
            self.server_error = ""
            self.events.add("info", f"Listening on {self.cfg.listen_host}:{self.cfg.listen_port}")
            try:
                while not self._stop.is_set():
                    try:
                        conn, addr = srv.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    self._new_client(conn, addr)
            finally:
                self.server_ok = False
                try:
                    srv.close()
                except OSError:
                    pass

    def _new_client(self, conn: socket.socket, addr) -> None:
        if self.listen_only:
            try:
                conn.close()
            except OSError:
                pass
            if time.time() - self._refused_logged > 10:
                self._refused_logged = time.time()
                self.events.add("info", f"Listen-only: refused connection from {addr[0]}")
            return
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        conn.settimeout(0.2)
        with self._client_lock:
            old = self._client
            self._client = conn
            self._client_addr = f"{addr[0]}:{addr[1]}"
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
            self.events.add("warn", "Previous client replaced by new connection")
        self.stats["client_connects"] += 1
        self.events.add("info", f"Client connected from {addr[0]}:{addr[1]}")
        t = threading.Thread(target=self._client_loop, args=(conn,), name="client", daemon=True)
        t.start()

    def _drop_client(self, why: str, only: socket.socket | None = None) -> None:
        with self._client_lock:
            c = self._client
            if c is None or (only is not None and c is not only):
                return
            self._client = None
            addr = self._client_addr
            self._client_addr = ""
        try:
            c.close()
        except OSError:
            pass
        self.events.add("info", f"Client {addr} disconnected ({why})")

    def _client_loop(self, conn: socket.socket) -> None:
        buf = b""
        buf_since = 0.0
        while not self._stop.is_set():
            with self._client_lock:
                if self._client is not conn:
                    return  # replaced
            try:
                data = conn.recv(4096)
            except socket.timeout:
                data = None
            except OSError as e:
                self._drop_client(f"{e}", only=conn)
                return
            if data == b"":
                self._drop_client("closed by client", only=conn)
                return
            if data:
                if not buf:
                    buf_since = time.time()
                buf += data
                msgs, buf = rcp1.extract_requests(buf)
                for m in msgs:
                    self._txq.put(m)
                if not buf:
                    buf_since = 0.0
            # flush a stuck partial message rather than hold it forever
            if buf and time.time() - buf_since > 0.2:
                self._txq.put(buf)
                buf = b""

    def _writer_loop(self) -> None:
        """Send controller messages to the router one at a time."""
        one = self.cfg.one_based
        timeouts_logged = 0
        while not self._stop.is_set():
            try:
                msg = self._txq.get(timeout=0.2)
            except queue.Empty:
                continue
            kind = rcp1.classify_request(msg)
            if self.cfg.pace_requests:
                if not self._idle.wait(self.cfg.reply_timeout_ms / 1000.0):
                    self.stats["reply_timeouts"] += 1
                    if timeouts_logged < 20 or self.stats["reply_timeouts"] % 100 == 0:
                        timeouts_logged += 1
                        self.events.add("warn", f"Router did not finish replying within "
                                                f"{self.cfg.reply_timeout_ms} ms; sending next "
                                                f"command anyway ({self.stats['reply_timeouts']} so far)")
            now = time.time()
            if kind == "poll":
                self._idle.clear()      # router will be busy answering
            if not self._to_router(msg):
                self._idle.set()
                self.stats["dropped_no_serial"] += 1
                if self.stats["dropped_no_serial"] in (1, 10, 100) or self.stats["dropped_no_serial"] % 1000 == 0:
                    self.events.add("warn", f"Serial port not open, dropped request "
                                            f"({self.stats['dropped_no_serial']} so far)")
                continue
            self._last_request = now
            if kind == "poll":
                self._pending_since = now
                self.stats["polls"] += 1
                self._poll_times.append(now)
                if self.cfg.log_detail == "all":
                    self.events.add("poll", rcp1.describe_request(msg, one), msg)
            elif kind == "take":
                self.stats["takes"] += 1
                desc = rcp1.describe_request(msg, one)
                self.last_take = desc
                self.events.add("take", desc, msg)
                if self.cfg.pace_requests and self.cfg.take_gap_ms > 0:
                    self._stop.wait(self.cfg.take_gap_ms / 1000.0)
            else:
                self.events.add("other", f"Unrecognised request: {rcp1.ascii_view(msg)}", msg)

    # ------------------------------------------------------------- diagnostics
    def test_query(self, dst_zero_based: int = 0) -> str:
        """Send one status request directly to the router (only while no client
        is connected, so Cerebrum's traffic is never disturbed)."""
        if not self.running or not self.serial_ok:
            return "Serial port is not open"
        with self._client_lock:
            if self._client is not None:
                return "A client is connected; test disabled to avoid disturbing it"
        self.test_result = None
        self._test_pending = time.time()
        req = bytes([rcp1.ESC]) + b"L" + f"{dst_zero_based:03d}".encode() + bytes([rcp1.CR])
        if not self._to_router(req):
            self._test_pending = 0.0
            return "Serial write failed"
        self.events.add("test", f"Test: status request dst "
                                f"{dst_zero_based + 1 if self.cfg.one_based else dst_zero_based:03d}", req)
        return ""

    def set_listen_only(self, on: bool) -> None:
        """Quiet the line: disconnect Cerebrum and refuse reconnects, so anything
        the router sends by itself shows up in the log."""
        self.listen_only = bool(on)
        if on:
            self._drop_client("listen-only mode")
            self.events.add("info", "Listen-only ON: Cerebrum disconnected; showing only what the "
                                    "router sends by itself")
        else:
            self.events.add("info", "Listen-only OFF: Cerebrum may reconnect")

    def _query_sync(self, dst0: int, timeout: float = 1.0):
        """Ask the router for one destination and wait for the reply.
        Returns (level-1 source, zero-based or None, raw reply bytes)."""
        self._idle.wait(0.3)
        ev = threading.Event()
        self._sync_reply = b""
        self._sync_wait = ev
        req = bytes([rcp1.ESC]) + b"L" + f"{dst0:03d}".encode() + bytes([rcp1.CR])
        try:
            if not self._to_router(req):
                return None, b""
            ev.wait(timeout)
        finally:
            self._sync_wait = None
        raw = self._sync_reply
        for m in rcp1.parse_report(raw):
            if "levels" in m and 1 in m["levels"]:
                return m["src"], raw
        return None, raw

    def _take_sync(self, src0: int, dst0: int, style: str) -> bytes:
        body = bytes([rcp1.SOH]) + b"A@" + f"{src0:03d}{dst0:03d}".encode()
        msg = body + (bytes([rcp1.CR]) if style == "CR" else bytes([rcp1.checksum(body)]))
        self._to_router(msg)
        return msg

    def test_take(self, src0: int, dst0: int) -> str:
        """Diagnostic: take src->dst directly (CR, then checksum style), read back,
        then restore. Only while Cerebrum is not connected."""
        if not self.running or not self.serial_ok:
            return "Serial port is not open"
        with self._client_lock:
            if self._client is not None:
                return "Cerebrum is connected; turn on Listen only first"
        if self.take_test and self.take_test.get("state") == "running":
            return "A test is already running"
        self.take_test = {"state": "running", "lines": [], "verdict": ""}
        threading.Thread(target=self._run_take_test, args=(src0, dst0), daemon=True).start()
        return ""

    def _run_take_test(self, src0: int, dst0: int) -> None:
        one = self.cfg.one_based
        n = (lambda x: "---" if x is None else f"{x + 1 if one else x:03d}")
        res = self.take_test
        def line(t, data=None):
            res["lines"].append(t)
            self.events.add("test", t, data)
        try:
            orig, raw = self._query_sync(dst0)
            if orig is None:
                line(f"No status for dst {n(dst0)} (reply: {rcp1.hexdump(raw) or 'none'})")
                res["verdict"] = "Router did not answer the status request."
                return
            line(f"dst {n(dst0)} is on src {n(orig)}")
            if orig == src0:
                res["verdict"] = "Already on that source. Pick a different source to test."
                return
            accepted = None
            for style in ("CR", "checksum"):
                msg = self._take_sync(src0, dst0, style)
                time.sleep(0.4)
                now, _ = self._query_sync(dst0)
                ok = now == src0
                line(f"Take src {n(src0)} -> dst {n(dst0)} ({style} terminator): "
                     f"dst now on src {n(now)} - {'ACCEPTED' if ok else 'ignored'}", msg)
                if ok:
                    accepted = style
                    break
            # restore
            for style in ("CR", "checksum"):
                self._take_sync(orig, dst0, style)
                time.sleep(0.4)
                back, _ = self._query_sync(dst0)
                if back == orig:
                    break
            line(f"Restored dst {n(dst0)} to src {n(orig)}: {'yes' if back == orig else 'NO - restore it from a panel'}")
            if accepted == "CR":
                res["verdict"] = "Router ACCEPTS takes (Cerebrum's CR format works)."
            elif accepted == "checksum":
                res["verdict"] = "Router only accepts takes ending in a CHECKSUM."
            else:
                res["verdict"] = ("Router IGNORED both. Destination locked/protected, the port lacks "
                                  "switching rights, or this card is the standby card.")
        except Exception as e:  # never kill the bridge over a diagnostic
            res["verdict"] = f"Test failed: {e}"
        finally:
            res["state"] = "done"
            self.events.add("test", f"Take test result: {res['verdict']}")

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        with self._client_lock:
            client = self._client_addr
        test = self.test_result
        if self._test_pending and time.time() - self._test_pending > 2.0:
            self._test_pending = 0.0
            self.test_result = test = {"ok": False, "t": time.time(),
                                       "text": "No reply from router within 2 s", "hex": ""}
            self.events.add("warn", "Test: no reply from router within 2 s")
        return {
            "running": self.running,
            "serial_ok": self.serial_ok,
            "serial_error": self.serial_error,
            "server_ok": self.server_ok,
            "server_error": self.server_error,
            "client": client,
            "uptime_s": round(time.time() - self.started_at) if self.running and self.started_at else 0,
            "polls_per_sec": self.polls_per_sec(),
            "last_take": self.last_take,
            "stats": dict(self.stats),
            "test": test,
            "listen_only": self.listen_only,
            "take_test": self.take_test,
        }
