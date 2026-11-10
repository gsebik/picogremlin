#!/usr/bin/env python3
"""
PicoGremlin HIL test: CONTINUOUS (gapless) streaming capture — LA command 0x43.

Unlike command 2 (capture a block, stop the PIO, send, repeat — so there is a gap
per batch), command 7 keeps PIO + DMA free-running into two chained half-buffers
and only sends the half that just completed. Sampling never stops.

  T1 start     — command 0x43 starts (STREAM_CONT_STARTED)
  T2 framing   — halves are `[u32 byte_count][bytes]`, byte_count = half*bps
  T3 gapless   — the delivered sample rate equals the requested rate (within 25%),
                 i.e. almost no samples are lost between halves (a stop/send loop
                 would show ~50% because the link is idle during each transfer)
  T4 timing    — the captured signal frequency matches the driven frequency
  T5 stop      — command 0x42 stops cleanly (STREAM_STOPPED)
  T6 no-wedge  — the LA command parser still responds after a continuous stream

Usage: python3 tests_la_continuous.py [--out DIR]
"""
import argparse
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import (
    SerialPort, find_ports, la_frame, la_build_capture_request,
    fix_configure, fix_identity,
)


def _rn(lp, leftover, n, timeout=3.0):
    deadline = time.time() + timeout
    while len(leftover) < n and time.time() < deadline:
        leftover.extend(lp.read(max(n - len(leftover), 4096), timeout=0.3))
    if len(leftover) < n:
        return None
    out = bytes(leftover[:n])
    del leftover[:n]
    return out


def cont_start(lp, channels, sample_rate, half):
    mode = 2 if max(channels) >= 16 else (1 if max(channels) >= 8 else 0)
    bps = {0: 1, 1: 2, 2: 4}[mode]
    req = la_build_capture_request(0, channels[0], 0, 0, channels, sample_rate,
                                   0, half, 0, 0, mode)
    lp.drain(0.5)
    lp.write(la_frame(0x43, req))
    lp.flush()
    buf = b""
    t0 = time.time()
    while b"STREAM_CONT_STARTED" not in buf and time.time() - t0 < 5:
        buf += lp.read(256, timeout=1)
    started = b"STREAM_CONT_STARTED" in buf
    tail = buf[buf.find(b"STREAM_CONT_STARTED\n") + 20:] if started else b""
    return started, bytearray(tail), bps


def cont_stop(lp, leftover):
    lp.write(la_frame(0x42))
    lp.flush()
    sb = bytes(leftover)
    t0 = time.time()
    while b"STREAM_STOPPED" not in sb and time.time() - t0 < 5:
        sb += lp.read(8192, timeout=1)
    return b"STREAM_STOPPED" in sb


