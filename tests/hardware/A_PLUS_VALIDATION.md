# A+ PIO2 Hardware Validation

Firmware: `build-aplus-final/picogremlin.uf2`

SHA-256: `1bdf37c743796e154c60d09447feb6b9f4a0fdff13b9571a1b96364367fe79bf`

Hardware: PicoGremlin RP2350A, USB serial `5C0559336694DAD6`, with the existing
RP2350 target fixture and GP0/GP1 trigger jumper.

## Results

| Check | Status | Result |
|---|---|---|
| Full flash backup before reflash | PASS | 2 MiB saved and verified |
| UF2 flash and verify | PASS | `picotool load -f -u -v -x` |
| USB composite enumeration | PASS | 6 interfaces, CDC0/CDC1 returned |
| SWD/GDB regression | PASS | 7/7 |
| LA edge/complex/fast/blast regression | PASS | 12/12 |
| Fast capture at 50 MHz | PASS | 12,000 samples |
| Fast capture at 100 MHz | PASS | 12,000 samples |
| Supported autobaud patterns | PASS | 7 supported cases plus recovery |
| Autobaud arbitrary 0x00-0xFF/PRNG | NOT SUPPORTED | documented divisor limitation |
| UART bridge matrix | FAIL | 3 intermittent initial/baud-switch checks; later recovery passed |
| LA measurement matrix | PASS | 22/22 |
| SWD/UART/LA sequencing | PASS | 6/6 |
| Fast-capture 100-cycle stress | NOT TESTED | 38/100 cycles passed before the 20-minute execution limit |
| True SWD transactions during active fast capture | PASS | five OpenOCD reads while a no-match fast trigger was armed |
| Fast capture plus active autobaud | PASS | both start orders; 474/512 `0x55` bytes and LA recovery |
| GP0/GP1 oscilloscope observation | NOT TESTED | no scope result recorded |

## Interpretation

The passing fast-capture tests exercise the A+ PIO2 sampler. A no-match fast
trigger remained armed while five OpenOCD SWD reads succeeded, then CDC1 cancel
and LA recovery passed. Autobaud also passed with fast capture active in both
start orders. The existing UART baud-switch intermittency and 100 MHz waveform
aliasing are known pre-existing limitations.
