#include "autobaud.h"
#include "board_merged_config.h"
#include "cdc_uart.h"

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/irq.h"
#include "hardware/pio.h"
#include "autobaud.pio.h"

/* This is intentionally a small polling state machine, not the debugprobe
 * FreeRTOS task. DMA_IRQ_1 is reserved for its one-word PIO FIFO transfers. */
static PIO ab_pio = AUTOBAUD_PIO;
static int ab_sm = -1, ab_dma = -1, ab_offset = -1;
static volatile bool ab_done;
static volatile uint32_t ab_sample;
static bool ab_active;
static uint32_t ab_min_cycles = UINT32_MAX, ab_samples;

static void __isr ab_dma_irq(void) {
    if (ab_dma >= 0 && dma_irqn_get_channel_status(1, (uint)ab_dma)) {
        dma_irqn_acknowledge_channel(1, (uint)ab_dma);
        ab_done = true;
    }
}

static void ab_arm_dma(void) {
    dma_channel_set_read_addr(ab_dma, &ab_pio->rxf[ab_sm], false);
    dma_channel_set_write_addr(ab_dma, (void *)&ab_sample, false);
    dma_channel_set_trans_count(ab_dma, 1, true);
}

bool autobaud_start(void) {
    if (ab_active) return true;
    ab_sm = pio_claim_unused_sm(ab_pio, false);
    if (ab_sm < 0 || !pio_can_add_program(ab_pio, &autobaud_program)) goto fail;
    ab_offset = (int)pio_add_program(ab_pio, &autobaud_program);
    ab_dma = dma_claim_unused_channel(false);
    if (ab_dma < 0) goto fail;
    float div = (float)clock_get_hz(clk_sys) / 125000000.0f;
    autobaud_program_init(ab_pio, (uint)ab_sm, (uint)ab_offset, PROBE_UART_RX, div);
    dma_channel_config c = dma_channel_get_default_config((uint)ab_dma);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_32);
    channel_config_set_read_increment(&c, false);
    channel_config_set_write_increment(&c, false);
    channel_config_set_dreq(&c, pio_get_dreq(ab_pio, (uint)ab_sm, false));
    dma_channel_configure((uint)ab_dma, &c, (void *)&ab_sample, &ab_pio->rxf[ab_sm], 1, false);
    irq_add_shared_handler(DMA_IRQ_1, ab_dma_irq, PICO_SHARED_IRQ_HANDLER_DEFAULT_ORDER_PRIORITY);
    dma_irqn_set_channel_enabled(1, (uint)ab_dma, true);
    irq_set_enabled(DMA_IRQ_1, true);
    ab_done = false; ab_samples = 0; ab_min_cycles = UINT32_MAX;
    pio_sm_set_enabled(ab_pio, (uint)ab_sm, true);
    ab_arm_dma();
    ab_active = true;
    return true;
fail:
    if (ab_dma >= 0) { dma_channel_unclaim((uint)ab_dma); ab_dma = -1; }
    if (ab_offset >= 0) { pio_remove_program(ab_pio, &autobaud_program, (uint)ab_offset); ab_offset = -1; }
    if (ab_sm >= 0) { pio_sm_unclaim(ab_pio, (uint)ab_sm); ab_sm = -1; }
    return false;
}

bool autobaud_stop(uint32_t timeout_ms) {
    (void)timeout_ms; /* all resources are core0-owned, so this is synchronous */
    if (!ab_active) return true;
    dma_irqn_set_channel_enabled(1, (uint)ab_dma, false);
    /* RP2350-E5: clear EN before aborting, otherwise an aborted channel can
     * re-trigger. This channel is not chained, so no other EN bit is needed. */
    hw_clear_bits(&dma_hw->ch[ab_dma].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_channel_abort((uint)ab_dma);
    dma_irqn_acknowledge_channel(1, (uint)ab_dma);
    irq_remove_handler(DMA_IRQ_1, ab_dma_irq);
    dma_channel_unclaim((uint)ab_dma); ab_dma = -1;
    pio_sm_set_enabled(ab_pio, (uint)ab_sm, false);
    pio_sm_unclaim(ab_pio, (uint)ab_sm); ab_sm = -1;
    pio_remove_program(ab_pio, &autobaud_program, (uint)ab_offset); ab_offset = -1;
    ab_active = false;
    return true;
}

bool autobaud_running(void) { return ab_active; }

void autobaud_task(void) {
    if (!ab_active || !ab_done) return;
    ab_done = false;
    uint32_t cycles = (UINT32_MAX - ab_sample) * 2u;
    if (cycles && cycles < ab_min_cycles) ab_min_cycles = cycles;
    if (++ab_samples >= 8 && ab_min_cycles != UINT32_MAX) {
        uint32_t baud = 125000000u / ab_min_cycles;
        if (baud >= 300 && baud <= 4000000) {
            cdc_uart_autobaud_end(baud);
            return; /* cdc_uart_autobaud_end releases PIO/DMA synchronously */
        }
    }
    ab_arm_dma();
}
