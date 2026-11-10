# Merged firmware — architecture

## USB: one composite device, 4 host functions / 6 interfaces

We build **one** TinyUSB device with a single descriptor set. Interface order is
chosen so existing host tools bind without changes:

```
Interface 0      Vendor (bulk)      CMSIS-DAP v2   -> openocd / probe-rs
Interface 5      Vendor (no EP)     Pico SDK reset -> picotool
Interface 1+2    CDC (ACM)          UART bridge    -> /dev/ttyACM0 (probe console)
Interface 3+4    CDC (ACM)          Logic Analyzer -> /dev/ttyACM1 (gusmanb app)
```

- DAP stays at **interface 0** (old Keil/openocd assume the probe is itf 0).
- We use **CMSIS-DAP v2 (vendor/bulk)** only, and drop the v1 HID transport — v2 is
  what openocd and probe-rs prefer and it's higher throughput. (HID can be added back
  later as a 4th function if some old tool needs it.)
- The **MS OS 2.0 / BOS descriptor** from debugprobe is kept so Windows auto-binds
  WinUSB to both vendor interfaces (no Zadig needed). The two CDCs bind to usbser.sys
  automatically.
- One VID/PID for the whole composite: `0x2E8A/0x0009`, the RP2350 runtime identity
  used by the composite descriptor so `picotool` can locate its reset interface.

Files that own this: `firmware/usb_descriptors.c`, `firmware/tusb_config.h`.

### CDC instance mapping (important)
With `CFG_TUD_CDC=2`, TinyUSB numbers CDC instances by descriptor order:
- **CDC instance 0 = UART bridge** → debugprobe's `cdc_uart.c` works unchanged (it
  uses `tud_cdc_*` == instance 0).
- **CDC instance 1 = Logic Analyzer** → rather than switching `LogicAnalyzer.c`'s
  `tud_cdc_*` calls to `tud_cdc_n_*(1, ...)` directly (which would still be core1
  calling TinyUSB — see "Reality A" in DESIGN_DEEPDIVE.md §3), `cdc_transfer()`,
  `sendResponse()`, and `processUSBInput()` are routed through `la_bridge_write()`/
  `la_bridge_getc()` (`firmware/la_bridge.{h,c}`) under `#ifdef MERGED_BUILD`. Core0's
  `la_bridge_service()` is the only code that actually calls `tud_cdc_n_*(1, ...)`.
  `stdio_init_all()` and the standalone build's own clock/systick setup are skipped
  under `MERGED_BUILD` in `la_main()` (that dependency is removed in the merged
  build — we own the descriptors, not the SDK).

## Runtime model: bare super-loop, no FreeRTOS

Debugprobe supports a non-threaded path (`#define THREADED 0`). We use it, because
the LA is a bare super-loop and mixing it into the FreeRTOS scheduler is more work
than it's worth for a first bring-up.

```
core0 main loop:
    tud_task();                 // USB device servicing
    cdc_uart_task();            // probe UART <-> CDC0 bridge
    if (tud_vendor_available()) // CMSIS-DAP v2 command
        DAP_ProcessCommand(...);
    la_poll();                  // LA command parser on CDC1 (non-blocking)

core1 (launched at boot):
    la_capture_engine();        // PIO + DMA capture, posts results to a shared buffer
```

The LA's blocking `sleep_ms()` calls inside its capture/transfer path (see
`LogicAnalyzer.c` main loop) **must not** run on core0 or they will stall
`tud_task()` and kill USB/DAP. Hence capture moves to core1; core0 only drains the
finished buffer to CDC1 in non-blocking chunks (the LA already chunks against
`tud_cdc_write_available()`).

## What changes in each source tree

### From `debugprobe/` — keep, lightly edit
- `usb_descriptors.c` → **replaced** by merged version (this dir).
- `tusb_config.h` → **replaced** by merged version (`CFG_TUD_CDC=2`).
- `main.c` → **replaced** by merged `main.c` (non-threaded, adds LA).
- `sw_dp_pio.c`, `DAP_config.h`, `get_serial.c` → reused as-is.
- `probe.c`, `probe.pio` → **patched in CMake's build-directory overlay** using
  `firmware/patches/debugprobe-picogremlin.patch`: `pio0` → `PROBE_PIO`, `PROBE_SM`
  → a dynamically claimed SM (`pio_claim_unused_sm`/`pio_sm_unclaim`), and
  SWCLK/SWDIO pindirs set independently since they're not adjacent GPIOs (23/29) on
  this board. The upstream submodule remains clean.
- `cdc_uart.c` → **not used**; `picogremlin/firmware/cdc_uart.c` replaces it
  (polling, no FreeRTOS).
- `tusb_edpt_handler.c` → **excluded from the build** (FreeRTOS
  task/queue APIs incompatible with the no-RTOS runtime; DAP is handled via
  `tud_vendor_*` in `main.c` instead).
