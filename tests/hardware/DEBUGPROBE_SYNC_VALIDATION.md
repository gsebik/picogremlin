# Debugprobe Upstream Sync Validation

Date: 2026-08-19

## Sync

- `debugprobe`: `3fff5b240ca8200c7ad538cb61c02dfc39bda831` (`origin/master`)
- `logicanalyzer`: `4e8d9ec9fff592f7bb84490d8e301061a36e7f22` (unchanged)
- Debugprobe integration patch: unchanged; patched files are byte-identical
  between the previous pin and the new pin.

The upstream debugprobe commit adds its own USB reset implementation, but it
does not affect this composite build: upstream `tusb_edpt_handler.c` is not
compiled and upstream `usb_descriptors.c` is replaced by the PicoGremlin
six-interface descriptor/reset implementation.

## Build Review

- Fresh configure/build: **PASS**
- Both integration patches applied with zero fuzz: **PASS**
- Fail-closed LA overlay assertions: **PASS**
- ELF size unchanged: `text=54276`, `data=0`, `bss=434152`
- Generated patched overlays compared with `build-aplus-final`: **identical**
- Upstream endpoint-driver symbols entered the image: **none**

UF2:

```text
build-sync-debugprobe/picogremlin.uf2
f63bcb137603f0d6d3f8392860ce4f3d9a63ed3dcdf9874851afcec560361412
```

## Hardware Tests

The synced UF2 was backed up, flashed, verified, and rebooted on the connected
PicoGremlin.

| Test | Result |
|---|---|
| USB composite enumeration, six interfaces | **PASS** |
| `picotool info -f` runtime-reset path | **PASS** |
| `tests_swd.py` | **PASS**, 7/7 |
| `tests_la.py` | **PASS**, 12/12 |
| `tests_uart.py` | **FAIL**, 1 intermittent baud-switch check out of 105 |
| `tests_autobaud.py` | **PASS** for supported patterns; arbitrary 0x00-0xFF/PRNG limitations remain documented |
| `tests_concurrency.py` | **PASS**, 6/6 |

The UART and autobaud non-PASS results are the documented pre-existing
limitations, not changes from this debugprobe commit. The concurrency helper was
updated to resolve `g_counter` from the target ELF instead of using the stale
pre-channel-engine address `0x200008f0`.

## Artifacts

- `artifacts/debugprobe-sync/`: first sync test run
- `artifacts/debugprobe-sync-final/`: final SWD/concurrency rerun after fixing
  the stale target-symbol address
- `artifacts/debugprobe_sync_preflash.bin`: full 2 MiB pre-flash backup
