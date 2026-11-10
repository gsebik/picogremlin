/*
 * Merged Probe + Logic Analyzer — board / pin configuration.
 *
 * Single source of truth for the Pico 2 W (RP2350A) pin assignment. MUST match
 * ../PINMAP.md. PicoGremlin supports this board only.
 *
 * Reached from debugprobe/src/probe_config.h via the shadow header
 * board_config.h in this same directory (it sits first on the include path and
 * redirects to this file) — see that file for why the redirect exists.
 */
#ifndef BOARD_MERGED_CONFIG_H_
#define BOARD_MERGED_CONFIG_H_

/* Pull in FreeRTOS type shim so any upstream header that references TaskHandle_t
 * compiles without the real FreeRTOS at runtime. */
#include "FreeRTOS.h"

#if !defined(MERGED_BOARD_PICO2W) || defined(MERGED_BOARD_RP2040)
#error "PicoGremlin supports only MERGED_BOARD_PICO2W (RP2350A)"
#endif

/* CMSIS-DAP v2 (bulk) transport — always vendor bulk, no HID. */
#define PROBE_IO_RAW
/* PROBE_CDC_UART is defined per-board below (only where UART GPIO are available). */

/* LA_CHANNEL_PIN(p) is defined per-board below: an exact-set membership check for
 * the LA's channel GPIOs, used by the collision guards at the bottom of this file.
 * It is deliberately NOT a flat [LA_INPUT_PIN_BASE, LA_CHANNEL_PIN_MAX] range check
 * — the Pico 2 W's channel map has a gap at 23/24/25, and pins in that gap (the
 * probe's own pins) are legitimately free even though they're numerically inside
 * [2, 28]. */

/* ----------- Pico 2 W / RP2350A (primary — actual hardware) ----------- *
 * U1 = Raspberry Pi Pico 2 W (RP2350A). Pinout confirmed against the             *
 * PICO_Gremlin_Rev.A_4L schematic (hardware owner, 2026-08-14 correction):        *
 *   - GP23/GP29 -> probe SWCLK/SWDIO (NOT GP0/GP1 as earlier docs assumed)        *
 *   - GP24/GP25 -> probe UART bridge (TX/RX) — previously assumed unavailable    *
 *   - GP0+GP1 are tied together in hardware and dedicated to the LA's advanced/  *
 *     complex trigger (loopback pair) — NOT used by the probe at all             *
 *   - GP2..22 + GP26..28 -> 24 LA capture channels, unchanged                    */
  /* Logic analyzer — unchanged channel layout (gusmanb BUILD_PICO_2).
   * GP2..22 + GP26..28 wired to IC1 GPIO2..22 + GPIO26..28 (24 channels).
   * LA_CHANNEL_PIN_MAX is the highest GPIO used by any channel.
   * NOTE: these LA_* macros are informational only in this build — the LA's own
   * LogicAnalyzer_Board_Settings.h (BUILD_PICO_2 branch) hardcodes its own
   * equivalents (INPUT_PIN_BASE, MAX_CHANNELS, COMPLEX_TRIGGER_*_PIN) rather than
   * reading these. Kept in sync manually; see the collision guards below, which
   * DO use LA_CHANNEL_PIN() and are load-bearing.
   * TODO(merge): wire LogicAnalyzer_Board_Settings.h's BUILD_PICO_2 branch to
   * actually consume these instead of duplicating the values. */
  #define LA_INPUT_PIN_BASE        2
  #define LA_MAX_CHANNELS          24
  #define LA_CHANNEL_PIN_MAX       28
  #define LA_CHANNEL_PIN(p) (((p) >= 2 && (p) <= 22) || ((p) >= 26 && (p) <= 28))

  /* GP0/GP1 are hardware-jumpered together and dedicated to the LA's advanced
   * (complex/fast/chained) trigger mode — this is a real, working feature on this
   * board revision, not a disabled one. LogicAnalyzer_Board_Settings.h already
   * defines SUPPORTS_COMPLEX_TRIGGER + COMPLEX_TRIGGER_OUT_PIN=0/IN_PIN=1 for
   * BUILD_PICO_2 independently of these macros; kept here for documentation/
   * collision-guard purposes only. */
  #define LA_COMPLEX_TRIGGER_OUT   0
  #define LA_COMPLEX_TRIGGER_IN    1
  /* No spare GPIO for a capture-activity LED (see LogicAnalyzer_Board_Settings.h's
   * MERGED_BUILD guard around GPIO_LED/LED_IO — GPIO25 is now the UART RX pin). */

  /* RP2350A has three PIO blocks. PIO0 is reserved for the LA modes that clear
   * their whole instruction store. PIO1 owns SWD and autobaud. PIO2 owns only
   * the fast LA sampler, so fast capture never tears down the probe. */
  #include "hardware/pio.h"

  /* Probe — SWCLK/SWDIO are NOT adjacent GPIOs on this board revision, so there is
   * no PROBE_PIN_OFFSET; each pin is set independently. The SM is claimed
   * dynamically via pio_claim_unused_sm() in probe_init()/released in
   * probe_deinit(). */
  #define PROBE_PIN_SWCLK          23   /* target SWCLK */
  #define PROBE_PIN_SWDIO          29   /* target SWDIO */
  #define PROBE_PIO                pio1
  #define AUTOBAUD_PIO             PROBE_PIO
  #define LA_FAST_CAPTURE_PIO      pio2

  #if NUM_PIOS != 3
  #error "PicoGremlin requires the RP2350 three-PIO implementation"
  #endif
  _Static_assert(PIO_NUM(LA_FAST_CAPTURE_PIO) != PIO_NUM(PROBE_PIO),
                 "LA fast capture must not share the probe PIO");
  /* No PROBE_PIN_RESET: software reset (SYSRESETREQ over SWD) is the sole reset
   * path in this build — this board revision has no hardware reset line wired. */

  /* UART bridge — now available on GP24 (TX, UART1) / GP25 (RX, UART1). Confirmed
   * against the RP2350 GPIO function-select table
   * (IO_BANK0_GPIO24_CTRL_FUNCSEL_VALUE_UART1_TX / ..._GPIO25..._UART1_RX). */
  #define PROBE_CDC_UART
  #define PROBE_UART_INTERFACE     uart1
  #define PROBE_UART_TX            24
  #define PROBE_UART_RX            25

  /* No PROBE_USB_CONNECTED_LED / PROBE_DAP_CONNECTED_LED / PROBE_DAP_RUNNING_LED /
   * UART TX/RX LEDs: none of these are populated on this board — leave undefined
   * so every LED code path in DAP_config.h / cdc_uart.c / main.c stays compiled
   * out, not just redefined to an unused pin. */

