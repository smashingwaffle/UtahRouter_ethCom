"""Entry point: python -m utah_eth2rs422 [--no-browser] [--config PATH]"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

from . import APP_NAME, __version__
from .config import ConfigStore
from .webui import App, serve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="utah-eth2rs422", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--config", type=Path, help="path to config.json")
    ap.add_argument("--no-browser", action="store_true", help="don't open the web UI")
    ap.add_argument("--ui-port", type=int, help="override web UI port")
    ap.add_argument("--ui-host", help="override web UI bind address (default 127.0.0.1)")
    ap.add_argument("--stopped", action="store_true", help="don't start the bridge automatically")
    args = ap.parse_args(argv)

    app = App(ConfigStore(args.config) if args.config else ConfigStore())
    if args.ui_port:
        app.cfg.ui_port = args.ui_port
    if args.ui_host:
        app.cfg.ui_host = args.ui_host

    try:
        httpd = serve(app)
    except OSError as e:
        print(f"Cannot start web UI on {app.cfg.ui_host}:{app.cfg.ui_port}: {e}")
        print("Is another copy already running?")
        return 1

    host = "localhost" if app.cfg.ui_host in ("127.0.0.1", "0.0.0.0") else app.cfg.ui_host
    url = f"http://{host}:{app.cfg.ui_port}/"
    print(f"{APP_NAME} {__version__}")
    print(f"Web UI : {url}")
    print(f"Config : {app.store.path}")
    print("Close this window or press Ctrl+C to stop.")

    if app.cfg.autostart and not args.stopped:
        app.start()
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
