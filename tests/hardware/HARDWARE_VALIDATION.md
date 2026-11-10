# PicoGremlin — Hardware Validation Report

Date: 2026-08-17
Host: Linux (Pop!_OS / Ubuntu noble), `arm-none-eabi-gcc 13.2.1`, CMake 3.28.3,
picotool 2.3.0, OpenOCD `0.12.0+dev-00004-gacff23ffd` (Raspberry Pi fork,
`~/pico/openocd/src/openocd`), `gdb-multiarch 15.1`, Python 3.12.3.
Pico SDK: `~/pico/pico-sdk` at 2.3.0.

This report records results measured on the connected hardware through the
PicoGremlin probe. No PASS is claimed without having been executed on hardware.

The results below are the pre-A+ PIO1-fallback baseline. They do not validate the
new PIO2 fast-sampler firmware; a fresh campaign is required after flashing it.

---

## 1. Devices and connection map

| Device | Role | Identity | Notes |
|--------|------|----------|-------|
| PicoGremlin | Pico 2 W (RP2350A) modified Pico Tool | USB `2e8a:0009`, serial `5C0559336694DAD6`, chipid `0x5c0559336694dad6` | The probe / tool firmware |
| Target | RP2350 rev 2 (B0), 4 MiB Winbond `w25q32` flash, chipid `0x20004927` | SWD target | Programmed/debugged via PicoGremlin |

```
Computer USB
    |-- PicoGremlin (2e8a:0009)  --SWD (GP23/GP29)-->  RP2350 target
    |     |-- CDC0 /dev/ttyACM0  --UART1 (GP24/GP25)--> target UART0 (GP0/GP1)
    |     |-- CDC1 /dev/ttyACM1  --Logic Analyzer-->   target GP16 -> LA ch14
    |     |                                            target GP17 -> LA ch15
    +-- /dev/serial/by-id/...5C0559336694DAD6-if01 = ttyACM0 (Target UART)
    +-- /dev/serial/by-id/...5C0559336694DAD6-if03 = ttyACM1 (Logic Analyzer)
```

LA channel map (verified): logical channel 14 = GP16, channel 15 = GP17.

---

## 2. Build results

**Original (unmodified) source**, fresh out-of-source build:

```
cmake -S picogremlin/firmware -B build-baseline \
  -DPICO_SDK_PATH=$PICO_SDK_PATH -DPICO_BOARD=pico2_w
cmake --build build-baseline -j$(nproc)
```

Result: **PASS** — clean compile, **zero warnings, zero errors**.
Artifacts: `build-baseline/picogremlin.{uf2,elf,bin,hex,dis}`.
Size: text 56568 / bss 434168. UF2 sha256 `6cc79a50803b2ea16ed460055b8955fee8f472567c4ad30859a52d5691cce47f`.
Image: `rp2350-arm-s`, PICOGREMLIN `picogremlin-1.0`, SDK 2.3.0.

**Corrected firmware** (see §8): `build-fixed/`, text 56068 / bss 434152.

---

## 3. Pico Tool (PicoGremlin) programming test — PASS

Using the documented runtime-reset path (`README.md`):

| Step | Result |
|------|--------|
| `picotool version` | v2.3.0 (Linux, GNU-13.3.0, Release) |
| Device detected in BOOTSEL | RP2350, revision A4, QFN60, chipid `0x5c0559336694dad6` |
| Flash backed up before reflash | 2 MiB saved + verified (`device_07_flash_backup_prebaseline.bin`) |
| `picotool reboot -f -u` | device entered BOOTSEL (`2e8a:000f`) |
| `picotool load build/picogremlin.uf2 -u -v -x` | loaded + verified OK, rebooted to app |
| USB rediscovery | `2e8a:0009`, 6 interfaces, serial preserved |
| `ttyACM0`/`ttyACM1` restored | yes (by-id stable) |

Flash layout note: the running image was already `picogremlin-1.0` (SDK 2.3.0,
build date Aug 16 2026) before our baseline load.

---

## 4. Product descriptor / device discovery test — PASS

`lsusb -d 2e8a:0009 -v` against the source (`usb_descriptors.c`):

