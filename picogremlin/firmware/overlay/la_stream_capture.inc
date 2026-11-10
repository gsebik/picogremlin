
/* ── PicoGremlin CMD=0x43 continuous stream (appended by CMakeLists) ───────
 * Double-buffered DMA: the PIO captures into two chained half-buffers without
 * ever stopping. A half-complete IRQ resets that channel's write pointer and
 * flags the half ready; the core1 command loop sends ready halves. Gapless
 * while the host drains a half faster than the DMA fills it. */
static volatile uint32_t pgStreamReady;   /* bit0 = half 0 done, bit1 = half 1 */
static uint32_t pgStreamHalfBytes;
static uint32_t pgStreamHalfSamples;
static uint32_t pgStreamWhich;            /* half currently being filled */
static uint8_t* pgStreamBase;

void __not_in_flash_func(pg_stream_dma_handler)(void)
{
    if (dma_channel_get_irq0_status(dmaPingPong0)) {
        dma_channel_acknowledge_irq0(dmaPingPong0);
        pgStreamReady |= (pgStreamWhich ? 2u : 1u);   /* the half that just finished */
        pgStreamWhich ^= 1u;
        dma_channel_set_write_addr(dmaPingPong0,
            (void*)(pgStreamBase + (pgStreamWhich ? pgStreamHalfBytes : 0u)), false);
        dma_channel_set_trans_count(dmaPingPong0, pgStreamHalfSamples, true);
    }
}

uint32_t pg_stream_ready(void) { return pgStreamReady; }
void     pg_stream_clear(uint32_t mask) { pgStreamReady &= ~mask; }
uint32_t pg_stream_half_bytes(void) { return pgStreamHalfBytes; }
uint32_t pg_stream_half_samples(void) { return pgStreamHalfSamples; }
uint8_t* pg_stream_half_ptr(uint32_t which) { return pgStreamBase + (which ? pgStreamHalfBytes : 0u); }

bool pg_stream_start(uint32_t freq, uint32_t halfSamples, const uint8_t* capturePins,
                     uint8_t capturePinCount, CHANNEL_MODE captureMode,
                     int triggerChannel, uint8_t inverted)
{
    enum dma_channel_transfer_size ts;
    uint32_t bps;
    switch (captureMode) {
        case MODE_8_CHANNEL:  ts = DMA_SIZE_8;  bps = 1; break;
        case MODE_16_CHANNEL: ts = DMA_SIZE_16; bps = 2; break;
        case MODE_24_CHANNEL: ts = DMA_SIZE_32; bps = 4; break;
        default: return false;
    }
    if (freq > MAX_BLAST_FREQ) return false;
    if (capturePinCount < 1 || capturePinCount > MAX_CHANNELS) return false;
    if (halfSamples < 1) return false;
    uint32_t halfBytes = halfSamples * bps;
    if (2u * halfBytes > CAPTURE_BUFFER_SIZE) return false;

    pgStreamHalfBytes = halfBytes;
    pgStreamHalfSamples = halfSamples;
    pgStreamBase = captureBuffer;
    pgStreamWhich = 0;
    pgStreamReady = 0;
    memset(captureBuffer, 0, 2u * halfBytes);

    lastPreSize = 0; lastPostSize = halfSamples; lastLoopCount = 0;
    lastCapturePinCount = capturePinCount; lastTriggerInverted = false;
    lastCaptureComplexFast = false; lastCaptureMode = captureMode;
    for (uint8_t i = 0; i < capturePinCount; i++)
        lastCapturePins[i] = pinMap[capturePins[i]];

    float clockDiv = (float)clock_get_hz(clk_sys) / (float)freq;

    capturePIO = pio0;
    pio_clear_instruction_memory(capturePIO);
    sm_Capture = pio_claim_unused_sm(capturePIO, true);
    pio_sm_clear_fifos(capturePIO, sm_Capture);
    pio_sm_restart(capturePIO, sm_Capture);
    captureOffset = pio_add_program(capturePIO, &BLAST_CAPTURE_program);
    captureProgram = &BLAST_CAPTURE_program;

    for (int i = 0; i < 24; i++)
        pio_sm_set_consecutive_pindirs(capturePIO, sm_Capture, pinMap[i], 1, false);
    for (uint8_t i = 0; i < 24; i++)
        pio_gpio_init(capturePIO, pinMap[i]);

    pio_sm_config smConfig = BLAST_CAPTURE_program_get_default_config(captureOffset);
    sm_config_set_in_pins(&smConfig, INPUT_PIN_BASE);
    sm_config_set_clkdiv(&smConfig, clockDiv);
    sm_config_set_in_shift(&smConfig, true, true, 0);
    int jmp_pin = INPUT_PIN_BASE;
    if (triggerChannel >= 0) {
        jmp_pin = pinMap[triggerChannel];
        pio_sm_set_consecutive_pindirs(capturePIO, sm_Capture, jmp_pin, 1, false);
        pio_gpio_init(capturePIO, jmp_pin);
        /* inverted=1 waits for LOW, 0 waits for HIGH (matches StartCaptureBlast) */
        gpio_set_inover(jmp_pin, inverted ? 0 : 1);
    }
    sm_config_set_jmp_pin(&smConfig, jmp_pin);
    pio_sm_set_enabled(capturePIO, sm_Capture, false);
    pio_sm_init(capturePIO, sm_Capture, captureOffset, &smConfig);
    if (triggerChannel < 0) {
        /* Immediate: skip the trigger-wait `jmp pin LOOP` and start capturing. */
        pio_sm_exec(capturePIO, sm_Capture, pio_encode_jmp(captureOffset + 1));
    }

    dmaPingPong0 = dma_claim_unused_channel(true);

    dma_channel_config c0 = dma_channel_get_default_config(dmaPingPong0);
    channel_config_set_read_increment(&c0, false);
    channel_config_set_write_increment(&c0, true);
    channel_config_set_transfer_data_size(&c0, ts);
    channel_config_set_dreq(&c0, pio_get_dreq(capturePIO, sm_Capture, false));
    channel_config_set_enable(&c0, true);

    dma_channel_acknowledge_irq0(dmaPingPong0);   /* drop a stale status bit from a
                                                  * previously unclaimed channel */
    dma_channel_set_irq0_enabled(dmaPingPong0, true);
    irq_set_exclusive_handler(DMA_IRQ_0, pg_stream_dma_handler);
    irq_set_enabled(DMA_IRQ_0, true);

    dma_channel_configure(dmaPingPong0, &c0, (void*)pgStreamBase,
                          &capturePIO->rxf[sm_Capture], halfSamples, true);

    pio_sm_set_enabled(capturePIO, sm_Capture, true);
    lastCaptureType = CAPTURE_TYPE_BLAST;
    captureFinished = false; captureProcessed = false;
    return true;
}

void pg_stream_stop(void)
{
    if (capturePIO != NULL) pio_sm_set_enabled(capturePIO, sm_Capture, false);
    hw_clear_bits(&dma_hw->ch[dmaPingPong0].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_channel_abort(dmaPingPong0);
    dma_channel_set_irq0_enabled(dmaPingPong0, false);
    irq_set_enabled(DMA_IRQ_0, false);
    irq_remove_handler(DMA_IRQ_0, pg_stream_dma_handler);
    dma_channel_unclaim(dmaPingPong0);
    pio_sm_unclaim(capturePIO, sm_Capture);
    if (captureProgram != NULL) {
        pio_remove_program(capturePIO, captureProgram, captureOffset);
        captureProgram = NULL;
    }
    captureFinished = true;
}
