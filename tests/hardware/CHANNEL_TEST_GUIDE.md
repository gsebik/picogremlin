# PicoGremlin 24-Channel LA Test — Description

This document describes the 24-channel logic-analyzer test campaign: what it
verifies, how the hardware is wired, how to build and program the target
fixture, how to run every stage, and what each stage expects. The most recent
results are in `CHANNEL_VALIDATION.md` and
`artifacts/channels/channels_test_results.json`.

The test requires the A+ PicoGremlin firmware (`build-aplus-final/picogremlin.uf2`)
already flashed on the probe.

---

## 1. Purpose

The PicoGremlin logic analyzer samples 24 input channels. On the probe these are
GPIO `2..22, 26, 27, 28` in that order (LA channel k = probe GPIO `pinMap[k]`).
A target RP2350 fixture drives its own GPIOs in the **same identity order**
(channel k = target GPIO `ch_gpio[k] = {2..22, 26, 27, 28}`), wired pin-to-pin.

The campaign verifies per channel:
- electrical continuity and correct mapping (T1),
- input direction and pull behaviour (T2),
- simultaneous identification via a frequency comb (T3),
- precision frequency and duty via a retargetable PIO generator (T4),
- that every channel can serve as a capture trigger (T5),
- that PIO2 fast capture leaves no channel stuck afterwards (T6),
- that all 24 switching lines do not disturb SWD / UART / autobaud (T7).

---

## 2. Wiring

| Target Pico 2 | PicoGremlin | LA channel |
|---|---|---|
| GP2 … GP22 | GP2 … GP22 (same number) | ch0 … ch20 |
| GP26, GP27, GP28 | GP26, GP27, GP28 | ch21, ch22, ch23 |

Already required, unchanged:
- target SWD header → probe GP23 (SWCLK), GP29 (SWDIO)
- target UART0 GP0 (TX) / GP1 (RX) → probe GP24 (TX) / GP25 (RX)
- common ground (a second GND jumper is recommended with 24 switching lines)

Prohibited connections (output drive contention): any target GPIO to probe
GP0/GP1 (LA trigger loopback), GP23 (SWCLK out), GP24 (UART TX out), GP29
(SWDIO).

---

## 3. Software layout

```
tests/hardware/
├── target_fixture/
│   ├── fixture.c        ← target firmware (commands + 24-ch engine)
│   ├── siggen.pio       ← precision PIO square-wave generator (2 SMs)
│   └── build/           ← generated build dir (gitignored)
├── picogremlin_hil.py   ← host harness (ports, LA protocol, fixture helpers)
├── tests_la_channels.py ← the campaign script (T1–T7, checkpointed)
├── pg_target_single.cfg ← OpenOCD config (single core, 250 kHz)
├── CHANNEL_VALIDATION.md
├── CHANNEL_TEST_GUIDE.md ← this file
└── artifacts/channels/   ← results (checkpointed JSON)
```

The campaign script is **checkpointed**: after every check it appends to
`channels_test_results.json`, so a timeout costs nothing.

---

## 4. Building and programming the target fixture

```bash
cmake -B tests/hardware/target_fixture/build \
  -S tests/hardware/target_fixture \
  -DPICO_SDK_PATH=/path/to/pico-sdk -DPICO_BOARD=pico2
cmake --build tests/hardware/target_fixture/build -j$(nproc)
```

Program through the PicoGremlin probe at 100 kHz (higher speeds intermittently
desync). No paths are hardcoded: `openocd` is taken from `$PATH` (override with
`$OPENOCD`), its scripts from `$OPENOCD_SCRIPTS`, and the board is selected by
`$PICOGREMLIN_SERIAL` when set (otherwise the sole connected CMSIS-DAP adapter):

