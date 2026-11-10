#!/usr/bin/env python3
"""HIL: PIO change-detector edge capture (CMD=11) — correctness first.

CMD=11 now captures N raw (dt, state) events into RAM **without** live USB
streaming, then transmits them together with firmware-side diagnostics:
  header = [magic, n, dup, single, multi, maxbits, invalid, fdebug, fifo, ring, div]

We feed the fixture's exact 1 kHz square wave on GP16 and, for each PIO divider
(8, 4, 2, 1), check that:
  * consecutive states genuinely alternate on the driven bit (no duplicate records),
  * the number of events matches ~2 x freq x capture time,
  * no FIFO loss (RXSTALL/RXUNDER clear), and
  * the interval distribution is consistent with 100 000 cycles per half-period.

Only GP16/17 (the PIO generator / LED pins) are ever driven — never toggle
arbitrary pins (a piezo buzzer may be wired to one).
"""
import argparse, json, os, re, struct, statistics, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picogremlin_hil import SerialPort, find_ports, la_frame, la_build_capture_request, fix_configure, fix_identity

CLK_HZ = 200_000_000
HDR = struct.Struct("<13I")
MAGIC = 0x45444731


def _rn(lp, leftover, n, timeout=3.0):
    deadline = time.time() + timeout
    while len(leftover) < n and time.time() < deadline:
        leftover.extend(lp.read(max(n - len(leftover), 4096), timeout=0.3))
    if len(leftover) < n:
        return None
    out = bytes(leftover[:n]); del leftover[:n]; return out


def edge_capture(lp, channels, div, want, timeout=6.0):
    """Send one RAM-capture request and return (header dict, [(dt, state)])."""
    mode = 2 if max(channels) >= 16 else (1 if max(channels) >= 8 else 0)
    req = la_build_capture_request(0, 0, 0, 0, channels, div, 0, want, 0, 0, mode)
    lp.drain(0.5)
    lp.write(la_frame(0x47, req)); lp.flush()

    leftover = bytearray()
    hdr = _rn(lp, leftover, HDR.size, timeout=timeout)
    if hdr is None:
        return None, []
    f = HDR.unpack(hdr)
    if f[0] != MAGIC:
        return {"magic": f[0], "bad": True}, []
    n = f[1]
    body = _rn(lp, leftover, n * 8, timeout=timeout)
    ev = []
    if body:
        for i in range(n):
            dt, st = struct.unpack_from("<II", body, i * 8)
            ev.append((dt, st))
    # consume the trailing "EDGE_DONE\n"
    t0 = time.time()
    while b"EDGE_DONE" not in leftover and time.time() - t0 < 2:
        leftover.extend(lp.read(256, timeout=0.3))
    d = {"n": n, "dup": f[2], "single": f[3], "multi": f[4], "maxbits": f[5],
         "invalid": f[6], "fdebug": f[7], "fifo": f[8], "ring": f[9], "div": f[10],
         "lo": f[11], "span": f[12]}
    return d, ev


def edge_live(lp, channels, div, secs=2.0):
    """CMD=12: live framed streaming. Stop first, then drain all frames so the PC
    count is directly comparable to the firmware's own event total."""
    mode = 2 if max(channels) >= 16 else (1 if max(channels) >= 8 else 0)
    req = la_build_capture_request(0, 0, 0, 0, channels, div, 0, 0, 0, 0, mode)
    lp.drain(0.5)
    lp.write(la_frame(0x48, req)); lp.flush()

    leftover = bytearray()
    t0 = time.time()
    while b"STREAM_EDGE_STARTED" not in leftover and time.time() - t0 < 5:
        leftover.extend(lp.read(256, timeout=1))
    started = b"STREAM_EDGE_STARTED" in leftover
    idx = leftover.find(b"STREAM_EDGE_STARTED\n")
    if idx >= 0:
        del leftover[:idx + len(b"STREAM_EDGE_STARTED\n")]

    ev = []
    stop_sent = False
    t0 = time.time()
    deadline = t0 + secs + 10
    while time.time() < deadline:
        if b"EDGE_SUMMARY" in leftover or b"STREAM_STOPPED" in leftover:
            break
        if not stop_sent and (time.time() - t0) > secs:
            lp.write(la_frame(0x42)); lp.flush()   # stop, then keep draining everything
            stop_sent = True
        hdr = _rn(lp, leftover, 8, timeout=1.0)
        if hdr is None:
            continue
        _seq, n = struct.unpack("<II", hdr)
        if n > 4096:   # garbage -> framing lost
            break
        body = _rn(lp, leftover, n * 8, timeout=1.0)
        if body is None:
            break
        for i in range(n):
            dt, st = struct.unpack_from("<II", body, i * 8)
            ev.append((dt, st))

    if not stop_sent:
        lp.write(la_frame(0x42)); lp.flush()
    t0 = time.time()
    while b"STREAM_STOPPED" not in leftover and time.time() - t0 < 8:
        leftover.extend(lp.read(8192, timeout=1))

    summary = {}
    m = re.search(rb"EDGE_SUMMARY ([^\n]+)", bytes(leftover))
    if m:
        for kv in m.group(1).split():
            k, _, v = kv.partition(b"=")
            k = k.decode(); vs = v.decode()
            summary[k] = int(vs, 16) if k == "fdebug" else int(vs, 10)
    summary["started"] = started
    summary["pc_events"] = len(ev)
    return ev, summary


