/*
 * Merged build: non-FreeRTOS UART bridge (replaces debugprobe/src/cdc_uart.c).
 *
 * Compiled whenever PROBE_CDC_UART is defined — on Pico 2 W that's GP24 (TX) /
 * GP25 (RX) on uart1.
 *
 * CDC instance 0  ←→  PROBE_UART_INTERFACE  (target serial console)
 */

#include "board_merged_config.h"

#ifdef PROBE_CDC_UART

#include <stdint.h>
#include <string.h>

#include "pico/stdlib.h"
#include "hardware/uart.h"
#include "hardware/irq.h"
#include "tusb.h"

#include "cdc_uart.h"
#include "autobaud.h"

/* Ring buffers for UART-RX → CDC and CDC-RX → UART-TX paths */
#define RING_SIZE 256
static uint8_t uart_to_cdc[RING_SIZE];
static volatile uint16_t u2c_head, u2c_tail;

static uint8_t cdc_to_uart[RING_SIZE];
static volatile uint16_t c2u_head, c2u_tail;
static bool dtr_open;
static bool rts_asserted;
static bool break_active;
static absolute_time_t break_deadline;

static inline uint16_t ring_used(uint16_t head, uint16_t tail)
{
    return (head - tail) & (RING_SIZE - 1);
}
static inline uint16_t ring_free(uint16_t head, uint16_t tail)
{
    return (RING_SIZE - 1) - ring_used(head, tail);
}

/* UART RX IRQ — stuffs incoming bytes into uart_to_cdc ring */
static void uart_rx_irq(void)
{
    while (uart_is_readable(PROBE_UART_INTERFACE)) {
        uint8_t b = uart_getc(PROBE_UART_INTERFACE);
        uint16_t next = (u2c_head + 1) & (RING_SIZE - 1);
        if (next != u2c_tail) {           /* drop if full */
            uart_to_cdc[u2c_head] = b;
            u2c_head = next;
        }
    }
}

/* Enable/disable the bridge TX-pin drive. With no host holding the CDC port open the pin
 * is left a high-Z input with a weak PULL-UP (not floating): on a robot the target's GP1
 * is the ultrasonic ECHO line, driven by the module, so holding TX idle-high would fight
 * it through the 200R series resistor; but letting it float would let the target pad's
 * pull-down read as a permanent break and flood the target's RX with 0x00. RX always stays
 * a UART input — listening is harmless. */
static void uart_tx_set_enabled(bool on)
{
    if (on) {
        gpio_set_function(PROBE_UART_TX, GPIO_FUNC_UART);
    } else {
        gpio_init(PROBE_UART_TX);            /* SIO function */
        gpio_set_dir(PROBE_UART_TX, GPIO_IN);
        gpio_pull_up(PROBE_UART_TX);
    }
}

static void uart_bridge_init(uint32_t baud)
{
    uart_init(PROBE_UART_INTERFACE, baud);
    gpio_set_function(PROBE_UART_TX, GPIO_FUNC_UART);
    gpio_set_function(PROBE_UART_RX, GPIO_FUNC_UART);
    uart_set_hw_flow(PROBE_UART_INTERFACE, false, false);
    uart_set_format(PROBE_UART_INTERFACE, 8, 1, UART_PARITY_NONE);
    uart_set_fifo_enabled(PROBE_UART_INTERFACE, true);

    uint irq = (PROBE_UART_INTERFACE == uart0) ? UART0_IRQ : UART1_IRQ;
    irq_set_exclusive_handler(irq, uart_rx_irq);
    irq_set_enabled(irq, true);
    uart_set_irq_enables(PROBE_UART_INTERFACE, true, false);

    uart_tx_set_enabled(dtr_open);   /* drive TX only while a host has the port open */
}

void cdc_uart_init(void)
{
    uart_bridge_init(PROBE_UART_BAUDRATE);
    dtr_open = false;
}

static void clear_bridge(void)
{
    /* RX IRQ is the only concurrent writer of u2c_head. Mask it before both
     * the ring reset and PL011 FIFO/error clear so stale bytes cannot straddle
     * a DTR, disconnect, reset, or autobaud transition. */
    uart_set_irq_enables(PROBE_UART_INTERFACE, false, false);
    u2c_head = u2c_tail = c2u_head = c2u_tail = 0;
    uart_get_hw(PROBE_UART_INTERFACE)->rsr = UART_UARTRSR_FE_BITS |
                                             UART_UARTRSR_PE_BITS |
                                             UART_UARTRSR_BE_BITS |
                                             UART_UARTRSR_OE_BITS;
    while (uart_is_readable(PROBE_UART_INTERFACE)) (void)uart_getc(PROBE_UART_INTERFACE);
    uart_set_irq_enables(PROBE_UART_INTERFACE, true, false);
}

void cdc_uart_reset(void)
{
    bool was_autobaud = autobaud_running();

    dtr_open = false;
    break_active = false;
    if (was_autobaud) {
        autobaud_stop(10);
        /* Autobaud deinitialized UART1 before claiming its RX pin. */
        uart_bridge_init(PROBE_UART_BAUDRATE);
    }
    uart_set_break(PROBE_UART_INTERFACE, false);
    clear_bridge();
    uart_tx_set_enabled(false);   /* no host → high-Z TX (robot GP1 / ECHO safety) */
}

