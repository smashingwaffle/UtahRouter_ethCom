#!/usr/bin/env python3
"""
SC-40 Serial 2 RCP-1 sweep / listen tool (read-only).

Sends ONLY the RCP-1 status request  ESC L ooo CR  (no TAKE commands).

Usage:
  python sc40_rcp1_sweep.py COM5 sweep            # try every baud/framing combo
  python sc40_rcp1_sweep.py COM5 sweep --outs 1 2 144
  python sc40_rcp1_sweep.py COM5 listen 19200 8N1 # passive sniff, Ctrl+C to stop
  python sc40_rcp1_sweep.py COM5 dump 19200 8N1   # full status, outputs 000-143
  python sc40_rcp1_sweep.py COM5 soak              # poll like Cerebrum, log failures
  python sc40_rcp1_sweep.py COM5 proxy --virt COM11
        # Cerebrum on COM10 (com0com pair COM10<->COM11), script bridges COM11<->COM5
        # and logs both directions to proxy_log.txt
  python sc40_rcp1_sweep.py COM5 tcpproxy --tcp 5001
        # no driver needed: Cerebrum connects via TCP to 127.0.0.1:5001

Confirmed working on this SC-40 Serial 2: 19200 8N1 (not the manual's 7E2).

LISTEN FIRST. Per RCP-1 v1.5e p.3, the SC-4/SC-40 status mode is fixed at
"matrix change": the controller should push STX..CR status on its own whenever
a crosspoint changes. Take a crosspoint from a panel while listening; that
tests the SC-40 -> PC path without depending on the PC -> SC-40 wiring at all.

Run "sweep" once per physical wiring state of the PC->SC-40 pair
(T+/T- normal, then T+/T- swapped). Polarity of the SC-40->PC pair does not
matter for this test: reversed polarity still produces bytes (garbage) and
lights RX, so ANY byte back is the signal you're looking for.
"""
import sys
import time
import argparse
import serial  # pip install pyserial

BAUDS = [19200, 38400, 9600, 57600, 4800, 115200]
FRAMINGS = {
    "7E2": (serial.SEVENBITS, serial.PARITY_EVEN, serial.STOPBITS_TWO),
    "7E1": (serial.SEVENBITS, serial.PARITY_EVEN, serial.STOPBITS_ONE),
    "8N1": (serial.EIGHTBITS, serial.PARITY_NONE, serial.STOPBITS_ONE),
    "8E1": (serial.EIGHTBITS, serial.PARITY_EVEN, serial.STOPBITS_ONE),
    "7O1": (serial.SEVENBITS, serial.PARITY_ODD, serial.STOPBITS_ONE),
    "8N2": (serial.EIGHTBITS, serial.PARITY_NONE, serial.STOPBITS_TWO),
}


def status_request(out_num: int) -> bytes:
    return b"\x1bL" + f"{out_num:03d}".encode("ascii") + b"\r"


def fmt(b: bytes) -> str:
    hx = b.hex(" ")
    asc = "".join(chr(c) if 32 <= c < 127 else "." for c in b)
    return f"{hx}   |{asc}|"


def levels(c1: str, c2: str):
    """Decode the two RCP-1 level characters (@..O bitmaps) to level numbers."""
    out = []
    for base, ch in ((0, c1), (4, c2)):
        bits = ord(ch) - 0x40
        out += [base + i + 1 for i in range(4) if bits & (1 << i)]
    return out


def checksum(body: bytes) -> int:
    x = 0
    for c in body:
        x ^= c
    return x | 0x20


def decode(buf: bytes):
    """Yield readable lines for every STX..CR status message in buf."""
    for chunk in buf.split(b"\x02")[1:]:
        msg = chunk.split(b"\r")[0]
        if len(msg) < 9:
            yield f"  short/partial message: {msg!r}"
            continue
        txt = msg.decode("ascii", "replace")
        lv = levels(txt[0], txt[1])
        inp, outp, ck = txt[2:5], txt[5:8], msg[8]
        ok = checksum(b"\x02" + msg[:8]) == ck
        note = "" if inp.isdigit() else "  (non-numeric input: no source/level unassigned?)"
        yield (f"  out {outp}  <- in {inp}  levels {lv}  "
               f"checksum {'OK' if ok else 'BAD'}{note}")


def open_port(port, baud, framing):
    bs, par, sb = FRAMINGS[framing]
    return serial.Serial(port, baudrate=baud, bytesize=bs, parity=par,
                         stopbits=sb, timeout=0.1, write_timeout=1,
                         rtscts=False, dsrdtr=False, xonxoff=False)


