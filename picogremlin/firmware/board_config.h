/*
 * Shadow header — shadows debugprobe/include/board_config.h.
 *
 * Upstream debugprobe (master, after v2.3.1) selects its board configuration
 * from the Pico SDK board macro:
 *
 *   RASPBERRYPI_DEBUG_PROBE            -> board_debug_probe_config.h
 *   RASPBERRYPI_PICO / RASPBERRYPI_PICO2 -> board_pico_config.h
 *   anything else                      -> #error Unsupported board
 *
 * Our board (pico2_w) defines only RASPBERRYPI_PICO2_W, which upstream does not
 * recognise, and the stock pinouts collide head-on with the LA's channel GPIOs
 * anyway. picogremlin/firmware/ is first on the include path (see CMakeLists.txt),
 * so this file is found before debugprobe/include/board_config.h, redirecting
 * every probe-side pin macro to the single source of truth: board_merged_config.h.
 * See ../PINMAP.md.
 */
#pragma once
#include "board_merged_config.h"
