#!/usr/bin/env python3
"""
PicoGremlin HIL test: continuous streaming capture (LA command 2/3).

Validates the STREAM_START / STREAM_STOP streaming mode added to the LA:
  T1 start        — command 0x41 starts a stream (STREAM_STARTED)
  T2 batches      — batches are correctly framed: 4-byte little-endian sample
                    count, then count*bps raw sample bytes (bps = 1/2/4 for
                    8/16/24-channel mode)
  T3 timing       — intra-batch sample timing matches a known driven frequency
                    (sample period == sample_rate / signal_freq, within 5%)
  T4 stop         — command 0x42 stops cleanly (STREAM_STOPPED)
  T5 no-wedge     — the LA command parser still responds after a stream (guards
                    the stop-frame parser-desync bug: a framed stop must not
                    leave trailing bytes that wedge later commands)

Note: command 0x41 (batch mode); each batch is an independent immediate capture, so batches are NOT
temporally contiguous — there is a gap between them. Timing is exact WITHIN a
batch; the stream is a sequence of correctly-timed windows, not gapless capture.

Usage: python3 tests_la_stream.py [--out DIR]
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
        leftover.extend(lp.read(max(n - len(leftover), 1024), timeout=0.3))
    if len(leftover) < n:
        return None
    out = bytes(leftover[:n])
    del leftover[:n]
    return out


def stream_start(lp, channels, sample_rate, batch):
    mode = 2 if max(channels) >= 16 else (1 if max(channels) >= 8 else 0)
    bps = {0: 1, 1: 2, 2: 4}[mode]
    req = la_build_capture_request(0, channels[0], 0, 0, channels, sample_rate,
                                   0, batch, 0, 0, mode)
    lp.drain(0.5)
    lp.write(la_frame(0x41, req))
    lp.flush()
    buf = b""
    t0 = time.time()
    while b"STREAM_STARTED" not in buf and time.time() - t0 < 5:
        buf += lp.read(256, timeout=1)
    started = b"STREAM_STARTED" in buf
    tail = buf[buf.find(b"STREAM_STARTED\n") + 15:] if started else b""
    return started, bytearray(tail), bps


def stream_stop(lp, leftover):
    lp.write(la_frame(0x42))      # framed stop (the case that used to wedge the parser)
    lp.flush()
    sb = bytes(leftover)
    t0 = time.time()
    while b"STREAM_STOPPED" not in sb and time.time() - t0 < 5:
        sb += lp.read(8192, timeout=1)
    return b"STREAM_STOPPED" in sb


def la_alive(lp):
    lp.drain(0.5)
    lp.write(la_frame(0))        # command 0 = identity
    lp.flush()
    return lp.read(80, timeout=3).startswith(b"LOGIC_ANALYZER")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "artifacts", "stream"))
    ap.add_argument("--rate", type=int, default=1_000_000)
    ap.add_argument("--sig", type=int, default=50_000)
    ap.add_argument("--batch", type=int, default=512)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    results = []

    def rec(name, status, **d):
        results.append({"name": name, "status": status, **d})
        print(f"[{status}] {name} {d}", flush=True)
        with open(os.path.join(args.out, "stream_test_results.json"), "w") as f:
            json.dump({"results": results}, f, indent=2)

    uart, la = find_ports()
    up = SerialPort(uart, 115200)
    lp = SerialPort(la, 115200)
    rec("fixture-ready", "PASS" if b"FIXTURE" in fix_identity(up) else "FAIL")

    channels = [14, 15]
    # drive a known frequency so T3 can check timing
    fix_configure(up, 14, args.sig, 500, 0)
    fix_configure(up, 15, args.sig, 500, 0)
    time.sleep(0.3)

    started, leftover, bps = stream_start(lp, channels, args.rate, args.batch)
    rec("T1 start", "PASS" if started else "FAIL")

    # T2: read a run of batches, check framing
    counts = []
    sizes_ok = True
    for _ in range(20):
        cnt = _rn(lp, leftover, 4)
        if cnt is None:
            sizes_ok = False
            break
        n = struct.unpack("<I", cnt)[0]
        data = _rn(lp, leftover, n * bps)
        if data is None or len(data) != n * bps:
            sizes_ok = False
            break
        counts.append(n)
    rec("T2 batches", "PASS" if (len(counts) >= 10 and sizes_ok and
        all(c == args.batch for c in counts)) else "FAIL",
        received=len(counts), expect=args.batch, got=counts[:3])

    # T3: timing — measure the most-active bit's period in the latest batch
    cnt = _rn(lp, leftover, 4)
    data = _rn(lp, leftover, struct.unpack("<I", cnt)[0] * bps) if cnt else None
    period = freq = duty = 0
    if data:
        smp = [int.from_bytes(data[i*bps:(i+1)*bps], "little")
               for i in range(len(data)//bps)]
        bit = max(range(bps*8), key=lambda b: sum(
            1 for i in range(1, len(smp)) if ((smp[i] >> b) & 1) != ((smp[i-1] >> b) & 1)))
        seq = [(s >> bit) & 1 for s in smp]
        edges = [i for i in range(1, len(seq)) if seq[i] == 1 and seq[i-1] == 0]
        if len(edges) > 1:
            period = (edges[-1] - edges[0]) / (len(edges) - 1)
            freq = args.rate / period
            duty = sum(seq) / len(seq)
    exp = args.rate / args.sig
    rec("T3 timing", "PASS" if (period and abs(period - exp) / exp < 0.05 and
        0.4 < duty < 0.6) else "FAIL",
        sample_rate=args.rate, signal=args.sig, period=round(period, 1),
        measured_hz=round(freq), duty_pct=round(duty*100))

    rec("T4 stop", "PASS" if stream_stop(lp, leftover) else "FAIL")

    time.sleep(0.2)
    rec("T5 no-wedge (LA responds after stream)", "PASS" if la_alive(lp) else "FAIL")

    up.close()
    lp.close()
    bad = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(bad)} not PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
