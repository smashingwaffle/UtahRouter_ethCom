"""Built-in web UI + JSON API (stdlib only)."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from serial.tools import list_ports

from . import APP_NAME, __version__
from .bridge import Bridge, EventLog
from .config import Config, ConfigStore

STATIC = Path(__file__).parent / "static"


def local_ips() -> list[str]:
    ips = {"0.0.0.0", "127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # primary outbound interface
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # TEST-NET, no packet is sent
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    order = {"0.0.0.0": 0, "127.0.0.1": 1}
    return sorted(ips, key=lambda ip: (order.get(ip, 2), ip))


class App:
    """Holds config + the current Bridge instance; restarts it on config change."""

    def __init__(self, store: ConfigStore):
        self.store = store
        self.cfg = store.load()
        self.events = EventLog()
        self.bridge = Bridge(self.cfg, self.events)
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            self.bridge.start()

    def stop(self):
        with self.lock:
            self.bridge.stop()

    def apply_config(self, data: dict) -> Config:
        merged = {**self.cfg.to_dict(), **data}
        cfg = Config.from_dict(merged)  # raises ValueError on bad input
        with self.lock:
            was_running = self.bridge.running
            ui_changed = (cfg.ui_host, cfg.ui_port) != (self.cfg.ui_host, self.cfg.ui_port)
            self.bridge.stop()
            self.store.save(cfg)
            self.cfg = cfg
            stats = self.bridge.stats
            self.bridge = Bridge(cfg, self.events)
            self.bridge.stats = stats  # keep counters across config changes
            self.events.add("info", "Settings saved" +
                            (" (web UI address changes apply after restarting the app)"
                             if ui_changed else ""))
            if was_running:
                self.bridge.start()
        return cfg

    def state(self) -> dict:
        return {
            "app": APP_NAME,
            "version": __version__,
            "config": self.cfg.to_dict(),
            "config_path": str(self.store.path),
            "log_dir": str(self.cfg.resolved_log_dir()),
            "status": self.bridge.status(),
            "last_seq": self.events.last_seq,
        }


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"UtahEth2RS422/{__version__}"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                body = (STATIC / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/state":
                self._json(app.state())
            elif u.path == "/api/events":
                q = parse_qs(u.query)
                since = int(q.get("since", ["0"])[0] or 0)
                self._json({"events": app.events.since(since), "last": app.events.last_seq})
            elif u.path == "/api/ports":
                ports = [{"device": p.device, "description": p.description or ""}
                         for p in sorted(list_ports.comports(), key=lambda p: p.device)]
                self._json({"ports": ports})
            elif u.path == "/api/interfaces":
                self._json({"ips": local_ips()})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            try:
                if u.path == "/api/config":
                    app.apply_config(self._body())
                    self._json(app.state())
                elif u.path == "/api/start":
                    app.start()
                    self._json(app.state())
                elif u.path == "/api/stop":
                    app.stop()
                    self._json(app.state())
                elif u.path == "/api/reset_stats":
                    app.bridge.reset_stats()
                    self._json(app.state())
                elif u.path == "/api/clear_log":
                    app.events.clear()
                    self._json(app.state())
                elif u.path == "/api/test":
                    dst = int(self._body().get("dst", 1))
                    zero = dst - 1 if app.cfg.one_based else dst
                    err = app.bridge.test_query(max(0, zero))
                    self._json({"error": err} if err else {"ok": True}, 400 if err else 200)
                else:
                    self._json({"error": "not found"}, 404)
            except ValueError as e:
                self._json({"error": str(e)}, 400)

    return Handler


def serve(app: App) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((app.cfg.ui_host, app.cfg.ui_port), make_handler(app))
    httpd.daemon_threads = True
    return httpd
