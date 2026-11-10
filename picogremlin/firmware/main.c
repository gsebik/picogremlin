/*
 * Merged Probe + Logic Analyzer — entry point.
 *
 * Runtime model (see ../ARCHITECTURE.md, ../DESIGN_DEEPDIVE.md §3):
 *   core0: USB device (tud_task) + CMSIS-DAP v2 + UART bridge (CDC0) + LA bridge svc
 *   core1: the logic-analyzer loop (gusmanb la_main), capture engine on PIO+DMA
 *
 * No FreeRTOS — debugprobe's non-threaded path. TinyUSB is touched ONLY on core0;
 * the LA on core1 does host I/O through la_bridge (SPSC rings drained here).
 *
 * Target: Pico 2 W (RP2350A). Remaining TODO(merge) items are called out inline.
 */
#include <pico/stdlib.h>
#include <pico/multicore.h>
#include <string.h>

#include "hardware/vreg.h"
#include "hardware/clocks.h"

#include "bsp/board_api.h"
#include "tusb.h"
#include "pico/usb_reset.h"

#include "board_merged_config.h"
#include "la_bridge.h"

// ---- debugprobe subsystem (reused from ../../debugprobe/src) ----
#include "probe.h"
#include "cdc_uart.h"
#include "get_serial.h"
#include "DAP_config.h"
#include "DAP.h"
#include "autobaud.h"

extern usbd_class_driver_t const picogremlin_reset_driver;
extern bool picogremlin_reset_service(void);

// ---- logic-analyzer entry, runs on core1 (gusmanb LogicAnalyzer.c, MERGED_BUILD) ----
extern void la_main(void);

/* DAP_Info reports DAP_PACKET_COUNT - 2 packets upstream. Keep exactly that much command and
 * response storage so a host can pipeline requests without silent loss. */
#define DAP_QUEUE_DEPTH 6u
#define DAP_PACKET_BYTES 64u
static uint8_t dap_rx[DAP_QUEUE_DEPTH][DAP_PACKET_BYTES];
static uint8_t dap_tx[DAP_QUEUE_DEPTH][DAP_PACKET_BYTES];
static uint16_t dap_tx_len[DAP_QUEUE_DEPTH];
static uint8_t dap_rx_head, dap_rx_tail, dap_tx_head, dap_tx_tail;
/* Mirrors debugprobe/include/DAP_config.h without importing its board timing
 * overrides after the CMSIS-DAP public header. */
TU_VERIFY_STATIC(DAP_PACKET_COUNT == DAP_QUEUE_DEPTH + 2u, "DAP queue advertisement mismatch");
TU_VERIFY_STATIC(DAP_PACKET_BYTES == 64u, "DAP packet size mismatch");

// CMSIS-DAP v2: vendor bulk OUT -> process -> vendor bulk IN.
static void dap_task(void)
{
    while (tud_vendor_available() && (uint8_t)(dap_rx_head - dap_rx_tail) < DAP_QUEUE_DEPTH) {
        uint32_t n = tud_vendor_read(dap_rx[dap_rx_head % DAP_QUEUE_DEPTH], DAP_PACKET_BYTES);
        if (!n) break;
        /* CMSIS-DAP v2 commands are one USB packet. Short packets are zero-filled
         * because the command processor reads packet-sized request buffers. */
        if (n < DAP_PACKET_BYTES) memset(dap_rx[dap_rx_head % DAP_QUEUE_DEPTH] + n, 0, DAP_PACKET_BYTES - n);
        dap_rx_head++;
    }
    while (dap_rx_tail != dap_rx_head && (uint8_t)(dap_tx_head - dap_tx_tail) < DAP_QUEUE_DEPTH) {
        uint8_t slot = dap_rx_tail % DAP_QUEUE_DEPTH;
        dap_tx_len[dap_tx_head % DAP_QUEUE_DEPTH] =
            (uint16_t)(DAP_ProcessCommand(dap_rx[slot], dap_tx[dap_tx_head % DAP_QUEUE_DEPTH]) & 0xffffu);
        dap_rx_tail++; dap_tx_head++;
    }
    while (dap_tx_tail != dap_tx_head) {
        uint8_t slot = dap_tx_tail % DAP_QUEUE_DEPTH;
        uint16_t len = dap_tx_len[slot];
        if (tud_vendor_write_available() < len) break;
        if (tud_vendor_write(dap_tx[slot], len) != len) break;
        dap_tx_tail++;
    }
    tud_vendor_flush();
}

