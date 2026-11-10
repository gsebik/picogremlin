/*
 * Merged composite USB device: CMSIS-DAP v2 (vendor) + 2x CDC (UART bridge, LA).
 * Derived from Raspberry Pi debugprobe usb_descriptors.c (MIT) — see ../../debugprobe.
 *
 * Interface layout (see ../ARCHITECTURE.md):
 *   ITF 0      Vendor (bulk)   CMSIS-DAP v2
 *   ITF 1,2    CDC             UART bridge   (CDC instance 0)
 *   ITF 3,4    CDC             LogicAnalyzer (CDC instance 1)
 *   ITF 5      Vendor/no EP    Pico SDK runtime-reset (picotool)
 */
#include "tusb.h"
#include "pico/usb_reset.h"
#include "get_serial.h"
#include "board_merged_config.h"

//--------------------------------------------------------------------+
// Device Descriptor
//--------------------------------------------------------------------+
tusb_desc_device_t const desc_device =
{
    .bLength            = sizeof(tusb_desc_device_t),
    .bDescriptorType    = TUSB_DESC_DEVICE,
    .bcdUSB             = 0x0210,            // 2.1 — required for BOS / MS OS 2.0
    // Composite: defer class to interfaces via IAD
    .bDeviceClass       = TUSB_CLASS_MISC,
    .bDeviceSubClass    = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol    = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,

    // RP2350 runtime identity lets stock picotool locate the reset interface.
    .idVendor           = 0x2E8A,
    .idProduct          = 0x0009,
    .bcdDevice          = 0x0232,            // bumped on descriptor changes (Windows caches MS OS data)
    .iManufacturer      = 0x01,
    .iProduct           = 0x02,
    .iSerialNumber      = 0x03,
    .bNumConfigurations = 0x01
};

uint8_t const * tud_descriptor_device_cb(void)
{
  return (uint8_t const *) &desc_device;
}

//--------------------------------------------------------------------+
// Configuration Descriptor
//--------------------------------------------------------------------+
enum
{
  ITF_NUM_DAP = 0,        // must stay 0 (openocd/Keil assume probe == itf0)
  ITF_NUM_CDC_UART_COM,   // CDC instance 0 -> probe UART bridge
  ITF_NUM_CDC_UART_DATA,
  ITF_NUM_CDC_LA_COM,     // CDC instance 1 -> logic analyzer
  ITF_NUM_CDC_LA_DATA,
  ITF_NUM_RESET,
  ITF_NUM_TOTAL
};

// Endpoint addresses (IN = 0x8x). All distinct.
#define EPNUM_DAP_OUT          0x01
#define EPNUM_DAP_IN           0x81
#define EPNUM_CDC_UART_NOTIF   0x82
#define EPNUM_CDC_UART_OUT     0x03
#define EPNUM_CDC_UART_IN      0x83
#define EPNUM_CDC_LA_NOTIF     0x84
#define EPNUM_CDC_LA_OUT       0x05
#define EPNUM_CDC_LA_IN        0x85

#define CONFIG_TOTAL_LEN  (TUD_CONFIG_DESC_LEN + TUD_VENDOR_DESC_LEN + 2 * TUD_CDC_DESC_LEN + TUD_RPI_RESET_DESC_LEN)

uint8_t const desc_configuration[] =
{
  TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL, 0, CONFIG_TOTAL_LEN, 0, 100),

  // CMSIS-DAP v2 bulk transport only: no HID, JTAG or SWO endpoints.
  TUD_VENDOR_DESCRIPTOR(ITF_NUM_DAP, 4, EPNUM_DAP_OUT, EPNUM_DAP_IN, 64),

  // CDC 0 — UART bridge
  TUD_CDC_DESCRIPTOR(ITF_NUM_CDC_UART_COM, 5, EPNUM_CDC_UART_NOTIF, 8,
                     EPNUM_CDC_UART_OUT, EPNUM_CDC_UART_IN, 64),

  // CDC 1 — Logic Analyzer
  TUD_CDC_DESCRIPTOR(ITF_NUM_CDC_LA_COM, 6, EPNUM_CDC_LA_NOTIF, 8,
                     EPNUM_CDC_LA_OUT, EPNUM_CDC_LA_IN, 64),

  TUD_RPI_RESET_DESCRIPTOR(ITF_NUM_RESET, 7),
};
TU_VERIFY_STATIC(sizeof(desc_configuration) == CONFIG_TOTAL_LEN, "Incorrect configuration size");

