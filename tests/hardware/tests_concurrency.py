#!/usr/bin/env python3
"""
PicoGremlin HIL test: concurrent SWD + UART + Logic Analyzer, and the
    PIO2 fast-capture recovery.

The target is kept RUNNING so the fixture keeps responding over UART and the
logic analyzer keeps sampling while SWD transactions are issued concurrently.
A fast LA capture uses PIO2 while PIO1 SWD remains resident; SWD is confirmed
before and after capture. The current helper remains sequential and does not
claim true transaction overlap.

Usage:
    python3 tests_concurrency.py [--out DIR]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import (
    SerialPort, find_ports, la_capture, fix_echo, fix_configure,
    target_symbol_address, oocd_argv, TARGET_FIXTURE_ELF,
)

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pg_target_single.cfg")
COUNTER_ADDR = target_symbol_address("g_counter")


def oocd_run(cmds, timeout=120):
    r = subprocess.run(oocd_argv(CFG, *cmds),
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


def read_counter_running():
    """Connect, run the target, read g_counter (AHB while running), disconnect."""
    rc, out = oocd_run(["-c", "init", "-c", "reset run", "-c", "sleep 100",
                        "-c", f"read_memory 0x{COUNTER_ADDR:08x} 32 1",
                        "-c", "shutdown"], timeout=60)
    m = re.findall(r"0x[0-9a-f]+", out)
    return rc, (m[-1] if m else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "artifacts"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    results = []

    def rec(name, status, **details):
        results.append({"name": name, "status": status, **details})
        print(f"[{status}] {name} {details}", flush=True)

    uart, la = find_ports()
    up = SerialPort(uart, 115200)
    lp = SerialPort(la, 115200)

    # baseline: fixture signals + UART echo + LA capture all healthy
    fix_configure(up, 0, 1000, 500, 0)
    fix_configure(up, 1, 1000, 500, 0)
    time.sleep(0.3)
    echo = fix_echo(up, b"concurrency-baseline", timeout=5)
    rec("pre-uart-echo", "PASS" if echo == b"concurrency-baseline" else "FAIL",
        got=echo)

    # ---- 1. open SWD connection, read a target variable (target running) --
    rc, counter = read_counter_running()
    rec("swd-open-read", "PASS" if rc == 0 and counter else "FAIL", counter=counter)

    # ---- 2. UART + LA while SWD is being used repeatedly ------------------
    swd_ok = True
    fails_detail = []
    for i in range(5):
        rc, counter = read_counter_running()
        if rc != 0:
            swd_ok = False
            fails_detail.append(f"swd{i}")
        time.sleep(0.5)
        echo = fix_echo(up, b"swd-uart-mix", timeout=5)
        if echo != b"swd-uart-mix":
            swd_ok = False
            fails_detail.append(f"uart{i}")
        cap = la_capture(lp, [14, 15], 1000000, 2000, 60000, trigger_type=0, trigger=14)
        if not cap.get("ok"):
            swd_ok = False
            fails_detail.append(f"la{i}:{cap.get('response')}")
    rec("swd+uart+la-concurrent-5x", "PASS" if swd_ok else "FAIL",
        fails=fails_detail)

    # ---- 3. fast capture while the PIO1 SWD backend remains resident -------
    fast_ok = True
    fast_ok &= fix_echo(up, b"before-fast", timeout=5) == b"before-fast"
    cap = la_capture(lp, [14, 15], 1000000, 2000, 60000, trigger_type=2,
                     trigger=14, inverted=1, trigger_value=1)
    fast_ok &= bool(cap.get("ok", False))
    rc, counter = read_counter_running()
    fast_ok &= bool(rc == 0 and counter)
    rec("fast-capture+swd-recovery", "PASS" if fast_ok else "FAIL",
        swd_rc=rc, counter=counter)

    # ---- 4. verify SWD still fully functional after fast capture ---------
    rc, out = oocd_run(["-c", "init", "-c", "halt",
                        "-c", "echo CNT0", "-c", f"read_memory 0x{COUNTER_ADDR:08x} 32 1",
                        "-c", "resume", "-c", "sleep 300", "-c", "halt",
                        "-c", "echo CNT1", "-c", f"read_memory 0x{COUNTER_ADDR:08x} 32 1",
                        "-c", "shutdown"], timeout=60)
    c0 = re.findall(r"CNT0\s+0x([0-9a-f]+)", out)
    c1 = re.findall(r"CNT1\s+0x([0-9a-f]+)", out)
    progressed = False
    if c0 and c1:
        try:
            progressed = ((int(c1[-1], 16) - int(c0[-1], 16)) & 0xffffffff) != 0
        except ValueError:
            progressed = False
    rec("swd-resume-after-fast", "PASS" if (rc == 0 and c0 and c1 and progressed)
        else "FAIL", rc=rc, c0=c0[-1] if c0 else None, c1=c1[-1] if c1 else None)

    # ---- 5. long SWD programming while UART+LA active ---------------------
    rc, out = oocd_run(["-c", f"program {TARGET_FIXTURE_ELF} verify reset",
                        "-c", "shutdown"], timeout=180)
    rec("swd-program-while-busy", "PASS" if rc == 0 and "Verified OK" in out else "FAIL")

    # leave running
    read_counter_running()

    up.close()
    lp.close()

    path = os.path.join(args.out, "concurrency_test_results.json")
    with open(path, "w") as f:
        json.dump({"results": results}, f, indent=2, default=str)
    fails = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(fails)} not PASS")
    print(f"results written to {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
