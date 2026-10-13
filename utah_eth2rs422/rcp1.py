"""Utah Scientific RCP-1 helpers: framing, checksums and human-readable decoding.

Reference: Utah Scientific "Remote Switcher Control Protocols" v1.5(e),
document 80101-0047, RCP-1 section.

Message shapes seen on an SC-40 (SC-4 family, "limited" RCP-1):
  Status request : ESC 'L' ooo CR                (single output)
                   ESC 'L' sss eee                (block of outputs)
  Take           : SOH <lev1-4> <lev5-8> iii ooo (CR | checksum)
  Status report  : STX <lev1-4> <lev5-8> iii ooo <checksum> CR
                   ... one STX message per level group ...
                   US (0x1F) terminates every report on the SC-40,
                   including single-output replies.
Input/output numbers are three ASCII digits, zero based (000-319).
Unassigned levels report a non-numeric input such as "31?".
"""

from __future__ import annotations

SOH = 0x01
STX = 0x02
CR = 0x0D
ESC = 0x1B
US = 0x1F


def checksum(data: bytes) -> int:
    """RCP-1 checksum: XOR of all bytes from the control code on, OR 0x20."""
    x = 0
    for c in data:
        x ^= c
    return x | 0x20


def levels(c1: int, c2: int) -> list[int]:
    """Decode the two level-bitmap characters ('@'..'O') to level numbers."""
    out = []
    for base, ch in ((0, c1), (4, c2)):
        bits = (ch - 0x40) & 0x0F
        out += [base + i + 1 for i in range(4) if bits & (1 << i)]
    return out


def _num(raw: bytes) -> int | None:
    try:
        s = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return int(s) if s.isdigit() else None


def _io(n: int | None, one_based: bool) -> str:
    if n is None:
        return "---"
    return f"{n + 1 if one_based else n:03d}"


def hexdump(data: bytes) -> str:
    return data.hex(" ")


def ascii_view(data: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 else "." for c in data)


def classify_request(data: bytes) -> str:
    """'take', 'poll' or 'other' for bytes sent by the controller (Cerebrum)."""
    if SOH in data:
        return "take"
    if data[:1] == bytes([ESC]):
        return "poll"
    return "other"


def describe_request(data: bytes, one_based: bool = True) -> str:
    """Human-readable description of controller -> router bytes."""
    parts = []
    i = 0
    while i < len(data):
        c = data[i]
        if c == ESC and i + 1 < len(data) and data[i + 1] == ord("L"):
            body = data[i + 2:]
            if len(body) >= 4 and body[3] == CR:
                parts.append(f"Status request dst {_io(_num(body[:3]), one_based)}")
                i += 6
                continue
            if len(body) >= 6:
                parts.append(
                    f"Status request dst {_io(_num(body[:3]), one_based)}"
                    f"-{_io(_num(body[3:6]), one_based)}")
                i += 8
                continue
        if c == SOH and len(data) - i >= 9:
            m = data[i:i + 9]
            lv = levels(m[1], m[2])
            src = _io(_num(m[3:6]), one_based)
            dst = _io(_num(m[6:9]), one_based)
            lvtxt = ",".join(str(x) for x in lv) or "none"
            parts.append(f"TAKE src {src} -> dst {dst} (level {lvtxt})")
            i += 10  # SOH + 8 + terminator
            continue
        i += 1
    return "; ".join(parts) if parts else ascii_view(data)


def parse_report(data: bytes) -> list[dict]:
    """Split router -> controller bytes into STX status messages."""
    msgs = []
    for chunk in data.split(bytes([STX]))[1:]:
        body = chunk.split(bytes([CR]))[0]
        if len(body) < 8:
            msgs.append({"ok": False, "raw": body})
            continue
        ck_ok = len(body) >= 9 and checksum(bytes([STX]) + body[:8]) == body[8]
        msgs.append({
            "ok": ck_ok,
            "levels": levels(body[0], body[1]),
            "src": _num(body[2:5]),
            "dst": _num(body[5:8]),
            "raw": body,
        })
    return msgs


def describe_report(data: bytes, one_based: bool = True) -> str:
    """Human-readable description of router -> controller bytes.
    Unassigned level groups (non-numeric source) are omitted."""
    out = []
    for m in parse_report(data):
        if "levels" not in m:
            out.append("partial message")
            continue
        if m["src"] is None:
            continue  # unassigned levels, e.g. "31?"
        lv = ",".join(str(x) for x in m["levels"])
        flag = "" if m["ok"] else " BAD CHECKSUM"
        out.append(f"dst {_io(m['dst'], one_based)} <- src {_io(m['src'], one_based)}"
                   f" (level {lv}){flag}")
    if not out:
        return ascii_view(data)
    return "; ".join(out)


def report_complete(buf: bytes) -> bool:
    """True when buf ends a full SC-40 report (US terminator)."""
    return buf.endswith(bytes([US]))


def report_has_bad_checksum(data: bytes) -> bool:
    return any(("levels" in m and not m["ok"]) for m in parse_report(data))
