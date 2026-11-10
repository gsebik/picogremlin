#!/usr/bin/env python3
"""
PicoGremlin HIL: upstream (gusmanb LogicAnalyzer) protocol compatibility.

The original desktop app and TerminalCapture must keep working against the
merged firmware, so the upstream command space (0..6) and the identity reply
must behave exactly like the stock LA firmware:

  T1 identity      exactly 5 lines (version, FREQ, BLASTFREQ, BUFFER, CHANNELS)
                   and nothing left in the buffer afterwards
  T2 wifi cmds     CMD 2 / 3 return ERR_UNKNOWN_MSG (stock non-WiFi behaviour)
  T3 blink         CMD 5 / 6 return BLINKON / BLINKOFF (stock)
  T4 unknown       an unknown command returns ERR_UNKNOWN_MSG (stock)
  T5 capture       CMD 1 (simple trigger) -> CAPTURE_STARTED + payload with edges
  T6 blast         CMD 1 (blast) -> CAPTURE_STARTED + payload
  T7 no desync     identity answers again after the captures
  T8 PG_INFO       CMD 0x40 -> PICOGREMLIN:/STREAM:/BRIDGE_TX: (our extension)
  T9 pg stream     CMD 0x43 / 0x42 stream and stop (our extension)

Usage: python3 tests_compat.py [--out DIR]
"""
import argparse
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import (  # noqa: E402
    SerialPort, find_ports, la_frame, la_build_capture_request, la_capture,
)


class LineReader:
    """Buffered line reader, like the stock driver's StreamReader."""

    def __init__(self, port):
        self.port = port
        self.buf = b""

    def line(self, timeout=3.0):
        deadline = time.time() + timeout
        while b"\n" not in self.buf and time.time() < deadline:
            self.buf += self.port.read(512, timeout=0.3)
        if b"\n" not in self.buf:
            return b""
        line, self.buf = self.buf.split(b"\n", 1)
        return line + b"\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "artifacts", "compat"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    results = []

    def rec(name, status, **d):
        results.append({"name": name, "status": status, **d})
        print(f"[{status}] {name} {d}", flush=True)

    _uart, la = find_ports()
    lp = SerialPort(la, 115200)
    rd = LineReader(lp)

    def cmd(code, payload=b"", nlines=1):
        lp.drain(0.3)
        lp.write(la_frame(code, payload))
        lp.flush()
        return [rd.line() for _ in range(nlines)]

    # ---- T1 identity: exactly the stock 5 lines, no leftovers ----------------
    lines = cmd(0, nlines=5)
    text = [x.decode(errors="replace").strip() for x in lines]
    ok = (text[0].startswith("LOGIC_ANALYZER_") and text[1].startswith("FREQ:")
          and text[2].startswith("BLASTFREQ:") and text[3].startswith("BUFFER:")
          and text[4].startswith("CHANNELS:24"))
    leftover = rd.buf + lp.read(64, timeout=0.4)
    rec("identity-5-lines", "PASS" if ok and not leftover else "FAIL",
        lines=text, leftover=leftover.decode(errors="replace")[:40])

    # ---- T2..T4 upstream command space ---------------------------------------
    # Upstream cases 2/3 live inside #ifdef USE_CYGW_WIFI: on a stock non-WiFi
    # build they fall through to ERR_UNKNOWN_MSG, which is what we must match.
    r = cmd(2)[0]
    rec("cmd2-stock", "PASS" if r == b"ERR_UNKNOWN_MSG\n" else "FAIL",
        response=r.decode(errors="replace").strip())
    r = cmd(3)[0]
    rec("cmd3-stock", "PASS" if r == b"ERR_UNKNOWN_MSG\n" else "FAIL",
        response=r.decode(errors="replace").strip())
    r = cmd(5)[0]
    rec("cmd5-blinkon", "PASS" if r == b"BLINKON\n" else "FAIL",
        response=r.decode(errors="replace").strip())
    r = cmd(6)[0]
    rec("cmd6-blinkoff", "PASS" if r == b"BLINKOFF\n" else "FAIL",
        response=r.decode(errors="replace").strip())
    r = cmd(0x77)[0]
    rec("unknown-command", "PASS" if r == b"ERR_UNKNOWN_MSG\n" else "FAIL",
        response=r.decode(errors="replace").strip())

    # ---- T5 simple capture (fixture square wave on ch14) ----------------------
    cap = la_capture(lp, [14, 15], 1_000_000, 1000, 20_000,
                     trigger_type=0, trigger=14, head_timeout=5)
    rec("capture-simple", "PASS" if cap["ok"] and cap["count"] == 21_000 else "FAIL",
        count=cap.get("count"), response=str(cap.get("response"))[:40])

    # ---- T6 blast capture (no trigger wait) -----------------------------------
    req = la_build_capture_request(3, 14, 0, 0, [14, 15], 10_000_000, 0, 5000,
                                   0, 0, 1)
    lp.drain(0.5)
    lp.write(la_frame(1, req))
    lp.flush()
    head = lp.read(4096, timeout=5)
    ok = b"CAPTURE_STARTED" in head
    if ok:
        head = head[head.index(b"CAPTURE_STARTED\n") + 15:]
        while len(head) < 4 + 5000 * 2 + 1:
            chunk = lp.read(8192, timeout=2)
            if not chunk:
                break
            head += chunk
        ok = len(head) >= 4 + 5000 * 2 + 1
    lp.drain(0.5)
    rec("capture-blast", "PASS" if ok else "FAIL", bytes=len(head))

    # ---- T7 no desync after captures -----------------------------------------
    lines = cmd(0, nlines=5)
    rec("identity-after-captures",
        "PASS" if lines[4] == b"CHANNELS:24\n" else "FAIL",
        last=lines[4].decode(errors="replace").strip())

    # ---- T8 our PG_INFO extension --------------------------------------------
    lp.drain(0.3)
    lp.write(la_frame(0x40))
    lp.flush()
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 2 and b"BRIDGE_TX:" not in buf:
        buf += lp.read(256, timeout=0.4)
    ok = (b"PICOGREMLIN:" in buf and b"STREAM:1" in buf and b"BRIDGE_TX:" in buf)
    rec("pg-info", "PASS" if ok else "FAIL",
        response=buf.decode(errors="replace").strip().replace("\n", " | "))

    # ---- T9 our stream commands ----------------------------------------------
    req = la_build_capture_request(0, 14, 0, 0, [14, 15], 200_000, 0, 4096,
                                   0, 0, 1)
    lp.drain(0.5)
    lp.write(la_frame(0x43, req))
    lp.flush()
    buf = b""
    t0 = time.time()
    while b"STREAM_CONT_STARTED" not in buf and time.time() - t0 < 5:
        buf += lp.read(256, timeout=1)
    started = b"STREAM_CONT_STARTED" in buf
    time.sleep(0.3)
    lp.write(la_frame(0x42))
    lp.flush()
    sb = b""
    t0 = time.time()
    while b"STREAM_STOPPED" not in sb and time.time() - t0 < 5:
        sb += lp.read(8192, timeout=1)
    rec("pg-stream", "PASS" if started and b"STREAM_STOPPED" in sb else "FAIL",
        started=started, stopped=b"STREAM_STOPPED" in sb)

    lines = cmd(0, nlines=5)
    rec("identity-final", "PASS" if lines[4] == b"CHANNELS:24\n" else "FAIL")
    lp.close()

    path = os.path.join(args.out, "compat_test_results.json")
    with open(path, "w") as f:
        json.dump({"results": results}, f, indent=2)
    fails = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(fails)} not PASS")
    print(f"results written to {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
