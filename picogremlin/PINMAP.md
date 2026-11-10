# Merged Probe + Logic Analyzer — Pin & Resource Map

Goal: one Pico 2 W that enumerates as a **single composite USB device** exposing
**CMSIS-DAP (debug) + Logic Analyzer (CDC)** simultaneously.

This document is the hardware contract. The firmware (`firmware/board_merged_config.h`)
must match these tables exactly. All assignments confirmed against **PICO_Gremlin_Rev.A_4L**
schematic.

---

## 1. The core problem: the two stock firmwares want the same pins

Stock pin usage on a Pico / Pico 2 (RP2350A, 26 header GPIO), using each project's
own upstream defaults:

| GPIO      | Logic Analyzer (gusmanb)                             | Debugprobe (`board_pico_config.h`) |
|-----------|------------------------------------------------------|------------------------------------|
| 0         | complex trigger OUT                                  | —                                  |
| 1         | complex trigger IN                                   | RESET                              |
| 2         | channel 0                                            | **SWCLK**                          |
| 3         | channel 1                                            | **SWDIO**                          |
| 4         | channel 2                                            | **UART TX**                        |
| 5         | channel 3                                            | **UART RX**                        |
| 6–22      | channels 4–20                                        | —                                  |
| 23,24,25  | *(skipped — SMPS/VBUS-sense/LED, internal)*          | 25 = USB LED                       |
| 26,27,28  | channels 21,22,23                                    | —                                  |
| 29        | *(internal — VSYS ADC, not on header)*               | —                                  |

**The LA's channel map is `{2..22, 26..28}` — not a clean contiguous block.** The
PIO reads GPIO 2–25 as a wide snapshot, but physical channel inputs are wired only
to `{2..22, 26..28}`.

This table is the two projects' *stock upstream defaults*, which collide almost
everywhere — it is **not** how the PicoGremlin board is actually wired. §2 gives the
real, schematic-confirmed pinout.

---

## 2. Resolution: the probe gets exactly four dedicated GPIO

**Corrected 2026-08-14, confirmed against the PICO_Gremlin_Rev.A_4L schematic by
the hardware owner.** The probe does **not** use GP0/GP1 as an earlier draft of
this document assumed. Instead:

- **GP23** → target SWCLK
- **GP29** → target SWDIO
- **GP24** → probe UART bridge TX (`uart1`)
- **GP25** → probe UART bridge RX (`uart1`)
- **GP0 + GP1** are tied together in hardware (a jumpered loopback pair) and are
  dedicated to the LA's advanced/complex trigger mode — **not** used by the probe.

The 24 LA capture channels (`{2..22, 26..28}`) are completely unchanged from the
original standalone LA firmware — every channel pin the gusmanb software used
before is still exactly where it was. Only the probe's four pins moved off the
LA's territory.

| GPIO      | Signal                              | Owner       | Notes                                                   |
|-----------|--------------------------------------|-------------|----------------------------------------------------------|
| 0 + 1     | LA advanced/complex trigger          | LA          | hardware-jumpered together; `COMPLEX_TRIGGER_OUT_PIN`/`IN_PIN` |
| 2–22      | LA channels 0–20                     | LA          | unchanged `pinMap`; `INPUT_PIN_BASE=2`                    |
| 23        | SWCLK                                | Probe       | PIO1, dynamically-claimed SM                              |
| 24        | UART TX                              | Probe       | `uart1` TX (`IO_BANK0_GPIO24_CTRL_FUNCSEL_VALUE_UART1_TX`) |
| 25        | UART RX                              | Probe       | `uart1` RX (`IO_BANK0_GPIO25_CTRL_FUNCSEL_VALUE_UART1_RX`) |
| 26–28     | LA channels 21–23                    | LA          | unchanged `pinMap`                                        |
| 29        | SWDIO                                | Probe       | PIO1, dynamically-claimed SM                               |

