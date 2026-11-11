#!/usr/bin/env python3
"""
PicoGremlin hardware-in-the-loop (HIL) test harness.

Dependency-free (standard library only: termios, tty, fcntl, select, struct,
binascii, json, argparse). Talks to the PicoGremlin over USB CDC:

  /dev/ttyACM0  CDC0 -> UART bridge to the target (GP24 TX / GP25 RX, uart1)
  /dev/ttyACM1  CDC1 -> 24-channel logic analyzer (gusmanb V2 protocol)

All device access is explicit; the harness never guesses ports. The exact
serial of the PicoGremlin is matched against /dev/serial/by-id to find stable
port names, then the USB interface number (if01 = CDC0, if03 = CDC1) selects
the right tty.

Usage examples
--------------
  # identity / probe
  python3 picogremlin_hil.py ports
  python3 picogremlin_hil.py la-ident
  python3 picogremlin_hil.py uart-echo --len 1024 --pattern 0x00 0xff

  # UART matrix
  python3 picogremlin_hil.py uart-matrix --bauds 9600,19200,38400,57600,115200,230400,460800,921600

  # logic analyzer
  python3 picogremlin_hil.py la-capture --freq 1000000 --pre 2000 --post 8000
  python3 picogremlin_hil.py measure --freq 1000000 --pre 4000 --post 16000 --channels 14,15 --duty-a 50 --duty-b 50
  python3 picogremlin_hil.py phase --freq 1000000 --pre 4000 --post 16000

  # autobaud trigger (sets CDC0 to 9728 -> PIO/DMA autobaud)
  python3 picogremlin_hil.py autobaud --baud 115200 --pattern 0x55

Results are logged as JSON to --out (default: artifacts/).
"""
import argparse
import binascii
import fcntl
import glob
import json
import math
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import tty


def _detect_serial():
    """Resolve the PicoGremlin USB serial without hardcoding a board.

    Order: the PICOGREMLIN_SERIAL env var, else the single PicoGremlin probe
    found on /dev/serial/by-id, else None (find_ports then matches any
    connected LogicAnalyzer device by its product string).
    """
    env = os.environ.get("PICOGREMLIN_SERIAL")
    if env:
        return env.strip()
    serials = set()
    for p in glob.glob("/dev/serial/by-id/*LogicAnalyzer*-if03"):
        m = re.search(r"_([0-9A-Fa-f]{16})-if03$", p)
        if m:
            serials.add(m.group(1).upper())
    return next(iter(serials)) if len(serials) == 1 else None


PICOGREMLIN_SERIAL = _detect_serial()
# Export for OpenOCD child processes: the .cfg files select the adapter via
# $::env(PICOGREMLIN_SERIAL) when it is set (else the sole CMSIS-DAP device).
if PICOGREMLIN_SERIAL:
    os.environ.setdefault("PICOGREMLIN_SERIAL", PICOGREMLIN_SERIAL)

TARGET_FIXTURE_ELF = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "target_fixture", "build", "target_fixture.elf")


def openocd_bin():
    """OpenOCD binary: $OPENOCD, else PATH, else the bare name."""
    return os.environ.get("OPENOCD") or shutil.which("openocd") or "openocd"


def oocd_argv(cfg, *extra):
    """Build an OpenOCD argv with no hardcoded paths.

    Honors $OPENOCD (binary) and $OPENOCD_SCRIPTS (script search dir); when the
    latter is unset, OpenOCD falls back to its built-in search path.
    """
    argv = [openocd_bin()]
    scripts = os.environ.get("OPENOCD_SCRIPTS")
    if scripts:
        argv += ["-s", scripts]
    argv += ["-f", cfg]
    argv += list(extra)
    return argv