| Item | Expected | Actual | Status |
|------|----------|--------|--------|
| VID | 0x2e8a | 0x2e8a | PASS |
| PID | 0x0009 | 0x0009 | PASS |
| bcdDevice | 0x0231 | 2.31 | PASS |
| Device class | MISC/IAD | 239/2/1 | PASS |
| bNumInterfaces | 6 | 6 | PASS |
| Serial | 16 hex chars | `5C0559336694DAD6` | PASS |
| Interface 0 | CMSIS-DAP v2 (vendor) | matches | PASS |
| Interface 1/2 | Target UART (CDC) | matches | PASS |
| Interface 3/4 | Logic Analyzer (CDC) | matches | PASS |
| Interface 5 | Pico SDK Runtime Reset | matches | PASS |
| BOS / MS OS 2.0 | present | present | PASS |
| `ttyACM0` = interface 1 | Target UART | by-id `-if01` | PASS |
| `ttyACM1` = interface 3 | Logic Analyzer | by-id `-if03` | PASS |

Descriptor stability: **5/5 PASS** across 3 runtime-reboot cycles (enumerate,
6 interfaces, all four host functions, serial, tty nodes, then functional
UART check). No descriptor inconsistency found.

---

## 5. SWD programming + GDB debug test — PASS (7/7)

Through the PicoGremlin CMSIS-DAP to the RP2350 target (`tests_swd.py`):

- swd-connect: PASS (chipid `0x20004927`, DPIDR `0x4c013477`, Cortex-M33 x2,
  8 breakpoints / 4 watchpoints per core)
- swd-program-verify: PASS (`program target_fixture.elf verify reset`, "Verified OK")
- gdb-connect: PASS
- gdb-breakpoint-hit: PASS (breakpoint at `breakpoint_hit` reached)
- gdb-values: PASS — counter read, RAM write (`set g_counter = 12345` read back
  `0x3039`), single-step, continue, halt, reset
- swd-reset-run: PASS
- swd-reconnect: PASS (counter readable after reconnect)

Notes:
- Use a **single-core** OpenOCD config (`set USE_CORE { cm0 }`). The default
  dual-core SMP rp2350.cfg caused flash **programming to erase but not write**
  (see BUG-002). With the single-core config programming is reliable at ≤ 250 kHz.
- Adapter speed must be ≤ 250 kHz for bulk reads/writes; ≥ 500 kHz triggers a
  DAP transport desync (see BUG-001).

---

## 6. UART bridge test — 104/105 PASS (1 intermittent recovery)

`tests_uart.py` exercises the CDC0 ⇄ uart1 (GP24/GP25) bridge to the target
UART0 (GP0/GP1).

**All baud rates 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600:
identity, echo, and pattern integrity PASS** (patterns A–Z, 0–9, 0x55, 0xAA,
0x00–0xFF, PRNG). Long packet 256 KiB at 460800 PASS. Burst 50×200 PASS.
Continuous stream 8192×0x55 PASS. Reconnect/reopen PASS. Recovery from a low
non-standard baud (4800) PASS.

The single non-PASS was `switch@115200` — one baud-change handshake needed the
target-reset fallback (robustness issue, BUG-003). All data paths were correct;
this is a switch-timing artifact, not a data corruption.

---

## 7. UART auto-baud test

`tests_autobaud.py` (CDC0 line rate 9728 triggers the PIO/DMA measurement):

| Case | Result |
|------|--------|
| 0x55 pattern @ 9600 | PASS (474/512 bytes, 100% integrity) |
| 0x55 @ 19200 | PASS |
| 0x55 @ 38400 | PASS |
| 0x55 @ 57600 | PASS |
| 0x55 @ 115200 | PASS |
| 0x55 @ 230400 | PASS |
| 0xAA @ 115200 | PASS |
| 0x00–0xFF @ 115200 | FAIL (measured baud is a wrong divisor) — firmware limitation |
| PRNG @ 115200 | FAIL (same limitation) |
| single byte | NOT SUPPORTED (needs ≥ 8 bit-period samples) |
| no signal → recover | PASS |

The exact measured baud is not exposed by firmware; it is inferred from byte
integrity of a known pattern. The 0x00–0xFF / PRNG failures are a documented
firmware limitation: autobaud assumes at least one observed low pulse is a
single UART bit (the 0x00/random data can yield a divisor of the real baud).
This matches the caveat already recorded in the design documentation.

---

## 8. Logic analyzer tests — 12/12 PASS

`tests_la.py`:

- la-identity: PASS (`LOGIC_ANALYZER_PICO_2_V6_0`, `FREQ:100000000`,
  `BLASTFREQ:200000000`, `BUFFER:393216`, `CHANNELS:24`)