**SWCLK (23) and SWDIO (29) are not adjacent GPIOs** — `probe.pio`'s pin-direction
setup sets each independently rather than assuming an offset+stride pair (see
`probe_sm_init()` in `debugprobe/src/probe.pio`).

No free GPIO remain for:
- A hardware reset line (software reset via SWD `SYSRESETREQ` is used instead)
- Status LEDs — none are populated on this board revision. `PROBE_USB_CONNECTED_LED`,
  `PROBE_DAP_CONNECTED_LED`, `PROBE_DAP_RUNNING_LED`, the UART TX/RX LEDs, and the
  LA's own `GPIO_LED`/`LED_IO` (which would otherwise claim GP25 — now the UART RX
  pin) are all left undefined/guarded out for this board.

---

## 3. USB device layout

The composite device exposes six interfaces (four host functions) on one USB cable:

| Interface        | USB class      | Linux device    | Purpose                       |
|------------------|----------------|-----------------|-------------------------------|
| Vendor (bulk)    | Vendor-specific | —              | CMSIS-DAP v2 (openocd / probe-rs) |
| Vendor (no EP)   | `ff/00/01` | WinUSB | Pico SDK runtime reset (`picotool`) |
| CDC ACM 0        | CDC            | `/dev/ttyACM0`  | UART bridge — target serial console |
| CDC ACM 1        | CDC            | `/dev/ttyACM1`  | Logic Analyzer (gusmanb app)  |

ttyACM0 carries real UART data on the Pico 2 W (GP24 TX / GP25 RX, `uart1`,
`PROBE_CDC_UART` defined in `board_merged_config.h`).

---

## 4. Shared silicon resources

### PIO

RP2350A has **three PIO blocks** (PIO0, PIO1 and PIO2), each with 4 state machines.

| Block | Assignment                                              |
|-------|---------------------------------------------------------|
| PIO0  | LA normal/complex/blast capture and fast trigger         |
| PIO1  | Probe SWD plus polling autobaud (`probe.pio`, dynamic SMs) |
| PIO2  | LA fast-capture sampler (`FAST_CAPTURE`, dynamic SM)    |

Complex/advanced trigger is **enabled** in the merged build (`SUPPORTS_COMPLEX_TRIGGER`,
GP0/GP1 — the hardware-jumpered pair, unrelated to the probe). GP0 is driven by the
trigger SM and GP1 is sampled by the capture SM; GP1 is never configured as an output.
The probe's SM comes from PIO1, claimed dynamically in `probe_init()` via
`pio_claim_unused_sm(PROBE_PIO, true)` and released in `probe_deinit()` via
`pio_sm_unclaim()` — never a hardcoded index.

### DMA

RP2350A has 16 DMA channels. Both subsystems claim channels dynamically via
`dma_claim_unused_channel` — no hard-coded channel numbers.

| Owner | IRQ          | Notes                                           |
|-------|--------------|-------------------------------------------------|
| LA    | `DMA_IRQ_0`  | exclusive; gusmanb capture ring                 |
| Probe autobaud | `DMA_IRQ_1`  | dynamically claimed while autobaud is active |

Fast capture uses PIO2 and does not cancel or reconfigure autobaud on PIO1.

### Cores

| Core  | Duties                                                   |
|-------|----------------------------------------------------------|
| Core0 | `tud_task`, `dap_task`, `cdc_task` (no-op), `la_bridge_service` |
| Core1 | `la_main` — LA capture engine (PIO + DMA); no TinyUSB calls |

### Clock

Default: `set_sys_clock_khz(200000, true)` (200 MHz). `TURBO_MODE` is **commented
out** by default in `CMakeLists.txt` — validate RP2350A stability before enabling.
USB runs from the independent 48 MHz USB PLL and is unaffected by the sys-clock
change. The SWD PIO clkdiv is derived from `clk_sys` and adapts automatically.

---

## 5. Verdict

PicoGremlin supports the Pico 2 W (RP2350A) only: 24 logic-analyzer channels,
CMSIS-DAP SWD on GP23/29, and a UART bridge on GP24/25.
