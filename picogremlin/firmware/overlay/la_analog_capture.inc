
/* ── PicoGremlin ADC analog capture (CMD=0x49 one-shot, 0x4A stream) ────────
 * Three external inputs share the RP2350 ADC on GP26..GP28 — the same pins as
 * LA digital channels 21..23 — plus the on-chip temperature sensor (input 4).
 * Analog and digital captures are mutually exclusive; the digital path
 * re-initialises those pins through pio_gpio_init() when it starts again.
 *
 * The ADC free-runs from the 48 MHz clock: one conversion takes 96 cycles, so
 * at most 500 kS/s *total* across the enabled inputs. Samples are interleaved
 * (sample i belongs to channels[i % channelCount]); the per-channel rate is
 * total / channelCount.
 *
 * One-shot captures reuse captureBuffer because the merged build has no spare
 * SRAM for another multi-100 kB buffer. */

#include "hardware/adc.h"

bool processCancel();                    /* LogicAnalyzer.c */

#define PG_ADC_EXT_CHANNELS 3u           /* GP26/27/28 = ADC0/1/2 */
#define PG_ADC_TEMP_INPUT   4u           /* on-chip temperature sensor */
#define PG_ADC_MAX_RATE     500000u      /* total conversions per second */
#define PG_ADC_MAX_SAMPLES  100000u      /* interleaved values per one-shot */

static const uint8_t pgAdcPins[PG_ADC_EXT_CHANNELS] = {26u, 27u, 28u};

static bool     pgAdcInited;
static uint32_t pgAdcChanCount;          /* enabled inputs in the current run */
static uint32_t pgAdcPerChanRate;        /* achieved samples/s per channel */
static bool     pgAdcEightBit;
static uint32_t pgAdcPre;                /* per-channel samples before trigger */
static uint32_t pgAdcPost;               /* per-channel samples after trigger */
static int      pgAdcTrigPos;            /* list position to trigger on, -1 = now */
static uint16_t pgAdcTrigLevel;
static bool     pgAdcTrigInverted;
static uint32_t pgAdcScanPos;            /* interleaved values already scanned */
static uint16_t pgAdcPrev;               /* previous value of the trigger input */
static bool     pgAdcHavePrev;
static uint32_t pgAdcDma;
static bool     pgAdcBurstOn;
static bool     pgAdcStreamOn;

static uint32_t pgAdcTxVals;             /* interleaved values in the DMA run */

static int pg_adc_input_of(uint8_t idx) {
    if (idx < PG_ADC_EXT_CHANNELS) return (int)idx;
    if (idx == PG_ADC_EXT_CHANNELS) return (int)PG_ADC_TEMP_INPUT;
    return -1;
}

uint8_t* pg_adc_buffer(void) { return captureBuffer; }
uint32_t pg_adc_rate(void) { return pgAdcPerChanRate; }

/* Configure the ADC for `count` inputs from the protocol channel list.
 * `rate` is the requested per-channel rate; the total is clamped to 500 kS/s. */
static bool pg_adc_configure(const uint8_t* channels, uint8_t count, uint32_t rate) {
    if (count < 1u || count > 4u) return false;
    uint32_t mask = 0;
    bool temp = false;
    for (uint8_t i = 0; i < count; i++) {
        int in = pg_adc_input_of(channels[i]);
        if (in < 0) return false;
        if (in == (int)PG_ADC_TEMP_INPUT) temp = true;
        else adc_gpio_init(pgAdcPins[in]);
        mask |= 1u << in;
    }
    if (!pgAdcInited) { adc_init(); pgAdcInited = true; }
    adc_set_temp_sensor_enabled(temp);

    if (rate == 0u) rate = 5000u;
    uint32_t total = rate * count;
    if (total > PG_ADC_MAX_RATE) total = PG_ADC_MAX_RATE;
    float div = 48000000.0f / (float)total - 1.0f;
    if (div < 95.0f) div = 0.0f;                        /* 96-cycle minimum period */
    adc_set_clkdiv(div);
    uint32_t period = (div == 0.0f) ? 96u : (uint32_t)(div + 1.0f);
    uint32_t totalActual = 48000000u / period;

    adc_fifo_setup(true, true, 1, false, false);
    adc_set_round_robin(mask);
    adc_select_input((uint)__builtin_ctz(mask));
    adc_run(false);
    while (!adc_fifo_is_empty()) (void)adc_fifo_get();

    pgAdcChanCount = count;
    pgAdcPerChanRate = totalActual / count;
    return true;
}

