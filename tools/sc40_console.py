#!/usr/bin/env python3
"""
sc40_console.py - simple logging terminal for the SC-40 debug port.

Front RJ-45 debug port of an SC-40 card -> Utah adapter 140100-2 -> RS-232 COM port.
Debug port settings: 19200 8N1, no flow control (SC-40 guide p. 3-4 / 4-2).

    python sc40_console.py COM1            (use your RS-232 adapter's COM port)

Everything the SC-40 prints (including boot messages after a reset) is shown
and saved to sc40_console_<date>_<time>.log in the current folder.
Type a command and press Enter to send it, e.g.:
    sc4config pl
    lockdump
    sc4config serial
    sc4config levels
Type  :quit  to exit.  (Avoid 'lockdump clear', 'chassis', 'settime' unless intended.)
"""
import sys
import threading
import time

import serial  # pip install pyserial


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    port = sys.argv[1]
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
    logname = time.strftime("sc40_console_%Y%m%d_%H%M%S.log")
    try:
        s = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=0.1,
                          xonxoff=False, rtscts=False, dsrdtr=False)
    except serial.SerialException as e:
        print(f"Cannot open {port}: {e}")
        print("Is another program (PuTTY, the bridge, Cerebrum) using it?")
        return 1

    log = open(logname, "a", encoding="utf-8", newline="")
    stop = threading.Event()
    print(f"Connected to {port} at {baud} 8N1. Logging to {logname}")
    print("Press Enter for the '/>' prompt. Type :quit to exit.\n")
    log.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} opened {port} {baud} 8N1 ===\n")

    def reader():
        while not stop.is_set():
            try:
                data = s.read(4096)
            except serial.SerialException as e:
                print(f"\n[serial error: {e}]")
                stop.set()
                return
            if data:
                text = data.decode("latin-1")
                sys.stdout.write(text)
                sys.stdout.flush()
                log.write(text)
                log.flush()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        while not stop.is_set():
            line = input()
            if line.strip() == ":quit":
                break
            log.write(f"\n>>> {line}\n")
            s.write(line.encode("latin-1") + b"\r")
    except (EOFError, KeyboardInterrupt):
        pass
    stop.set()
    t.join(1)
    s.close()
    log.write(f"\n=== closed {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    log.close()
    print(f"\nSaved {logname}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
