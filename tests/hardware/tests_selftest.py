#!/usr/bin/env python3
"""
PicoGremlin one-shot self-test.

One quick pass over the whole tool, for a freshly flashed probe wired to an
RP2350 target fixture (see README.md for the wiring):

    la-identity       CDC1 answers the LA identity request (USB composite + LA)
    swd-connect       OpenOCD reaches the target over SWD through the probe
    fixture-program   programs + verifies the target fixture over SWD
    uart-identity     fixture answers 'I' through the CDC0 UART bridge
    uart-echo         a byte pattern survives the UART bridge round trip
    la-capture        1 kHz (GP16) and 2 kHz (GP17) captured and measured

For a "is the probe alive" smoke test without a wired target, use --skip-swd
(the UART/LA checks then need a fixture already running for the signals).

Usage:
    python3 tests_selftest.py [--out DIR] [--skip-swd]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import (  # noqa: E402
    SerialPort, find_ports, la_capture, la_read_response, LA_ID_CMD,
    fix_identity, fix_echo, fix_configure, fix_send, FIX_CMD_DEFAULT,
    channel_metrics, oocd_argv,
)

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "pg_target_single.cfg")
ELF = os.path.join(HERE, "target_fixture", "build", "target_fixture.elf")
FIXTURE_IDENTITY = "PICOGREMLIN_TARGET_FIXTURE_V1"


def run_openocd(cmds, timeout=180):
    r = subprocess.run(oocd_argv(CFG, *cmds), capture_output=True,
                       text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


def run(outdir, skip_swd):
    results = []

    def rec(name, status, **details):
        results.append({"name": name, "status": status, **details})
        print(f"[{status}] {name} {details}", flush=True)

    uart_path, la_path = find_ports()
    if not la_path:
        rec("ports", "FAIL", uart=uart_path, la=la_path,
            hint="no LA (CDC1) port found for this probe")
        return results
    rec("ports", "PASS" if uart_path else "FAIL", uart=uart_path, la=la_path)
    if not uart_path:
        return results

    # --- 1. probe USB side: LA identity over CDC1 -------------------------
    lp = SerialPort(la_path, 115200)
    lp.drain(1.0)
    lp.write(LA_ID_CMD)
    lp.flush()
    resp = lp.read_until(b"CHANNELS:24", timeout=5, maxlen=4096)
    rec("la-identity", "PASS" if b"CHANNELS:24" in resp else "FAIL",
        response=resp.decode(errors="replace").strip()[:80])

    # --- 2. debugger: SWD connect + fixture programming -------------------
    if skip_swd:
        print("--skip-swd: skipping swd-connect and fixture-program", flush=True)
    else:
        try:
            rc, out = run_openocd(
                ["-c", "init", "-c", "halt",
                 "-c", "read_memory 0x40000000 32 1",
                 "-c", "shutdown"], timeout=60)
        except Exception as e:
            rc, out = 1, f"exception: {e}"
        chips = re.findall(r"0x[0-9a-f]{8}", out)
        rec("swd-connect", "PASS" if rc == 0 and "Cortex-M33" in out else "FAIL",
            chipid=chips[-1] if chips else None, tail=out[-160:])

        if not os.path.exists(ELF):
            rec("fixture-program", "FAIL", error=f"missing {ELF} (run ./pg fixture)")
        else:
            try:
                rc, out = run_openocd(
                    ["-c", f"program {ELF} verify reset", "-c", "shutdown"],
                    timeout=240)
            except Exception as e:
                rc, out = 1, f"exception: {e}"
            rec("fixture-program",
                "PASS" if rc == 0 and "Verified OK" in out else "FAIL",
                tail=out[-160:] if rc != 0 else "")

    # --- 3. UART bridge: fixture identity + echo --------------------------
    up = SerialPort(uart_path, 115200)
    ident = fix_identity(up, timeout=5).decode(errors="replace").strip()
    rec("uart-identity", "PASS" if FIXTURE_IDENTITY in ident else "FAIL",
        response=ident[:60])

    payload = bytes((i * 7 + 3) & 0xFF for i in range(256))
    echo = fix_echo(up, payload, timeout=10)
    rec("uart-echo", "PASS" if echo == payload else "FAIL",
        sent=len(payload), received=len(echo))

    # --- 4. LA: capture GP16/GP17 square waves ----------------------------
    fix_configure(up, 0, 1000, 500, 0)
    fix_configure(up, 1, 2000, 500, 0)
    time.sleep(0.3)
    cap = la_capture(lp, [14, 15], 1000000, 2000, 60000,
                     trigger_type=0, trigger=14)
    if cap["ok"]:
        m = channel_metrics(cap["samples"], cap["width"], 1000000, nch=2)
        f_a = m[0]["frequency_hz"]
        f_b = m[1]["frequency_hz"]
        err_a = abs(f_a - 1000) / 1000 * 100
        err_b = abs(f_b - 2000) / 2000 * 100
        rec("la-capture",
            "PASS" if m[0]["edge_count"] > 10 and m[1]["edge_count"] > 10
            and err_a <= 5 and err_b <= 5 else "FAIL",
            gp16_hz=round(f_a, 1), gp17_hz=round(f_b, 1),
            err_a_pct=round(err_a, 2), err_b_pct=round(err_b, 2))
    else:
        rec("la-capture", "FAIL", response=str(cap.get("response"))[:80])

    # leave the fixture in its default state
    fix_send(up, FIX_CMD_DEFAULT)
    time.sleep(0.1)
    fix_configure(up, 0, 0, 0, 0)
    fix_configure(up, 1, 0, 0, 0)

    lp.close()
    up.close()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "artifacts", "selftest"))
    ap.add_argument("--skip-swd", action="store_true",
                    help="skip the SWD checks (no wired target needed)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    results = run(args.out, args.skip_swd)
    path = os.path.join(args.out, "selftest_results.json")
    with open(path, "w") as f:
        json.dump({"results": results}, f, indent=2)
    fails = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(fails)} not PASS")
    print(f"results written to {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