def sweep(port, outs, wait):
    hits = []
    for baud in BAUDS:
        for framing in FRAMINGS:
            try:
                s = open_port(port, baud, framing)
            except serial.SerialException as e:
                print(f"open failed {baud} {framing}: {e}")
                continue
            with s:
                s.reset_input_buffer()
                got = b""
                for o in outs:
                    s.write(status_request(o))
                    s.flush()
                    t_end = time.time() + wait
                    while time.time() < t_end:
                        got += s.read(256)
                tag = f"{baud:>6} {framing}"
                if got:
                    print(f"[HIT] {tag}: {fmt(got)}")
                    for line in decode(got):
                        print(line)
                    hits.append((baud, framing, got))
                else:
                    print(f"      {tag}: nothing")
    print("\nSummary:", "no response at any setting" if not hits else
          f"{len(hits)} setting(s) produced bytes; a clean STX(02)...CR "
          "reply marks the real one, garbage marks a near-miss or polarity issue")


def dump(port, baud, framing, start, end):
    """ESC L <start> <end>: block status request, terminated by US (1F)."""
    req = b"\x1bL" + f"{start:03d}{end:03d}".encode() + b"\r"
    with open_port(port, baud, framing) as s:
        s.reset_input_buffer()
        s.write(req)
        got, t_end = b"", time.time() + 5
        while time.time() < t_end and not got.endswith(b"\x1f"):
            got += s.read(4096)
    for line in decode(got):
        print(line)
    if not got.endswith(b"\x1f"):
        print("  (no US terminator seen; report may be incomplete)")


def soak(port, baud, framing, interval, timeout, outs_max, logfile):
    """Poll ESC L ooo CR cyclically like Cerebrum does; log every anomaly."""
    stats = {"ok": 0, "timeout": 0, "badck": 0, "garbage": 0, "unsolicited": 0}
    log = open(logfile, "a", encoding="ascii")

    def note(kind, o, raw):
        stats[kind] += 1
        line = (f"{time.strftime('%H:%M:%S')} {kind.upper():11s} out {o:03d} "
                f"{fmt(raw) if raw else '(no bytes)'}")
        print(line)
        log.write(line + "\n")
        log.flush()

    print(f"Soak on {port} {baud} {framing}: interval {interval*1000:.0f} ms, "
          f"timeout {timeout*1000:.0f} ms, outputs 000-{outs_max:03d}. "
          f"Logging anomalies to {logfile}. Ctrl+C to stop.")
    t0 = time.time()
    with open_port(port, baud, framing) as s:
        s.reset_input_buffer()
        o = 0
        try:
            while True:
                # anything sitting in the buffer before we ask = unsolicited
                pre = s.read(s.in_waiting or 0)
                if pre:
                    note("unsolicited", o, pre)
                s.write(status_request(o))
                s.flush()
                got, t_end = b"", time.time() + timeout
                while time.time() < t_end and not got.endswith(b"\x1f"):
                    got += s.read(256)
                if not got:
                    note("timeout", o, got)
                elif not got.endswith(b"\x1f") or not got.startswith(b"\x02"):
                    note("garbage", o, got)
                else:
                    msgs = [m for m in got.split(b"\x02")[1:]]
                    bad = False
                    for m in msgs:
                        body = m.split(b"\r")[0]
                        if len(body) < 9 or checksum(b"\x02" + body[:8]) != body[8]:
                            bad = True
                        elif int(body[5:8]) != o:
                            bad = True
                    if bad:
                        note("badck", o, got)
                    else:
                        stats["ok"] += 1
                o = 0 if o >= outs_max else o + 1
                if o == 0:
                    el = time.time() - t0
                    print(f"[{el/60:5.1f} min] " + "  ".join(f"{k}={v}" for k, v in stats.items()))
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
    print("Final:", stats)
    log.close()


def proxy(real_port, virt_port, baud, framing, logfile):
    """Sit between Cerebrum (on a com0com virtual port) and the SC-40 (real port).
    Forwards bytes both ways untouched and logs everything with timestamps."""
    import threading
    log = open(logfile, "a", encoding="ascii")
    lock = threading.Lock()
    t0 = time.time()

    def write_log(tag, data):
        stamp = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        line = f"{stamp} +{time.time() - t0:9.3f}s {tag} {fmt(data)}"
        with lock:
            print(line)
            log.write(line + "\n")
            log.flush()

    real = open_port(real_port, baud, framing)
    virt = open_port(virt_port, baud, framing)
    stop = threading.Event()

    def pump(src, dst, tag):
        while not stop.is_set():
            try:
                data = src.read(4096)
            except serial.SerialException as e:
                write_log(tag, f"[port error: {e}]".encode())
                stop.set()
                break
            if data:
                dst.write(data)
                write_log(tag, data)

    print(f"Proxy: Cerebrum <-> {virt_port}  ==  {real_port} <-> SC-40, "
          f"{baud} {framing}. Logging to {logfile}. Ctrl+C to stop.")
    print("CER> = Cerebrum to SC-40,  <SC40 = SC-40 to Cerebrum")
    threads = [threading.Thread(target=pump, args=(virt, real, "CER>"), daemon=True),
               threading.Thread(target=pump, args=(real, virt, "<SC40"), daemon=True)]
    for t in threads:
        t.start()
    try:
        while not stop.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    stop.set()
    real.close()
    virt.close()
    log.close()


