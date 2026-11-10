#pragma once

#include <stdbool.h>
#include <stdint.h>

#define AUTOBAUD_MAGIC_BAUD 9728u

/* Core0-only polling API. The PIO block is selected by board configuration and
 * is independent of all LA capture PIO resources. */
bool autobaud_start(void);
bool autobaud_stop(uint32_t timeout_ms);
void autobaud_task(void);
bool autobaud_running(void);
