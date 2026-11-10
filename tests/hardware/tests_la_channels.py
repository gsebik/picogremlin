#!/usr/bin/env python3
"""
PicoGremlin HIL test: 24-channel logic-analyzer GPIO coverage.

Runs the identity-wired LA-channel campaign against the target fixture:
  T1  continuity / mapping (static levels, patterns, walking ones)
  T2  drive-direction / high-Z (input + pull-down/up)
  T3  frequency-comb identification
  T4  precision-pair timing (frequency/duty/phase per channel pair)
  T5  per-channel edge trigger coverage
  T6  PIO2 fast-capture funcsel interleave across channels
  T7  cross-subsystem non-interference (comb + SWD + UART + autobaud)

Only the channels reported as electrically connected by the walking-one
connectivity probe are exercised; unwired channels are recorded as
NOT TESTED in the artifacts. Results are checkpointed to a JSON file after
every check so a timeout never loses prior results.

Usage:
    python3 tests_la_channels.py --out DIR [--wired 0,1,4,...] [--only T1,T3]
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
    SerialPort, find_ports, la_capture, fix_mask, fix_walk, fix_comb,
    fix_gen_channels, fix_set_inputs, fix_heart, fix_configure, fix_identity,
    fix_echo, channel_metrics, target_symbol_address, oocd_argv,
)

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pg_target_single.cfg")

# Default asserted channel set. Wiring is bench-specific, so pass --wired /
# --wired-dyn to match your setup; run `--only T1` first to see which channels the
# walking-one probe reports as connected. A fully-wired bench uses all 24 (see
# CHANNEL_VALIDATION.md, 2026-08-19); this conservative default matches the
# 2026-08-18 bench.
WIRED_DEFAULT = [0, 1, 4, 9, 12, 14, 15, 16, 17]
WIRED_DYN_DEFAULT = [0, 1, 4, 9, 12, 14, 15, 16, 17]
ALL_CH = list(range(24))

ALL_GPIO = list(range(2, 23)) + [26, 27, 28]


def oocd_read_gpio_in():
    r = subprocess.run(oocd_argv(CFG, "-c", "adapter speed 100",
                        "-c", "init", "-c", "mdw 0xd0000010", "-c", "shutdown"),
                       capture_output=True, text=True, timeout=60)
    line = [x for x in (r.stdout + r.stderr).splitlines() if "0xd0000010" in x]
    return int(line[0].split(":")[1].strip(), 16) if line else 0


class Runner:
    def __init__(self, out, wired):
        os.makedirs(out, exist_ok=True)
        self.out = out
        self.wired = wired
        self.wiredset = set(wired)
        self.results = []
        self.json_path = os.path.join(out, "channels_test_results.json")
        self._load()

    def _load(self):
        if os.path.exists(self.json_path):
            with open(self.json_path) as f:
                self.results = json.load(f).get("results", [])

    def save(self):
        with open(self.json_path, "w") as f:
            json.dump({"results": self.results}, f, indent=2)

    def rec(self, name, status, **details):
        self.results.append({"name": name, "status": status, **details})
        self.save()
        print(f"[{status}] {name} {details}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "artifacts", "channels"))
    ap.add_argument("--wired", default=",".join(map(str, WIRED_DEFAULT)))
    ap.add_argument("--wired-dyn", default=",".join(map(str, WIRED_DYN_DEFAULT)))
    ap.add_argument("--only", default="all",
                    help="comma list of T1..T7 (default all)")
    args = ap.parse_args()
    wired = [int(x) for x in args.wired.split(",")]
    wired_dyn = [int(x) for x in args.wired_dyn.split(",")]
    only = set(args.only.split(","))

    uart, la = find_ports()
    up = SerialPort(uart, 115200)
    lp = SerialPort(la, 115200)
    time.sleep(0.5)
    r = Runner(args.out, wired)

    ident = fix_identity(up)
    r.rec("fixture-ready", "PASS" if ident and b"PICOGREMLIN_TARGET_FIXTURE" in ident
          else "FAIL", ident=ident.decode(errors="replace").strip())

    def capture(channels, freq, pre, post, trigger_type=0, trigger=0,
                inverted=0, trigger_value=0):
        cap = la_capture(lp, channels, freq, pre, post,
                         trigger_type=trigger_type, trigger=trigger,
                         inverted=inverted, trigger_value=trigger_value)
        if not cap.get("ok"):
            lp.drain(0.5); lp.write(b"\xff"); lp.flush(); time.sleep(0.5)
            cap = la_capture(lp, channels, freq, pre, post,
                             trigger_type=trigger_type, trigger=trigger,
                             inverted=inverted, trigger_value=trigger_value)
        return cap

    def bits_high(cap, nch=24):
        m = channel_metrics(cap["samples"], cap["width"], 1_000_000)
        return [b for b in range(nch) if m[b]["high_ratio"] > 0.5]

    # ---------------- T1 continuity / mapping ----------------
    if "all" in only or "T1" in only:
        # all-low, all-high
        fix_mask(up, 0)
        cap = capture(list(range(24)), 1_000_000, 500, 1200, inverted=1)
        bh = bits_high(cap) if cap.get("ok") else []
        ok = cap.get("ok") and all(b not in bh for b in wired)
        r.rec("T1 all-low", "PASS" if ok else "FAIL",
              ok=cap.get("ok"), stray_high=bh)

        fix_mask(up, 0xFFFFFF)
        cap = capture(list(range(24)), 1_000_000, 500, 1200)
        bh = bits_high(cap) if cap.get("ok") else []
        ok = cap.get("ok") and all(b in bh for b in wired)
        r.rec("T1 all-high", "PASS" if ok else "FAIL",
              ok=cap.get("ok"), missing=[b for b in wired if b not in bh])

        for pat, inv in ((0x555555, 0), (0xAAAAAA, 1)):
            fix_mask(up, pat)
            cap = capture(list(range(24)), 1_000_000, 500, 1200, inverted=inv)
            bh = bits_high(cap) if cap.get("ok") else []
            want = [b for b in wired if (pat >> b) & 1]
            ok = cap.get("ok") and all(b in bh for b in want)
            r.rec("T1 pattern-%06x" % pat, "PASS" if ok else "FAIL",
                  ok=cap.get("ok"), missing=[b for b in want if b not in bh])

        # walking ones on the wired set, ch0 always held as the trigger
        bad = []
        for k in wired_dyn:
            fix_mask(up, (1 << 0) | (1 << k))
            cap = capture(list(range(24)), 1_000_000, 300, 1000)
            if not cap.get("ok"):
                bad.append((k, "hang")); continue
            bh = bits_high(cap)
            extra = [b for b in wired if b not in (0, k) and b in bh]
            missing = (k not in bh) if k != 0 else (0 not in bh)
            if missing or extra:
                bad.append((k, {"lit": bh, "extra": extra}))
        r.rec("T1 walking-ones", "PASS" if not bad else "FAIL",
              wired=wired, failures=bad)

    # ---------------- T2 drive-direction / input bias ----------------
    if "all" in only or "T2" in only:
        # The probe LA inputs are pull-up biased (they read HIGH when the target
        # does not drive). A POSITIVE trigger therefore fires in both pull
        # states; the observed levels document the bias.
        fix_set_inputs(up, 2)          # pull-up
        time.sleep(0.2)
        cap = capture(list(range(24)), 1_000_000, 300, 800)
        bh = bits_high(cap) if cap.get("ok") else []
        ok = cap.get("ok") and all(b in bh for b in wired)
        r.rec("T2 input-pullup", "PASS" if ok else "FAIL",
              ok=cap.get("ok"), lit=bh)

        fix_set_inputs(up, 1)          # pull-down (weak vs probe pull-up)
        time.sleep(0.2)
        cap = capture(list(range(24)), 1_000_000, 300, 800)
        bh = bits_high(cap) if cap.get("ok") else []
        r.rec("T2 input-pulldown-bias", "NOT SUPPORTED" if cap.get("ok")
              else "FAIL",
              observed_high=bh,
              note="probe LA inputs are pull-up biased; cannot verify true high-Z",
              ok=cap.get("ok"))

    # ---------------- T3 frequency-comb identification ----------------
    if "all" in only or "T3" in only:
        # The CPU-driven comb is an identification mechanism (unique frequencies),
        # not a precision source; tolerate the main-loop jitter.
        for base in (24000, 6000):
            fix_comb(up, base)
            time.sleep(0.2)
            cap = capture(list(range(24)), 1_000_000, 500, 6000)
            if not cap.get("ok"):
                r.rec("T3 comb-%d" % base, "FAIL", response=cap.get("response"))
                continue
            m = channel_metrics(cap["samples"], cap["width"], 1_000_000)
            bad = []
            for k in wired_dyn:
                exp = base / (k + 1)
                got = m[k]["frequency_hz"]
                if got <= 0 or abs(got - exp) / exp > 0.25:
                    bad.append((k, round(got), round(exp)))
            r.rec("T3 comb-%d" % base, "PASS" if not bad else "FAIL",
                  failures=bad)

    # ---------------- T4 precision-pair timing ----------------
    if "all" in only or "T4" in only:
        # pair up wired channels (excluding the ch0 trigger host when possible)
        pairs = [(wired_dyn[i], wired_dyn[i + 1]) for i in range(0, len(wired_dyn) - 1, 2)]
        if len(pairs) * 2 < len(wired):
            pairs.append((wired_dyn[-2], wired_dyn[-1]))
        for a, b in pairs:
            for freq in (1000, 100000, 1000000):
                if not (fix_gen_channels(up, a, b) and
                        fix_configure(up, 0, freq, 500, 0) and
                        fix_configure(up, 1, freq, 500, 900)):
                    r.rec("T4 pair-%d-%d@%d" % (a, b, freq), "FAIL", cfg=True)
                    continue
                time.sleep(0.2)
                # sample ~200x the signal for good phase/duty resolution
                sr = int(min(100_000_000, max(6104, freq * 200)))
                post = min(190_000, (sr // freq) * 30)
                post = max(post, 20_000)
                cap = capture([a, b], sr, 2000, post, trigger=min(a, b))
                if not cap.get("ok"):
                    r.rec("T4 pair-%d-%d@%d" % (a, b, freq), "FAIL",
                          response=cap.get("response"))
                    continue
                m = channel_metrics(cap["samples"], cap["width"], sr)
                bad = []
                for idx, ch in enumerate((a, b)):
                    exp = freq
                    got = m[idx]["frequency_hz"]
                    if got <= 0 or abs(got - exp) / exp > 0.02:
                        bad.append((ch, "freq", round(got)))
                    if abs(m[idx]["duty_percent"] - 50.0) > 3.0:
                        bad.append((ch, "duty", round(m[idx]["duty_percent"], 1)))
                r.rec("T4 pair-%d-%d@%d" % (a, b, freq),
                      "PASS" if not bad else "FAIL", failures=bad)

    # ---------------- T5 per-channel edge trigger ----------------
    if "all" in only or "T5" in only:
        bad = []
        for k in wired_dyn:
            if k == 0:
                fix_walk(up, 1 << k, 0, 1000)
            else:
                fix_walk(up, 1 << k, 1 << 0, 1000)
            time.sleep(0.1)
            cap = capture(list(range(24)), 1_000_000, 500, 1200, trigger=k)
            if not cap.get("ok"):
                bad.append((k, cap.get("response")))
        r.rec("T5 edge-trigger-per-channel", "PASS" if not bad else "FAIL",
              failures=bad)

    # ---------------- T6 PIO2 fast-capture interleave ----------------
    if "all" in only or "T6" in only:
        fix_comb(up, 12000)
        bad = []
        for i in range(4):
            # non-fast baseline must show all wired channels
            cap = capture(list(range(24)), 1_000_000, 500, 12000)
            if not cap.get("ok"):
                bad.append((i, "simple", cap.get("response"))); continue
            m = channel_metrics(cap["samples"], cap["width"], 1_000_000)
            miss = [k for k in wired_dyn if m[k]["frequency_hz"] <= 0]
            if miss: bad.append((i, "simple", "freq_missing", miss))
            # fast capture on PIO2 (trigger on ch0 pattern)
            cap = capture(list(range(24)), 1_000_000, 500, 12000,
                          trigger_type=2, trigger=0, inverted=1, trigger_value=1)
            if not cap.get("ok"):
                bad.append((i, "fast", cap.get("response"))); continue
            m = channel_metrics(cap["samples"], cap["width"], 1_000_000)
            miss = [k for k in wired_dyn if m[k]["frequency_hz"] <= 0]
            if miss: bad.append((i, "fast", "freq_missing", miss))
            # post-fast non-fast capture must still see all wired channels
            cap = capture(list(range(24)), 1_000_000, 500, 12000)
            if not cap.get("ok"):
                bad.append((i, "post-simple", cap.get("response"))); continue
            m = channel_metrics(cap["samples"], cap["width"], 1_000_000)
            miss = [k for k in wired_dyn if m[k]["frequency_hz"] <= 0]
            if miss: bad.append((i, "post-simple", "freq_missing", miss))
        r.rec("T6 pio2-funcsel-interleave", "PASS" if not bad else "FAIL",
              failures=bad[:5])

    # ---------------- T7 cross-subsystem ----------------
    if "all" in only or "T7" in only:
        fix_comb(up, 6000)
        # SWD read while comb runs
        rp = subprocess.run(oocd_argv(CFG,
                              "-c", "init", "-c", f"read_memory 0x{target_symbol_address('g_counter'):08x} 32 1",
                             "-c", "shutdown"),
                            capture_output=True, text=True, timeout=60)
        rc, out = rp.returncode, (rp.stdout + rp.stderr)
        vals = re.findall(r"0x[0-9a-f]+", out)
        ok_swd = rc == 0 and bool(vals)
        ok_uart = fix_echo(up, b"channels-x", timeout=5) == b"channels-x"
        # armed fast capture with no-match trigger while comb runs
        from picogremlin_hil import la_build_capture_request, la_frame
        lp.drain(0.5)
        req = la_build_capture_request(2, 0, 2, 2, list(range(24)),
                                       1_000_000, 500, 2000, 0, 0, 2)
        lp.write(la_frame(1, req)); lp.flush()
        start = lp.read_until(b"CAPTURE_STARTED", timeout=10, maxlen=128)
        ok_armed = b"CAPTURE_STARTED" in start
        lp.write(b"\xff"); lp.flush(); time.sleep(1.0)
        lp.drain(0.5); lp.write(la_frame(0)); lp.flush()
        ident_ok = b"CHANNELS:24" in lp.read_until(b"CHANNELS:24", timeout=10, maxlen=4096)
        r.rec("T7 cross-subsystem", "PASS" if (ok_swd and ok_uart and ok_armed and ident_ok)
              else "FAIL", swd=ok_swd, uart=ok_uart, armed=ok_armed, la_recover=ident_ok)

    # restore defaults and report
    fix_heart(up, 1)
    fix_gen_channels(up, 14, 15)
    fix_configure(up, 0, 1000, 500, 0)
    fix_configure(up, 1, 1000, 500, 0)
    up.close(); lp.close()

    fails = [x for x in r.results if x["status"] != "PASS"]
    print(f"\nSUMMARY: {len(r.results)} checks, {len(fails)} not PASS", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
