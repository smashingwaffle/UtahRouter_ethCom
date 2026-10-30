#!/usr/bin/env python3
"""
rcp1_console.py - type RCP-1 commands to a Utah router by hand and see the replies.

    python rcp1_console.py COM5            (19200 8N1)
    python rcp1_console.py COM5 38400      (other baud)

Stop the bridge first (only one program can open the COM port).
Numbers are 1-based like Cerebrum / Utah panels.

Commands:
  q <dst>                 status of one destination            (ESC L ddd CR)
  t <src> <dst> [lv]      TAKE, CR terminated                  (SOH lv ... CR)
  tc <src> <dst> [lv]     TAKE, checksum terminated
                          lv = level chars, default "A@" (level 1);
                          "O@" = levels 1-4, "OO" = all 8 levels
  v                       program version request              (ESC V)
  m                       mode / station name request          (ESC O)
  b                       enable matrix-change reports         (ESC B)
  raw <hex bytes>         send anything, e.g.  raw 1b 4c 30 30 30 0d
  listen [seconds]        just show what the router sends (default 10 s)
  quit

Everything is logged to rcp1_console_<date>_<time>.log.
"""
import sys
import time

import serial  # pip install pyserial

ESC, SOH, STX, CR, US = 0x1B, 0x01, 0x02, 0x0D, 0x1F


def ck(data: bytes) -> int:
    x = 0
    for c in data:
        x ^= c
    return x | 0x20


def fmt(b: bytes) -> str:
    return b.hex(" ") + "   |" + "".join(chr(c) if 32 <= c < 127 else "." for c in b) + "|"


def decode(b: bytes) -> list[str]:
    out = []
    for chunk in b.split(bytes([STX]))[1:]:
        body = chunk.split(bytes([CR]))[0]
        if len(body) >= 9:
            lv = []
            for base, ch in ((0, body[0]), (4, body[1])):
                bits = (ch - 0x40) & 0x0F
                lv += [base + i + 1 for i in range(4) if bits & (1 << i)]
            src, dst = body[2:5].decode(errors="replace"), body[5:8].decode(errors="replace")
            ok = ck(bytes([STX]) + body[:8]) == body[8]
            s = f"{int(src) + 1:03d}" if src.isdigit() else f"({src}=unassigned)"
            d = f"{int(dst) + 1:03d}" if dst.isdigit() else dst
            out.append(f"  dst {d} <- src {s}  levels {lv}  checksum {'ok' if ok else 'BAD'}")
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    port = sys.argv[1]
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
    s = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=0.1)
    logname = time.strftime("rcp1_console_%Y%m%d_%H%M%S.log")
    log = open(logname, "a", encoding="utf-8")

    def out(line=""):
        print(line)
        log.write(line + "\n")
        log.flush()

    def send(b: bytes, wait: float = 0.8):
        s.reset_input_buffer()
        s.write(b)
        s.flush()
        out(f"  sent  {fmt(b)}")
        buf, end = b"", time.time() + wait
        while time.time() < end:
            buf += s.read(256)
            if buf.endswith(bytes([US])):
                break
        if buf:
            out(f"  reply {fmt(buf)}")
            for l in decode(buf):
                out(l)
        else:
            out("  reply (nothing)")

    out(f"Connected to {port} {baud} 8N1. Type 'help' for commands. Log: {logname}")
    while True:
        try:
            line = input("rcp1> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        log.write(f"rcp1> {line}\n")
        p = line.split()
        cmd = p[0].lower()
        try:
            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print(__doc__)
            elif cmd == "q":
                send(bytes([ESC]) + b"L" + f"{int(p[1]) - 1:03d}".encode() + bytes([CR]))
            elif cmd in ("t", "tc"):
                src, dst = int(p[1]) - 1, int(p[2]) - 1
                lv = (p[3] if len(p) > 3 else "A@").encode()
                body = bytes([SOH]) + lv + f"{src:03d}{dst:03d}".encode()
                send(body + (bytes([CR]) if cmd == "t" else bytes([ck(body)])), wait=0.5)
                time.sleep(0.3)
                out("  read back:")
                send(bytes([ESC]) + b"L" + f"{dst:03d}".encode() + bytes([CR]))
            elif cmd == "v":
                send(bytes([ESC]) + b"V")
            elif cmd == "m":
                send(bytes([ESC]) + b"O")
            elif cmd == "b":
                send(bytes([ESC]) + b"B")
            elif cmd == "raw":
                send(bytes.fromhex("".join(p[1:])))
            elif cmd == "listen":
                secs = float(p[1]) if len(p) > 1 else 10
                out(f"  listening {secs:g} s...")
                end = time.time() + secs
                while time.time() < end:
                    b = s.read(256)
                    if b:
                        out(f"  {time.strftime('%H:%M:%S')} {fmt(b)}")
                        for l in decode(b):
                            out(l)
                out("  done")
            else:
                print("Unknown command. Type 'help'.")
        except (IndexError, ValueError) as e:
            print(f"  bad arguments ({e}). Type 'help'.")
    s.close()
    log.close()
    print(f"Saved {logname}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
