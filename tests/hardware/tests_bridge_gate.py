#!/usr/bin/env python3
"""HIL: the UART bridge must not drive the target's GP1 while no host holds the CDC port
open.

On the robot the probe's bridge TX is wired to the ultrasonic ECHO line, so holding it
idle-high would fight that module (through the 200R series resistor). The probe reports
the bridge TX pin's state in its CMD=0x40 (PG_INFO) response:

    BRIDGE_TX:1   the pin is a UART output (a host has the CDC port open)
    BRIDGE_TX:0   the pin is a high-Z input (no host — safe for the robot)

This checks the *logic*; the analogue level still needs a bench check (a load resistor).
"""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import SerialPort, find_ports, la_frame

TIMEOUT = 3.0


def bridge_tx(la):
    """Read the probe's BRIDGE_TX field (1 = driven, 0 = high-Z), or None if absent."""
    la.drain(0.3)
    la.write(la_frame(0x40)); la.flush()      # PicoGremlin info command
    buf = b""
    t0 = time.time()
    while time.time() - t0 < TIMEOUT:
        chunk = la.read(256, timeout=0.4)
        if not chunk:
            if b"BRIDGE_TX:" in buf:
                break
            continue
        buf += chunk
        if b"BRIDGE_TX:" in buf:
            break
    for line in buf.decode(errors="replace").splitlines():
        if line.startswith("BRIDGE_TX:"):
            return int(line.split(":", 1)[1].strip())
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts", "bridge"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    results = []
    def rec(name, status, **d):
        results.append({"name": name, "status": status, **d})
        print(f"[{status}] {name} {d}", flush=True)
        with open(os.path.join(args.out, "bridge_gate_results.json"), "w") as f:
            json.dump({"results": results}, f, indent=2)

    uart, la = find_ports()
    lp = SerialPort(la, 115200)

    # 1. No host on the bridge -> the probe must not drive GP1.
    idle = bridge_tx(lp)
    rec("tx-idle-no-host", "PASS" if idle == 0 else "FAIL", bridge_tx=idle)

    # 2. A host opens the bridge -> the probe drives GP1 (UART output).
    up = SerialPort(uart, 115200)
    time.sleep(0.4)
    opened = bridge_tx(lp)
    rec("tx-open-host", "PASS" if opened == 1 else "FAIL", bridge_tx=opened)
    up.close()

    # 3. Host closed -> back to high-Z.
    time.sleep(0.6)
    reclosed = bridge_tx(lp)
    rec("tx-reclosed", "PASS" if reclosed == 0 else "FAIL", bridge_tx=reclosed)

    lp.close()
    bad = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(bad)} not PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
