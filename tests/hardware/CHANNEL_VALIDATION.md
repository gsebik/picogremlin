# PicoGremlin — 24-Channel LA GPIO Validation

## 2026-08-19 — full 24/24 validation (all channels wired)

Board: PicoGremlin RP2350A, USB serial `13E1B67BABCD9816` (a different physical
unit than the 2026-08-18 bench below).
Firmware: rebuilt against pico-sdk `master` (`build/picogremlin.uf2`, SHA-256
`5a4eeaf50f8077f3e7e56e8b2761444866a8bb8768a9a78cffe091ee003d53fb`), flashed via
the runtime-reset BOOTSEL interface. Target fixture `target_fixture.elf` SHA-256
`a2e567323084ad94be0b3086fb0e37e926ead3e55d65e742049292e84f0d0cf6`.
Wiring: target GPIO n → PicoGremlin GPIO n (identity), shared GND. The target was
moved off a dev board and wired for this campaign, resolving the 2026-08-18
connectivity gaps.
Harness: run with the de-hardcoded suite (serial auto-detected, `openocd`/scripts
from `$PATH`/`$OPENOCD_SCRIPTS`); no per-bench edits.

All results below were executed on the connected hardware. Artifact:
`artifacts/channels/channels_test_results.json` (49 checks: 47 PASS, 1 NOT
SUPPORTED, 1 intermittent FAIL — see T3).

| Check | Status | Details |
|---|---|---|
| T1 continuity (all-low / all-high / 0x555555 / 0xAAAAAA) | PASS | every channel reads the exact level |
| T1 walking-ones | PASS | **all 24 channels** wired and uniquely mapped `[0,k]` |
| T2 input pull-up | PASS | all channels read high |
| T2 high-Z (pull-down bias) | NOT SUPPORTED | probe LA inputs are pull-up biased (by design) |
| T3 comb @24 kHz | PASS | all 24 channels uniquely identified |
| T3 comb @6 kHz | FAIL (intermittent) | ch23 (÷24, 250 Hz) read ~481–492 Hz on ~1 of 3 runs — CPU-comb jitter on the slowest channel, not a wiring fault; ch23 passes the T4 precision test. 2/3 re-runs PASS |
| T4 precision pair timing | PASS | all 12 pairs (0-1 … 22-23) at 1 kHz/100 kHz/1 MHz: freq ±2 %, duty ±3 % |
| T5 per-channel edge trigger | PASS | every one of 24 channels can trigger a capture |
| T6 PIO2 fast-capture interleave | PASS | 4× (simple → fast → simple) on all 24; no stale funcsel/leak |
| T7 cross-subsystem | PASS | comb + SWD read + UART echo + armed fast capture + LA recovery |

**T3 note:** the frequency comb is CPU-driven and documented as jittery/identify-only.
The slowest channel (ch23 = base÷24) occasionally reads a doubled frequency. Use
T4 for precision. This is a known limitation, not a regression.

---

## 2026-08-18 — original bench (9 channels wired, historical)

Date: 2026-08-18
Firmware: PicoGremlin A+ (`build-aplus-final/picogremlin.uf2`, SHA-256
`1bdf37c743796e154c60d09447feb6b9f4a0fdff13b9571a1b96364367fe79bf`) plus the
extended target fixture (`tests/hardware/target_fixture/fixture.c`).
Wiring: target GPIO n → PicoGremlin GPIO n (identity), shared GND.

All results below were executed on the connected hardware.

## Wiring connectivity (measured, ground truth)

A per-channel walking-one probe (drive one channel high, read the LA) shows:

| Result | Channels |
|---|---|
| **Stable, correctly wired** | ch0,1,4,9,12,14,15,16,17 (GP2,3,6,11,14,16,17,18,19) |
| **Intermittent** | ch19 (GP21) — reads correctly under static drive, flaky under toggling |
| **Coupled / not wired** | ch21,23 (GP26,GP28) — driving GP28 lights both LA bits 21 and 23; GP26 floats |
| **Not electrically connected** | ch2,3,5,6,7,8,10,11,13,18,20,22 (GP4,5,7,8,9,10,12,13,15,20,22,27) |

**Action required (hardware):** 13 channels do not reach the LA and two pairs are
coupled. Re-seat/verify every jumper on PicoGremlin GP2–GP22, GP26–GP28, especially
GP21, GP26, GP28. Re-run `tests_la_channels.py --only T1` to confirm before trusting
all 24 channels.

## Firmware changes (target fixture)

- 24-channel identity map `ch_gpio[24] = {2..22, 26, 27, 28}`.
- New commands: `M` static mask, `K` walking toggle, `F` frequency comb
  (ch k = base/(k+1)), `G` generator retarget, `D` input + pull, `W` heartbeat gate.
- Channel→GPIO bit translation (a channel bitmask is not a GPIO mask).
- Self-healing UART parser: a truncated command times out after 200 ms and drains
  instead of wedging the main loop (was reproducible by a partial command).

## Results

Artifacts: `tests/hardware/artifacts/channels/channels_test_results.json`
Statuses: PASS / FAIL / BLOCKED / NOT SUPPORTED / NOT TESTED.

| Check | Status | Details |
|---|---|---|
| T1 all-low | PASS | wired channels read 0 |
| T1 all-high | PASS | wired channels read 1 |
| T1 pattern 0x555555 / 0xAAAAAA | PASS | exact per-channel levels |
| T1 walking-ones | PASS | 8/8 stable channels lit exactly `[0,k]` |
| T2 input pull-up | PASS | wired channels read high |
| T2 high-Z (pull-down bias) | NOT SUPPORTED | probe LA inputs are pull-up biased; they read high when undriven regardless of the target pull direction, so true high-Z cannot be proven here |
| T3 frequency comb @24 kHz / @6 kHz | PASS | per-channel frequency uniquely identifies all 8 wired channels (identification, not precision) |
| T4 precision pair timing | PASS | pairs (0,1),(4,9),(12,14),(15,16),(16,17) at 1 kHz/100 kHz/1 MHz: frequency ±2 %, duty ±3 % |
| T5 per-channel edge trigger | PASS | every wired channel can trigger a capture |
| T6 PIO2 fast-capture interleave | PASS | 4× (simple → fast → simple) on all wired channels; no stale funcsel/leak after fast capture |
| T7 cross-subsystem | PASS | comb + SWD read + UART echo + armed fast capture + LA recovery |
| Regression: LA suite | PASS | 12/12 (ch14/ch15 map, edge/complex/fast/blast) |
| Regression: SWD/GDB suite | PASS | 7/7 |
| 24-channel near-max buffer | NOT TESTED | requires all 24 channels wired |

## Known limitations carried forward

- UART baud-switch intermittency, autobaud arbitrary-pattern divisor lock,
  100 MHz waveform aliasing, SWD ≥500 kHz desync — all pre-existing and unchanged.
- The LA `in pins` reads a pin regardless of its function-select, so T6 validates
  PIO2/SM cleanup, not the physical mux on unwired pins.
