# Third-party components

PicoGremlin is released under the **GNU General Public License v3.0**
(see [`LICENSE`](LICENSE)). The firmware is a combined work: it links the
GPL-3.0 LogicAnalyzer capture engine with the MIT Raspberry Pi debugprobe, so
the built binary must be distributed under the GPL-3.0. The components below
keep their own upstream licenses inside the submodule trees; their notices
are preserved.

| Component | Where | License | Upstream |
|---|---|---|---|
| LogicAnalyzer firmware (capture engine, LA command loop) | `logicanalyzer/` submodule | GPL-3.0 | <https://github.com/gusmanb/logicanalyzer> |
| debugprobe (CMSIS-DAP firmware, SWD PIO, UART bridge) | `debugprobe/` submodule | MIT | <https://github.com/raspberrypi/debugprobe> |
| CMSIS-DAP (`DAP.c`, `DAP_vendor.c`, `JTAG_DP.c`, `SWO.c`) and CMSIS Core headers | `debugprobe/CMSIS_DAP/` | Apache-2.0 (SPDX headers in each file; © ARM Limited) | <https://github.com/ARM-software/CMSIS-DAP> |
| Raspberry Pi Pico SDK | external (`PICO_SDK_PATH`, ≥ 2.3.0) | BSD-3-Clause | <https://github.com/raspberrypi/pico-sdk> |
| TinyUSB (bundled with the Pico SDK) | `$PICO_SDK_PATH/lib/tinyusb` | MIT | <https://github.com/hathach/tinyusb> |
| `picotool` (flash/build tooling, not linked) | external | BSD-3-Clause | <https://github.com/raspberrypi/picotool> |

The PicoGremlin integration layer (`picogremlin/`) and the hardware tests
(`tests/`) are original work released under the same GPL-3.0. The host-side
logic-analyzer tools (`la` CLI + Qt GUI) live in their own repository,
[picogremlin-la](https://github.com/gsebik/picogremlin-la), also GPL-3.0.

License texts:

- GPL-3.0 — [`LICENSE`](LICENSE), also `logicanalyzer/LICENSE`
- MIT — `debugprobe/LICENSE`
- Apache-2.0 — per-file SPDX headers under `debugprobe/CMSIS_DAP/`
- BSD-3-Clause — in the Pico SDK checkout (`$PICO_SDK_PATH/LICENSE.TXT`)