void cdc_uart_autobaud_begin(void)
{
    clear_bridge();
    uart_set_irq_enables(PROBE_UART_INTERFACE, false, false);
    uart_deinit(PROBE_UART_INTERFACE);
}

void cdc_uart_autobaud_end(uint32_t baud)
{
    if (autobaud_running()) autobaud_stop(10);
    uart_bridge_init(baud);
    clear_bridge();
}

/* Called from the core0 super-loop. Drains both directions non-blockingly.
 * Returns true if any bytes were moved. */
bool cdc_task(void)
{
    bool active = false;
    /* Self-heal the TX-pin drive to match the host's DTR, in case the line-state callback
     * was missed or the boot ordering left it stale (the pin would stay high-Z). */
    static bool tx_drive = false;
    if (dtr_open != tx_drive) { uart_tx_set_enabled(dtr_open); tx_drive = dtr_open; }
    if (break_active && !is_nil_time(break_deadline) && absolute_time_diff_us(get_absolute_time(), break_deadline) <= 0) {
        uart_set_break(PROBE_UART_INTERFACE, false);
        break_active = false;
    }
    if (autobaud_running()) return false;
    if (!dtr_open) return false; /* DTR is a logical forwarding gate; no GPIO exists. */

    /* UART → CDC0 */
    while (u2c_tail != u2c_head && tud_cdc_write_available() > 0) {
        uint16_t avail = ring_used(u2c_head, u2c_tail);
        uint32_t space = tud_cdc_write_available();
        uint16_t n = (avail < (uint16_t)space) ? avail : (uint16_t)space;
        for (uint16_t i = 0; i < n; i++) {
            tud_cdc_write_char(uart_to_cdc[u2c_tail]);
            u2c_tail = (u2c_tail + 1) & (RING_SIZE - 1);
        }
        tud_cdc_write_flush();
        active = true;
    }

    /* CDC0 → UART TX */
    while (tud_cdc_available() > 0 && uart_is_writable(PROBE_UART_INTERFACE)) {
        uint8_t b;
        uint32_t n = tud_cdc_read(&b, 1);
        if (n) {
            uart_putc_raw(PROBE_UART_INTERFACE, b);
            active = true;
        }
    }

    return active;
}

/* TinyUSB callback — host changed baud rate. Update UART to match. */
void tud_cdc_line_coding_cb(uint8_t itf, cdc_line_coding_t const *coding)
{
    if (itf != 0) return;   /* only the UART bridge is on CDC0 */
    if (coding->bit_rate == AUTOBAUD_MAGIC_BAUD) {
        if (!autobaud_running()) {
            cdc_uart_autobaud_begin();
            if (!autobaud_start()) cdc_uart_autobaud_end(PROBE_UART_BAUDRATE);
        }
        return;
    }
    if (autobaud_running()) cdc_uart_autobaud_end(coding->bit_rate ? coding->bit_rate : PROBE_UART_BAUDRATE);
    if (coding->bit_rate == 0) return;
    uart_set_baudrate(PROBE_UART_INTERFACE, coding->bit_rate);
    uint data_bits = coding->data_bits;
    if (data_bits < 5 || data_bits > 8) data_bits = 8;
    uart_parity_t parity = UART_PARITY_NONE;
    if (coding->parity == 1) parity = UART_PARITY_ODD;
    else if (coding->parity == 2) parity = UART_PARITY_EVEN;
    /* CDC 1.5-stop encoding has no PL011 equivalent; map it to one stop bit. */
    uint stop_bits = coding->stop_bits == 2 ? 2 : 1;
    uart_set_format(PROBE_UART_INTERFACE, data_bits, stop_bits, parity);
}

void tud_cdc_line_state_cb(uint8_t itf, bool dtr, bool rts)
{
    if (itf != 0) return;
    dtr_open = dtr;
    rts_asserted = rts; /* recorded for diagnostics; this board has no RTS wire. */
    (void)rts_asserted;
    if (dtr) uart_tx_set_enabled(true);   /* a host opened the port → drive TX */
    else cdc_uart_reset();
}

void tud_cdc_send_break_cb(uint8_t itf, uint16_t duration_ms)
{
    if (itf != 0) return;
    if (duration_ms == 0) {
        uart_set_break(PROBE_UART_INTERFACE, false);
        break_active = false;
        return;
    }
    uart_set_break(PROBE_UART_INTERFACE, true);
    break_active = true;
    break_deadline = duration_ms == 0xffff ? nil_time : make_timeout_time_ms(duration_ms);
}

void tud_mount_cb(void) { cdc_uart_reset(); }
void tud_umount_cb(void) { cdc_uart_reset(); }

/* Not used in merged build — the FreeRTOS task entry point. */
void cdc_thread(void *ptr) { (void)ptr; }

#endif /* PROBE_CDC_UART */
