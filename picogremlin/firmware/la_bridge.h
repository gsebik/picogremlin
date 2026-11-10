/*
 * la_bridge — lock-free SPSC byte bridge between the logic-analyzer loop (core1)
 * and the USB-owning main loop (core0).
 *
 * Why this exists: TinyUSB must be driven from ONE core (see DESIGN_DEEPDIVE.md §3A).
 * The LA loop runs on core1 but must not call tud_* itself. Instead it reads/writes
 * these rings; core0 moves the bytes to/from CDC instance 1 in la_bridge_service().
 *
 *   RX ring: core0 (USB) --produces--> core1 (LA reads commands)
 *   TX ring: core1 (LA writes) --produces--> core0 (USB drains to host)
 *
 * Single-producer/single-consumer on each ring → no locks, just a memory barrier.
 */
#ifndef LA_BRIDGE_H
#define LA_BRIDGE_H

#include <stdint.h>
#include <stdbool.h>

/* CDC instance 1 = logic analyzer. Instance 0 = probe UART bridge (debugprobe). */
#define LA_CDC_ITF 1

/* ---- core1 side: the LA uses these in place of stdio / tud_cdc ---- */
int      la_bridge_getc(void);                          /* next RX byte, or -1 if empty   */
uint32_t la_bridge_write(const void *buf, uint32_t len);/* queue to TX; returns bytes taken*/
uint32_t la_bridge_write_available(void);               /* free space in the TX ring       */
void     la_bridge_write_flush(void);                   /* no-op: core0 flushes the USB EP */
bool     la_bridge_connected(void);                     /* is the host's CDC1 port open?   */

/* ---- core0 side: call once per iteration of the USB-owning main loop ---- */
void     la_bridge_service(void);
void     la_bridge_reset(void); /* core0 reset quiesce: invalidate both rings */

#endif /* LA_BRIDGE_H */