void dap_transport_reset(void)
{
    dap_rx_head = dap_rx_tail = dap_tx_head = dap_tx_tail = 0;
}

// Clock/voltage setup. Must run on core0 BEFORE tusb_init() and before launching the
// LA, because set_sys_clock_khz() reconfigures clk_sys for the whole chip (SWD PIO
// clkdiv + capture timing derive from it). Mirrors the gusmanb LA's own setup.
static void merged_clock_init(void)
{
#if defined(TURBO_MODE)
    vreg_disable_voltage_limit();
    vreg_set_voltage(VREG_VOLTAGE_1_30);
    sleep_ms(1);
    set_sys_clock_khz(400000, true);   // TODO(merge): validate SWD at this overclock
#else
    set_sys_clock_khz(200000, true);
#endif
}

int main(void)
{
    merged_clock_init();

    board_init();
    usb_serial_init();      // fills usb_serial[] from the RP2350 OTP chip id (string idx 3)

    cdc_uart_init();        // probe UART bridge on CDC instance 0

    DAP_Setup();            // CMSIS-DAP core init
    // Resource separation: PIO0 is LA-owned, PIO1 owns SWD/autobaud, and PIO2
    // owns the fast LA sampler. SMs and DMA channels are claimed dynamically.

    tusb_init();

    // Launch the logic-analyzer loop on core1. It captures via PIO+DMA and talks to
    // the host only through la_bridge; it makes ZERO TinyUSB calls itself.
    multicore_launch_core1(la_main);

#ifdef PROBE_USB_CONNECTED_LED
    gpio_init(PROBE_USB_CONNECTED_LED);
    gpio_set_dir(PROBE_USB_CONNECTED_LED, GPIO_OUT);
#endif

    while (1) {
        tud_task();             // USB device servicing — must never be starved
        if (picogremlin_reset_service()) continue;
        cdc_task();             // probe UART <-> CDC0 (debugprobe cdc_uart.c)
        autobaud_task();        // board-selected PIO/DMA_IRQ_1 polling state machine
        dap_task();             // CMSIS-DAP v2 command processing
        la_bridge_service();    // move bytes between the LA's rings and CDC1

#ifdef PROBE_USB_CONNECTED_LED
        gpio_put(PROBE_USB_CONNECTED_LED, tud_ready() ? 1 : 0);
#endif
    }
    return 0;
}

// CMSIS-DAP v2 WinUSB binding: serve the MS OS 2.0 descriptor on vendor control req.
extern uint8_t const desc_ms_os_20[];
bool tud_vendor_control_xfer_cb(uint8_t rhport, uint8_t stage, tusb_control_request_t const * request)
{
    if (stage != CONTROL_STAGE_SETUP) return true;
    if (request->bmRequestType_bit.type == TUSB_REQ_TYPE_VENDOR &&
        request->bRequest == 1 && request->wIndex == 7) {
        uint16_t total_len;
        memcpy(&total_len, desc_ms_os_20 + 8, 2);
        return tud_control_xfer(rhport, request, (void*) desc_ms_os_20, total_len);
    }
    return false;
}

/* pico_usb_reset is an application TinyUSB driver because this composite already
 * supplies an app-driver callback for its vendor control descriptor. */
usbd_class_driver_t const *usbd_app_driver_get_cb(uint8_t *driver_count)
{
    *driver_count = 1;
    return &picogremlin_reset_driver;
}
