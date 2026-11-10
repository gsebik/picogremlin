# Merged Pico Probe + Logic Analyzer

One Pico, one USB cable, four host functions at once:

- **CMSIS-DAP v2** debug probe (openocd / probe-rs)
- **Pico SDK runtime reset** (`picotool` BOOTSEL and flash reboot)
- **UART bridge** — target serial console (CDC, `/dev/ttyACM0`)
- **Logic Analyzer** — gusmanb-compatible 24-channel capture (CDC, `/dev/ttyACM1`)

## Read these in order
1. **[PINMAP.md](PINMAP.md)** — the pin & resource compatibility analysis. The two
   stock firmwares collide on almost every GPIO; this is how they're separated.
   - RP2350A / Pico 2 W → full 24-ch LA + full probe, **no compromise**.
     Only four GPIO (23/24/25/29) are dedicated to the probe.
2. **[ARCHITECTURE.md](ARCHITECTURE.md)** — the composite USB layout, the
   no-FreeRTOS dual-core runtime model, and exactly what changes in each upstream tree.
3. **[DESIGN_DEEPDIVE.md](DESIGN_DEEPDIVE.md)** — how it works in depth: hw/sw block
   model, behaviour over time, and the dual-core safety analysis (USB-on-one-core +
   lock-free SPSC + the LA's `multicore_lockout`).

## firmware/ — the merge layer
| File | Status |
|------|--------|
| `board_merged_config.h` | ✅ Pico 2 W pin map + compile-time collision guard |
| `tusb_config.h`         | ✅ `CFG_TUD_CDC=2` + vendor (DAP v2) |
| `usb_descriptors.c`     | ✅ DAP + 2×CDC + runtime reset; WinUSB binds both vendor interfaces |
| `la_bridge.{h,c}`       | ✅ lock-free SPSC CDC bridge (core1 LA ↔ core0 USB on CDC1) |
| `main.c`                | ✅ core0 loop: clock init, launch `la_main` on core1, service bridge |
| `CMakeLists.txt`        | ✅ pulls both trees, `MERGED_BUILD`/`BUILD_PICO_2` defines |

The LA's `LogicAnalyzer.c` is wired in with `#ifdef MERGED_BUILD` guards (reversible):
`main()`→`la_main()` on core1, `stdio_init_all()` dropped, and all host I/O
(`getchar`, `printf`/`sendResponse`, `cdc_transfer`) routed through `la_bridge` on
**CDC instance 1**.

### Build and validation status

The project targets `pico2_w` (RP2350A) and produces `build/picogremlin.uf2` when
configured successfully. This repository does not record a completed hardware
bring-up or an automated firmware test suite; a build alone does not establish USB,
SWD, UART, or capture correctness. See [`../CONTRIBUTING.md`](../CONTRIBUTING.md)
for setup, build commands, and the hardware checklist.

```bash
cmake -B build -S picogremlin/firmware \
  -DPICO_SDK_PATH=$PICO_SDK_PATH \
  -DPICO_BOARD=pico2_w
cmake --build build -j$(nproc)
```

**Runtime note:** autobaud is a core0 polling PIO1/DMA_IRQ_1 state machine. Setting
CDC0 to 9728 baud starts it and temporarily takes ownership of UART RX. The LA
fast sampler runs on PIO2, so fast capture does not cancel autobaud or reconfigure
the UART bridge.

The pinout is confirmed against the `PICO_Gremlin_Rev.A_4L` schematic (see
[PINMAP.md](PINMAP.md)); runtime behavior still requires hardware validation.

## Next steps after the firmware boots
- Host GUI: PySide6 (Qt) app reusing the LA serial protocol + sigrok decoders
  (`../logicanalyzer/Software/decoders/`). Linux-first, Windows-supported.
