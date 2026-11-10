# PicoGremlin

One **Pico 2 W (RP2350A)**, one USB cable, four host functions at once:

| Interface | Device (Linux) | Purpose |
|-----------|---------------|---------|
| CMSIS-DAP v2 (vendor bulk) | — | SWD debug probe — openocd / probe-rs |
| Pico SDK runtime reset | WinUSB | `picotool` BOOTSEL / flash reboot |
| CDC ACM | `/dev/ttyACM0` | UART bridge — target serial console (GP24/GP25, `uart1`) |
| CDC ACM | `/dev/ttyACM1` | 24-channel Logic Analyzer — gusmanb app |

The firmware merges [raspberrypi/debugprobe](https://github.com/raspberrypi/debugprobe)
and [gusmanb/logicanalyzer](https://github.com/gusmanb/logicanalyzer) into one composite
USB device. See [`picogremlin/ARCHITECTURE.md`](picogremlin/ARCHITECTURE.md) for the
design and [`CONTRIBUTING.md`](CONTRIBUTING.md) for build, overlay, and validation
guidance.

---

## Quick start

```bash
git clone --recurse-submodules https://github.com/gsebik/picogremlin.git
cd picogremlin
./pg install     # submodules + pico-sdk 2.3.1 + build  ->  build/picogremlin.uf2
./pg flash       # flash the probe (with it plugged in)
```

With more than one board attached, pass its serial (shown by `./pg list`):

```bash
./pg list                                    # serial, mode, ports of every board
PICOGREMLIN_SERIAL=13E1B67BABCD9816 ./pg flash
```

`./pg install` does the rest: initializes the submodules, clones **pico-sdk
2.3.1** to `$PICO_SDK_PATH` (default `~/pico-sdk`) if you don't already have a
compatible one, checks the toolchain, and builds. If a system package is missing
it stops and prints the exact install command for your OS.

```bash
./pg doctor      # just check the environment (prints fix hints for anything missing)
./pg all         # doctor -> build -> flash -> fixture -> fixture-flash -> test
```

Only `git`, `cmake`, `arm-none-eabi-gcc` and `python3` need to be installed first;
`./pg install` handles the SDK and the build. Install the prerequisites with:

```bash
# Arch
sudo pacman -S git cmake arm-none-eabi-gcc ninja python
# Debian / Ubuntu
sudo apt install git cmake gcc-arm-none-eabi ninja-build python3
# Fedora
sudo dnf install git cmake arm-none-eabi-gcc-cs ninja-build python3
# macOS (Homebrew)
brew install git cmake arm-none-eabi-gcc ninja python
```

`./pg doctor` prints the right command for your OS if anything is still missing.
The step-by-step sections below document what `pg` runs under the hood.

---

## Hardware requirement

**Raspberry Pi Pico 2 W (RP2350A)** — the carrier board (`PICO_Gremlin_Rev.A`) is
designed for this module.

Only four GPIO are dedicated to the probe: GP23 (SWCLK), GP29 (SWDIO), GP24/GP25
(UART bridge, `uart1`). Everything else — GP2–GP22 + GP26–GP28 (24 channels) plus
the hardware-jumpered GP0+GP1 trigger pair — belongs to the logic analyzer, exactly
as in the original standalone LA firmware. No hardware reset line — software reset
(`SYSRESETREQ` over SWD) is used instead. See
[`picogremlin/PINMAP.md`](picogremlin/PINMAP.md) for the full analysis.

---

## Build environment setup

### 1 — Toolchain

| Tool | Min version | Arch Linux | Debian / Ubuntu |
|------|-------------|------------|-----------------|
| `arm-none-eabi-gcc` | 10+ | `sudo pacman -S arm-none-eabi-gcc` | `sudo apt install gcc-arm-none-eabi` |
| `cmake` | 3.13 | `sudo pacman -S cmake` | `sudo apt install cmake` |
| `make` or `ninja` | any | bundled / `sudo pacman -S ninja` | `sudo apt install ninja-build` |
| `picotool` | any | `sudo pacman -S picotool` | build from source |

Verify:
```bash
arm-none-eabi-gcc --version   # should print a version
cmake --version               # should be ≥ 3.13
```

### 2 — Pico SDK

> **Requires pico-sdk ≥ 2.3.0** (2.3.1 is the pinned, tested release). The
> runtime-reset interface depends on the `pico_usb_reset` library / public
> `<pico/usb_reset.h>` header, first shipped in SDK 2.3.0. Older SDKs (≤ 2.2.0,
> including the pico VS Code extension's managed 2.1.1 / 2.2.0) keep those
> constants in `<pico/usb_reset_interface.h>` and fail the configure-time guard
> in `picogremlin/firmware/CMakeLists.txt` with instructions.
> Also needs **picotool ≥ 2.3.0**.

```bash
git clone --recurse-submodules --branch 2.3.1 https://github.com/raspberrypi/pico-sdk ~/pico-sdk
echo 'export PICO_SDK_PATH=~/pico-sdk' >> ~/.bashrc
source ~/.bashrc
```

If you already have the SDK without submodules:
```bash
git -C ~/pico-sdk submodule update --init
```

### 3 — Clone this repo

```bash
git clone --recurse-submodules https://github.com/gsebik/picogremlin.git
cd picogremlin
```

If you cloned without `--recurse-submodules`:
```bash
git submodule update --init
```

The PicoGremlin-specific edits to the two upstream submodules are stored as patch
files in `picogremlin/firmware/patches/`. CMake applies them only to copies in the
build directory, so `patch` must be installed and the submodules remain clean. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) before updating either submodule.

---

## The `pg` helper (command reference)

`pg` wraps every step below — it auto-detects the SDK, picotool and OpenOCD (each
overridable by env var; see `./pg help`), so no manual path wrangling is needed:

```bash
./pg install         # one-shot bootstrap: submodules + pico-sdk 2.3.1 + build
./pg doctor          # check toolchain + SDK (verifies pico_usb_reset), print status
./pg setup           # clone pico-sdk 2.3.1 to $PICO_SDK_PATH if missing/too old
./pg build           # -> build/picogremlin.uf2
./pg list            # serial, mode, ports, first-flashed date of attached boards
./pg udev install    # friendly /dev/picogremlin-<serial>-{debugprobe,logicanalyzer} links
./pg flash           # flash (auto-picks the only board; --all: every board)
./pg selftest        # flash + one-pass USB/SWD/UART/LA check via a wired fixture
./pg qualify         # one board end-to-end: flash → full HIL suite → target cleared
./pg fixture         # build the HIL target fixture
./pg fixture-flash   # program the fixture onto the target over SWD
./pg test [names]    # run the HIL suites, stream logs to logs/, print a summary
./pg all             # doctor -> build -> flash -> fixture -> fixture-flash -> test
```

### Board identity and the flash log

Each RP2350 reports its **OTP chip ID** as the USB serial, identical in BOOTSEL
mode and in the application, so a board is recognised before and after flashing.
`./pg flash` uses it to target the right board with `picotool --ser` when several
are attached, and records what was flashed:

- `boards.csv` (local, gitignored) — one row per board ever flashed: `chip_id,first_seen,label,notes`.
  Fill in `label`/`notes` to map serials to physical units.
- `logs/flash_log.csv` (per PC) — one row per flash attempt:
  `timestamp,chip_id,result,mode_before,fw_version,git,uf2_sha256,host,user`.

The HIL suites and OpenOCD use the same value through `PICOGREMLIN_SERIAL`.

The kernel's `/dev/serial/by-id` names describe the whole device, so both CDC
ports share one long prefix. `./pg udev install` (needs sudo) adds a readable
link per function instead:

```
/dev/picogremlin-<serial>-debugprobe      target UART bridge   (if01)
/dev/picogremlin-<serial>-logicanalyzer   logic analyzer       (if03)
```

`./pg list` and `./pg doctor` show these names once installed; without them
(`./pg udev status` reports which) `./pg list` falls back to tty and interface
numbers.

### One-command self-test

With the probe wired to the target fixture (wiring in
[`tests/hardware/README.md`](tests/hardware/README.md)), `./pg selftest` flashes
the probe and then does one quick pass over the whole tool: USB enumeration,
SWD connect, fixture programming, UART bridge identity + echo, and a 1 kHz/2 kHz
LA capture — about a minute, exit code 0 only if everything passes.
`./pg selftest --no-flash` runs the checks without reflashing. Stop any running
OpenOCD server first: only one program can claim the probe.

### Qualifying boards, one at a time

For bringing up several boards through one jig: plug in a board, run `./pg
qualify` (add `PICOGREMLIN_SERIAL=...` when several are attached), then swap in
the next board. It starts with a quick SWD check so an unpowered or unwired
target fails in seconds, then flashes the current build, runs the full HIL suite
through that probe, erases the target fixture so the jig is left clean, and
records the outcome next to the serial:

- `boards.csv` — the serial is registered on flash, as always
- `logs/qualify_log.csv` (per PC) — timestamp, chip_id, result, suites, fw/git, log path
- `logs/qualify-<serial>.log` — the complete suite output

`--no-flash` re-runs the suite on the board already in the jig. No separate
"test firmware" is needed: the suite exercises the exact binary that ships
(`build/picogremlin.uf2`), so a pass qualifies that build.
`./pg qualify --quick` swaps the full suite for the ~1-minute selftest
(`tests_selftest.py`) — the practical choice when qualifying boards one by one.

The manual steps below still work and document what `pg` runs under the hood.

## Host tools (`la` CLI + GUI)

The host-side logic-analyzer tools live in their own repository,
[picogremlin-la](https://github.com/gsebik/picogremlin-la). Install them with
`./pg tools install` or directly:

```bash
curl -fsSL https://raw.githubusercontent.com/gsebik/picogremlin-la/main/install.sh | bash
curl -fsSL .../install.sh | bash -s -- --no-gui   # CLI only (no Qt)

la info                                # probe identity (stock reply + PicoGremlin extras)
la cap -c 14,15 -t 14                  # one-shot capture; per-channel freq/duty/edges
la cap -c 0-7 -r 1M -n 5000 -o cap     # -> cap.csv + cap.vcd
la stream -c 14,15 -r 200k --seconds 1 -o rec   # gapless stream -> rec.csv/.vcd
la gui                                 # Qt GUI: pin picker, live scope, single-shot, Collect
```

- The probe is picked via `PICOGREMLIN_SERIAL` (or the only board attached).
  With several PicoGremlins attached, the GUI has a **board selector** by OTP
  serial (remembered across runs; `● openocd` marks a board whose debug
  interface another process holds — its LA/UART ports are still free), and the
  CLI takes `PICOGREMLIN_SERIAL=<serial>`.
- One-shot captures are fast (~0.7 s): the overlay is patched at build time to
  notice a finished capture within ~20 ms instead of the stock firmware's ~2 s
  polling. Stream modes report their true throughput (~100% of the requested
  rate when the link keeps up).
- Data packing differs: one-shot packs the requested channels into consecutive
  low bits; streams send the raw PIO word (bit *k* = channel *k*). The tool
  reports both correctly; the CSV/VCD exports use channel names.
- The GUI's **Collect** mode measures every triggered burst from the live
  stream (bounce count, settling time, min/avg/max pulse widths) into an
  exportable results table — everyday button-bounce work; **⚡ Single** gives
  MHz-sampling one-shots when you need to see very short glitches.
- The wire protocol (stock CMD 0..6 + PicoGremlin 0x40..0x48) is documented in
  [`docs/LA_PROTOCOL.md`](docs/LA_PROTOCOL.md).

## Analog inputs (ADC)

Firmware `picogremlin-1.1`+ also exposes the RP2350's ADC, so the same probe
can check the analog side of a lab circuit:

- **A0 / A1 / A2 = GP26 / GP27 / GP28** — the same header pins as digital
  channels 21/22/23 (digital *or* analog at a time), plus the on-chip
  **temperature** sensor;
- **0–3.3 V only** — these pins are not 5 V tolerant; divide/clamp anything
  larger;
- 12-bit (0.8 mV) or 8-bit, **500 kS/s total** shared between the enabled
  inputs (1 channel = 500 kS/s, 3 channels ≈ 166 kS/s each);
- one-shot captures, gapless streaming and level triggering, driven from the
  host tools ([picogremlin-la](https://github.com/gsebik/picogremlin-la)):
  `la acap`, `la astream`, `la acheck` and the GUI's **analog** tab;
- wire protocol: [`docs/LA_PROTOCOL.md`](docs/LA_PROTOCOL.md).

```bash
la acap -a 0,1 -r 100k -n 2000          # A0/A1 one-shot -> summary, CSV/VCD
la acheck -a A0 --mean '1.65V±5%'       # PASS/FAIL a lab measurement (exit 1 on FAIL)
```

The temperature input's slope is only ~1.7 mV/°C, so use 12-bit for it (8-bit
is coarse there); 8-bit is fine for voltages and doubles the stream headroom.

## Build

```bash
cmake -B build -S picogremlin/firmware \
  -DPICO_SDK_PATH=$PICO_SDK_PATH \
  -DPICO_BOARD=pico2_w

cmake --build build -j$(nproc)
```

The build produces `build/picogremlin.uf2`.

To enable 400 MHz turbo mode (validate RP2350A stability first), uncomment in `CMakeLists.txt`:
```cmake
# TURBO_MODE=1              # uncomment for 400 MHz — validate stability on RP2350A first
```

---

## Flash

### Method A — picotool runtime update (no button press once PicoGremlin is running)

```bash
picotool reboot -f -u
picotool load build/picogremlin.uf2 -u -v -x
```

PicoGremlin exposes the Pico SDK runtime-reset interface while its firmware is
running, so `picotool` can reboot it into BOOTSEL and flash an update without a
physical button press.

### Method B — BOOTSEL drag and drop recovery

1. Hold **BOOTSEL** while connecting USB (or press BOOTSEL + RESET on boards that have it)
2. A drive named `RP2350` mounts on your computer
3. Copy `build/picogremlin.uf2` onto the drive — it flashes and reboots automatically

Use this recovery method if the runtime-reset interface is unavailable, such as
after flashing incompatible firmware.

### Method C — openocd via another probe

```bash
openocd -f interface/cmsis-dap.cfg -f target/rp2350.cfg \
  -c "program build/picogremlin.elf verify reset exit"
```

---

## Verify after flashing

```bash
# Intended device identity: 2e8a:0009, with 6 interfaces
lsusb -d 2e8a:0009 -v | grep -E "bNumInterfaces|bInterfaceClass|iInterface"

# CMSIS-DAP: connect to a target over SWD
openocd -f interface/cmsis-dap.cfg -f target/rp2350.cfg -c "init; reset halt; exit"

# UART bridge: open a serial terminal to the target
picocom -b 115200 /dev/ttyACM0

# Logic analyzer: open the gusmanb app and connect to /dev/ttyACM1
```

---

## Pinout (confirmed — PICO_Gremlin_Rev.A schematic, corrected 2026-08-14)

| GPIO | Signal | Notes |
|------|--------|-------|
| 0 + 1 | LA advanced/complex trigger | tied together in hardware (jumpered loopback pair) |
| 2–22 | LA channels 0–20 | `INPUT_PIN_BASE=2`; wired to IC1 GPIO2–GPIO22 |
| 23 | target SWCLK | probe, PIO1, dynamically-claimed SM |
| 24 | probe UART bridge TX | `uart1` TX |
| 25 | probe UART bridge RX | `uart1` RX |
| 26–28 | LA channels 21–23 | wired to IC1 GPIO26–GPIO28 |
| 29 | target SWDIO | probe, PIO1, dynamically-claimed SM |

SWCLK/SWDIO are not adjacent GPIOs on this board revision. No hardware reset line
and no LEDs are populated — see [`picogremlin/PINMAP.md`](picogremlin/PINMAP.md).

---

## Repository layout

```
picogremlin/          ← repo root
├── README.md         ← this file
├── LICENSE           ← GPL-3.0 (third-party notices: THIRD_PARTY.md)
├── CONTRIBUTING.md   ← upstream-sync workflow + review gates
├── pg                ← build / flash / qualify / selftest / udev helper
├── docs/             ← LA_PROTOCOL.md (wire protocol for the host tools)
├── debugprobe/       ← submodule: raspberrypi/debugprobe
├── logicanalyzer/    ← submodule: gusmanb/logicanalyzer
├── udev/             ← udev rules for friendly /dev/picogremlin-* links
├── tests/hardware/   ← HIL suites, RP2350 target fixture, validation reports
└── picogremlin/      ← integration layer (our firmware + design docs)
    ├── firmware/     ← CMakeLists.txt + sources + patches/ + overlay/ fragments
    ├── ARCHITECTURE.md
    ├── PINMAP.md
    ├── DESIGN_DEEPDIVE.md
    └── README.md
```

---

## Known limitations (v1)

- **Autobaud:** setting CDC0 to 9728 baud starts a polling PIO1/DMA (`DMA_IRQ_1`)
  measurement. It is independent of LA capture. Autobaud assumes at least one observed low pulse is
  a single UART bit — patterns such as `0x00..0xFF` or pseudo-random data can
  produce a wrong divisor (see `tests/hardware/HARDWARE_VALIDATION.md` §7).
- **Analog inputs share pins with digital channels 21–23** (GP26–28) and accept
  0–3.3 V only. Analog and digital captures are exclusive, and the ADC's
  500 kS/s is the *total* across enabled inputs (12-bit streams top out near
  350 kS/s because of the USB link).
- **PIO allocation:** RP2350A PIO0 hosts the LA capture/trigger programs, PIO1 hosts
  SWD and autobaud, and PIO2 hosts the fast LA sampler. The GP0/GP1 jumper is a
  physical trigger link; GP0 drives it and GP1 is sampled as an input.
- **SWD bulk transfers:** use `adapter speed 250` (or lower) and a single-core
  OpenOCD config (`tests/hardware/pg_target_single.cfg`). Sustained SWD reads at
  ≥ 500 kHz can hit a CMSIS-DAP transport desync, and the default dual-core SMP
  RP2350 config can erase flash without writing (see `tests/hardware/` for the
  validated workaround).

## Hardware validation

A hardware-in-the-loop test campaign has been executed against a real Pico 2 W
tool and an RP2350 target. Results, reproduction instructions and the bug list
are in [`tests/hardware/HARDWARE_VALIDATION.md`](tests/hardware/HARDWARE_VALIDATION.md).
Coverage: USB descriptor stability, picotool runtime update, SWD programming +
GDB debugging, the UART bridge at 9600–921600 baud, auto-baud, the logic analyzer
(identity, channel map, edge/complex/fast/blast capture, cancel, large buffers),
frequency/duty/phase measurement, the two-channel generator upper-frequency limit,
and the historical concurrent SWD + UART + analyzer baseline. The existing report
predates the A+ PIO2 allocation and must not be treated as validation of this build.
The build no longer links the default stdio-uart driver, so the firmware binary
no longer falsely advertises GP0/GP1 as a UART console.

## License

PicoGremlin is released under the **GNU General Public License v3.0** — see
[`LICENSE`](LICENSE). The firmware is a combined work of the GPL-3.0
[LogicAnalyzer](https://github.com/gusmanb/logicanalyzer) capture engine and the
MIT [Raspberry Pi debugprobe](https://github.com/raspberrypi/debugprobe), so the
built binary is distributed under the GPL-3.0. All third-party components,
their licenses and where their notices live are listed in
[`THIRD_PARTY.md`](THIRD_PARTY.md).
