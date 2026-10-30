#!/usr/bin/env python3
"""
take_test.py - does the SC-40 accept RCP-1 takes on this port?

Sends ONE take to ONE destination you choose, in two terminator styles
(CR, then checksum), and reads the destination back after each try.
It then puts the destination back to its original source.

    python take_test.py COM5 <src> <dst>        (1-based, like Cerebrum/panels)
    python take_test.py COM5 5 10

Pick a SPARE destination - this really switches the router.
Stop the bridge / Cerebrum first (only one program can open the COM port).
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


def query(s: serial.Serial, out0: int):
    s.reset_input_buffer()
    s.write(bytes([ESC]) + b"L" + f"{out0:03d}".encode() + bytes([CR]))
    buf, end = b"", time.time() + 1.0
    while time.time() < end and not buf.endswith(bytes([US])):
        buf += s.read(64)
    for chunk in buf.split(bytes([STX]))[1:]:
        body = chunk.split(bytes([CR]))[0]
        if len(body) >= 8 and (body[0] - 0x40) & 1:   # message that includes level 1
            src = body[2:5].decode(errors="replace")
            return (int(src) if src.isdigit() else None), buf
    return None, buf


def take(s: serial.Serial, src0: int, out0: int, style: str) -> bytes:
    body = bytes([SOH]) + b"A@" + f"{src0:03d}{out0:03d}".encode()
    msg = body + (bytes([CR]) if style == "CR" else bytes([ck(body)]))
    s.write(msg)
    s.flush()
    return msg


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    port, src1, dst1 = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    src0, out0 = src1 - 1, dst1 - 1
    s = serial.Serial(port, 19200, bytesize=8, parity="N", stopbits=1, timeout=0.1)
    with s:
        orig, raw = query(s, out0)
        if orig is None:
            print(f"No status for dst {dst1:03d}. Raw reply: {raw.hex(' ')}")
            print("Check port/settings (19200 8N1) and that nothing else has the COM port open.")
            return 1
        print(f"dst {dst1:03d} is on src {orig + 1:03d} (raw input {orig:03d})")
        if orig == src0:
            print("It's already on that source - pick a different src to make the test meaningful.")
            return 1

        result = {}
        for style in ("CR", "checksum"):
            msg = take(s, src0, out0, style)
            time.sleep(0.4)
            now, raw = query(s, out0)
            ok = now == src0
            result[style] = ok
            print(f"Take src {src1:03d} -> dst {dst1:03d} with {style:8s} terminator "
                  f"[{msg.hex(' ')}]  ->  dst now on src "
                  f"{'---' if now is None else f'{now + 1:03d}'}   {'ACCEPTED' if ok else 'ignored'}")
            if ok:
                break

        # put it back
        take(s, orig, out0, "CR")
        time.sleep(0.4)
        back, _ = query(s, out0)
        if back != orig:
            take(s, orig, out0, "checksum")
            time.sleep(0.4)
            back, _ = query(s, out0)
        print(f"Restored dst {dst1:03d} to src {orig + 1:03d}: "
              f"{'yes' if back == orig else 'NO - please restore it from a panel'}")

        print()
        if result.get("CR"):
            print("RESULT: the router accepts Cerebrum-style takes (CR). Problem is elsewhere.")
        elif result.get("checksum"):
            print("RESULT: the router only accepts takes ending in a CHECKSUM. "
                  "The bridge can convert Cerebrum's takes - tell Claude.")
        else:
            print("RESULT: the router ignored both. Likely the destination is locked/protected,\n"
                  "or this serial port isn't allowed to switch. On the SC-40 debug port try:\n"
                  "  lockdump\n  sc4config serial\n  sc4config dstattrs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