- Pin macros (`PROBE_PIN_*`, `PROBE_UART_*`, `PROBE_PIO`) → come from merged
  `board_merged_config.h`, reached via two shadow headers
  (`board_debug_probe_config.h`, `board_pico_config.h`) in `firmware/` that
  redirect `probe_config.h`'s `#ifdef DEBUG_ON_PICO` fallback there instead of the
  stock retail Debug Probe pinout.

### From `logicanalyzer/Firmware/LogicAnalyzer_V2/` — keep, edit
- Capture engine (`LogicAnalyzer_Capture.c`, `LogicAnalyzer.pio`) → the PIO program
  is reused. CMake copies `LogicAnalyzer_Capture.c` to the build overlay and applies
  the reviewed PIO2 fast-sampler and cleanup changes. `Shared_Buffers.c`/
  `Event_Machine.c` are WiFi-only and excluded. The patch and checked overlay
  transforms must be reviewed whenever upstream capture code changes.
- `LogicAnalyzer.c` → patched in CMake's build-directory overlay using
  `firmware/patches/logicanalyzer-picogremlin.patch`, under `#ifdef MERGED_BUILD`:
  (a) skip `stdio_init_all()` and the
  standalone build's own clock/systick setup in `la_main()`, (b) route
  `cdc_transfer()`/`sendResponse()`/`processUSBInput()` through
  `la_bridge_write()`/`la_bridge_getc()` instead of `tud_cdc_*` directly, (c) no-op
  the `INIT_LED()`/`LED_ON()`/`LED_OFF()` macros (no LED type is defined for this
  board — see `LogicAnalyzer_Board_Settings.h` below).
- `LogicAnalyzer_Board_Settings.h` → the `BUILD_PICO_2` block's `GPIO_LED`/`LED_IO 25`
  is guarded with `#ifndef MERGED_BUILD` (GP25 is now the probe's UART RX pin).
  Everything else in that block (`INPUT_PIN_BASE`, `MAX_CHANNELS`,
  `SUPPORTS_COMPLEX_TRIGGER`, `COMPLEX_TRIGGER_*_PIN`) is unchanged — it does
  **not** read from `board_merged_config.h`'s `LA_*` macros, which are informational
  only in this build (`// TODO(merge):` left in `board_merged_config.h`).
- `LogicAnalyzer_Board_Settings.h` → its `BUILD_PICO_2` branch currently keeps its
  own channel base/count definitions. Those values must remain aligned with
  `board_merged_config.h` and PINMAP.md; the integration header's matching macros
  are not yet its source of truth.
- Drop WiFi (`*_WiFi.c`, lwip) for the merged build — out of scope for v1.

## Resource claiming (implemented)
The PIO/DMA contention is resolved as follows (see PINMAP.md §4):

- **PIO instance** — PIO0 is exclusively LA-owned and is cleared by LA capture
  modes. The probe uses `PROBE_PIO=pio1`; autobaud uses the same board-selected
  block with a dynamic SM/DMA channel and DMA_IRQ_1. The LA fast trigger remains
  on PIO0, while its 8-instruction sampler uses PIO2, so capture never removes
  the probe or cancels autobaud.
- **PIO SM** — probe SWD claims its SM dynamically (`pio_claim_unused_sm` in
  `probe_init`, released via `pio_sm_unclaim` in `probe_deinit`); the LA already
  used `pio_claim_unused_sm`. No fixed-SM collisions.
- **DMA channels** — already dynamic on both sides (`dma_claim_unused_channel`).
- **DMA IRQ** — the LA installs an **exclusive** handler on `DMA_IRQ_0`; autobaud
  uses its shared handler on `DMA_IRQ_1`, so the two paths do not conflict.

These live in `board_merged_config.h` (`PROBE_PIO`, `PROBE_PIN_SWCLK`/`SWDIO` = 23/29
— not adjacent GPIOs, see PINMAP.md §2), the `board_debug_probe_config.h`/
`board_pico_config.h` shadow headers in `firmware/` (redirect `probe_config.h`'s
`#ifdef DEBUG_ON_PICO` fallback to `board_merged_config.h` instead of the stock
retail Debug Probe pinout), and `probe.c`/`probe.pio` (`pio0`→`PROBE_PIO` + dynamic
SM claim throughout). Normal and complex LA captures use PIO0. The generated
`LogicAnalyzer_Capture.c` overlay selects PIO2 for the fast sampler while PIO1
remains available to SWD and autobaud.

## Validation status

The integration layer contains the composite descriptor, core split, source overlays,
and three-PIO allocation described above. The checked-in hardware report is a
historical PIO1-fallback baseline; the A+ PIO2 allocation requires a fresh hardware
campaign. Build and hardware validation steps are in [CONTRIBUTING.md](../CONTRIBUTING.md).
