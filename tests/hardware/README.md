# PicoGremlin hardware test suite

Automated hardware-in-the-loop tests for the PicoGremlin probe + an attached
RP2350 target. Everything here was executed on real hardware (see
`HARDWARE_VALIDATION.md` for the full measured results).

## Layout

```
tests/hardware/
├── HARDWARE_VALIDATION.md   ← full measured results and bug report
├── README.md                ← this file
├── pg_cmsis_dap.cfg         ← CMSIS-DAP interface (serial-selected)
├── pg_target_single.cfg     ← single-core RP2350 SWD config (use this, not the
│                               default dual-core SMP config — see BUG-002)
├── picogremlin_hil.py       ← dependency-free Python HIL library (std-lib only)
├── tests_uart.py            ← UART bridge matrix (baud/pattern/stream/burst)
├── tests_autobaud.py        ← auto-baud via CDC0 9728 line rate
├── tests_swd.py             ← SWD program + GDB debug session
├── tests_selftest.py        ← one-pass selftest: USB/SWD/fixture/UART/LA
├── tests_la.py              ← LA identity / channel map / capture modes
├── tests_measure.py         ← freq / duty / period / phase matrix + Test A–E
├── tests_range.py           ← upper-frequency limit (Test F) + diff freq (Test G)
├── tests_concurrency.py     ← SWD+UART+LA sequencing and PIO2 fast-capture recovery
├── tests_la_channels.py     ← 24-channel LA GPIO campaign (T1–T7, checkpointed)
├── CHANNEL_VALIDATION.md    ← 24-channel LA results + wiring findings
├── CHANNEL_TEST_GUIDE.md    ← test description for the 24-channel campaign
├── DEBUGPROBE_SYNC_VALIDATION.md ← debugprobe upstream sync/build/HIL results
├── target_fixture/          ← RP2350 target test firmware (UART + GP16/17 PIO)
└── artifacts/               ← JSON results + logs (generated)
```

## Prerequisites

- Linux with the USB-connected PicoGremlin (`2e8a:0009`) and an RP2350 target
  wired: target SWD to GP23/GP29, target UART0 (GP0/GP1) to GP24/GP25, target
  GP16→probe GP16 (LA ch14), target GP17→probe GP17 (LA ch15), common ground.
- Pico SDK (`PICO_SDK_PATH`), `arm-none-eabi-gcc`, `cmake`.
- Raspberry Pi OpenOCD fork with `target/rp2350.cfg`, on `$PATH` as `openocd`
  (or point `$OPENOCD` at the binary and `$OPENOCD_SCRIPTS` at its scripts dir).
- `gdb-multiarch` for the debug test (or set `$GDB`).
- `picotool` for reprogramming the PicoGremlin tool itself.

All the Python here uses only the standard library (termios, fcntl, struct,
select, json). No pyserial / pyusb needed.

## Build the target fixture

```bash
cmake -S tests/hardware/target_fixture -B tests/hardware/target_fixture/build \
  -DPICO_SDK_PATH=$PICO_SDK_PATH -DPICO_BOARD=pico2
cmake --build tests/hardware/target_fixture/build -j$(nproc)
```

Program the target through the PicoGremlin probe (NOT picotool — that is only
for the PicoGremlin tool itself):

```bash
# openocd from $PATH; scripts from $OPENOCD_SCRIPTS; board from $PICOGREMLIN_SERIAL
openocd ${OPENOCD_SCRIPTS:+-s "$OPENOCD_SCRIPTS"} -f tests/hardware/pg_target_single.cfg \
  -c "program tests/hardware/target_fixture/build/target_fixture.elf verify reset"
```

## Build + flash the PicoGremlin tool

```bash
cmake -S picogremlin/firmware -B build-fixed \
  -DPICO_SDK_PATH=$PICO_SDK_PATH -DPICO_BOARD=pico2_w
cmake --build build-fixed -j$(nproc)
picotool reboot -f -u
picotool load build-fixed/picogremlin.uf2 -u -v -x
```

## Run the tests

```bash
cd tests/hardware
python3 tests_la.py --out artifacts          # logic analyzer
python3 tests_swd.py --out artifacts          # SWD + GDB
python3 tests_measure.py --out artifacts      # freq/duty/phase matrix + Test A-E
python3 tests_range.py --out artifacts        # Test F (upper limit) + Test G
python3 tests_concurrency.py --out artifacts  # concurrent SWD+UART+LA
python3 tests_uart.py --out artifacts         # UART matrix (takes a few minutes)
python3 tests_autobaud.py --out artifacts     # auto-baud
```

Each writes a `*_test_results.json` into `artifacts/` and exits non-zero if any
check is not PASS. `HARDWARE_VALIDATION.md` records the expected/measured values.

For a quick all-in-one check (also available as `./pg selftest`, which flashes
the probe first):

```bash
python3 tests_selftest.py --out artifacts/selftest   # add --skip-swd to skip the wired-target steps
```

## Important SWD note (BUG-002)

Use `pg_target_single.cfg` (single core) and `adapter speed 250` for target
programming. The default dual-core SMP rp2350.cfg silently fails to write flash,
and adapter speeds ≥ 500 kHz cause a CMSIS-DAP transport desync (BUG-001).
