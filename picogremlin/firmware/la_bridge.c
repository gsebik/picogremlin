/*
 * la_bridge — see la_bridge.h. Lock-free SPSC rings; indices are free-running
 * uint32_t masked to the (power-of-two) ring size, so (head - tail) is the live count
 * and survives wraparound. Each ring has exactly one producer and one consumer, so a
 * single data-memory barrier between the payload write and the index publish is enough.
 */
#include "la_bridge.h"
#include "tusb.h"

#define RX_SIZE 512u          /* host->LA: short framed commands */
#define TX_SIZE 8192u         /* LA->host: capture dumps stream through here */
#define RX_MASK (RX_SIZE - 1u)
#define TX_MASK (TX_SIZE - 1u)

static volatile uint8_t  rx_buf[RX_SIZE];
static volatile uint32_t rx_head, rx_tail;   /* head: core0 writes · tail: core1 reads */
static volatile uint8_t  tx_buf[TX_SIZE];
static volatile uint32_t tx_head, tx_tail;   /* head: core1 writes · tail: core0 reads */
static volatile bool     cdc_connected;
static volatile bool     bridge_quiesced;

static inline void barrier(void) { __asm volatile ("dmb" ::: "memory"); }

void la_bridge_reset(void)
{
    /* Reset is issued on core0; publishing zero indices invalidates buffered
     * protocol fragments before the reboot and prevents stale forwarding. */
    rx_head = rx_tail = tx_head = tx_tail = 0;
    cdc_connected = false;
    bridge_quiesced = true;
    barrier();
}

/* ---------------- core1: consumes RX, produces TX ---------------- */

int la_bridge_getc(void)
{
    uint32_t t = rx_tail;
    if (t == rx_head) return -1;             /* empty */
    uint8_t b = rx_buf[t & RX_MASK];
    barrier();
    rx_tail = t + 1u;
    return (int) b;
}

uint32_t la_bridge_write_available(void)
{
    return TX_SIZE - (tx_head - tx_tail);
}

uint32_t la_bridge_write(const void *buf, uint32_t len)
{
    const uint8_t *p = (const uint8_t *) buf;
    uint32_t freebytes = TX_SIZE - (tx_head - tx_tail);
    if (len > freebytes) len = freebytes;
    uint32_t h = tx_head;
    for (uint32_t i = 0; i < len; i++)
        tx_buf[(h + i) & TX_MASK] = p[i];
    barrier();
    tx_head = h + len;
    return len;
}

void la_bridge_write_flush(void) { /* core0 flushes the USB endpoint in service() */ }

bool la_bridge_connected(void) { return cdc_connected; }

/* ---------------- core0: produces RX, consumes TX, drives USB ---------------- */

void la_bridge_service(void)
{
    if (bridge_quiesced) return;
    cdc_connected = tud_cdc_n_connected(LA_CDC_ITF);

    /* USB -> RX ring (one byte at a time; commands are tiny) */
    while (tud_cdc_n_available(LA_CDC_ITF)) {
        if (RX_SIZE - (rx_head - rx_tail) == 0) break;   /* ring full */
        uint8_t b;
        if (tud_cdc_n_read(LA_CDC_ITF, &b, 1) != 1) break;
        rx_buf[rx_head & RX_MASK] = b;
        barrier();
        rx_head = rx_head + 1u;
    }

    /* TX ring -> USB (contiguous chunks, respecting ring wrap + EP space) */
    bool wrote = false;
    while (tx_tail != tx_head) {
        uint32_t avail = tud_cdc_n_write_available(LA_CDC_ITF);
        if (avail == 0) break;
        uint32_t pending = tx_head - tx_tail;
        uint32_t n = pending < avail ? pending : avail;
        uint32_t off = tx_tail & TX_MASK;
        uint32_t to_end = TX_SIZE - off;
        if (n > to_end) n = to_end;                       /* don't cross the wrap */
        uint32_t w = tud_cdc_n_write(LA_CDC_ITF, (const void *) &tx_buf[off], n);
        if (w == 0) break;
        barrier();
        tx_tail = tx_tail + w;
        wrote = true;
    }
    if (wrote) tud_cdc_n_write_flush(LA_CDC_ITF);
}