```bash
# optional overrides:
#   export OPENOCD=/path/to/openocd
#   export OPENOCD_SCRIPTS=/path/to/openocd/scripts
#   export PICOGREMLIN_SERIAL=<16-hex>   # only needed if >1 probe is attached
openocd \
  ${OPENOCD_SCRIPTS:+-s "$OPENOCD_SCRIPTS"} \
  -f tests/hardware/pg_target_single.cfg \
  -c "adapter speed 100" \
  -c "program tests/hardware/target_fixture/build/target_fixture.elf verify reset" \
  -c "shutdown"
```

Verify the fixture is alive on the UART bridge (expect
`PICOGREMLIN_TARGET_FIXTURE_V1`):

```bash
python3 -c "import sys; sys.path.insert(0,'tests/hardware'); \
from picogremlin_hil import *; u,l=find_ports(); p=SerialPort(u,115200); \
print(fix_identity(p)); p.close()"
```

---

## 5. Running the campaign

```bash
# all stages:
python3 tests/hardware/tests_la_channels.py --out tests/hardware/artifacts/channels

# a single stage, e.g. the connectivity check:
python3 tests/hardware/tests_la_channels.py --only T1 --out tests/hardware/artifacts/channels

# declare which channels are wired (used as the assertion set):
python3 tests/hardware/tests_la_channels.py \
  --wired 0,1,4,9,12,14,15,16,17 \
  --wired-dyn 0,1,4,9,12,14,15,16,17 \
  --out tests/hardware/artifacts/channels
```

- `--only T1,T3` runs a subset of stages.
- `--wired` is the set asserted for static/connectivity checks.
- `--wired-dyn` is the set asserted for dynamic (toggling/trigger) checks.
- Results are written to `artifacts/channels/channels_test_results.json` with
  statuses `PASS` / `FAIL` / `NOT SUPPORTED` / `NOT TESTED`. Delete that file to
  discard stale results before a fresh run.

---

## 6. Stages and pass criteria

| Stage | Method | Pass bar |
|---|---|---|
| T1 all-low / all-high / 0x555555 / 0xAAAAAA | static masks with matching trigger polarity | every wired channel reads the exact level |
| T1 walking-ones | one channel driven high, ch0 held high as trigger | bit k lit iff channel k driven, no other bit |
| T2 input pull-up | pins to input + pull-up | wired channels read high |
| T2 input pull-down | pins to input + pull-down | **NOT SUPPORTED**: probe LA inputs are pull-up biased, so the net reads high regardless of the target pull |
| T3 comb @24 kHz / @6 kHz | CPU frequency comb, ch k toggles at base/(k+1) | each wired channel measures a unique frequency within ±25 % (identification only) |
| T4 pair timing | PIO generator retargeted to pairs (a,b) at 1 kHz / 100 kHz / 1 MHz, 50 % duty, 90° phase | frequency ±2 %, duty 50±3 % |
| T5 per-channel trigger | walk-trigger each wired channel | capture completes on every channel |
| T6 PIO2 interleave | simple → fast → simple, ×4 | every wired channel still toggles after fast capture |
| T7 cross-subsystem | comb + SWD read + UART echo + armed fast capture + LA recovery | all four succeed |

### LA capture polarity reference
- Static all-low → `trigger_type=0, inverted=1`
- Static all-high → `trigger_type=0, inverted=0`
- Toggling signal → `inverted=0`
- Pattern (fast/complex) triggers only address channels 0–15 (`base+count ≤ 16`);
  edge and blast triggers accept all 24.

---

## 7. Notes

- The simple-capture trigger fires on a **level**: `inverted=0` fires when the
  trigger pin is HIGH, `inverted=1` when LOW. A capture whose trigger pin stays
  in the opposite state waits indefinitely (that is expected behaviour, not a
  fault).
- The frequency comb is CPU-driven and jittery at high rates; use it for channel
  identification, and T4 for precision.
- When a capture is left armed on a never-matching trigger, the LA is busy until
  a `0xFF` (cancel) byte is sent on CDC1.