/* Shared probe UART config (PROBE_UART_INTERFACE is set per-board above) */
#ifdef PROBE_CDC_UART
#define PROBE_UART_BAUDRATE    115200
#endif

#define PROBE_PRODUCT_STRING   "CMSIS-DAP-Debugger+LogicAnalyzer"

/* Compile-time guard: probe pins must NOT fall within the LA's actual (non-
 * contiguous) channel GPIO set {2..22, 26..28} — see LA_CHANNEL_PIN() above. A
 * flat [LA_INPUT_PIN_BASE, LA_CHANNEL_PIN_MAX] range check would false-positive
 * on the Pico 2 W's probe pins (23/24/25 sit inside that numeric range but
 * outside the actual channel set — the LA simply doesn't wire a channel there). */
#if LA_CHANNEL_PIN(PROBE_PIN_SWCLK)
#error "SWCLK is within the LA channel GPIO set — check pin assignment"
#endif
#if LA_CHANNEL_PIN(PROBE_PIN_SWDIO)
#error "SWDIO is within the LA channel GPIO set — check pin assignment"
#endif
#if defined(PROBE_CDC_UART)
  #if LA_CHANNEL_PIN(PROBE_UART_TX)
  #error "Probe UART TX is within the LA channel GPIO set"
  #endif
  #if LA_CHANNEL_PIN(PROBE_UART_RX)
  #error "Probe UART RX is within the LA channel GPIO set"
  #endif
#endif
#if defined(PROBE_PIN_RESET)
  #if LA_CHANNEL_PIN(PROBE_PIN_RESET)
  #error "Probe RESET is within the LA channel GPIO set"
  #endif
#endif

#endif /* BOARD_MERGED_CONFIG_H_ */
