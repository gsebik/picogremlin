/*
 * cdc_uart.h — UART bridge public API for the merged build.
 *
 * Shadows debugprobe/src/cdc_uart.h. When PROBE_CDC_UART is defined, the real
 * functions in cdc_uart.c are used on the Pico 2 W (GP24 TX / GP25 RX, uart1).
 */
#pragma once

#include <stdbool.h>
#include "board_merged_config.h"

#ifdef PROBE_CDC_UART

void cdc_uart_init(void);
bool cdc_task(void);
void cdc_uart_reset(void);
void cdc_uart_autobaud_begin(void);
void cdc_uart_autobaud_end(uint32_t baud);

#else

static inline void cdc_uart_init(void) {}
static inline bool cdc_task(void) { return false; }

#endif /* PROBE_CDC_UART */