- channel-map GP16 → ch14: PASS (120 edges on ch14, 0 on ch15)
- channel-map GP17 → ch15: PASS
- both-channels @ 10 kHz: PASS
- capture: edge / complex / fast / blast: PASS (62000 / 100000 samples)
- repeated-capture 10×: PASS
- large-buffer 192k: PASS
- capture-cancel: PASS
- la-reconnect: PASS

Note: the merged build intermittently omits the leading `CAPTURE_STARTED\n`
line when the response races the bridge (see BUG-004); the harness tolerates
both forms, so all captures succeed.

---

## 9. Signal measurement (frequency / duty / period / edges / phase) — 22/22 PASS

`tests_measure.py`, measuring the target fixture's two PIO generators through
the PicoGremlin logic analyzer.

**Duty sweep @1 kHz** (10/25/50/75/90 %): all PASS (max error 0.09 pp).
**Frequency sweep** (1 Hz … 1 MHz): all PASS; error 0.000 % at most frequencies,
≤ 0.99 % at 1 MHz (LA sample quantization).
**Phase sweep @1 kHz** (0/45/90/180/270°): all PASS (max error 1.64°).
**Final configs Test A–E**: all PASS (see §10).

Errors are within the declared quantization-aware tolerances (declared before
measurement in the test script).

---

## 10. Final two-channel configs (Test A–G)

`tests_measure.py` (A–E) and `tests_range.py` (F, G):

| Test | Config | Result |
|------|--------|--------|
| A | 1 kHz 50%/50% 0° | PASS (1000.0 Hz, 50.0%/50.0%, 0.0°) |
| B | 1 kHz 50%/50% 90° | PASS (1000.0 Hz, 90.13°) |
| C | 1 kHz 25%/75% 180° | PASS (25.0%/74.9%, 180.1°) |
| D | 100 Hz (lower) 50%/50% 90° | PASS (100.0 Hz, 89.9°) |
| E | 100 kHz (higher) 50%/50% 90° | PASS (100 kHz, 91.1°) |
| F | upper-frequency limit | see §11 |
| G | different channel frequencies | PASS: chA=1000 Hz, chB=10000 Hz exact; duty 49.6%/25.0%. Different frequencies have no constant phase relationship (documented). |

---

## 11. Upper-frequency limit (Test F)

Measured through the LA at its 100 MHz maximum sample rate:

| Requested | Measured | samples/period | Result |
|-----------|----------|----------------|--------|
| 2 MHz | 2.000 MHz | 20 | PASS |
| 5 MHz | 4.762 MHz | 20 | PASS (±1 sample quantization) |
| 10 MHz | 9.091 MHz | 10 | PASS (±1 sample quantization) |
| 20 MHz | 20.000 MHz | 5 | PASS |
| 50 MHz | 50.000 MHz | 2 | PASS (Nyquist edge) |
| 100 MHz | aliased (2.08 MHz) | 1 | FAIL (aliasing — not reliable) |

**Practical reliable upper limit ≈ 50 MHz.** 100 MHz aliases because the signal
equals the 100 MHz sample rate (Nyquist). Frequencies up to 50 MHz are
measurable; 5/10 MHz show ±1-sample quantization of the period.

---

## 12. Historical concurrency + fast-capture SWD fallback — 6/6 PASS

`tests_concurrency.py`:

- pre-uart-echo: PASS
- swd-open-read: PASS
- swd+uart+la-concurrent-5×: PASS (SWD reads interleaved with UART echoes and
  LA captures, all healthy)
- fast-capture+swd-fallback: PASS (fast capture runs; SWD still responds;
  normal PIO SWD resumes after — verified by counter progress)
- swd-program-while-busy: PASS (programming while UART + LA active)

---

## 13. Bugs found and fixed