/* ── one-shot (CMD=0x49) ─────────────────────────────────────────────────── */

bool pg_adc_capture_start(uint32_t rate, uint32_t pre, uint32_t post,
                          const uint8_t* channels, uint8_t count, bool eightBit,
                          int triggerChannel, uint16_t triggerLevel, bool inverted) {
    if (post == 0u) post = 1u;
    /* the DMA run is longer than the reply window so a trigger found anywhere
     * after the pre-window has both the full pre and post samples available */
    uint32_t txVals = (2u * pre + post) * count;
    if (txVals * 2u > CAPTURE_BUFFER_SIZE) return false;
    if (!pg_adc_configure(channels, count, rate)) return false;

    pgAdcEightBit = eightBit;
    pgAdcPre = pre;
    pgAdcPost = post;
    pgAdcTrigLevel = triggerLevel;
    pgAdcTrigInverted = inverted;
    pgAdcTrigPos = -1;
    if (triggerChannel >= 0) {
        for (uint8_t i = 0; i < count; i++)
            if ((int)channels[i] == triggerChannel) { pgAdcTrigPos = (int)i; break; }
        if (pgAdcTrigPos < 0) return false;             /* trigger input not enabled */
    }
    pgAdcScanPos = 0u;
    pgAdcHavePrev = false;
    pgAdcTxVals = txVals;

    pgAdcDma = dma_claim_unused_channel(true);
    dma_channel_config c = dma_channel_get_default_config(pgAdcDma);
    channel_config_set_read_increment(&c, false);
    channel_config_set_write_increment(&c, true);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_16);
    channel_config_set_dreq(&c, DREQ_ADC);
    dma_channel_configure(pgAdcDma, &c, captureBuffer, &adc_hw->fifo, txVals, false);
    adc_run(true);
    dma_channel_start(pgAdcDma);
    pgAdcBurstOn = true;
    return true;
}

static void pg_adc_burst_restart(void) {
    dma_channel_abort(pgAdcDma);
    while (!adc_fifo_is_empty()) (void)adc_fifo_get();
    pgAdcScanPos = 0u;
    pgAdcHavePrev = false;
    dma_channel_set_write_addr(pgAdcDma, captureBuffer, false);
    dma_channel_set_trans_count(pgAdcDma, pgAdcTxVals, true);
}

