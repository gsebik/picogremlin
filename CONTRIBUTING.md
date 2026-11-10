# Contributing to PicoGremlin

PicoGremlin integrates `debugprobe` and `logicanalyzer` for the Raspberry Pi Pico 2 W
(RP2350A). The supported board, pin assignment, and shared-resource contract are in
[picogremlin/PINMAP.md](picogremlin/PINMAP.md); read it before changing GPIO, PIO, DMA,
or USB-related code.

## Prerequisites and checkout

Install Git, CMake 3.13 or newer, `patch`, an ARM bare-metal toolchain
(`arm-none-eabi-gcc`), and Make or Ninja. Install a complete
[Raspberry Pi Pico SDK](https://github.com/raspberrypi/pico-sdk), including its
submodules, and set `PICO_SDK_PATH` to that checkout.

```bash
git clone --recurse-submodules https://github.com/gsebik/picogremlin.git
cd picogremlin
git submodule update --init --recursive
git -C "$PICO_SDK_PATH" submodule update --init
```

The firmware overlays are reviewed against the exact submodule revisions recorded by
the superproject. Do not advance either submodule incidentally: configure will reject
an unexpected revision. A leading `-` in `git submodule status` means the checkout is
incomplete.

## Configure, build, and clean

Use an out-of-source build and explicitly select the only supported board:

```bash
cmake -S picogremlin/firmware -B build \
  -DPICO_SDK_PATH="$PICO_SDK_PATH" \
  -DPICO_BOARD=pico2_w
cmake --build build -j"$(nproc)"
```

The expected artifact is `build/picogremlin.uf2`. For an incremental clean, run
`cmake --build build --target clean`. If CMake configuration, SDK, or submodule inputs
change, remove only the repository-local `build/` directory and configure again; do
not delete or regenerate the upstream submodules.

## Validation

There is no repository-owned automated firmware test suite and no recorded hardware
bring-up result. A successful build is necessary but does not validate USB, SWD, UART,
or capture timing. Before submitting a firmware change, run at least:

```bash
cmake --build build -j"$(nproc)"
git diff --check
git submodule status
```

When hardware is available, record the board revision, host OS/tool versions, and
results for the affected items:

- USB enumeration: six interfaces — CMSIS-DAP v2, UART CDC, logic-analyzer CDC, and
  the Pico SDK runtime-reset interface.
- CMSIS-DAP connection and reset of a known target.
- Bidirectional UART bridge on CDC0 (GP24 TX / GP25 RX).
- Logic-analyzer control and a normal capture on CDC1.
- Fast capture on PIO2 while SWD remains active on PIO1, including return to normal
  capture operation.
- Autobaud transition (CDC0 line rate `9728`) and return to a usable UART bridge.

## Hardware and resource safety

- Pico 2 W (RP2350A) is the only supported target. Do not infer a pinout from a
  generic Pico board.
- GP23 is SWCLK, GP29 is SWDIO, and GP24/GP25 are UART1 TX/RX. GP2–GP22 and
  GP26–GP28 are logic-analyzer channels; GP0/GP1 are the jumpered complex-trigger
  pair. There is no hardware reset or populated status LED.
- Preserve dynamic PIO state-machine and DMA-channel claims. PIO0 is LA-owned;
  SWD and autobaud use PIO1; the fast sampler uses PIO2.
- The GP0/GP1 jumper is a single physical trigger net. GP0 is the only output
  driver and GP1 is sampled as an input by the capture state machine.
- Autobaud is started by setting CDC0 to 9728 baud. It owns UART RX, a PIO1 state
  machine, a DMA channel, and `DMA_IRQ_1`; fast capture must not cancel it.

## Development and source overlays

`picogremlin/firmware/` is the integration layer. Its CMake configuration copies a
small set of upstream sources into `build/picogremlin-overlays/` and applies the
patches in `picogremlin/firmware/patches/`. It applies the PIO2 fast-capture and
checked DMA cleanup changes in the copied `LogicAnalyzer_Capture.c`; this keeps the
submodules clean.

- Make integration changes in `picogremlin/firmware/` or its patch files, not by
  committing local edits inside `debugprobe/` or `logicanalyzer/`.
- When an upstream update is intentional, update the submodule pointer, the pinned
  revision in `firmware/CMakeLists.txt`, and the matching overlay patch together.
  Reconfigure from a fresh build directory and inspect the resulting overlay.
- Treat checked CMake overlay anchors as integration code, not incidental build glue.
  An upstream source change must fail configuration or be reviewed in the generated
  overlay. Verify `capturePIO = LA_CAPTURE_PIO_FAST` resolves through
  `LA_FAST_CAPTURE_PIO=pio2`, the PIO0 trigger, and blast DMA ownership.
- Keep `TODO(merge)` comments until the corresponding behavior is implemented and
  validated. Do not copy an upstream file over an integration-owned replacement.

## Commits and pull requests

Keep commits focused and use an imperative summary (for example, `fix: isolate LA
fast capture on PIO2`). Separate upstream synchronization from functional changes.
Pull requests should explain the affected interface or resource, list build and
hardware validation performed, identify untested paths, and update the relevant
README or design document when user-visible behavior changes.
