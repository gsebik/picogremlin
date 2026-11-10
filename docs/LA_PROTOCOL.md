# PicoGremlin LA wire protocol

The logic analyzer is CDC interface **if03** (if01 is the UART bridge). This
document covers the frames, commands and replies. The stock command set
(CMD 0..6) is byte-compatible with the upstream gusmanb LogicAnalyzer
firmware; the PicoGremlin extensions live in CMD 0x40..0x48.

Implementations: the host tools live in their own repository
(`picogremlin-la` — `la` CLI + Qt GUI); the firmware-side HIL harness is
`tests/hardware/picogremlin_hil.py`.

## Framing (host → device)

```
55 AA <cmd> <escaped payload> AA 55
```

- payload bytes `0xAA`, `0x55` and `0xF0` are escaped as `F0 <byte XOR F0>`
- commands without payload: `55 AA <cmd> AA 55`
- a bare `0xFF` byte cancels a one-shot capture that is still waiting for its
  trigger

## Replies (device → host)

- ASCII replies are newline-terminated (`\n`) lines
- binary payloads follow their documented header immediately, unframed

## Stock commands (0..6)

| CMD | Request | Reply |
|---|---|---|
| 0 | identity | 5 lines: `LOGIC_ANALYZER_<board>_<fw>`, `FREQ:<hz>`, `BLASTFREQ:<hz>`, `BUFFER:<samples>`, `CHANNELS:<n>` |
| 1 | capture (`CAPTURE_REQUEST`) | `CAPTURE_STARTED\n` then `[u32 count][count × width bytes][u8 stampLen][stampLen × u32 stamps]` |
| 2 | WiFi settings | WiFi builds only; non-WiFi builds answer `ERR_UNKNOWN_MSG` |
| 3 | power status | WiFi builds only; non-WiFi builds answer `ERR_UNKNOWN_MSG` |
| 4 | reboot to BOOTSEL | `RESTARTING_BOOTLOADER\n` |
| 5 / 6 | LED blink on / off | `BLINKON\n` / `BLINKOFF\n` |
| other | — | `ERR_UNKNOWN_MSG\n` |

`CAPTURE_STARTED` may be omitted when the USB link was momentarily
disconnected; parsers must tolerate both cases (check the first bytes for a
response line, else treat them as the `u32` count).

### CAPTURE_REQUEST (CMD 1, 0x41, 0x43..0x48)

48 bytes, packed, little-endian:

| offset | type | field |
|---|---|---|
| 0 | u8 | trigger type (0 simple, 1 complex, 2 fast, 3 blast) |
| 1 | u8 | trigger channel (0..23) |
| 2 | u8 | inverted / loop count |
| 3 | — | pad |
| 4 | u16 | trigger value (complex trigger) |
| 6 | u8[24] | channel list (padded) |
| 30 | u8 | channel count |
| 31 | — | pad |
| 32 | u32 | sample rate (Hz) |
| 36 | u32 | pre-trigger samples |
| 40 | u32 | post-trigger samples |
| 44 | u8 | loop count |
| 45 | u8 | measure |
| 46 | u8 | capture mode: 0 = 8-bit, 1 = 16-bit, 2 = 24-bit |
| 47 | — | pad |

One-shot captures pack the requested channels into consecutive low bits of
each sample word. Stream modes send the raw PIO word, where bit *k* is
channel *k* (channel *k* is GPIO 2+k; channels 21..23 are GP26..28).

## PicoGremlin extensions (0x40..0x48)

| CMD | Request | Reply / stream |
|---|---|---|
| 0x40 | PG_INFO | `PICOGREMLIN:<version>\nSTREAM:1\n` (+ `BRIDGE_TX:0\|1\n` when the UART bridge is enabled) |
| 0x41 | STREAM_START: batch stream, request = `CAPTURE_REQUEST` | `STREAM_STARTED\n`, then per batch `[u32 byte length][raw capture bytes]`; stop with 0x42 |
| 0x42 | STREAM_STOP | `STREAM_STOPPED\n`; the only way to stop 0x41/0x43..0x48 |
| 0x43 | STREAM_CONTINUOUS: gapless double-buffered stream; request = `CAPTURE_REQUEST`, `postSamples` = half-buffer size | `STREAM_CONT_STARTED\n`, then `[u32 bytes][payload]` per half-buffer |
| 0x44 | STREAM_CONT_TRIGGERED: as 0x43, but arms on the trigger first | same as 0x43 |
| 0x45 | STREAM_DELTA: sends only level changes; request = `CAPTURE_REQUEST` | `STREAM_CONT_STARTED\n`, then `[u32 seq]` followed by `[u32 index][u32 word]` pairs, ending with `[0xFFFFFFFF][0xFFFFFFFF]`; a sample is sent at least every 4096 samples even when unchanged |
| 0x46 | STREAM_DELTA_TRIGGERED: as 0x45, but arms on the trigger first | same as 0x45 |
| 0x47 | STREAM_EDGE: RAM-only PIO change-detect capture (diagnostics); request = `CAPTURE_REQUEST` | binary header `[13 × u32]` = `'EDG1', n, dup, single, multi, maxbits, invalid, fdebug, fifo, 0, div, lo, span`, then `n × [u32 dt][u32 state]`, then `EDGE_DONE\n` |
| 0x48 | STREAM_EDGE_LIVE: PIO change-detect stream | `STREAM_EDGE_STARTED\n`, then `[u32 seq][u32 n]` + `n × [u32 dt][u32 state]`; on 0x42: `EDGE_SUMMARY total=… dup=… single=… multi=… maxbits=… invalid=… fdebug=… fifo=… lo=… span=…\n` then `STREAM_STOPPED\n` |