uint8_t const * tud_descriptor_configuration_cb(uint8_t index)
{
  (void) index;
  return desc_configuration;
}

//--------------------------------------------------------------------+
// String Descriptors
//--------------------------------------------------------------------+
char const* string_desc_arr [] =
{
  (const char[]) { 0x09, 0x04 },        // 0: English (0x0409)
  "PicoGremlin",                         // 1: Manufacturer
  PROBE_PRODUCT_STRING,                  // 2: Product
  usb_serial,                            // 3: Serial (RP2350 OTP chip ID — unique, survives reflash)
  "CMSIS-DAP v2 Interface",              // 4: upstream-compatible DAP name
  "Target UART",                         // 5: CDC0
  "Logic Analyzer",                      // 6: CDC1
  "Pico SDK Runtime Reset",              // 7: picotool reset interface
};

static uint16_t _desc_str[64];

uint16_t const* tud_descriptor_string_cb(uint8_t index, uint16_t langid)
{
  (void) langid;
  uint8_t chr_count;

  if (index == 0) {
    memcpy(&_desc_str[1], string_desc_arr[0], 2);
    chr_count = 1;
  } else {
    if (!(index < sizeof(string_desc_arr)/sizeof(string_desc_arr[0]))) return NULL;
    const char* str = string_desc_arr[index];
    chr_count = strlen(str);
    if (chr_count > 63) chr_count = 63;
    for (uint8_t i = 0; i < chr_count; i++) _desc_str[1+i] = str[i];
  }
  _desc_str[0] = (TUSB_DESC_STRING << 8) | (2*chr_count + 2);
  return _desc_str;
}

//--------------------------------------------------------------------+
// BOS + MS OS 2.0 — WinUSB auto-bind for both vendor interfaces.
//--------------------------------------------------------------------+
#define DAP_MS_OS_20_FUNCTION_LEN 0xA0
#define MS_OS_20_DESC_LEN  (0x0A + 0x08 + DAP_MS_OS_20_FUNCTION_LEN + RPI_RESET_MS_OS_20_DESC_LEN)
#define BOS_TOTAL_LEN      (TUD_BOS_DESC_LEN + TUD_BOS_MICROSOFT_OS_DESC_LEN)

uint8_t const desc_bos[] =
{
  TUD_BOS_DESCRIPTOR(BOS_TOTAL_LEN, 1),
  TUD_BOS_MS_OS_20_DESCRIPTOR(MS_OS_20_DESC_LEN, 1)
};

