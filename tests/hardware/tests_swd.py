#!/usr/bin/env python3
"""
PicoGremlin HIL test: SWD programming + GDB debugging of the RP2350 target
through the PicoGremlin CMSIS-DAP probe.

Usage:
    python3 tests_swd.py [--out DIR]
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import target_symbol_address, oocd_argv

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pg_target_single.cfg")
ELF = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "target_fixture", "build", "target_fixture.elf")
GDB = (os.environ.get("GDB") or shutil.which("gdb-multiarch")
       or shutil.which("arm-none-eabi-gdb") or "gdb-multiarch")
GDB_PORT = 3333
COUNTER_ADDR = target_symbol_address("g_counter")


class OpenOcdSession:
    def __init__(self, gdb_port=GDB_PORT):
        self.proc = subprocess.Popen(
            oocd_argv(CFG, "-c", f"gdb_port {gdb_port}"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self._wait_ready()

    def _wait_ready(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if "Listening on port" in line:
                return
            if self.proc.poll() is not None:
                raise RuntimeError("OpenOCD exited early")
        raise TimeoutError("OpenOCD did not become ready")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


def run_openocd_cmd(cmds, timeout=180):
    args = oocd_argv(CFG, *cmds)
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


def run_gdb(actions, timeout=120):
    cmd = [GDB, "-batch", "-ex", "set pagination off"] + \
          [x for pair in [["-ex", a] for a in actions] for x in pair]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


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

    # ---- 1. connect + identify -------------------------------------------
    rc, out = run_openocd_cmd(["-c", "init", "-c", "halt",
                               "-c", "read_memory 0x40000000 32 1",
                               "-c", "shutdown"], timeout=60)
    m = re.findall(r"0x[0-9a-f]{8}", out)
    chipid = m[-1] if m else None
    rec("swd-connect", "PASS" if rc == 0 and "Cortex-M33" in out else "FAIL",
        chipid=chipid, dpidr=m[0] if m else None)

    # ---- 2. program fixture via SWD ---------------------------------------
    rc, out = run_openocd_cmd(
        ["-c", f"program {ELF} verify reset", "-c", "shutdown"], timeout=180)
    rec("swd-program-verify", "PASS" if rc == 0 and "Verified OK" in out else "FAIL")

    # ---- 3. GDB debug session ---------------------------------------------
    gdb_actions = [
        f"file {ELF}",
        f"target extended-remote :{GDB_PORT}",
        "monitor reset halt",
        "b breakpoint_hit",
        "c",
        "p/x g_bp_hits",
        "p/x g_counter",
        "p g_toggle_state",
        "p/x g_toggle_count",
        "p/x g_ch_a_freq",
        "si",
        "set g_counter = 12345",
        "p/x g_counter",
        "c",
        "monitor halt",
        "p/x g_counter",
        "monitor reset halt",
        "p/x g_counter",
        "detach",
    ]
    ocd = None
    try:
        ocd = OpenOcdSession()
        rc, out = run_gdb(gdb_actions, timeout=120)
    except Exception as e:
        out = f"exception: {e}"
    finally:
        if ocd:
            ocd.stop()

    log = os.path.join(args.out, "swd_gdb_session.txt")
    with open(log, "w") as f:
        f.write(out)
    rec("gdb-connect", "PASS" if ("Remote debugging" in out or
                                  "Remote target" in out or
                                  "Breakpoint 1," in out) else "FAIL",
        log=out[-400:])
    rec("gdb-breakpoint-hit", "PASS" if "Breakpoint 1," in out else "FAIL")
    vals = re.findall(r"\$[0-9]+ = (0x[0-9a-f]+)", out)
    rec("gdb-values", "PASS" if len(vals) >= 6 else "FAIL", values=vals)

    # ---- 4. reset + reconnect cycle ---------------------------------------
    rc, out = run_openocd_cmd(["-c", "init", "-c", "reset run",
                               "-c", "sleep 500", "-c", "shutdown"], timeout=60)
    rec("swd-reset-run", "PASS" if rc == 0 else "FAIL")

    rc, out = run_openocd_cmd(["-c", "init", "-c", "halt",
                               "-c", f"read_memory 0x{COUNTER_ADDR:08x} 32 1",
                               "-c", "shutdown"], timeout=60)
    m = re.findall(r"0x[0-9a-f]+", out)
    counter = m[-1] if m else None
    rec("swd-reconnect", "PASS" if rc == 0 and counter else "FAIL", counter=counter)

    # leave the target running
    run_openocd_cmd(["-c", "init", "-c", "reset run",
                     "-c", "sleep 300", "-c", "shutdown"], timeout=60)

    path = os.path.join(args.out, "swd_test_results.json")
    with open(path, "w") as f:
        json.dump({"results": results}, f, indent=2)
    fails = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(fails)} not PASS")
    print(f"results written to {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