def la_alive(lp):
    lp.drain(0.5)
    lp.write(la_frame(0))
    lp.flush()
    return lp.read(80, timeout=3).startswith(b"LOGIC_ANALYZER")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "artifacts", "continuous"))
    ap.add_argument("--rate", type=int, default=200_000)
    ap.add_argument("--sig", type=int, default=1_000)
    ap.add_argument("--half", type=int, default=5_000)
    ap.add_argument("--window", type=float, default=1.2)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    results = []

    def rec(name, status, **d):
        results.append({"name": name, "status": status, **d})
        print(f"[{status}] {name} {d}", flush=True)
        with open(os.path.join(args.out, "continuous_test_results.json"), "w") as f:
            json.dump({"results": results}, f, indent=2)

    uart, la = find_ports()
    up = SerialPort(uart, 115200)
    lp = SerialPort(la, 115200)
    rec("fixture-ready", "PASS" if b"FIXTURE" in fix_identity(up) else "FAIL")

    channels = [14, 15]
    fix_configure(up, 0, args.sig, 500, 0)   # fixture channel A = GP16
    fix_configure(up, 1, args.sig, 500, 0)   # fixture channel B = GP17
    time.sleep(0.3)

    started, leftover, bps = cont_start(lp, channels, args.rate, args.half)
    rec("T1 start", "PASS" if started else "FAIL")
    if not started:
        up.close(); lp.close(); return 1

    # T2 + T3: read halves for `window` seconds; check framing and delivered rate.
    t0 = time.time()
    total = 0
    halves = 0
    framing_ok = True
    last_data = None
    while time.time() - t0 < args.window:
        cnt = _rn(lp, leftover, 4, timeout=2.0)
        if cnt is None:
            framing_ok = False
            break
        n = struct.unpack("<I", cnt)[0]
        data = _rn(lp, leftover, n, timeout=2.0)
        if data is None or len(data) != n:
            framing_ok = False
            break
        halves += 1
        total += n // bps
        last_data = data
    elapsed = time.time() - t0
    measured = total / elapsed if elapsed > 0 else 0
    rec("T2 framing", "PASS" if (halves >= 3 and framing_ok and
        total == halves * args.half) else "FAIL",
        halves=halves, half=args.half, bytes_per_sample=bps)
    ratio = measured / args.rate if args.rate else 0
    rec("T3 gapless", "PASS" if 0.75 < ratio < 1.25 else "FAIL",
        measured_hz=round(measured), requested_hz=args.rate, ratio=round(ratio, 2))

    # T4: timing. CMD=7 sends the RAW PIO word (bit k = GPIO 2+k), so the driven
    # fixture pins GP16/GP17 are bits 14/15 — not bits 0/1 (those are floating GP2/3).
    period = freq = duty = 0
    if last_data:
        smp = [int.from_bytes(last_data[i*bps:(i+1)*bps], "little")
               for i in range(len(last_data)//bps)]
        cand = [b for b in (14, 15) if b < bps * 8] or [0]
        bit = max(cand, key=lambda b: sum(
            1 for i in range(1, len(smp)) if ((smp[i] >> b) & 1) != ((smp[i-1] >> b) & 1)))
        seq = [(s >> bit) & 1 for s in smp]
        edges = [i for i in range(1, len(seq)) if seq[i] == 1 and seq[i-1] == 0]
        if len(edges) > 1:
            period = (edges[-1] - edges[0]) / (len(edges) - 1)
            freq = args.rate / period
            duty = sum(seq) / len(seq)
    exp = args.rate / args.sig
    rec("T4 timing", "PASS" if (period and abs(period - exp) / exp < 0.06 and
        0.4 < duty < 0.6) else "FAIL",
        sample_rate=args.rate, signal=args.sig, period=round(period, 1),
        measured_hz=round(freq), duty_pct=round(duty*100))

    rec("T5 stop", "PASS" if cont_stop(lp, leftover) else "FAIL")
    time.sleep(0.2)
    rec("T6 no-wedge (LA responds after continuous stream)", "PASS" if la_alive(lp) else "FAIL")

    # ── CMD=0x45 delta stream: only changes are sent over USB ──────────────────────
    # T7 start, T8 framing (seq + (index, raw) entries ending in a sentinel),
    # T9 gapless (delivered rate over a window), T10 timing from the edge list.
    def delta_start(lp, channels, rate, half, cmd=0x45, trigger=(0, 0)):
        mode = 2 if max(channels) >= 16 else (1 if max(channels) >= 8 else 0)
        bps = {0: 1, 1: 2, 2: 4}[mode]
        req = la_build_capture_request(0, trigger[0], trigger[1], 0, channels, rate, 0, half, 0, 0, mode)
        lp.drain(0.5)
        lp.write(la_frame(cmd, req))
        lp.flush()
        buf = b""
        t0 = time.time()
        while b"STREAM_CONT_STARTED" not in buf and time.time() - t0 < 5:
            buf += lp.read(256, timeout=1)
        started = b"STREAM_CONT_STARTED" in buf
        tail = buf[buf.find(b"STREAM_CONT_STARTED\n") + 20:] if started else b""
        return started, bytearray(tail), bps

    def read_delta_half(lp, leftover, timeout=3.0):
        sb = _rn(lp, leftover, 4, timeout=timeout)
        if sb is None:
            return None
        seq = struct.unpack("<I", sb)[0]
        entries = []
        while True:
            e = _rn(lp, leftover, 8, timeout=timeout)
            if e is None:
                return None
            i, w = struct.unpack("<II", e)
            if i == 0xFFFFFFFF:
                return seq, entries
            entries.append((i, w))

    dstarted, dleft, dbps = delta_start(lp, channels, args.rate, args.half)
    rec("T7 delta start", "PASS" if dstarted else "FAIL")

    if dstarted:
        ok_framing = True
        seqs = []
        total = 0
        t0 = time.time()
        edge_list = None
        while time.time() - t0 < args.window:
            r = read_delta_half(lp, dleft)
            if r is None:
                ok_framing = False
                break
            seq, entries = r
            idxs = [i for i, _ in entries]
            if not idxs or idxs != sorted(idxs) or any(i >= args.half for i in idxs):
                ok_framing = False
                break
            seqs.append(seq)
            total += args.half
            edge_list = entries
        elapsed = time.time() - t0
        seq_ok = all(seqs[i] == seqs[i-1] + 1 for i in range(1, len(seqs)))
        rec("T8 delta framing", "PASS" if (len(seqs) >= 3 and ok_framing and seq_ok) else "FAIL",
            halves=len(seqs), seq_contiguous=seq_ok)
        measured = total / elapsed if elapsed > 0 else 0
        ratio = measured / args.rate if args.rate else 0
        # The threshold is loose because this pure-Python reader parses each 8-byte
        # entry over the serial port (the Rust server path measures ~100% here).
        rec("T9 delta gapless", "PASS" if 0.65 < ratio < 1.3 else "FAIL",
            measured_hz=round(measured), requested_hz=args.rate, ratio=round(ratio, 2))

        # T10: reconstruct ch bit changes to check the signal frequency (GP16 = raw bit 14)
        period = freq = 0
        if edge_list:
            bit = 14
            last_lvl = None
            rises = []
            base = 0
            for (i, w) in edge_list:
                lvl = (w >> bit) & 1
                if last_lvl is None:
                    last_lvl = lvl
                elif lvl != last_lvl:
                    if lvl == 1:
                        rises.append(i)
                    last_lvl = lvl
            if len(rises) > 1:
                period = (rises[-1] - rises[0]) / (len(rises) - 1)
                freq = args.rate / period
        exp_period = args.rate / args.sig
        rec("T10 delta timing", "PASS" if (period and abs(period - exp_period) / exp_period < 0.1) else "FAIL",
            signal=args.sig, period=round(period, 1), measured_hz=round(freq))

        rec("T11 delta stop", "PASS" if cont_stop(lp, dleft) else "FAIL")
        time.sleep(0.2)
        rec("T12 no-wedge after delta", "PASS" if la_alive(lp) else "FAIL")

    # ── CMD=10: triggered delta (arm on GP16 low, then delta-stream) ────────────
    tstarted, tleft, tbps = delta_start(lp, channels, args.rate, args.half, cmd=0x46, trigger=(14, 1))
    rec("T13 triggered-delta start", "PASS" if tstarted else "FAIL")
    if tstarted:
        r = read_delta_half(lp, tleft)
        rec("T14 triggered-delta frame", "PASS" if (r is not None and r[1]) else "FAIL",
            entries=(len(r[1]) if r else 0))
        rec("T15 triggered-delta stop", "PASS" if cont_stop(lp, tleft) else "FAIL")

    up.close()
    lp.close()
    bad = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(bad)} not PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
