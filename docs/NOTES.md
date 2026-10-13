# Engineering notes: SC-40 ⇄ Cerebrum over RCP-1

Findings from commissioning, October 2026.

## Hardware

* Controller: Utah Scientific SC-40 (SC-400 cards), controlling a UDS-144 (144×144, one level in use).
* Rear serial ports carry RCP-1 only. RS-232/RS-422 is selected by internal jumpers JP1/JP2 (labelled "DP1/DP2" in one place in the guide) under the top access plate. The rear-panel label does not reflect the jumper setting.
* The SC-40 Ethernet port is **not** usable for control on this unit.
* Front RJ-45 debug port: 19200 8N1. Use `sc4config serial` to read the actual serial port parameters.

## Serial

* Serial 2 answers at **19200 8N1**. The manual's 7E2 default gets no reply. 8N2 also works, because the receiver tolerates the extra stop bit.
* The reply to `ESC L ooo CR` is two STX messages (levels 1–4, then 5–8) followed by **US (0x1F)**, even for single-output requests. The protocol document only mentions US for block requests.
* Unassigned levels report the source as `31?` (non-numeric).
* Numbering is 0-based in the protocol. Cerebrum displays 1-based (protocol 002 ← 102 is DST-0003 ← SRC-0103).
* Router response time to `ESC L` is roughly 15–50 ms.

## Cerebrum (v2.9)

* The router type list includes **Utah RCP-1** and **Utah UDS**, although the 2.9 manual only documents Utah RCP-3.
* Cerebrum does not pace polls by the Refresh Rate setting. It sends the next `ESC L` as soon as the previous reply arrives, sweeping destinations 000–143.
* **Drop behaviour:** the direct serial link (USB RS-422 adapter) cycled through Active, No Connection (about 3 s), "No Connection Active" (1–7 min) and back to Active. A soak test with the same request pattern from a script showed zero failures, so the router and line are fine.
* Over TCP via a bridge:
  * v1 forwarded after a fixed 50 ms read wait, so replies usually arrived whole: stable.
  * v2 forwarded bytes the instant they arrived, so replies were fragmented: the drops came back.
  * v3 (this project) assembles complete reports up to US, then forwards immediately. It passes the fragmenting-router test; confirm stability on site with a soak run in Cerebrum.
* Conclusion: Cerebrum's Utah RCP-1 driver does not reassemble a reply that arrives across multiple reads. Worth reporting to EVS with these notes.

## Windows gotchas met along the way

* com0com's unsigned driver is blocked on Windows 10/11 (Code 52). Avoid it on a Cerebrum server.
* Something else on the Cerebrum PC (most likely Cerebrum itself) already listens on TCP 5001, so don't use 5001 for the bridge. With `SO_REUSEADDR` on Windows, two programs can bind the same port and connections go to the wrong one. The bridge doesn't set it on Windows, so a taken port fails with a clear error, and it defaults to port 5101.
* Browsers save downloads with " (1)" or a trailing space in the filename. Check with `dir` before running a script.