uint8_t const desc_ms_os_20[] =
{
  U16_TO_U8S_LE(0x000A), U16_TO_U8S_LE(MS_OS_20_SET_HEADER_DESCRIPTOR), U32_TO_U8S_LE(0x06030000), U16_TO_U8S_LE(MS_OS_20_DESC_LEN),
  U16_TO_U8S_LE(0x0008), U16_TO_U8S_LE(MS_OS_20_SUBSET_HEADER_CONFIGURATION), 0, 0, U16_TO_U8S_LE(MS_OS_20_DESC_LEN-0x0A),
  U16_TO_U8S_LE(0x0008), U16_TO_U8S_LE(MS_OS_20_SUBSET_HEADER_FUNCTION), ITF_NUM_DAP, 0, U16_TO_U8S_LE(DAP_MS_OS_20_FUNCTION_LEN),
  U16_TO_U8S_LE(0x0014), U16_TO_U8S_LE(MS_OS_20_FEATURE_COMPATBLE_ID), 'W', 'I', 'N', 'U', 'S', 'B', 0x00, 0x00,
  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
  U16_TO_U8S_LE(DAP_MS_OS_20_FUNCTION_LEN-0x08-0x14), U16_TO_U8S_LE(MS_OS_20_FEATURE_REG_PROPERTY),
  U16_TO_U8S_LE(0x0007), U16_TO_U8S_LE(0x002A),
  'D', 0x00, 'e', 0x00, 'v', 0x00, 'i', 0x00, 'c', 0x00, 'e', 0x00, 'I', 0x00, 'n', 0x00, 't', 0x00, 'e', 0x00,
  'r', 0x00, 'f', 0x00, 'a', 0x00, 'c', 0x00, 'e', 0x00, 'G', 0x00, 'U', 0x00, 'I', 0x00, 'D', 0x00, 's', 0x00, 0x00, 0x00,
  U16_TO_U8S_LE(0x0050),
  '{', 0x00, 'C', 0x00, 'D', 0x00, 'B', 0x00, '3', 0x00, 'B', 0x00, '5', 0x00, 'A', 0x00, 'D', 0x00, '-', 0x00,
  '2', 0x00, '9', 0x00, '3', 0x00, 'B', 0x00, '-', 0x00, '4', 0x00, '6', 0x00, '6', 0x00, '3', 0x00, '-', 0x00,
  'A', 0x00, 'A', 0x00, '3', 0x00, '6', 0x00, '-', 0x00, '1', 0x00, 'A', 0x00, 'A', 0x00, 'E', 0x00, '4', 0x00,
  '6', 0x00, '4', 0x00, '6', 0x00, '3', 0x00, '7', 0x00, '7', 0x00, '6', 0x00, '}', 0x00, 0x00, 0x00, 0x00, 0x00,
  RPI_RESET_MS_OS_20_DESCRIPTOR(ITF_NUM_RESET)
};
TU_VERIFY_STATIC(sizeof(desc_ms_os_20) == MS_OS_20_DESC_LEN, "Incorrect size");
TU_VERIFY_STATIC(DAP_MS_OS_20_FUNCTION_LEN == 0xA0, "Incorrect DAP subset size");
TU_VERIFY_STATIC(RPI_RESET_MS_OS_20_DESC_LEN == 0x9c, "Incorrect reset subset size");
TU_VERIFY_STATIC(MS_OS_20_DESC_LEN == 0x0a + 0x08 + DAP_MS_OS_20_FUNCTION_LEN + RPI_RESET_MS_OS_20_DESC_LEN, "Incorrect MS OS total size");
TU_VERIFY_STATIC(ITF_NUM_TOTAL == 6, "Unexpected interface count");
TU_VERIFY_STATIC(EPNUM_DAP_OUT != EPNUM_CDC_UART_OUT && EPNUM_DAP_OUT != EPNUM_CDC_LA_OUT && EPNUM_CDC_UART_OUT != EPNUM_CDC_LA_OUT, "Duplicate OUT endpoint");
TU_VERIFY_STATIC(EPNUM_DAP_IN != EPNUM_CDC_UART_NOTIF && EPNUM_DAP_IN != EPNUM_CDC_UART_IN && EPNUM_DAP_IN != EPNUM_CDC_LA_NOTIF && EPNUM_DAP_IN != EPNUM_CDC_LA_IN && EPNUM_CDC_UART_NOTIF != EPNUM_CDC_UART_IN && EPNUM_CDC_UART_NOTIF != EPNUM_CDC_LA_NOTIF && EPNUM_CDC_UART_NOTIF != EPNUM_CDC_LA_IN && EPNUM_CDC_UART_IN != EPNUM_CDC_LA_NOTIF && EPNUM_CDC_UART_IN != EPNUM_CDC_LA_IN && EPNUM_CDC_LA_NOTIF != EPNUM_CDC_LA_IN, "Duplicate IN endpoint");

uint8_t const * tud_descriptor_bos_cb(void)
{
  return desc_bos;
}
