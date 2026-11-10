#!/usr/bin/env python3
"""
PicoGremlin HIL test: signal measurement via the logic analyzer.

Configures the target fixture's two PIO generators over UART, captures with the
PicoGremlin logic analyzer, and computes frequency / period / duty / edges /
phase for a sweep of configurations.

Usage:
    python3 tests_measure.py [--out DIR]
"""
import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import (
    SerialPort, find_ports, la_capture, analyze_channels, fix_configure,
    calc_error_percent,
)

def measure_once(up, lp, freq, duty_a, duty_b, phase_b):
    """Configure both channels, capture, return measurement dict or None."""
    if not fix_configure(up, 0, freq, int(duty_a * 10), 0):
        return None
    if not fix_configure(up, 1, freq, int(duty_b * 10), int(phase_b * 10)):
        return None
    time.sleep(0.4)
    # choose sample rate: ~200 samples/period for good phase resolution, but
    # never below the LA minimum, and bounded so the capture fits the buffer
    LA_MIN = 6104
    sr = int(min(100_000_000, max(LA_MIN, freq * 200)))
    pre = 2000
    # ~30 periods of post samples, at least 20k samples, capped at the buffer
    post = min(190_000, (sr // (freq or 1)) * 30)
    post = max(post, 20_000)
    post = min(post, 190_000)
    cap = la_capture(lp, [14, 15], sr, pre, post, trigger_type=0, trigger=14)
    if not cap["ok"]:
        return {"ok": False, "response": cap.get("response")}
    ch = analyze_channels(cap["samples"], cap["width"], sr)
    # phase: ch1 rising relative to ch0 rising
    r0 = ch[0]["rising"]
    r1 = ch[1]["rising"]
    phase = None
    if r0 and r1:
        p0_ticks = ch[0]["period_avg_s"] * sr
        deltas = []
        j = 0
        for e0 in r0:
            while j < len(r1) and r1[j] < e0:
                j += 1
            if j >= len(r1):
                break
            deltas.append((r1[j] - e0) % round(p0_ticks))
        if deltas and p0_ticks:
            phase = (sum(deltas) / len(deltas)) / p0_ticks * 360.0
    return {
        "ok": True,
        "sample_rate": sr,
        "samples": cap["count"],
        "freq_a_hz": ch[0]["frequency_hz"],
        "freq_b_hz": ch[1]["frequency_hz"],
        "duty_a_pct": ch[0]["duty_percent"],
        "duty_b_pct": ch[1]["duty_percent"],
        "high_a_s": ch[0]["high_avg_s"],
        "low_a_s": ch[0]["low_avg_s"],
        "high_b_s": ch[1]["high_avg_s"],
        "low_b_s": ch[1]["low_avg_s"],
        "edges_a": ch[0]["edge_count"],
        "edges_b": ch[1]["edge_count"],
        "rising_a": ch[0]["rising_count"],
        "falling_a": ch[0]["falling_count"],
        "phase_deg": phase,
    }


def run(up_path, la_path, outdir):
    results = []

    def rec(name, status, **details):
        results.append({"name": name, "status": status, **details})
        print(f"[{status}] {name} {details}", flush=True)

    up = SerialPort(up_path, 115200)
    lp = SerialPort(la_path, 115200)

    # -------- duty sweep at 1 kHz (phase 0) -------------------------------
    for duty in [10, 25, 50, 75, 90]:
        m = measure_once(up, lp, 1000, duty, duty, 0)
        if not m or not m.get("ok"):
            rec(f"duty:{duty}%", "FAIL", response=m.get("response"))
            continue
        spp = m["sample_rate"] / 1000.0
        duty_tol = max(2.0, 100.0 / spp * 3)
        ok = abs(m["duty_a_pct"] - duty) <= duty_tol
        rec(f"duty:{duty}%", "PASS" if ok else "FAIL",
            measured=m["duty_a_pct"], requested=duty, tol=duty_tol)

    # -------- frequency sweep ----------------------------------------------
    for freq in [1, 10, 100, 1000, 10000, 100000, 1000000]:
        m = measure_once(up, lp, freq, 50, 50, 0)
        if not m or not m.get("ok"):
            rec(f"freq:{freq}", "FAIL", response=m.get("response"))
            continue
        spp = m["sample_rate"] / freq
        freq_tol = max(0.5, 100.0 / spp * 3)
        err = calc_error_percent(m["freq_a_hz"], freq)
        ok = err <= freq_tol
        rec(f"freq:{freq}Hz", "PASS" if ok else "FAIL",
            measured=m["freq_a_hz"], requested=freq, err_pct=err, tol=freq_tol)

    # -------- phase sweep at 1 kHz -----------------------------------------
    for phase in [0, 45, 90, 180, 270]:
        m = measure_once(up, lp, 1000, 50, 50, phase)
        if not m or not m.get("ok"):
            rec(f"phase:{phase}", "FAIL", response=m.get("response"))
            continue
        spp = m["sample_rate"] / 1000.0
        phase_tol = max(3.0, 360.0 / spp * 3)
        if m["phase_deg"] is None:
            rec(f"phase:{phase}", "FAIL", measured=None)
            continue
        err = abs(m["phase_deg"] - phase) % 360.0
        err = min(err, 360.0 - err)
        ok = err <= phase_tol
        rec(f"phase:{phase}", "PASS" if ok else "FAIL",
            measured=m["phase_deg"], requested=phase, err_deg=err, tol=phase_tol)

    # -------- mixed duty/phase final configs (Tests A-C) -------------------
    tests = [
        ("TestA", 1000, 50, 50, 0),
        ("TestB", 1000, 50, 50, 90),
        ("TestC", 1000, 25, 75, 180),
        ("TestD", 100, 50, 50, 90),
        ("TestE", 100000, 50, 50, 90),
    ]
    for tname, freq, da, db, pb in tests:
        m = measure_once(up, lp, freq, da, db, pb)
        if not m or not m.get("ok"):
            rec(tname, "FAIL", response=m.get("response"))
            continue
        spp = m["sample_rate"] / freq
        freq_tol = max(0.5, 100.0 / spp * 3)
        duty_tol = max(2.0, 100.0 / spp * 3)
        phase_tol = max(3.0, 360.0 / spp * 3)
        ferr = calc_error_percent(m["freq_a_hz"], freq)
        derr_a = abs(m["duty_a_pct"] - da)
        derr_b = abs(m["duty_b_pct"] - db)
        perr = None
        if m["phase_deg"] is not None:
            perr = abs(m["phase_deg"] - pb) % 360.0
            perr = min(perr, 360.0 - perr)
        ok = ferr <= freq_tol and derr_a <= duty_tol and derr_b <= duty_tol and \
             (perr is None or perr <= phase_tol)
        rec(tname, "PASS" if ok else "FAIL",
            freq_req=freq, freq_meas=m["freq_a_hz"], freq_err_pct=ferr, freq_tol=freq_tol,
            duty_a_req=da, duty_a_meas=m["duty_a_pct"], duty_a_err=derr_a,
            duty_b_req=db, duty_b_meas=m["duty_b_pct"], duty_b_err=derr_b,
            phase_req=pb, phase_meas=m["phase_deg"], phase_err=perr, phase_tol=phase_tol)

    up.close()
    lp.close()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "artifacts"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    uart, la = find_ports()
    if not uart or not la:
        print("ERROR: ports not found")
        return 2
    results = run(uart, la, args.out)
    path = os.path.join(args.out, "measure_test_results.json")
    with open(path, "w") as f:
        json.dump({"results": results}, f, indent=2)
    fails = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(fails)} not PASS")
    print(f"results written to {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())