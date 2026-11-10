#!/usr/bin/env python3
"""
PicoGremlin HIL: ADC analog capture (CMD 0x49 one-shot, 0x4A stream).

The three external analog inputs share GP26..GP28 (LA digital channels 21..23)
and the on-chip temperature sensor is input 3. The temperature channel gives a
deterministic, stable reference without any external wiring, so the suite runs
without the target fixture; GP26/27 are measured and only checked for sane
ranges (whatever the bench happens to drive).

  A1  capability      PG_INFO says ANALOG:3,<rate>,1
  A2  temp 12-bit     plausible, stable degrees Celsius
  A3  temp 8-bit      consistent with the 12-bit result
  A4  interleaving    one channel alone == the same channel in a 2-ch capture
  A5  reported rate   the firmware echoes the achieved per-channel rate
  A6  duration        capture-now takes about samples/rate
  A7  trigger         level trigger fires on channel 0 and windows around it
  A8  cancel          an impossible level times out, cancels and recovers
  A9  stream rate     gapless stream at ~the requested per-channel rate
  A10 stream 8-bit    frames carry one byte per sample
  A11 digital after   a digital capture still works (pins returned to PIO)
"""
import argparse
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import (  # noqa: E402
    SerialPort, find_ports, la_frame, la_build_capture_request, la_capture,
    fix_identity, fix_configure,
)

# Bench wiring for the waveform checks: the target fixture's generator outputs
# (GP16/GP17) must reach the probe's analog inputs A0/A1 (GP26/GP27). Any RC
# filter in between is fine — the checks use the mean, frequency and duty.
FIXTURE_WIRING = "wire fixture GP16 -> probe GP26 (A0) and GP17 -> probe GP27 (A1)"


VREF = 3.3


def volts(raw):
    return raw * VREF / 4096.0


def temp_c(raw):
    return 27.0 - (volts(raw) - 0.706) / 0.001721