Edge streams (0x47/0x48): the PIO pushes the input word only when it changes.
`dt` is in TIMER1 cycles (clk_sys, ~5 ns at 200 MHz); `state` carries the
selected contiguous pin span, and `lo`/`span` (0x47 header / `EDGE_SUMMARY`)
describe which window that is. `div` is the PIO clock divider.

Streams stop when the 0x42 frame arrives; the firmware then drains leftover
bytes so the framed parser stays in sync. A device that is streaming ignores
other commands until it sees the stop frame.

## Analog capture (0x49 / 0x4A)

The RP2350 ADC exposes three external inputs on **GP26..GP28** — the same pins
as LA digital channels 21..23 — plus the on-chip **temperature sensor**
(input 4). GP29/ADC3 is the target's SWDIO and is not usable. Analog and
digital captures are mutually exclusive (a later digital capture re-initialises
the pins through PIO).

`PG_INFO` (0x40) advertises the feature when the firmware supports it:

```
ANALOG:<external channels>,<max conversions/s>,<temp:0|1>     e.g. ANALOG:3,500000,1
```

Conversions:

```
volts = raw * 3.3 / 4096
temp_C = 27 - (volts - 0.706) / 0.001721        (temperature input)
```

Samples are **interleaved**: value `i` belongs to `channels[i % channelCount]`,
and the per-channel rate is `totalRate / channelCount`. The ADC free-runs at
48 MHz / 96 cycles = **500 kS/s total** (round-robin over the enabled inputs).

### 0x49 ANALOG_CAPTURE (one-shot)

Request: `CAPTURE_REQUEST` with analog semantics.

| field | meaning |
|---|---|
| `trigger type` | 3 = capture now; 0 = level trigger |
| `trigger` | ADC input to trigger on (0..2, 3 = temperature) |
| `inverted` | 1 = falling through the level, 0 = rising |
| `trigger value` | 12-bit threshold |
| `channel list`, `count` | ADC inputs, in interleave order (1..4) |
| `frequency` | requested **per-channel** sample rate |
| `pre` / `post` | samples **per channel** before / after the trigger |
| `capture mode` | 1 = 8-bit samples, 2 = 12-bit in `u16` (default) |

Reply:

```
ANALOG_STARTED <achieved per-channel rate>

[u32 values][values x width][u8 stampLen = 0]      width = 1 (8-bit) or 2 (12-bit)
```

`values = (pre + post) * channelCount`, interleaved. A trigger found inside the
pre-window is skipped (the next period is used); if the trigger never comes the
device keeps looking until the host cancels with `0xFF`, then answers
`ANALOG_ERROR`.

8-bit samples keep the top 8 bits (one step = 16 counts = 13 mV), which is
plenty for a voltage trace but coarse for the ~1.7 mV/°C temperature slope —
use 12-bit for the temperature input.

### 0x4A ANALOG_STREAM (gapless)

Request: `CAPTURE_REQUEST` (`frequency` = per-channel rate, `postSamples` =
interleaved values per half-frame, `capture mode` 1 = 8-bit / 2 = 12-bit).

```
ANALOG_STREAM_STARTED <achieved per-channel rate>

[u32 byte count][payload] ...                       payload interleaved samples
```

Stop with the 0x42 frame → `STREAM_STOPPED
`. Bandwidth: the USB link carries
about 700 kB/s, so 12-bit streams top out near 350 kS/s (8-bit near 500 kS/s)
in total, split across the enabled inputs.

## Errors

- `ERR_UNKNOWN_MSG\n` — unknown command
- `STREAM_ERROR\n` — stream could not start
- `CAPTURE_ERROR\n` — capture could not start or was cancelled