void pg_adc_capture_abort(void) {
    if (!pgAdcBurstOn) return;
    adc_run(false);
    hw_clear_bits(&dma_hw->ch[pgAdcDma].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_channel_abort(pgAdcDma);
    dma_channel_unclaim(pgAdcDma);
    pgAdcBurstOn = false;
    while (!adc_fifo_is_empty()) (void)adc_fifo_get();
}

/* Wait for the capture (and, when armed, for the trigger window). Returns
 * false when cancelled. On success the reply data starts at pg_adc_buffer(). */
bool pg_adc_capture_wait(void) {
    const uint32_t txVals = pgAdcTxVals;
    const uint32_t preVals = pgAdcPre * pgAdcChanCount;
    const uint32_t postVals = pgAdcPost * pgAdcChanCount;
    const uint32_t replyVals = preVals + postVals;

    while (true) {
        uint32_t have = txVals - dma_channel_hw_addr(pgAdcDma)->transfer_count;
        if (have > pgAdcScanPos) {
            const uint16_t* buf = (const uint16_t*)captureBuffer;
            for (uint32_t i = pgAdcScanPos; i < have; i++) {
                bool onTrig = (pgAdcTrigPos >= 0) &&
                              ((i % pgAdcChanCount) == (uint32_t)pgAdcTrigPos);
                uint16_t s = buf[i];
                if (onTrig) {
                    bool hit = pgAdcHavePrev &&
                        (pgAdcTrigInverted ? (s < pgAdcTrigLevel && pgAdcPrev >= pgAdcTrigLevel)
                                           : (s >= pgAdcTrigLevel && pgAdcPrev < pgAdcTrigLevel));
                    pgAdcPrev = s;
                    pgAdcHavePrev = true;
                    /* a hit inside the pre-window is skipped: the next period
                     * will land far enough into the run */
                    if (hit && i >= preVals && i + postVals <= txVals) {
                        uint32_t start = i - preVals;
                        uint32_t end = i + postVals;
                        /* let the DMA finish the post window, then linearise */
                        while (txVals - dma_channel_hw_addr(pgAdcDma)->transfer_count < end) {
                            if (processCancel()) return false;
                            sleep_ms(1);
                        }
                        if (start != 0u)
                            memmove(captureBuffer, captureBuffer + start * 2u,
                                    replyVals * 2u);
                        pg_adc_capture_abort();
                        return true;
                    }
                }
            }
            if (have > pgAdcScanPos) pgAdcScanPos = have;
        }
        if (!dma_channel_is_busy(pgAdcDma)) {
            if (pgAdcTrigPos < 0) {                 /* free-run capture complete */
                pg_adc_capture_abort();
                return true;
            }
            pg_adc_burst_restart();                 /* no usable trigger yet */
            continue;
        }
        if (processCancel()) return false;
        sleep_ms(1);
    }
}

/* Compact 16-bit ADC values to the high 8 bits, in place. */
void pg_adc_pack8_inplace(uint8_t* buf, uint32_t values) {
    uint16_t* w = (uint16_t*)buf;
    for (uint32_t i = 0; i < values; i++) buf[i] = (uint8_t)(w[i] >> 4);
}

/* ── gapless stream (CMD=0x4A) ───────────────────────────────────────────── */

bool pg_adc_stream_start(uint32_t rate, uint32_t halfSamples,
                         const uint8_t* channels, uint8_t count, bool eightBit) {
    if (halfSamples < 16u) halfSamples = 16u;
    uint32_t halfBytes = halfSamples * 2u;
    if (2u * halfBytes > CAPTURE_BUFFER_SIZE) return false;
    if (!pg_adc_configure(channels, count, rate)) return false;

    pgAdcEightBit = eightBit;
    pgStreamHalfBytes = halfBytes;
    pgStreamHalfSamples = halfSamples;
    pgStreamBase = captureBuffer;
    pgStreamWhich = 0u;
    pgStreamReady = 0u;
    memset(captureBuffer, 0, 2u * halfBytes);

    dmaPingPong0 = dma_claim_unused_channel(true);
    dma_channel_config c = dma_channel_get_default_config(dmaPingPong0);
    channel_config_set_read_increment(&c, false);
    channel_config_set_write_increment(&c, true);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_16);
    channel_config_set_dreq(&c, DREQ_ADC);

    dma_channel_acknowledge_irq0(dmaPingPong0);   /* drop a stale status bit from a
                                                  * previously unclaimed channel */
    dma_channel_set_irq0_enabled(dmaPingPong0, true);
    irq_set_exclusive_handler(DMA_IRQ_0, pg_stream_dma_handler);
    irq_set_enabled(DMA_IRQ_0, true);

    dma_channel_configure(dmaPingPong0, &c, pgStreamBase, &adc_hw->fifo,
                          halfSamples, false);
    adc_run(true);
    dma_channel_start(dmaPingPong0);
    pgAdcStreamOn = true;
    return true;
}

void pg_adc_stream_stop(void) {
    adc_run(false);
    if (!pgAdcStreamOn) return;
    hw_clear_bits(&dma_hw->ch[dmaPingPong0].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_channel_abort(dmaPingPong0);
    dma_channel_set_irq0_enabled(dmaPingPong0, false);
    irq_set_enabled(DMA_IRQ_0, false);
    irq_remove_handler(DMA_IRQ_0, pg_stream_dma_handler);
    dma_channel_unclaim(dmaPingPong0);
    while (!adc_fifo_is_empty()) (void)adc_fifo_get();
    pgStreamReady = 0u;
    pgAdcStreamOn = false;
    captureFinished = true;
}

bool pg_adc_stream_is_eight_bit(void) { return pgAdcEightBit; }