def measure_wave(vals, rate, level):
    """Frequency/duty from threshold crossings (hysteresis = 10% of the swing)."""
    lo, hi = min(vals), max(vals)
    hyst = max(0.02, (hi - lo) * 0.1)
    state = 1 if vals[0] > level else 0
    rises, falls = [], []
    for i, v in enumerate(vals[1:], 1):
        if state == 0 and v > level + hyst:
            state, _ = 1, rises.append(i)
        elif state == 1 and v < level - hyst:
            state, _ = 0, falls.append(i)
    periods = [rises[k + 1] - rises[k] for k in range(len(rises) - 1)]
    freq = rate / (sorted(periods)[len(periods) // 2]) if periods else 0.0
    highs = []
    for r in rises:
        f = next((x for x in falls if x > r), None)
        if f is not None:
            highs.append(f - r)
    period_samples = sum(periods) / len(periods) if periods else 0
    duty = (sum(highs) / len(highs) / period_samples * 100.0) if period_samples else 0.0
    return {"freq": freq, "duty": duty}


def pg_caps(port, timeout=2.0):
    port.drain(0.3)
    port.write(la_frame(0x40))
    port.flush()
    buf = b""
    t0 = time.time()
    while time.time() - t0 < timeout:
        chunk = port.read(256, timeout=0.3)
        buf += chunk
        if b"ANALOG:" in buf or b"BRIDGE_TX:" in buf:
            break
    caps = {}
    for line in buf.decode(errors="replace").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            caps[k.strip()] = v.strip()
    return caps


def acap(port, chans, rate, pre, post, eight=False, trig=None, level=0,
         inverted=False, head_timeout=5.0, drain=0.3):
    """CMD 0x49 — returns {"ok", "rate", "values"} or {"ok": False, "why"}."""
    req = la_build_capture_request(3 if trig is None else 0, trig or 0,
                                   1 if inverted else 0, level, chans, rate,
                                   pre, post, 0, 0, 1 if eight else 2)
    port.drain(drain)
    port.write(la_frame(0x49, req))
    port.flush()
    head = port.read_until(b"\n", timeout=head_timeout, maxlen=4096)
    if b"ANALOG_STARTED" not in head:
        if b"ANALOG_ERROR" in head:
            return {"ok": False, "why": "ANALOG_ERROR"}
        return {"ok": False, "why": "no response"}
    nl = head.index(b"\n") + 1
    line = head[:nl].decode(errors="replace").split()
    rate_rep = int(line[1]) if len(line) > 1 else 0
    rest = bytearray(head[nl:])
    while len(rest) < 4:
        rest += port.read(4 - len(rest), timeout=5)
    n = struct.unpack("<I", rest[:4])[0]
    width = 1 if eight else 2
    want = 4 + n * width + 1
    while len(rest) < want:
        chunk = port.read(want - len(rest), timeout=20)
        if not chunk:
            return {"ok": False, "why": "payload timeout"}
        rest += chunk
    fmt = "<%d%s" % (n, "B" if eight else "H")
    values = list(struct.unpack(fmt, rest[4:4 + n * width]))
    return {"ok": True, "rate": rate_rep, "values": values}


def astream(port, chans, rate, half, seconds, eight=False):
    """CMD 0x4A — returns (rate_rep, per_channel_lists, frames) or (None, None, 0)."""
    req = la_build_capture_request(3, 0, 0, 0, chans, rate, 0, half, 0, 0,
                                   1 if eight else 2)
    port.write(la_frame(0x42))
    port.flush()
    port.drain(0.5)
    port.write(la_frame(0x4A, req))
    port.flush()
    buf = b""
    t0 = time.time()
    while b"ANALOG_STREAM_STARTED" not in buf and time.time() - t0 < 5:
        chunk = port.read(256, timeout=0.4)
        buf += chunk
        if b"ANALOG_ERROR" in buf:
            return None, None, 0
    if b"ANALOG_STREAM_STARTED" not in buf:
        return None, None, 0
    nl = buf.index(b"\n") + 1
    rate_rep = int(buf[:nl].split()[1])
    leftover = bytearray(buf[nl:])
    chans_out = [[] for _ in chans]
    frames = 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        while len(leftover) < 4:
            chunk = port.read(4 - len(leftover), timeout=0.5)
            if not chunk:
                break
            leftover += chunk
        if len(leftover) < 4:
            break
        n = struct.unpack("<I", bytes(leftover[:4]))[0]
        del leftover[:4]
        while len(leftover) < n:
            chunk = port.read(n - len(leftover), timeout=0.5)
            if not chunk:
                break
            leftover += chunk
        if len(leftover) < n:
            break
        data = bytes(leftover[:n])
        del leftover[:n]
        frames += 1
        if eight:
            vals = list(data)
            for i, v in enumerate(vals):
                chans_out[i % len(chans)].append(v)
        else:
            vals = struct.unpack("<%dH" % (n // 2), data)
            for i, v in enumerate(vals):
                chans_out[i % len(chans)].append(v)
    port.write(la_frame(0x42))
    port.flush()
    stop = b""
    t0 = time.time()
    while b"STREAM_STOPPED" not in stop and time.time() - t0 < 5:
        stop += port.read(4096, timeout=0.4)
    return rate_rep, chans_out, frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "artifacts", "analog"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    results = []

    def rec(name, status, **d):
        results.append({"name": name, "status": status, **d})
        print(f"[{status}] {name} {d}", flush=True)
        with open(os.path.join(args.out, "analog_test_results.json"), "w") as f:
            json.dump({"results": results}, f, indent=2)

    uart, la = find_ports()
    lp = SerialPort(la, 115200)

    # ---- A1 capability -----------------------------------------------------
    caps = pg_caps(lp)
    analog = caps.get("ANALOG")
    ok = False
    info = {}
    if analog:
        parts = [p.strip() for p in analog.split(",")]
        ok = len(parts) >= 2 and int(parts[0]) == 3 and int(parts[1]) >= 100000
        info = {"channels": parts[0], "max_rate": parts[1],
                "temp": parts[2] if len(parts) > 2 else "?"}
    rec("A1-capability", "PASS" if ok else "FAIL", analog=analog)

    # ---- A2 temperature, 12-bit -------------------------------------------
    r = acap(lp, [3], 5000, 0, 500)
    if r["ok"]:
        t = [temp_c(v) for v in r["values"]]
        tmin, tmax, tavg = min(t), max(t), sum(t) / len(t)
        ok = 10.0 <= tavg <= 75.0 and (tmax - tmin) <= 5.0
        rec("A2-temp-12bit", "PASS" if ok else "FAIL",
            avg_c=round(tavg, 1), min_c=round(tmin, 1), max_c=round(tmax, 1),
            raw=[min(r["values"]), max(r["values"])])
        temp12 = tavg
    else:
        rec("A2-temp-12bit", "FAIL", why=r["why"])
        temp12 = None

    # ---- A3 8-bit mode -----------------------------------------------------
    # 8-bit keeps the top 8 bits (one step = 16 counts = 13 mV), which is fine
    # for a voltage channel and coarse for the ~1.7 mV/°C temperature slope.
    r12v = acap(lp, [0], 20000, 0, 1000)
    r8 = acap(lp, [0], 20000, 0, 1000, eight=True)
    r8t = acap(lp, [3], 5000, 0, 500, eight=True)
    if r12v["ok"] and r8["ok"] and r8t["ok"] and temp12 is not None:
        m12 = sum(r12v["values"]) / len(r12v["values"])
        m8 = sum(v << 4 for v in r8["values"]) / len(r8["values"])
        t8 = sum(temp_c(v << 4) for v in r8t["values"]) / len(r8t["values"])
        ok = abs(m8 - m12) <= 32 and abs(t8 - temp12) <= 10.0
        rec("A3-8bit-mode", "PASS" if ok else "FAIL",
            a0_12bit=round(m12), a0_8bit=round(m8),
            temp_8bit_c=round(t8, 1), temp_12bit_c=round(temp12, 1))
    else:
        rec("A3-8bit-mode", "FAIL",
            why=r12v.get("why") or r8.get("why") or r8t.get("why") or "no reference")

    # ---- A4 interleaving ---------------------------------------------------
    one = acap(lp, [0], 20000, 0, 1000)
    two = acap(lp, [0, 1], 20000, 0, 1000)
    if one["ok"] and two["ok"]:
        a0_one = sum(one["values"]) / len(one["values"])
        a0_two = sum(two["values"][0::2]) / len(two["values"][0::2])
        a1_two = sum(two["values"][1::2]) / len(two["values"][1::2])
        ok = (len(two["values"]) == 2000 and len(one["values"]) == 1000
              and abs(a0_one - a0_two) <= 60)
        rec("A4-interleaving", "PASS" if ok else "FAIL",
            a0_single=round(a0_one, 0), a0_interleaved=round(a0_two, 0),
            a1_interleaved=round(a1_two, 0), values=len(two["values"]))
    else:
        rec("A4-interleaving", "FAIL", why=one.get("why") or two.get("why"))

    # ---- A5 reported rate --------------------------------------------------
    r10 = acap(lp, [0], 10000, 0, 200)
    r200 = acap(lp, [0], 200000, 0, 2000)
    if r10["ok"] and r200["ok"]:
        ok = (abs(r10["rate"] - 10000) <= 100 and
              abs(r200["rate"] - 200000) <= 2000)
        rec("A5-reported-rate", "PASS" if ok else "FAIL",
            at_10k=r10["rate"], at_200k=r200["rate"])
    else:
        rec("A5-reported-rate", "FAIL", why=r10.get("why") or r200.get("why"))

    # ---- A6 capture duration ----------------------------------------------
    t0 = time.monotonic()
    r500 = acap(lp, [0], 100000, 0, 5000, drain=0.02)
    dt = time.monotonic() - t0
    expect = 5000 / 100000.0
    rec("A6-duration", "PASS" if (r500["ok"] and expect * 0.5 <= dt <= expect * 3.0 + 0.15)
        else "FAIL", elapsed_s=round(dt, 3), expected_s=round(expect, 3))

    # ---- A7 level trigger --------------------------------------------------
    base = acap(lp, [0], 20000, 0, 2000)
    if base["ok"]:
        vals = base["values"]
        level = int(sum(vals) / len(vals))
        trig = acap(lp, [0], 20000, 200, 400, trig=0, level=level)
        if trig["ok"] and len(trig["values"]) == 600:
            prev, at = trig["values"][199], trig["values"][200]
            # the firmware triggers on the first sample that crosses the level
            # upwards, so the sample before the window boundary is below it
            ok = prev < level <= at
            rec("A7-trigger", "PASS" if ok else "FAIL",
                level=level, before=prev, at_trigger=at,
                pp=max(vals) - min(vals))
        else:
            rec("A7-trigger", "FAIL", why=trig.get("why", "short window"))
    else:
        rec("A7-trigger", "FAIL", why=base.get("why"))

    # ---- A8 impossible level -> timeout, cancel, recover -------------------
    r = acap(lp, [0], 20000, 0, 100, trig=0, level=4095, head_timeout=1.0)
    lp.write(b"\xff")                      # cancel the waiting capture
    lp.flush()
    lp.drain(0.5)
    rec_ok = (not r["ok"]) and r["why"] in ("no response", "ANALOG_ERROR")
    after = acap(lp, [3], 5000, 0, 100)
    rec("A8-cancel", "PASS" if rec_ok and after["ok"] else "FAIL",
        first=r.get("why"), recovered=after["ok"])

    # ---- A9 gapless stream -------------------------------------------------
    rate_rep, chans, frames = astream(lp, [0, 1], 20000, 500, 0.4)
    if rate_rep and chans and chans[0]:
        per_ch = len(chans[0])
        measured = per_ch / 0.4
        ok = (abs(measured - 20000) / 20000.0 <= 0.25 and rate_rep == 20000)
        rec("A9-stream-rate", "PASS" if ok else "FAIL",
            measured=round(measured), requested=rate_rep, frames=frames,
            per_channel=per_ch)
    else:
        rec("A9-stream-rate", "FAIL", why="stream did not start")

    # ---- A10 8-bit stream --------------------------------------------------
    rate_rep8, chans8, frames8 = astream(lp, [0], 50000, 1000, 0.2, eight=True)
    if rate_rep8 and chans8 and chans8[0]:
        vals = chans8[0]
        ok = (len(vals) > 100 and max(vals) <= 255 and min(vals) < 250)
        rec("A10-stream-8bit", "PASS" if ok else "FAIL",
            values=len(vals), raw_min=min(vals), raw_max=max(vals),
            frames=frames8)
    else:
        rec("A10-stream-8bit", "FAIL", why="8-bit stream did not start")

    # ---- A11 digital capture after analog ---------------------------------
    cap = la_capture(lp, [0, 1, 2, 3, 4, 5, 6, 7], 1_000_000, 0, 2000,
                     trigger_type=3, head_timeout=5)
    rec("A11-digital-after-analog",
        "PASS" if cap.get("ok") and cap.get("count") == 2000 else "FAIL",
        count=cap.get("count"), response=str(cap.get("response"))[:40])

    # ---- A12/A13 waveform references from the target fixture ---------------
    # Needs a wire from the probe's GP16 (fixture ch14) to GP26 (A0); without it
    # the inputs are undriven and the checks are reported as NOT TESTED.
    up = SerialPort(uart, 115200)
    fixture = fix_identity(up)
    if not fixture:
        rec("A12-fixture-wave", "NOT TESTED",
            why="target fixture not responding — program it with ./pg fixture-flash")
    else:
        ok_cfg = (fix_configure(up, 0, 1000, 500, 0) and
                  fix_configure(up, 1, 1000, 250, 0))
        time.sleep(0.5)
        if not ok_cfg:
            rec("A12-fixture-wave", "NOT TESTED", why="fixture did not accept the configuration")
        else:
            r = acap(lp, [0, 1], 100000, 0, 4000)
            if not r["ok"]:
                rec("A12-fixture-wave", "FAIL", why=r["why"])
            else:
                a0 = [volts(v) for v in r["values"][0::2]]
                a1 = [volts(v) for v in r["values"][1::2]]
                m0, m1 = sum(a0) / len(a0), sum(a1) / len(a1)
                pp0 = max(a0) - min(a0)
                mm = measure_wave(a0, r["rate"], (max(a0) + min(a0)) / 2.0)
                wired = pp0 > 2.0 and abs(mm["freq"] - 1000) <= 100
                if not wired:
                    rec("A12-fixture-wave", "NOT TESTED", a0=round(m0, 3),
                        a0_pp=round(pp0, 3), note=FIXTURE_WIRING)
                else:
                    ok = (abs(m0 - 1.65) <= 0.17 and abs(m1 - 0.825) <= 0.17
                          and abs(mm["freq"] - 1000) <= 20
                          and abs(mm["duty"] - 50) <= 5)
                    rec("A12-fixture-wave", "PASS" if ok else "FAIL",
                        a0_50pct=round(m0, 3), a1_25pct=round(m1, 3),
                        freq_hz=round(mm["freq"], 1), duty_pct=round(mm["duty"], 1),
                        note=None if ok else FIXTURE_WIRING)
        fix_configure(up, 0, 1000, 500, 0)
        fix_configure(up, 1, 1000, 500, 0)
    up.close()

    lp.close()
    bad = [x for x in results if x["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(bad)} not PASS")
    print(f"results written to {os.path.join(args.out, 'analog_test_results.json')}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
