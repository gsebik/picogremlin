#include "tusb.h"
#include "pico/bootrom.h"
#include "hardware/watchdog.h"
#include "pico/usb_reset.h"
#include "cdc_uart.h"
#include "la_bridge.h"

extern void dap_transport_reset(void);
static uint8_t reset_itf;
static volatile uint8_t reset_pending;
static volatile bool reset_bootsel;

static void reset_init(void) {}
static void reset_reset(uint8_t rhport) { (void)rhport; reset_itf = 0; }
static uint16_t reset_open(uint8_t rhport, tusb_desc_interface_t const *d, uint16_t max_len) {
    (void)rhport; if (max_len < sizeof(*d) || d->bInterfaceClass != TUSB_CLASS_VENDOR_SPECIFIC || d->bInterfaceSubClass != RESET_INTERFACE_SUBCLASS || d->bInterfaceProtocol != RESET_INTERFACE_PROTOCOL) return 0;
    reset_itf = d->bInterfaceNumber; return sizeof(*d);
}
static bool reset_control(uint8_t rhport, uint8_t stage, tusb_control_request_t const *r) {
    (void)rhport; if (stage != CONTROL_STAGE_SETUP || r->wIndex != reset_itf) return stage != CONTROL_STAGE_SETUP;
    if (r->bRequest == RESET_REQUEST_BOOTSEL || r->bRequest == RESET_REQUEST_FLASH) { reset_bootsel = r->bRequest == RESET_REQUEST_BOOTSEL; reset_pending = 1; return true; }
    return false;
}
static bool reset_xfer(uint8_t rhport, uint8_t ep, xfer_result_t result, uint32_t bytes) { (void)rhport; (void)ep; (void)result; (void)bytes; return true; }
usbd_class_driver_t const picogremlin_reset_driver = { .init = reset_init, .reset = reset_reset, .open = reset_open, .control_xfer_cb = reset_control, .xfer_cb = reset_xfer, .sof = NULL };

bool picogremlin_reset_service(void) {
    if (!reset_pending) return false;
    reset_pending = 0; cdc_uart_reset(); dap_transport_reset(); la_bridge_reset();
    if (reset_bootsel) rom_reset_usb_boot_extra(-1, PICO_USB_RESET_BOOTSEL_INTERFACE_DISABLE_MASK, false);
    watchdog_reboot(0, 0, PICO_USB_RESET_RESET_TO_FLASH_DELAY_MS);
    return true;
}
