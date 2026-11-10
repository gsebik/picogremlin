#!/usr/bin/env python3
"""
PicoGremlin HIL test: UART autobaud (CDC0 line rate 9728 triggers the probe's
PIO/DMA measurement of the incoming UART bit rate on GP25).

The fixture is commanded to autonomously transmit a known pattern at a known
baud; the probe measures the incoming rate. Byte integrity of the received
pattern proves the measured baud matches the configured baud (the exact
detected value is not exposed by firmware).

Usage:
    python3 tests_autobaud.py [--out DIR]
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import (
    SerialPort, find_ports, fix_identity, fix_pattern_bytes, oocd_argv,
    PATTERN_55, PATTERN_AA, PATTERN_00FF, PATTERN_PRNG,
)

AUTOBAUD_MAGIC = 9728
BAUDS = [9600, 19200, 38400, 57600, 115200, 230400]

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pg_target_single.cfg")


def reset_target():
    subprocess.run(oocd_argv(CFG, "-c", "init",
                    "-c", "reset run", "-c", "sleep 1500", "-c", "shutdown"),
                   capture_output=True, timeout=60)
    time.sleep(1)


def trigger_autobaud(port, baud, pat, length, timeout=10):
    """Command the fixture to transmit at 'baud', switch bridge to autobaud."""
    port.set_baud(115200)
    payload = b"A" + baud.to_bytes(4, "little") + bytes([pat]) + length.to_bytes(4, "little")
    port.write(payload)
    port.flush()
    # switch to autobaud magic within the fixture's 300 ms delay window
    time.sleep(0.05)
    port.set_baud(AUTOBAUD_MAGIC)
    time.sleep(2.5)          # let autobaud measure and the bytes arrive
    port.set_baud(115200)    # restore normal bridge operation
    time.sleep(0.2)
    return port.read(length + 16, timeout=timeout)


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

    uart, _la = find_ports()
    if not uart:
        print("ERROR: no UART port")
        return 2

    reset_target()
    p = SerialPort(uart, 115200)
    ident = fix_identity(p, timeout=5)
    rec("fixture-ready", "PASS" if ident == b"PICOGREMLIN_TARGET_FIXTURE_V1\n"
        else "FAIL", ident=ident.decode(errors="replace"))

    # standard bauds, 0x55 pattern (clean single-bit-low intervals)
    for baud in BAUDS:
        n = 512
        buf = trigger_autobaud(p, baud, PATTERN_55, n)
        got = buf.count(0x55)
        total = len(buf)
        ok = got >= n * 0.9 and total >= n * 0.9
        rec(f"autobaud-0x55@{baud}", "PASS" if ok else "FAIL",
            expected=n, received_55=got, total_bytes=total)

    # 0xAA pattern at 115200
    buf = trigger_autobaud(p, 115200, PATTERN_AA, 512)
    got = buf.count(0xAA)
    rec("autobaud-0xAA@115200", "PASS" if got >= 512 * 0.9 else "FAIL",
        expected=512, received_AA=got, total_bytes=len(buf))

    # 0x00..0xFF pattern at 115200 (full byte range, shift-tolerant match)
    reset_target()
    p = SerialPort(uart, 115200)
    buf = trigger_autobaud(p, 115200, PATTERN_00FF, 512)
    expected = bytes(i & 0xFF for i in range(512))
    best = 0
    for shift in range(0, min(16, len(buf))):
        run = 0
        for i in range(shift, min(len(buf), 512 + shift)):
            if i - shift < 512 and buf[i] == expected[i - shift]:
                run += 1
        best = max(best, run)
    # a failure here is the documented algorithm limitation, not a regression
    rec("autobaud-00FF@115200", "PASS" if best >= 512 * 0.9 else "NOT SUPPORTED",
        best_match=best, total=512, got=len(buf),
        reason="firmware autobaud locks onto a wrong divisor for data whose "
               "minimum low-pulse in the measurement window is not 1 UART bit "
               "(documented limitation, see HARDWARE_VALIDATION.md)")

    # pseudo-random pattern: same limitation expected
    reset_target()
    p = SerialPort(uart, 115200)
    buf = trigger_autobaud(p, 115200, PATTERN_PRNG, 512)
    expected = fix_pattern_bytes(PATTERN_PRNG, 512)
    best = 0
    for shift in range(0, min(24, len(buf))):
        run = 0
        for i in range(shift, min(len(buf), 512 + shift)):
            if i - shift < 512 and buf[i] == expected[i - shift]:
                run += 1
        best = max(best, run)
    rec("autobaud-PRNG@115200", "PASS" if best >= 512 * 0.9 else "NOT SUPPORTED",
        best_match=best, total=512, got=len(buf),
        reason="same autobaud divisor limitation as 0x00-FF")

    # single byte transmission: autobaud needs >= 8 low-pulse samples, so a
    # single byte cannot produce a measurement -> documented limitation.
    reset_target()
    p = SerialPort(uart, 115200)
    buf = trigger_autobaud(p, 115200, PATTERN_55, 1)
    rec("autobaud-single-byte@115200", "NOT SUPPORTED",
        reason="autobaud requires >=8 bit-period samples; a single byte "
               "cannot supply them", got=len(buf))

    # no signal: bridge enters autobaud, no traffic arrives, then recovers
    reset_target()
    p = SerialPort(uart, 115200)
    p.drain(0.5)
    p.set_baud(AUTOBAUD_MAGIC)
    time.sleep(2.0)
    p.set_baud(115200)
    time.sleep(0.5)
    buf = p.read(64, timeout=2)
    ident = fix_identity(p, timeout=5)
    rec("autobaud-no-signal-recover", "PASS" if len(buf) == 0 and ident
        else "FAIL", stray_bytes=len(buf),
        ident=ident.decode(errors="replace").strip())

    # restore and report
    reset_target()
    p.close()

    path = os.path.join(args.out, "autobaud_test_results.json")
    with open(path, "w") as f:
        json.dump({"results": results}, f, indent=2)
    fails = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(fails)} not PASS")
    print(f"results written to {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())