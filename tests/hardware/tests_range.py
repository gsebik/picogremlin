#!/usr/bin/env python3
"""
PicoGremlin HIL test: high-frequency range (Test F) and different-channel
frequencies (Test G).

Usage:
    python3 tests_range.py [--out DIR]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import (
    SerialPort, find_ports, la_capture, analyze_channels, fix_configure,
)

HIGH_FREQS = [2000000, 5000000, 10000000, 20000000, 50000000, 100000000]


def run(up_path, la_path, outdir):
    results = []

    def rec(name, status, **details):
        results.append({"name": name, "status": status, **details})
        print(f"[{status}] {name} {details}", flush=True)

    up = SerialPort(up_path, 115200)
    lp = SerialPort(la_path, 115200)

    # ---- Test F: practical upper-frequency limit ---------------------------
    # The LA samples at up to 100 MHz; a frequency f has ~100e6/f samples per
    # period, so measurement is subject to +/-1 sample quantization. Tolerances
    # are quantization-aware. Above ~50 MHz (2 samples/period) the capture
    # aliases and is not a reliable measurement.
    for freq in HIGH_FREQS:
        fix_configure(up, 0, freq, 500, 0)
        fix_configure(up, 1, freq, 500, 0)
        time.sleep(0.3)
        sr = min(100_000_000, freq * 20)   # >=20 samples/period
        cap = la_capture(lp, [14, 15], sr, 2000, 60000, trigger_type=0, trigger=14)
        if not cap.get("ok"):
            rec(f"freq:{freq}", "FAIL", response=cap.get("response"))
            continue
        ch = analyze_channels(cap["samples"], cap["width"], sr)
        m = ch[0]["frequency_hz"]
        spp = sr / freq
        tol = 100.0 / spp + 2.0   # +/-1 sample quantization + margin
        err = abs(m - freq) / freq * 100
        ok = err <= tol and spp >= 2.0
        # sr is capped at the 100 MHz sample-rate limit, so the 100 MHz fixture
        # setting gets only 1 sample/period and aliases by design (see the note
        # above): record that measurement for information, do not score it.
        status = "PASS" if ok else ("INFO" if spp < 2.0 else "FAIL")
        rec(f"freq:{freq}", status,
            measured=m, requested=freq, err_pct=round(err, 2), tol=round(tol, 2),
            samples_per_period=round(spp, 1),
            edges=ch[0]["edge_count"], duty=round(ch[0]["duty_percent"], 1))

    # ---- Test G: different frequencies on the two channels ----------------
    # chA (GP16) at 1 kHz, chB (GP17) at 10 kHz. A constant phase relationship
    # does not exist for different frequencies; only the individual frequencies
    # and duty cycles are validated.
    fix_configure(up, 0, 1000, 500, 0)
    fix_configure(up, 1, 10000, 250, 0)
    time.sleep(0.4)
    sr = 2000000   # >=200 samples/period for both
    cap = la_capture(lp, [14, 15], sr, 2000, 60000, trigger_type=0, trigger=14)
    if cap.get("ok"):
        ch = analyze_channels(cap["samples"], cap["width"], sr)
        err_a = abs(ch[0]["frequency_hz"] - 1000) / 1000 * 100
        err_b = abs(ch[1]["frequency_hz"] - 10000) / 10000 * 100
        ok = err_a <= 2.0 and err_b <= 2.0
        rec("TestG-diff-freq", "PASS" if ok else "FAIL",
            chA_hz=ch[0]["frequency_hz"], chB_hz=ch[1]["frequency_hz"],
            chA_err_pct=round(err_a, 2), chB_err_pct=round(err_b, 2),
            chA_duty=round(ch[0]["duty_percent"], 1),
            chB_duty=round(ch[1]["duty_percent"], 1),
            note="different frequencies: no constant phase relationship")
    else:
        rec("TestG-diff-freq", "FAIL", response=cap.get("response"))

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
    path = os.path.join(args.out, "range_test_results.json")
    with open(path, "w") as f:
        json.dump({"results": results}, f, indent=2)
    fails = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(fails)} not PASS")
    print(f"results written to {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())