def analyze(ev, freq):
    """Return (bit, alternating, stats) for the driven bit."""
    if len(ev) < 4:
        return None, False, {}
    states = [s for _, s in ev]
    dts = [d for d, _ in ev]

    trans = [0] * 32
    prev = None
    for st in states:
        if prev is not None:
            x = st ^ prev
            for b in range(32):
                if (x >> b) & 1:
                    trans[b] += 1
        prev = st
    bit = max(range(32), key=lambda b: trans[b])

    # Alternation: after pull-ups steady the unselected lines, every event should be a
    # single-bit flip of a driven line, and that line must alternate on every event.
    alt = (len(states) >= 2 and all(states[i] != states[i - 1] for i in range(1, len(states)))
           and all(bin(states[i] ^ states[i - 1]).count("1") == 1 for i in range(1, len(states))))

    # Intervals between changes of `bit` (accumulate dt across non-bit events).
    iv = []; acc = 0; prev = None
    for dt, st in zip(dts, states):
        acc += dt
        if prev is not None and (((st ^ prev) >> bit) & 1):
            iv.append(acc); acc = 0
        prev = st
    exp = CLK_HZ / (2 * freq)
    good = [d for d in iv if d < exp * 1.5]
    st_stats = {}
    if good:
        good_sorted = sorted(good)
        def pct(p):
            return good_sorted[min(len(good_sorted) - 1, int(len(good_sorted) * p))]
        st_stats = {"min": min(good), "median": round(statistics.median(good)),
                    "p99": pct(0.99), "max": max(good),
                    "mad": round(statistics.median([abs(d - statistics.median(good)) for d in good]))}
    return bit, alt, {"exp": round(exp), "n_iv": len(iv), "n_good": len(good), **st_stats}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sig", type=int, default=1000)
    ap.add_argument("--count", type=int, default=256)
    ap.add_argument("--secs", type=float, default=1.5)
    ap.add_argument("--divs", default="8,4,2,1")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--live", action="store_true", help="use CMD=12 live streaming instead of RAM capture")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts", "edge"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    divs = [int(x) for x in args.divs.split(",")]
    results = []
    def rec(name, status, **d):
        results.append({"name": name, "status": status, **d})
        print(f"[{status}] {name} {d}", flush=True)
        with open(os.path.join(args.out, "edge_test_results.json"), "w") as f:
            json.dump({"results": results}, f, indent=2)

    uart, la = find_ports()
    up = SerialPort(uart, 115200); lp = SerialPort(la, 115200)
    rec("fixture-ready", "PASS" if b"FIXTURE" in fix_identity(up) else "FAIL")
    fix_configure(up, 0, args.sig, 500, 0)   # GP16 driven
    fix_configure(up, 1, args.sig, 500, 0)   # GP17 ALSO driven -> must be ignored by the span
    time.sleep(0.3)

    SEL = [14]          # read ONLY GP16; GP17 toggles but must not produce events
    DRIVEN_CH = 14      # -> state bit = (32 - span) + (ch - lo)

    for div in divs:
        if args.live:
            ev, s = edge_live(lp, SEL, div, secs=args.secs)
            if not s.get("started"):
                rec(f"live div{div}", "FAIL", note="not started")
                continue
            bit, alt, stats = analyze(ev, args.sig)
            fdbg = s.get("fdebug", 0)
            exp_bit = (32 - s.get("span", 0)) + (DRIVEN_CH - s.get("lo", 0))
            match = (s.get("total") == s.get("pc_events"))
            ok = (s.get("dup") == 0 and s.get("invalid") == 0
                  and not (fdbg & 0x0F) and not (fdbg & 0x0F00)
                  and alt and match and bit == exp_bit
                  and stats.get("mad", 10**9) < stats.get("exp", 1) * 0.02)
            rec(f"live div{div}", "PASS" if ok else "FAIL",
                fw_total=s.get("total"), pc_events=s.get("pc_events"), counts_match=match,
                dup=s.get("dup"), multi=s.get("multi"), invalid=s.get("invalid"),
                fdebug=hex(fdbg), fifo=s.get("fifo"), lo=s.get("lo"), span=s.get("span"),
                bit=bit, exp_bit=exp_bit, alternating=alt, stats=stats)
            continue

        d, ev = edge_capture(lp, SEL, div, args.count)
        if not d or d.get("bad"):
            rec(f"div{div}", "FAIL", note="no/short header", got=d)
            continue
        rxstall = bool(d["fdebug"] & 0x0F)
        rxunder = bool(d["fdebug"] & 0x0F00)
        bit, alt, stats = analyze(ev, args.sig)
        exp_bit = (32 - d["span"]) + (DRIVEN_CH - d["lo"])
        if args.dump and not alt:
            for i, (dt, st) in enumerate(ev[:12]):
                prev = ev[i - 1][1] if i else 0
                ch = st ^ prev
                print(f"    ev{i:3d} dt={dt:8d} state=0x{st:08X} chg=0x{ch:08X}", flush=True)
        ok = (d["dup"] == 0 and d["invalid"] == 0 and not rxstall and not rxunder
              and alt and d["single"] == d["n"] - 1 and bit == exp_bit
              and stats.get("n_good", 0) >= 4 and stats.get("mad", 10**9) < stats.get("exp", 1) * 0.02)
        rec(f"div{div}", "PASS" if ok else "FAIL",
            n=d["n"], dup=d["dup"], single=d["single"], multi=d["multi"],
            maxbits=d["maxbits"], invalid=d["invalid"],
            rxstall=rxstall, rxunder=rxunder, fdebug=hex(d["fdebug"]), fifo=d["fifo"],
            lo=d["lo"], span=d["span"], bit=bit, exp_bit=exp_bit, alternating=alt, stats=stats)

    up.close(); lp.close()
    bad = [r for r in results if r["status"] != "PASS"]
    print(f"\nSUMMARY: {len(results)} checks, {len(bad)} not PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