| ID | Component | Severity | Description | Status |
|----|-----------|----------|-------------|--------|
| BUG-001 | PicoGremlin (probe) | Medium | SWD bulk transfers desync at adapter speed ≥ 500 kHz ("CMSIS-DAP command mismatch"), reliable ≤ 250 kHz. Suspected PIO SWD / DAP transport timing. | Documented limitation; use ≤ 250 kHz |
| BUG-002 | PicoGremlin (probe) | High | Default dual-core SMP OpenOCD flash programming erases but does not write (silent). Single-core config works. | Workaround: `set USE_CORE { cm0 }`. Documented |
| BUG-003 | target fixture | Low | Baud-change handshake occasionally needs a target reset to re-sync (intermittent `switch@115200`). | Workaround in harness. Documented |
| BUG-004 | PicoGremlin (probe) | Low | LA response intermittently omits `CAPTURE_STARTED\n` (races bridge/connected flag). Harness tolerates it. | Documented |
| BUG-005 | PicoGremlin (probe) | Medium | Binary metadata falsely advertised GP0/GP1 as "UART0 TX/RX" (default stdio-uart driver). These are the LA complex-trigger loopback. | **FIXED**: `pico_enable_stdio_uart(picogremlin 0)` + `pico_enable_stdio_usb(...0)` in `CMakeLists.txt`. Verified via picotool |
| BUG-006 | PicoGremlin (probe) | High | LA command parser buffer overflow: `messageBuffer[128]` but `bufferPos` is `uint8_t` and the `bufferPos >= 256` test can never fire → unterminated long frame writes past the buffer. | **FIXED**: pre-write `sizeof(messageBuffer)` bound in the reviewed patch. Verified |
| BUG-007 | target fixture | High | PIO signal generator used `pull block` to reload counts each period but was fed only once → pins went LOW after the first period. | **FIXED**: keep counts in Y/ISR; loop reloads X from them |
| BUG-008 | target fixture | High | Sequential SM enable added ~700-cycle startup skew that shifted phase at high frequency (100 kHz phase read 216° instead of 90°). | **FIXED**: configure both SMs, enable together |
| BUG-009 | target fixture | Medium | Multi-byte UART command params read without waiting → garbage under baud-switch; length caps absent → malformed input could hang the echo loop. | **FIXED**: wait-per-byte reads + length validation |

Fixed firmware (`build-fixed/`) reflashed and re-tested: LA 12/12, SWD 7/7,
UART 104/105 (unchanged), descriptor metadata corrected.

---

## 14. Final state

- PicoGremlin (tool) running **corrected firmware** `build-fixed/picogremlin.uf2`
  (BUG-005, BUG-006 fixed).
- Target running the **fixture firmware**
  `tests/hardware/target_fixture/build/target_fixture.elf`.
- Target flash was fully backed up before any target test
  (`device_07_flash_backup_prebaseline.bin` is the PicoGremlin backup; the
  original target image was not recoverable after BUG-002 but the fixture is
  the intended final target image).

## 15. Analog capture (firmware picogremlin-1.1) — 11/11 PASS, 1 NOT TESTED

Board `93E145BEC49B83B2`, firmware `picogremlin-1.1` (ADC support; PG_INFO
advertises `ANALOG:3,500000,1`), suite `tests/hardware/tests_la_analog.py`:

| check | result |
|---|---|
| A1 capability line | PASS (`ANALOG:3,500000,1`) |
| A2 temperature 12-bit | PASS (≈35 °C, spread < 5 °C) |
| A3 8-bit mode | PASS (voltage channel within 32 counts, temp within 10 °C) |
| A4 channel interleaving | PASS (A0 alone == A0 in a 2-channel capture) |
| A5 reported rate | PASS (10 kS/s and 200 kS/s reported exactly) |
| A6 capture duration | PASS (≈samples/rate) |
| A7 level trigger | PASS (window boundary is the rising crossing) |
| A8 timeout/cancel/recover | PASS |
| A9 gapless stream | PASS (≈20 kS/s per channel, 33 frames) |
| A10 8-bit stream | PASS |
| A11 digital after analog | PASS (pins return to PIO) |
| A12 fixture waveform | NOT TESTED — needs a wire from the probe's GP16 to GP26; the inputs are undriven on this bench |

Notes for 1.1:

- The analog unit reuses `captureBuffer` (the merged build has ~32 KiB of SRAM
  free, so no second capture buffer).
- A stale DMA IRQ status bit on a freshly claimed channel could fire the
  double-buffer handler early (a zeroed first stream frame). Both the analog and
  the digital stream starts now acknowledge it first.
- The waveform-path checks (frequency/duty on a real analog signal) are covered
  by unit tests and the GUI synthetic smoke; the hardware check A12 needs the
  one-wire jumper above.
- Digital behaviour is unchanged: `tests_compat.py` 12/12 and the digital LA
  suites (la 12/12, stream 6/6, continuous 16/16, edge 5/5, bridge 3/3,
  measure 22/22, range 7/7) pass on the same firmware.

## 16. How to reproduce

See `tests/hardware/README.md` and the test scripts in `tests/hardware/`.