def tcp_proxy(real_port, tcp_port, baud, framing, logfile):
    """Cerebrum connects over TCP (e.g. 127.0.0.1:<tcp_port>); bytes are bridged
    to the SC-40 on the real COM port and logged both ways. No virtual COM driver."""
    import socket
    import threading
    log = open(logfile, "a", encoding="ascii")
    lock = threading.Lock()
    t0 = time.time()

    def write_log(tag, data):
        stamp = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        line = f"{stamp} +{time.time() - t0:9.3f}s {tag} {fmt(data)}"
        with lock:
            print(line)
            log.write(line + "\n")
            log.flush()

    real = open_port(real_port, baud, framing)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", tcp_port))
    srv.listen(1)
    srv.settimeout(0.5)
    print(f"TCP proxy: Cerebrum -> tcp/{tcp_port}  ==  {real_port} {baud} {framing} -> SC-40. "
          f"Logging to {logfile}. Ctrl+C to stop.")
    print("CER> = Cerebrum to SC-40,  <SC40 = SC-40 to Cerebrum")
    try:
        while True:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            write_log("----", f"[Cerebrum connected from {addr[0]}:{addr[1]}]".encode())
            conn.settimeout(0.05)
            stop = threading.Event()

            def serial_to_tcp():
                while not stop.is_set():
                    data = real.read(4096)
                    if data:
                        write_log("<SC40", data)
                        try:
                            conn.sendall(data)
                        except OSError:
                            stop.set()

            th = threading.Thread(target=serial_to_tcp, daemon=True)
            th.start()
            try:
                while not stop.is_set():
                    try:
                        data = conn.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    real.write(data)
                    write_log("CER>", data)
            except OSError:
                pass
            stop.set()
            th.join(1)
            conn.close()
            write_log("----", b"[Cerebrum disconnected]")
    except KeyboardInterrupt:
        pass
    srv.close()
    real.close()
    log.close()


def listen(port, baud, framing):
    with open_port(port, baud, framing) as s:
        print(f"Listening on {port} {baud} {framing}. Take a crosspoint from a "
              "panel now. Ctrl+C to stop.")
        try:
            while True:
                b = s.read(256)
                if b:
                    print(time.strftime("%H:%M:%S"), fmt(b))
                    for line in decode(b):
                        print(line)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("mode", choices=["sweep", "listen", "dump", "soak", "proxy", "tcpproxy"])
    ap.add_argument("baud", nargs="?", type=int, default=19200)
    ap.add_argument("framing", nargs="?", default="8N1", choices=FRAMINGS)
    ap.add_argument("--outs", nargs="+", type=int, default=[0, 1])
    ap.add_argument("--range", nargs=2, type=int, default=[0, 143],
                    help="dump mode: start and end output (default 0 143)")
    ap.add_argument("--interval", type=float, default=0.1,
                    help="soak mode: seconds between polls (default 0.1, like Cerebrum)")
    ap.add_argument("--timeout", type=float, default=0.5,
                    help="soak mode: seconds to wait for each reply")
    ap.add_argument("--log", default=None, help="log file (soak/proxy)")
    ap.add_argument("--tcp", type=int, default=5001,
                    help="tcpproxy mode: TCP port Cerebrum connects to (default 5001)")
    ap.add_argument("--virt", default="COM11",
                    help="proxy mode: com0com port the script opens (Cerebrum uses the other end)")
    ap.add_argument("--wait", type=float, default=0.6,
                    help="seconds to wait for a reply after each request")
    a = ap.parse_args()
    if a.mode == "sweep":
        sweep(a.port, a.outs, a.wait)
    elif a.mode == "soak":
        soak(a.port, a.baud, a.framing, a.interval, a.timeout, a.range[1],
             a.log or "soak_log.txt")
    elif a.mode == "tcpproxy":
        tcp_proxy(a.port, a.tcp, a.baud, a.framing, a.log or "proxy_log.txt")
    elif a.mode == "proxy":
        proxy(a.port, a.virt, a.baud, a.framing, a.log or "proxy_log.txt")
    elif a.mode == "dump":
        dump(a.port, a.baud, a.framing, *a.range)
    else:
        listen(a.port, a.baud, a.framing)