def target_symbol_address(symbol):
    """Resolve a target-fixture symbol instead of relying on a stale address."""
    result = subprocess.run(["arm-none-eabi-nm", "-C", TARGET_FIXTURE_ELF],
                            capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[-1] == symbol:
            return int(fields[0], 16)
    raise RuntimeError(f"target fixture symbol not found: {symbol}")

# ---- fixture protocol ---------------------------------------------------------
FIX_CMD_ID = b"I"
FIX_CMD_STATUS = b"R"
FIX_CMD_CFG = b"C"
FIX_CMD_ECHO = b"E"
FIX_CMD_PATTERN = b"P"
FIX_CMD_STREAM = b"S"
FIX_CMD_STOP = b"X"
FIX_CMD_BAUD = b"B"
FIX_CMD_BURST = b"T"
FIX_CMD_DEFAULT = b"Z"
FIX_CMD_MASK = b"M"
FIX_CMD_EXCLUDE = b"N"
FIX_CMD_WALK = b"K"
FIX_CMD_COMB = b"F"
FIX_CMD_GEN_CH = b"G"
FIX_CMD_INPUT = b"D"
FIX_CMD_HEART = b"W"

PATTERN_AZ = 0
PATTERN_09 = 1
PATTERN_55 = 2
PATTERN_AA = 3
PATTERN_00FF = 4
PATTERN_PRNG = 5

# ---- LA protocol -------------------------------------------------------------
LA_ID_CMD = bytes([0x55, 0xAA, 0x00, 0xAA, 0x55])
LA_FRAME_START = bytes([0x55, 0xAA])
LA_FRAME_END = bytes([0xAA, 0x55])
LA_ESC = 0xF0


class TestResult:
    def __init__(self, name):
        self.name = name
        self.status = "NOT TESTED"
        self.details = {}

    def set(self, status, **details):
        self.status = status
        self.details.update(details)
        return self

    def to_dict(self):
        return {"name": self.name, "status": self.status, **self.details}


# Linux termios2 ioctl numbers (not exposed by Python's termios module).
_TCGETS2 = 0x802C542A
_TCSETS2 = 0x402C542B
_BOTHER = 0x1000
_TERMIOS2_SIZE = 44


class SerialPort:
    """Thin wrapper over a Linux tty with baud/DTR/read-write control."""

    def __init__(self, path, baud=115200, timeout=2.0):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        self.baud = baud
        attrs = termios.tcgetattr(self.fd)
        attrs[0] &= ~(termios.IGNBRK | termios.IGNPAR | termios.PARMRK |
                      termios.ISTRIP | termios.INLCR | termios.IGNCR | termios.ICRNL)
        attrs[1] = 0  # no output processing
        attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0  # raw input
        attrs[6] = [0] * 32  # cc
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.set_baud(baud)
        self.timeout = timeout
        self.set_dtr(True)

    def set_baud(self, baud):
        """Set arbitrary baud via termios2 (BOTHER), falling back to standard."""
        self.baud = baud
        try:
            buf = bytearray(_TERMIOS2_SIZE)
            fcntl.ioctl(self.fd, _TCGETS2, buf)
            struct.pack_into("I", buf, 0, 0)   # c_iflag
            struct.pack_into("I", buf, 4, 0)   # c_oflag
            struct.pack_into("I", buf, 8, _BOTHER | termios.CS8 | termios.CREAD | termios.CLOCAL)
            struct.pack_into("I", buf, 12, 0)  # c_lflag
            buf[16] = 0                        # c_line
            buf[17:36] = b"\x00" * 19          # c_cc
            struct.pack_into("I", buf, 36, baud)  # c_ispeed
            struct.pack_into("I", buf, 40, baud)  # c_ospeed
            fcntl.ioctl(self.fd, _TCSETS2, buf)
            return
        except (OSError, ValueError):
            pass
        std = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
               57600: termios.B57600, 115200: termios.B115200, 230400: termios.B230400,
               460800: termios.B460800, 921600: termios.B921600}
        if baud in std:
            attrs = termios.tcgetattr(self.fd)
            termios.cfsetispeed(attrs, std[baud])
            termios.cfsetospeed(attrs, std[baud])
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            return
        raise ValueError(f"unsupported baud {baud}")

    def set_dtr(self, on):
        m = fcntl.ioctl(self.fd, termios.TIOCMGET, struct.pack("I", 0))
        bits = struct.unpack("I", m)[0]
        if on:
            bits |= termios.TIOCM_DTR
        else:
            bits &= ~termios.TIOCM_DTR
        fcntl.ioctl(self.fd, termios.TIOCMSET, struct.pack("I", bits))

    def read(self, n, timeout=None):
        """Read up to n bytes with timeout; returns bytes."""
        t = timeout if timeout is not None else self.timeout
        deadline = time.monotonic() + t
        out = b""
        while len(out) < n:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(self.fd, n - len(out))
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            if time.monotonic() > deadline:
                break
        return out

    def read_until(self, marker, timeout=2.0, maxlen=65536):
        deadline = time.monotonic() + timeout
        out = b""
        while marker not in out and len(out) < maxlen:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            if time.monotonic() > deadline:
                break
        return out

    def drain(self, timeout=0.5):
        """Read and discard pending input."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if not r:
                continue
            try:
                os.read(self.fd, 4096)
            except OSError:
                break

    def write(self, data):
        return os.write(self.fd, data)

    def flush(self):
        termios.tcdrain(self.fd)

    def close(self):
        try:
            self.set_dtr(False)
        except OSError:
            pass
        os.close(self.fd)


def drain_quiet(port, quiet=0.15, max_time=2.0):
    """Read and discard until the link has been silent for `quiet` seconds."""
    deadline = time.monotonic() + max_time
    while time.monotonic() < deadline:
        if not port.read(65536, timeout=quiet):
            return


def la_recover(port):
    """Put the LA back into the command parser: stop any stream, cancel any
    capture, then drain until quiet. Makes a suite self-healing after an
    aborted read (a crashed test can leave the CDC buffers full of data)."""
    for _ in range(2):
        port.write(LA_FRAME_START + bytes([0x42]) + LA_FRAME_END)
        port.write(b"\xff")
    port.flush()
    drain_quiet(port, quiet=0.15, max_time=2.0)


def find_ports():
    """Return (uart_path, la_path) for the PicoGremlin.

    Matches the LogicAnalyzer composite device on /dev/serial/by-id, filtered by
    USB serial when PICOGREMLIN_SERIAL is known (env or auto-detected); interface
    if01 = CDC0 (UART bridge), if03 = CDC1 (logic analyzer).
    """
    uart = la = None
    for p in sorted(glob.glob("/dev/serial/by-id/*LogicAnalyzer*")):
        if PICOGREMLIN_SERIAL and PICOGREMLIN_SERIAL not in p:
            continue
        target = os.path.realpath(p)
        if p.endswith("-if01"):
            uart = target
        elif p.endswith("-if03"):
            la = target
    return uart, la


# ---- LA protocol helpers -----------------------------------------------------
def la_escape(payload):
    out = bytearray()
    for b in payload:
        if b in (0xAA, 0x55, 0xF0):
            out.append(LA_ESC)
            out.append(b ^ 0xF0)
        else:
            out.append(b)
    return bytes(out)


def la_frame(command, payload=b""):
    return LA_FRAME_START + bytes([command]) + la_escape(payload) + LA_FRAME_END


def la_build_capture_request(trigger_type, trigger, inverted_or_count,
                             trigger_value, channels, frequency,
                             pre_samples, post_samples, loop_count=0,
                             measure=0, capture_mode=1):
    ch = channels + [0] * (24 - len(channels))
    return struct.pack(
        "<BBBxH24sBxIIIBBBx",
        trigger_type, trigger, inverted_or_count, trigger_value,
        bytes(ch), len(channels), frequency, pre_samples, post_samples,
        loop_count, measure, capture_mode,
    )


def la_read_response(port, timeout=10):
    """Read a newline-terminated response line from the LA CDC."""
    data = port.read_until(b"\n", timeout=timeout, maxlen=2048)
    return data


def la_capture(port, channels, frequency, pre, post, trigger_type=0,
               trigger=14, inverted=0, trigger_value=0, loop_count=0,
               measure=0, head_timeout=30, data_timeout=90, progress=None,
               drain=1.0):
    """Issue a capture request and read the binary capture payload.

    The firmware may or may not send a leading "CAPTURE_STARTED\\n" line
    (it is dropped when la_bridge_connected() is momentarily false), so the
    parser tolerates both cases.
    """
    drain_quiet(port, quiet=0.15, max_time=max(0.5, drain))
    mode = 2 if max(channels) >= 16 else (1 if max(channels) >= 8 else 0)
    req = la_build_capture_request(trigger_type, trigger, inverted,
                                   trigger_value, channels, frequency,
                                   pre, post, loop_count, measure, mode)
    port.write(la_frame(1, req))
    port.flush()
    if progress:
        progress("armed")

    # read the first chunk (response line and/or start of data)
    head = port.read(4096, timeout=head_timeout)
    if b"CAPTURE_STARTED" in head:
        idx = head.index(b"CAPTURE_STARTED") + len(b"CAPTURE_STARTED\n")
        head = head[idx:]
    elif b"CAPTURE_ERROR" in head or b"ERR_" in head:
        return {"ok": False, "response": head.decode(errors="replace")[:80]}
    # head now should start with the 4-byte sample count
    if len(head) < 4:
        head += port.read(4 - len(head), timeout=10)
        if len(head) < 4:
            return {"ok": False, "response": "timeout waiting for data"}
    count = struct.unpack("<I", head[0:4])[0]
    if count == 0 or count > 400000:
        return {"ok": False, "response": f"bad count {count}", "count": count}
    width = 1 if mode == 0 else (2 if mode == 1 else 4)
    total = count * width + 1   # samples + timestamp byte (already have 4 count bytes)
    need = total - (len(head) - 4)
    deadline = time.monotonic() + data_timeout
    streamed = False
    while need > 0 and time.monotonic() < deadline:
        chunk = port.read(min(8192, need), timeout=2.0)
        if not chunk:
            continue
        if progress and not streamed:
            progress("streaming")     # the trigger fired; data is flowing
            streamed = True
        head += chunk
        need = total - (len(head) - 4)
    if need > 0:
        return {"ok": False, "response": "timeout", "count": count,
                "got": len(head) - 4, "want": total}
    samples_raw = head[4:4 + count * width]
    idx = 4 + count * width
    if idx >= len(head) or head[idx] > 64 or idx + 1 + head[idx] * 4 > len(head):
        la_recover(port)
        return {"ok": False, "response": "desynced capture payload (recovered)"}
    stamp_len = head[idx]
    timestamps = []
    idx += 1
    for _ in range(stamp_len):
        timestamps.append(struct.unpack("<I", head[idx:idx + 4])[0])
        idx += 4
    samples = []
    for i in range(count):
        if width == 1:
            samples.append(samples_raw[i])
        elif width == 2:
            samples.append(struct.unpack("<H", samples_raw[2 * i:2 * i + 2])[0])
        else:
            samples.append(struct.unpack("<I", samples_raw[4 * i:4 * i + 4])[0])
    return {"ok": True, "count": count, "mode": mode, "width": width,
            "stamp_len": stamp_len, "timestamps": timestamps, "samples": samples}


def extract_channel(samples, width, ch_index):
    """Extract per-sample bit for requested channel ch_index (0-based)."""
    out = []
    for s in samples:
        if width == 1:
            out.append((s >> ch_index) & 1)
        elif width == 2:
            out.append((s >> ch_index) & 1)
        else:
            out.append((s >> ch_index) & 1)
    return out


def analyze_channels(samples, width, sample_rate):
    """Return a dict of per-channel timing metrics.

    samples: list of raw sample words; channel i is bit i.
    """
    nch = width * 8
    dt = 1.0 / sample_rate
    ch = {}
    for c in range(2):  # we only care about the first two requested channels
        bits = [((s >> c) & 1) for s in samples]
        edges = []
        for i in range(1, len(bits)):
            if bits[i] != bits[i - 1]:
                edges.append((i, bits[i]))  # (index, new value)
        rising = [i for (i, v) in edges if v == 1]
        falling = [i for (i, v) in edges if v == 0]
        # periods between consecutive rising edges (median is robust to a
        # single glitched/missed edge in a long capture)
        periods = [rising[k + 1] - rising[k] for k in range(len(rising) - 1)]
        if periods:
            sp = sorted(periods)
            med = sp[len(sp) // 2]
            period_avg = med * dt
            freq = 1.0 / period_avg
        else:
            period_avg = 0.0
            freq = 0.0
        # per-period duty: measure high fraction between consecutive rising edges
        # (avoids the all-low pre-trigger region skewing a whole-window average)
        period_duties = []
        for k in range(len(rising) - 1):
            s = rising[k]
            e = rising[k + 1]
            if e > s:
                period_duties.append(sum(bits[s:e]) / (e - s) * 100.0)
        duty = sum(period_duties) / len(period_duties) if period_duties else \
            ((sum(bits) / len(bits) * 100.0) if bits else 0.0)
        # high/low run lengths
        runs = []
        cur = bits[0] if bits else 0
        run = 0
        for b in bits:
            if b == cur:
                run += 1
            else:
                runs.append((cur, run))
                cur = b
                run = 1
        runs.append((cur, run))
        high_runs = [r for (v, r) in runs if v == 1]
        low_runs = [r for (v, r) in runs if v == 0]
        ch[c] = {
            "bits": bits,
            "rising": rising,
            "falling": falling,
            "edge_count": len(edges),
            "rising_count": len(rising),
            "falling_count": len(falling),
            "period_avg_s": period_avg,
            "frequency_hz": freq,
            "duty_percent": duty,
            "high_avg_ticks": (sum(high_runs) / len(high_runs)) if high_runs else 0,
            "low_avg_ticks": (sum(low_runs) / len(low_runs)) if low_runs else 0,
            "high_avg_s": ((sum(high_runs) / len(high_runs)) * dt) if high_runs else 0.0,
            "low_avg_s": ((sum(low_runs) / len(low_runs)) * dt) if low_runs else 0.0,
        }
    return ch


# ---- fixture protocol helpers -------------------------------------------------
def fix_pattern_bytes(pat_id, length, seed=0x12345678):
    prng = seed
    out = bytearray()
    for i in range(length):
        if pat_id == PATTERN_AZ:
            out.append(ord("A") + (i % 26))
        elif pat_id == PATTERN_09:
            out.append(ord("0") + (i % 10))
        elif pat_id == PATTERN_55:
            out.append(0x55)
        elif pat_id == PATTERN_AA:
            out.append(0xAA)
        elif pat_id == PATTERN_00FF:
            out.append(i & 0xFF)
        else:
            prng ^= (prng << 13) & 0xFFFFFFFF
            prng ^= prng >> 17
            prng ^= (prng << 5) & 0xFFFFFFFF
            prng &= 0xFFFFFFFF
            out.append((prng >> 24) & 0xFF)
    return bytes(out)


def fix_crc(data):
    return binascii.crc32(data) & 0xFFFFFFFF


def fix_send(port, cmd, payload=b""):
    port.write(cmd + payload)
    port.flush()


def fix_echo(port, payload, timeout=10):
    port.drain(0.3)
    fix_send(port, FIX_CMD_ECHO, struct.pack("<I", len(payload)) + payload)
    return port.read(len(payload), timeout=timeout)


def fix_pattern(port, pat_id, length, timeout=30):
    fix_send(port, FIX_CMD_PATTERN, bytes([pat_id]) + struct.pack("<I", length))
    buf = port.read(length + 4, timeout=timeout)
    return buf


def fix_identity(port, timeout=5):
    port.drain(0.5)
    fix_send(port, FIX_CMD_ID)
    return port.read_until(b"\n", timeout=timeout)


def fix_configure(port, channel, freq, duty_permille, phase_tenths, timeout=2):
    payload = bytes([channel]) + struct.pack("<IHH", freq, duty_permille, phase_tenths)
    fix_send(port, FIX_CMD_CFG, payload)
    r = port.read(1, timeout=timeout)
    return r == b"K"


def fix_status(port, timeout=5):
    fix_send(port, FIX_CMD_STATUS)
    return port.read_until(b"\n", timeout=timeout, maxlen=65536)


def fix_set_baud(port, baud, timeout=2):
    fix_send(port, FIX_CMD_BAUD, struct.pack("<I", baud))
    return port.read(1, timeout=timeout) == b"K"


def fix_stop_stream(port, timeout=2):
    fix_send(port, FIX_CMD_STOP)
    return port.read(1, timeout=timeout) == b"K"


# ---- 24-channel fixture helpers (identity-wired LA pins) --------------------
def fix_mask(port, mask, timeout=2):
    """Drive all 24 LA pins to `mask` (bit k = channel k) as SIO outputs."""
    fix_send(port, FIX_CMD_MASK, struct.pack("<I", mask))
    return port.read(1, timeout=timeout) == b"K"


def fix_exclude(port, mask, timeout=2):
    """Channels the target fixture must never drive (bit k = channel k)."""
    fix_send(port, FIX_CMD_EXCLUDE, struct.pack("<I", mask))
    return port.read(1, timeout=timeout) == b"K"


def fix_walk(port, toggle_mask, static_mask, hz, timeout=2):
    """Toggle `toggle_mask` bits at hz, hold `static_mask` constant."""
    fix_send(port, FIX_CMD_WALK, struct.pack("<III", toggle_mask, static_mask, hz))
    return port.read(1, timeout=timeout) == b"K"


def fix_comb(port, base_hz, timeout=2):
    """Frequency comb: channel k toggles at base_hz/(k+1); 0 stops."""
    fix_send(port, FIX_CMD_COMB, struct.pack("<I", base_hz))
    return port.read(1, timeout=timeout) == b"K"


def fix_gen_channels(port, ch_a, ch_b, timeout=2):
    """Retarget the precision PIO generator pair to channels ch_a/ch_b."""
    fix_send(port, FIX_CMD_GEN_CH, bytes([ch_a, ch_b]))
    return port.read(1, timeout=timeout) == b"K"


def fix_set_inputs(port, pull, timeout=2):
    """All 24 pins to SIO input with pull 0=none, 1=down, 2=up."""
    fix_send(port, FIX_CMD_INPUT, bytes([pull]))
    return port.read(1, timeout=timeout) == b"K"


def fix_heart(port, enable, timeout=2):
    """Enable/disable the GP22 heartbeat toggle."""
    fix_send(port, FIX_CMD_HEART, bytes([1 if enable else 0]))
    return port.read(1, timeout=timeout) == b"K"


def channel_metrics(samples, width, sample_rate, nch=None):
    """Per-bit timing metrics for every captured channel.

    bit k of each sample word corresponds to the k-th requested channel.
    """
    if nch is None:
        nch = width * 8
    dt = 1.0 / sample_rate
    out = {}
    for c in range(nch):
        bits = [((s >> c) & 1) for s in samples]
        edges = [(i, bits[i]) for i in range(1, len(bits)) if bits[i] != bits[i - 1]]
        rising = [i for (i, v) in edges if v == 1]
        falling = [i for (i, v) in edges if v == 0]
        periods = [rising[k + 1] - rising[k] for k in range(len(rising) - 1)]
        if periods:
            sp = sorted(periods)
            med = sp[len(sp) // 2]
            period_avg = med * dt
            freq = 1.0 / period_avg
        else:
            period_avg = 0.0
            freq = 0.0
        period_duties = []
        for k in range(len(rising) - 1):
            s, e = rising[k], rising[k + 1]
            if e > s:
                period_duties.append(sum(bits[s:e]) / (e - s) * 100.0)
        duty = sum(period_duties) / len(period_duties) if period_duties else \
            ((sum(bits) / len(bits) * 100.0) if bits else 0.0)
        out[c] = {
            "edge_count": len(edges),
            "rising_count": len(rising),
            "falling_count": len(falling),
            "period_avg_s": period_avg,
            "frequency_hz": freq,
            "duty_percent": duty,
            "high_ratio": (sum(bits) / len(bits)) if bits else 0.0,
        }
    return out


# ---- measurement helper -------------------------------------------------------
def calc_error_percent(measured, reference):
    if reference == 0:
        return 0.0 if measured == 0 else float("inf")
    return abs(measured - reference) / reference * 100.0


# ---- subcommands ---------------------------------------------------------------
def cmd_ports(args):
    uart, la = find_ports()
    print(json.dumps({"uart_cdc0": uart, "la_cdc1": la, "serial": PICOGREMLIN_SERIAL},
                     indent=2))
    return 0


def cmd_la_ident(args):
    uart, la = find_ports()
    if not la:
        print("ERROR: LA port not found")
        return 1
    p = SerialPort(la, 115200)
    p.drain(1.0)
    p.write(LA_ID_CMD)
    p.flush()
    resp = p.read_until(b"CHANNELS:24", timeout=5, maxlen=4096)
    print(resp.decode(errors="replace"))
    p.close()
    return 0


def cmd_la_capture(args):
    uart, la = find_ports()
    if not la:
        print("ERROR: LA port not found")
        return 1
    p = SerialPort(la, 115200)
    chans = [int(x) for x in args.channels.split(",")]
    res = la_capture(p, chans, args.freq, args.pre, args.post,
                     trigger_type=args.trigger_type, trigger=args.trigger)
    if not res["ok"]:
        print(f"capture failed: {res}")
        return 1
    ch = analyze_channels(res["samples"], res["width"], args.freq)
    print(json.dumps({k: ch[k] for k in ch}, indent=2, default=str))
    p.close()
    return 0


def cmd_measure(args):
    uart, la = find_ports()
    if not uart or not la:
        print("ERROR: ports not found")
        return 1
    up = SerialPort(uart, 115200)
    # configure both channels
    fix_configure(up, 0, args.freq, int(args.duty_a * 10), 0)
    fix_configure(up, 1, args.freq, int(args.duty_b * 10), int(args.phase_b * 10))
    up.close()
    time.sleep(0.2)
    lp = SerialPort(la, 115200)
    chans = [14, 15]
    res = la_capture(lp, chans, args.freq, args.pre, args.post, trigger_type=0, trigger=14)
    lp.close()
    if not res["ok"]:
        print(json.dumps({"ok": False, "response": res.get("response")}, indent=2))
        return 1
    ch = analyze_channels(res["samples"], res["width"], args.freq)
    # phase: for ch1 rising relative to ch0 rising
    r0 = ch[0]["rising"]
    r1 = ch[1]["rising"]
    phase_deg = None
    if r0 and r1:
        p0_ticks = ch[0]["period_avg_s"] * args.freq
        deltas = []
        j = 0
        for e0 in r0:
            while j < len(r1) and r1[j] < e0:
                j += 1
            if j >= len(r1):
                break
            deltas.append((r1[j] - e0) % round(p0_ticks))
        if deltas:
            avg = sum(deltas) / len(deltas)
            if p0_ticks:
                phase_deg = avg / p0_ticks * 360.0
    out = {
        "ok": True,
        "requested_freq_hz": args.freq,
        "sample_rate_hz": args.freq,
        "channel_A": {
            "frequency_hz": ch[0]["frequency_hz"],
            "duty_percent": ch[0]["duty_percent"],
            "high_s": ch[0]["high_avg_s"],
            "low_s": ch[0]["low_avg_s"],
            "rising_edges": ch[0]["rising_count"],
            "falling_edges": ch[0]["falling_count"],
        },
        "channel_B": {
            "frequency_hz": ch[1]["frequency_hz"],
            "duty_percent": ch[1]["duty_percent"],
            "high_s": ch[1]["high_avg_s"],
            "low_s": ch[1]["low_avg_s"],
            "rising_edges": ch[1]["rising_count"],
            "falling_edges": ch[1]["falling_count"],
        },
        "phase_deg_chB_vs_chA": phase_deg,
        "samples": res["count"],
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_phase(args):
    return cmd_measure(args)


def main():
    ap = argparse.ArgumentParser(description="PicoGremlin HIL harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports")
    sub.add_parser("la-ident")

    p = sub.add_parser("la-capture")
    p.add_argument("--freq", type=int, default=1000000)
    p.add_argument("--pre", type=int, default=2000)
    p.add_argument("--post", type=int, default=8000)
    p.add_argument("--channels", default="14,15")
    p.add_argument("--trigger", type=int, default=14)
    p.add_argument("--trigger-type", type=int, default=0)

    p = sub.add_parser("measure")
    p.add_argument("--freq", type=int, default=1000000)
    p.add_argument("--pre", type=int, default=4000)
    p.add_argument("--post", type=int, default=16000)
    p.add_argument("--duty-a", type=float, default=50)
    p.add_argument("--duty-b", type=float, default=50)
    p.add_argument("--phase-b", type=float, default=0)

    p = sub.add_parser("phase")
    p.add_argument("--freq", type=int, default=1000000)
    p.add_argument("--pre", type=int, default=4000)
    p.add_argument("--post", type=int, default=16000)

    args = ap.parse_args()
    if args.cmd == "ports":
        return cmd_ports(args)
    if args.cmd == "la-ident":
        return cmd_la_ident(args)
    if args.cmd == "la-capture":
        return cmd_la_capture(args)
    if args.cmd == "measure" or args.cmd == "phase":
        return cmd_measure(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
