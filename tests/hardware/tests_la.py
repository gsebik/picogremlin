#!/usr/bin/env python3
"""
PicoGremlin HIL test: logic analyzer identity, channel mapping and capture modes.

Usage:
    python3 tests_la.py [--out DIR]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import (
    SerialPort, find_ports, la_capture, analyze_channels, la_read_response,
    fix_configure, fix_identity as _fi, LA_ID_CMD,
)

def run(port_path, outdir):
    results = []

    def rec(name, status, **details):
        results.append({"name": name, "status": status, **details})
        print(f"[{status}] {name} {details}", flush=True)

    lp = SerialPort(port_path, 115200)
    up = SerialPort(find_ports()[0], 115200)

    # --- identity ----------------------------------------------------------
    lp.drain(1.0)
    lp.write(LA_ID_CMD)
    lp.flush()
    resp = lp.read_until(b"CHANNELS:24", timeout=5, maxlen=4096).decode(errors="replace")
    expect_lines = {
        "name": "LOGIC_ANALYZER_PICO_2_V6_0",
        "freq": "FREQ:100000000",
        "blastfreq": "BLASTFREQ:200000000",
        "buffer": "BUFFER:393216",
        "channels": "CHANNELS:24",
    }
    ok = all(x in resp for x in expect_lines.values())
    rec("la-identity", "PASS" if ok else "FAIL", response=resp.strip())

    # --- channel mapping: GP16 = ch14, GP17 = ch15 --------------------------
    # channel A (GP16) at 1 kHz; channel B disabled (freq=0 -> not configured)
    fix_configure(up, 0, 1000, 500, 0)
    fix_configure(up, 1, 0, 0, 0)
    time.sleep(0.3)
    cap = la_capture(lp, [14, 15], 1000000, 2000, 60000, trigger_type=0, trigger=14)
    if cap["ok"]:
        ch = analyze_channels(cap["samples"], cap["width"], 1000000)
        a_edges = ch[0]["edge_count"]
        b_edges = ch[1]["edge_count"]
        rec("channel-map-gp16->ch14", "PASS" if a_edges > 10 else "FAIL",
            edges_a=a_edges, edges_b=b_edges)
    else:
        rec("channel-map-gp16->ch14", "FAIL", response=cap.get("response"))

    fix_configure(up, 0, 0, 0, 0)
    fix_configure(up, 1, 1000, 500, 0)
    time.sleep(0.3)
    cap = la_capture(lp, [14, 15], 1000000, 2000, 60000, trigger_type=0, trigger=15)
    if cap["ok"]:
        ch = analyze_channels(cap["samples"], cap["width"], 1000000)
        a_edges = ch[0]["edge_count"]
        b_edges = ch[1]["edge_count"]
        rec("channel-map-gp17->ch15", "PASS" if b_edges > 10 else "FAIL",
            edges_a=a_edges, edges_b=b_edges)
    else:
        rec("channel-map-gp17->ch15", "FAIL", response=cap.get("response"))

    # --- both channels at same freq ----------------------------------------
    fix_configure(up, 0, 10000, 500, 0)
    fix_configure(up, 1, 10000, 500, 0)
    time.sleep(0.3)
    cap = la_capture(lp, [14, 15], 1000000, 2000, 60000, trigger_type=0, trigger=14)
    ok = cap["ok"]
    if ok:
        ch = analyze_channels(cap["samples"], cap["width"], 1000000)
        ok = ch[0]["edge_count"] > 100 and ch[1]["edge_count"] > 100
        rec("both-channels@10k", "PASS" if ok else "FAIL",
            edges_a=ch[0]["edge_count"], edges_b=ch[1]["edge_count"])
    else:
        rec("both-channels@10k", "FAIL", response=cap.get("response"))

    # --- edge-triggered capture modes --------------------------------------
    for ttype, tname in [(0, "edge"), (1, "complex"), (2, "fast"), (3, "blast")]:
        try:
            if ttype == 0:
                cap = la_capture(lp, [14, 15], 1000000, 2000, 60000, trigger_type=0, trigger=14)
            elif ttype == 1:
                cap = la_capture(lp, [14, 15], 1000000, 2000, 60000, trigger_type=1,
                                 trigger=14, inverted=1, trigger_value=1)
            elif ttype == 2:
                cap = la_capture(lp, [14, 15], 1000000, 2000, 60000, trigger_type=2,
                                 trigger=14, inverted=1, trigger_value=1)
            else:
                cap = la_capture(lp, [14, 15], 200000000, 0, 100000, trigger_type=3,
                                 trigger=14)
            ok = cap["ok"] and cap["count"] > 0
            rec(f"capture:{tname}", "PASS" if ok else "FAIL",
                count=cap.get("count"), response=cap.get("response", "")[:60])
        except Exception as e:
            rec(f"capture:{tname}", "FAIL", error=str(e))

    # --- repeated captures ---------------------------------------------------
    good = 0
    for i in range(10):
        cap = la_capture(lp, [14, 15], 1000000, 2000, 60000, trigger_type=0, trigger=14)
        if cap["ok"]:
            good += 1
        time.sleep(0.2)
    rec("repeated-capture-10x", "PASS" if good == 10 else "FAIL", good=good)

    # --- large buffer --------------------------------------------------------
    cap = la_capture(lp, [14, 15], 1000000, 2000, 190000, trigger_type=0, trigger=14)
    ok = cap["ok"] and cap["count"] == 192000
    rec("large-buffer-192k", "PASS" if ok else "FAIL",
        count=cap.get("count"), response=cap.get("response", "")[:60])

    # --- cancel ---------------------------------------------------------------
    # issue a capture, then cancel with 0xFF
    req = None
    from picogremlin_hil import la_build_capture_request, la_frame
    mode = 1
    chans = [14, 15]
    req = la_build_capture_request(0, 14, 0, 0, chans, 1000000, 2000, 190000, 0, 0, mode)
    lp.drain(1.0)
    lp.write(la_frame(1, req)); lp.flush()
    resp = la_read_response(lp, timeout=10)
    if b"CAPTURE_STARTED" in resp:
        time.sleep(1.0)
        lp.write(b"\xff"); lp.flush()
        time.sleep(2.0)
        lp.drain(1.0)
        # device should accept a new identity request after cancel
        lp.write(LA_ID_CMD); lp.flush()
        resp2 = lp.read_until(b"CHANNELS:24", timeout=5, maxlen=4096)
        rec("capture-cancel", "PASS" if b"CHANNELS:24" in resp2 else "FAIL",
            post_cancel=resp2.decode(errors="replace")[:40])
    else:
        rec("capture-cancel", "FAIL", response=resp.decode(errors="replace")[:60])

    # --- reconnect -----------------------------------------------------------
    lp.close()
    time.sleep(1)
    lp2 = SerialPort(port_path, 115200)
    lp2.write(LA_ID_CMD); lp2.flush()
    resp = lp2.read_until(b"CHANNELS:24", timeout=5, maxlen=4096)
    rec("la-reconnect", "PASS" if b"CHANNELS:24" in resp else "FAIL",
        response=resp.decode(errors="replace")[:40])
    lp2.close()

    up.close()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "artifacts"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    _u, la = find_ports()
    if not la:
        print("ERROR: no LA (CDC1) port found")
        return 2
    results = run(la, args.out)
    path = os.path.join(args.out, "la_test_results.json")
    with open(path, "w") as f:
        json.dump({"results": results}, f, indent=2)
    fails = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(fails)} not PASS")
    print(f"results written to {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())