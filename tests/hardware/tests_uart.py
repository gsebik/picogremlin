#!/usr/bin/env python3
"""
PicoGremlin HIL test: UART bridge (CDC0 <-> probe uart1 <-> target UART0).

Tests bidirectional UART at all required baud rates with several payload
patterns, short/long/burst/continuous streams, and drop/corruption detection.

Usage:
    python3 tests_uart.py [--out DIR]

Exit status: 0 if all tests pass, 1 if any fail.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import (
    SerialPort, find_ports, fix_identity, fix_echo, fix_pattern,
    fix_stop_stream, fix_crc, fix_pattern_bytes, oocd_argv,
    PATTERN_AZ, PATTERN_09, PATTERN_55, PATTERN_AA, PATTERN_00FF, PATTERN_PRNG,
)

BAUDS = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
PATTERNS = [
    ("A-Z", PATTERN_AZ),
    ("0-9", PATTERN_09),
    ("0x55", PATTERN_55),
    ("0xAA", PATTERN_AA),
    ("0x00-0xFF", PATTERN_00FF),
    ("PRNG", PATTERN_PRNG),
]

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pg_target_single.cfg")


def reset_target_to_default():
    """Reboot the target so the fixture runs at its default 115200 baud."""
    subprocess.run(
        oocd_argv(CFG, "-c", "init", "-c", "reset run",
                  "-c", "sleep 1500", "-c", "shutdown"),
        capture_output=True, timeout=60)
    time.sleep(1)


def switch_baud(port, new_baud, timeout=3):
    """Tell the fixture to move its UART to new_baud, then move the bridge.

    Verifies with an identity round-trip and retries; on failure the caller
    should reset the target via SWD.
    """
    for attempt in range(3):
        port.set_baud(port.baud)
        port.write(b"B" + new_baud.to_bytes(4, "little"))
        port.flush()
        time.sleep(0.3)
        port.drain(0.2)
        port.set_baud(new_baud)
        time.sleep(0.2)
        if fix_identity(port, timeout=5) == b"PICOGREMLIN_TARGET_FIXTURE_V1\n":
            return True
        time.sleep(0.3)
    return False


def run(port_path, outdir):
    results = []

    def rec(name, status, **details):
        results.append({"name": name, "status": status, **details})
        print(f"[{status}] {name} {details}", flush=True)

    reset_target_to_default()
    p = SerialPort(port_path, 115200)

    ident = fix_identity(p, timeout=5)
    rec("identity@115200", "PASS" if ident == b"PICOGREMLIN_TARGET_FIXTURE_V1\n"
        else "FAIL", ident=ident.decode(errors="replace"))

    for baud in BAUDS:
        # Always move the FIXTURE, not just the host port. After the 57600 case the fixture
        # is still at 57600, so the old host-only `else` branch for 115200 read the fixture
        # at the wrong baud and failed — a test bug, not a bridge bug.
        ok = switch_baud(p, baud)
        if not ok:
            rec(f"switch@{baud}", "FAIL")
            reset_target_to_default()
            p = SerialPort(port_path, 115200)
            continue
        rec(f"identity@{baud}", "PASS")

        # echo
        payload = b"hello picogremlin uart bridge 0123456789"
        echoed = fix_echo(p, payload, timeout=10)
        rec(f"echo@{baud}", "PASS" if echoed == payload else "FAIL",
            sent=len(payload), got=len(echoed))

        # patterns; keep transfer time < ~3 s per test at this baud
        max_len = min(65536, max(64, int(baud * 3 // 10)))
        for pname, pid in PATTERNS:
            for plen in (64, max_len):
                buf = fix_pattern(p, pid, plen, timeout=max(10, plen * 11 // baud + 5))
                expected = fix_pattern_bytes(pid, plen)
                ok = len(buf) == plen + 4 and buf[:plen] == expected and \
                     int.from_bytes(buf[plen:plen + 4], "little") == fix_crc(expected)
                rec(f"pattern:{pname}@{baud}:{plen}", "PASS" if ok else "FAIL",
                    got=len(buf), want=plen + 4)

    # burst test at 115200
    if not switch_baud(p, 115200):
        reset_target_to_default()
        p = SerialPort(port_path, 115200)
    p.drain(0.5)
    n, size = 50, 200
    p.write(b"T" + bytes([n]) + size.to_bytes(4, "little"))
    p.flush()
    expected = bytearray()
    for i in range(n):
        chunk = bytes([(i * size + j) & 0xFF for j in range(size)])
        expected += chunk + (fix_crc(chunk) & 0xFFFFFFFF).to_bytes(4, "little")
    buf = p.read(len(expected), timeout=30)
    rec("burst:50x200@115200", "PASS" if buf == bytes(expected) else "FAIL",
        want=len(expected), got=len(buf))

    # continuous stream at 115200
    p.write(b"S" + bytes([PATTERN_55]) + (64).to_bytes(4, "little"))
    p.flush()
    ack = p.read(1, timeout=3)
    buf = p.read(8192, timeout=6)
    ok = ack == b"K" and len(buf) == 8192 and buf.count(0x55) == len(buf)
    rec("stream:0x55@115200", "PASS" if ok else "FAIL", got=len(buf))
    fix_stop_stream(p)

    # long packet (256 KiB) at 460800
    if not switch_baud(p, 460800):
        reset_target_to_default()
        p = SerialPort(port_path, 115200)
    p.drain(0.5)
    plen = 262144
    buf = fix_pattern(p, PATTERN_00FF, plen, timeout=60)
    expected = fix_pattern_bytes(PATTERN_00FF, plen)
    ok = len(buf) == plen + 4 and buf[:plen] == expected and \
         int.from_bytes(buf[plen:plen + 4], "little") == fix_crc(expected)
    rec("pattern:00FF@460800:262144", "PASS" if ok else "FAIL",
        got=len(buf), want=plen + 4)

    # recovery from a low, non-standard baud
    if switch_baud(p, 4800):
        ident = fix_identity(p, timeout=5)
        rec("recover-after-4800", "PASS" if ident else "FAIL",
            ident=ident.decode(errors="replace").strip())
    else:
        rec("recover-after-4800", "FAIL", ident="switch failed")
    switch_baud(p, 115200)
    p.close()

    # disconnect/reconnect: close and reopen the CDC port
    reset_target_to_default()
    q = SerialPort(port_path, 115200)
    ident = fix_identity(q, timeout=5)
    rec("reopen-after-reconnect", "PASS" if ident == b"PICOGREMLIN_TARGET_FIXTURE_V1\n"
        else "FAIL", ident=ident.decode(errors="replace").strip())
    q.close()

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "artifacts"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    uart, _la = find_ports()
    if not uart:
        print("ERROR: no UART (CDC0) port found")
        return 2
    results = run(uart, args.out)
    path = os.path.join(args.out, "uart_test_results.json")
    with open(path, "w") as f:
        json.dump({"results": results}, f, indent=2)
    fails = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(fails)} not PASS")
    print(f"results written to {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())