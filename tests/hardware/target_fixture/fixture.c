/*
 * PicoGremlin hardware-test target fixture.
 *
 * Runs on the RP2350 target (Pico 2 / Pico 2 W) wired to the PicoGremlin probe
 * through SWD, UART0 and GPIO16/GPIO17.
 *
 * Features (one firmware, all runtime-configurable):
 *   - volatile debugger-visible state (counter, toggle GPIO, config) for GDB
 *   - deterministic breakpoint function for SWD debug tests
 *   - UART0 command interface on GP0 (TX) / GP1 (RX) for UART bridge tests
 *   - two PIO square-wave generators on GPIO16 / GPIO17 with independent
 *     frequency, duty cycle and phase
 *
 * UART framing (8N1, host sets baud through the PicoGremlin CDC0 bridge):
 *   'I'                    -> identity text (LF terminated)
 *   'R'                    -> status text (LF terminated)
 *   'C' ch f(4LE) d(2LE 0-1000) p(2LE 0-3600)   -> configure channel
 *   'E' len(4LE) <len bytes>                     -> echo len bytes back
 *   'P' pat len(4LE)                             -> transmit len pattern bytes + CRC32(4LE)
 *   'S' pat len(4LE)                             -> start continuous stream (pattern repeated)
 *   'X'                                          -> stop stream
 *   'B' baud(4LE)                                -> reinit UART0 at baud
 *   'T' n(1) size(4LE)                           -> burst: n packets of size bytes + CRC32 each
 *   'Z'                                          -> reset generator to defaults
 *   'M' mask(4LE)                                -> drive all 24 LA pins to mask (SIO out)
 *   'K' toggle(4LE) static(4LE) hz(4LE)          -> toggle mask bits at hz, hold rest
 *   'F' base_hz(4LE)                             -> frequency comb, ch k = base/(k+1); 0 stops
 *   'G' ch_a(1) ch_b(1)                          -> retarget the precision generator pair
 *   'D' pull(1)                                  -> all 24 pins SIO input (0 none, 1 down, 2 up)
 *   'W' en(1)                                    -> enable/disable the GP22 heartbeat toggle
 *
 * Pattern ids: 0 = A..Z, 1 = 0..9, 2 = 0x55, 3 = 0xAA, 4 = 0x00..0xFF, 5 = PRNG.
 *
 * Responses: 'K' (ok ack), '!' (error). Echo command returns the payload itself.
 */
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "pico/stdlib.h"
#include "hardware/uart.h"
#include "hardware/pio.h"
#include "hardware/clocks.h"
#include "siggen.pio.h"

/* ------------------------------------------------------------------ */
/* Debugger-visible state (volatile, not optimized away)              */
/* ------------------------------------------------------------------ */
volatile uint32_t g_counter = 0;        /* increments once per main-loop iteration */
volatile uint32_t g_bp_hits = 0;        /* times breakpoint_hit() ran              */
volatile uint32_t g_toggle_count = 0;   /* GPIO toggle events                      */
volatile bool     g_toggle_state = false;
volatile uint32_t g_toggle_pin = 22;    /* GPIO toggled every TogglePeriodMs       */
volatile uint32_t g_toggle_period_ms = 100;
volatile uint32_t g_loop_speed = 0;     /* main-loop iterations/second estimate    */

#define CHAN_A_GPIO_DEFAULT 16
#define CHAN_B_GPIO_DEFAULT 17
volatile uint32_t g_ch_a_freq = 1000;   /* Hz     */
volatile uint32_t g_ch_a_duty = 500;    /* 0-1000 permille */
volatile uint32_t g_ch_a_phase = 0;     /* 0-3600 tenths of a degree */
volatile uint32_t g_ch_b_freq = 1000;
volatile uint32_t g_ch_b_duty = 500;
volatile uint32_t g_ch_b_phase = 0;
volatile bool     g_fast_mode = false;  /* fast 50 % toggle mode active */

/* LA channels are identity-wired: LA channel k connects to target GPIO
 * ch_gpio[k]. The LA's own pinMap (probe side) is {2..22, 26, 27, 28}. */
static const uint8_t ch_gpio[24] = {
    2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,
    14, 15, 16, 17, 18, 19, 20, 21, 22, 26, 27, 28
};
static uint32_t all_pin_mask(void)
{
    uint32_t m = 0;
    for (int k = 0; k < 24; k++) m |= (1u << ch_gpio[k]);
    return m;
}

/* Translate a channel bitmask (bit k = channel k) into GPIO-level bits. */
static uint32_t channels_to_gpio(uint32_t chmask)
{
    uint32_t v = 0;
    for (int k = 0; k < 24; k++)
        if ((chmask >> k) & 1) v |= (1u << ch_gpio[k]);
    return v;
}

/* The two precision PIO generators can be retargeted to any channel pair. */
static int g_gen_a_gpio = CHAN_A_GPIO_DEFAULT;
static int g_gen_b_gpio = CHAN_B_GPIO_DEFAULT;

/* CPU-driven channel engine state (exclusive of the PIO generators) */
static bool comb_active;
static uint32_t comb_base;              /* Hz of channel 0; ch k = base/(k+1) */
static absolute_time_t comb_start;
static uint32_t comb_last[24];
static bool walk_active;
static uint32_t walk_toggle, walk_static, walk_hz;
static uint32_t exclude_mask;      /* channels never driven (bit k = channel k) */
static uint64_t walk_start_us;
static bool heart_en = true;            /* GP22 heartbeat gate */

/* ------------------------------------------------------------------ */
/* UART0 on GP0 (TX) / GP1 (RX)                                        */
/* ------------------------------------------------------------------ */
#define UART_ID uart0
#define UART_TX_PIN 0
#define UART_RX_PIN 1
#define UART_BAUD_DEFAULT 115200

static bool stream_active = false;
static uint8_t stream_pat = 0;
static uint32_t stream_len = 1;

static void uart_put_byte(uint8_t b) { uart_putc_raw(UART_ID, b); }

static void uart_str(const char *s)
{
    while (*s) uart_put_byte((uint8_t)*s++);
}

static void uart_put_u32le(uint32_t v)
{
    uart_put_byte(v & 0xff);
    uart_put_byte((v >> 8) & 0xff);
    uart_put_byte((v >> 16) & 0xff);
    uart_put_byte((v >> 24) & 0xff);
}

static volatile bool uart_cmd_ok = true;
static bool uart_read_byte(uint8_t *b)
{
    absolute_time_t deadline = make_timeout_time_us(200000);
    while (absolute_time_diff_us(get_absolute_time(), deadline) > 0) {
        if (uart_is_readable(UART_ID)) { *b = (uint8_t)uart_getc(UART_ID); return true; }
    }
    uart_cmd_ok = false;
    return false;
}

static uint32_t uart_get_u32le_wait(void)
{
    uint32_t v = 0;
    for (int i = 0; i < 4 && uart_cmd_ok; i++) {
        uint8_t b;
        if (!uart_read_byte(&b)) break;
        v |= ((uint32_t)b) << (8 * i);
    }
    return v;
}

static uint16_t uart_get_u16_wait(void)
{
    uint16_t v = 0;
    for (int i = 0; i < 2 && uart_cmd_ok; i++) {
        uint8_t b;
        if (!uart_read_byte(&b)) break;
        v |= ((uint16_t)b) << (8 * i);
    }
    return v;
}

static uint8_t uart_getc_wait(void)
{
    uint8_t b = 0;
    if (uart_cmd_ok) (void)uart_read_byte(&b);
    return b;
}

static void uart_put_u32_dec(uint32_t v)
{
    char tmp[12];
    int ti = 0;
    if (v == 0) { uart_put_byte('0'); return; }
    while (v) { tmp[ti++] = (char)('0' + (v % 10)); v /= 10; }
    while (ti) uart_put_byte((uint8_t)tmp[--ti]);
}

static void uart_init_hw(uint32_t baud)
{
    uart_init(UART_ID, baud);
    gpio_set_function(UART_TX_PIN, GPIO_FUNC_UART);
    gpio_set_function(UART_RX_PIN, GPIO_FUNC_UART);
    /* Idle a floating RX high: with the probe's bridge TX high-Z (no host has the serial
     * port open), the pad's default pull-down would otherwise read as a permanent break. */
    gpio_pull_up(UART_RX_PIN);
    uart_set_hw_flow(UART_ID, false, false);
    uart_set_format(UART_ID, 8, 1, UART_PARITY_NONE);
    uart_set_fifo_enabled(UART_ID, true);
}

/* ------------------------------------------------------------------ */
/* CRC32 (IEEE 802.3) for payload integrity verification               */
/* ------------------------------------------------------------------ */
static uint32_t crc32_update(uint32_t crc, const uint8_t *data, uint32_t len)
{
    for (uint32_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++)
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return crc;
}

/* ------------------------------------------------------------------ */
/* Pseudo-random generator (xorshift32) for pattern id 5               */
/* ------------------------------------------------------------------ */
static uint32_t prng_state = 0x12345678u;
static uint8_t prng_byte(void)
{
    prng_state ^= prng_state << 13;
    prng_state ^= prng_state >> 17;
    prng_state ^= prng_state << 5;
    return (uint8_t)(prng_state >> 24);
}

static void make_pattern(uint8_t id, uint32_t idx, uint8_t *out)
{
    switch (id) {
    case 0: *out = (uint8_t)('A' + (idx % 26)); break;
    case 1: *out = (uint8_t)('0' + (idx % 10)); break;
    case 2: *out = 0x55; break;
    case 3: *out = 0xAA; break;
    case 4: *out = (uint8_t)idx; break;
    default: *out = prng_byte(); break;
    }
}

/* ------------------------------------------------------------------ */
/* PIO square-wave generator on GPIO16 (SM A) and GPIO17 (SM B)        */
/* ------------------------------------------------------------------ */
static PIO sig_pio = pio0;
static int sm_a = -1, sm_b = -1;
static int off_sig = -1, off_fast = -1;

#define SM_OVERHEAD 2u   /* set + mov ticks per phase */

static void pio_release_all(void)
{
    if (sm_a >= 0) { pio_sm_set_enabled(sig_pio, (uint)sm_a, false); pio_sm_unclaim(sig_pio, (uint)sm_a); sm_a = -1; }
    if (sm_b >= 0) { pio_sm_set_enabled(sig_pio, (uint)sm_b, false); pio_sm_unclaim(sig_pio, (uint)sm_b); sm_b = -1; }
    if (off_sig >= 0) { pio_remove_program(sig_pio, &siggen_program, (uint)off_sig); off_sig = -1; }
    if (off_fast >= 0) { pio_remove_program(sig_pio, &siggen_fast_program, (uint)off_fast); off_fast = -1; }
}

static void delay_ticks(uint32_t ticks)
{
    while (ticks--) { __asm volatile("nop"); }
}

/* Fast mode: both channels same frequency, fixed 50 % duty, period = 2 SM
 * clock ticks. Phase B is applied by delaying B's start relative to A. */
static bool pio_start_fast(uint32_t freq_hz, uint32_t phase_tenths_deg)
{
    uint32_t sysclk = clock_get_hz(clk_sys);
    if (freq_hz == 0 || freq_hz > sysclk / 2) return false;

    off_fast = pio_add_program(sig_pio, &siggen_fast_program);
    sm_a = pio_claim_unused_sm(sig_pio, true);
    sm_b = pio_claim_unused_sm(sig_pio, true);
    if (off_fast < 0 || sm_a < 0 || sm_b < 0) { pio_release_all(); return false; }

    float div = (float)sysclk / (float)(freq_hz * 2u);
    if (div < 1.0f) div = 1.0f;

    pio_sm_config c = siggen_fast_program_get_default_config(off_fast);
    sm_config_set_set_pins(&c, g_gen_a_gpio, 1);
    sm_config_set_clkdiv(&c, div);
    pio_sm_init(sig_pio, (uint)sm_a, off_fast, &c);
    pio_sm_set_consecutive_pindirs(sig_pio, (uint)sm_a, g_gen_a_gpio, 1, true);
    pio_gpio_init(sig_pio, g_gen_a_gpio);

    pio_sm_config cb = siggen_fast_program_get_default_config(off_fast);
    sm_config_set_set_pins(&cb, g_gen_b_gpio, 1);
    sm_config_set_clkdiv(&cb, div);
    pio_sm_init(sig_pio, (uint)sm_b, off_fast, &cb);
    pio_sm_set_consecutive_pindirs(sig_pio, (uint)sm_b, g_gen_b_gpio, 1, true);
    pio_gpio_init(sig_pio, g_gen_b_gpio);

    uint32_t delay_ticks_sys = 0;
    if (phase_tenths_deg > 0) {
        uint64_t period_ticks = (uint64_t)sysclk / freq_hz;
        delay_ticks_sys = (uint32_t)((period_ticks * phase_tenths_deg) / 3600u);
        if (delay_ticks_sys == 0) delay_ticks_sys = 1;
    }

    pio_sm_set_enabled(sig_pio, (uint)sm_a, true);
    if (delay_ticks_sys) delay_ticks(delay_ticks_sys);
    pio_sm_set_enabled(sig_pio, (uint)sm_b, true);
    g_fast_mode = true;
    return true;
}

/* Normal mode: independent freq / duty / phase via pull-based program. */
static bool pio_start_normal(uint32_t freq_hz, uint32_t duty_permille,
                             uint32_t phase_tenths_deg, int gpio, int *sm_out)
{
    uint32_t sysclk = clock_get_hz(clk_sys);
    uint64_t period = (uint64_t)sysclk / freq_hz;
    if (period < 12) return false;
    uint64_t high = (period * duty_permille) / 1000u;
    uint64_t low = period - high;
    // `jmp x-- loop` runs x+1 times, so a phase of `n` ticks needs x = n-3
    // (set + mov + (x+1) jumps). Require room for x >= 1.
    if (high < SM_OVERHEAD + 2 || low < SM_OVERHEAD + 2) return false;

    int sm = pio_claim_unused_sm(sig_pio, true);
    if (sm < 0) return false;

    pio_sm_config c = siggen_program_get_default_config(off_sig);
    sm_config_set_set_pins(&c, (uint)gpio, 1);
    pio_sm_init(sig_pio, (uint)sm, off_sig, &c);
    pio_sm_set_consecutive_pindirs(sig_pio, (uint)sm, gpio, 1, true);
    pio_gpio_init(sig_pio, gpio);
    pio_sm_set_pins(sig_pio, (uint)sm, 0);            /* start LOW */

    uint32_t phase_ticks = 0;
    if (phase_tenths_deg > 0) {
        phase_ticks = (uint32_t)((period * phase_tenths_deg) / 3600u);
        if (phase_ticks == 0) phase_ticks = 1;
    }

    pio_sm_put_blocking(sig_pio, (uint)sm, phase_ticks);
    pio_sm_put_blocking(sig_pio, (uint)sm, (uint32_t)(high - SM_OVERHEAD - 1));
    pio_sm_put_blocking(sig_pio, (uint)sm, (uint32_t)(low - SM_OVERHEAD - 1));
    /* SM is NOT enabled here; apply_signal_config() enables both together so
     * their relative phase comes only from the programmed phase_delay and not
     * from the CPU time spent configuring the second SM. */
    *sm_out = sm;
    return true;
}

/* Reconfigure both channels from the volatile globals. */
static void apply_signal_config(void)
{
    comb_active = false;
    walk_active = false;
    pio_release_all();
    g_fast_mode = false;
    off_sig = pio_add_program(sig_pio, &siggen_program);

    uint32_t sysclk = clock_get_hz(clk_sys);
    bool fast_a = (g_ch_a_freq == 0) || ((uint64_t)sysclk / g_ch_a_freq) < 12;
    bool fast_b = (g_ch_b_freq == 0) || ((uint64_t)sysclk / g_ch_b_freq) < 12;

    if (fast_a && fast_b && g_ch_a_freq == g_ch_b_freq && g_ch_a_duty == 500 && g_ch_b_duty == 500) {
        pio_start_fast(g_ch_a_freq, g_ch_b_phase);
        return;
    }

    if (!fast_a) pio_start_normal(g_ch_a_freq, g_ch_a_duty, g_ch_a_phase, g_gen_a_gpio, &sm_a);
    if (!fast_b) pio_start_normal(g_ch_b_freq, g_ch_b_duty, g_ch_b_phase, g_gen_b_gpio, &sm_b);

    /* Start both SMs simultaneously so the phase offset equals the difference
     * of the programmed phase delays (and not the CPU time between two
     * sequential pio_sm_set_enabled calls). */
    if (sm_a >= 0) pio_sm_set_enabled(sig_pio, (uint)sm_a, true);
    if (sm_b >= 0) pio_sm_set_enabled(sig_pio, (uint)sm_b, true);
}

/* ------------------------------------------------------------------ */
/* CPU-driven 24-channel engine (static / walk / comb / input modes)   */
/* ------------------------------------------------------------------ */
static void engine_stop(void)
{
    comb_active = false;
    walk_active = false;
    pio_release_all();
    g_fast_mode = false;
}

static void engine_claim_outputs(void)
{
    for (int k = 0; k < 24; k++) {
        gpio_init(ch_gpio[k]);
        /* an excluded channel (e.g. a buzzer on the bench) stays an input, so
         * no signal mode can ever drive it */
        gpio_set_dir(ch_gpio[k], (exclude_mask & (1u << k)) ? GPIO_IN : GPIO_OUT);
    }
}

static void signal_set_static(uint32_t mask)
{
    engine_stop();
    heart_en = false;
    engine_claim_outputs();
    gpio_put_masked(all_pin_mask(), channels_to_gpio(mask & ~exclude_mask));
}

static void signal_set_inputs(uint8_t pull)
{
    engine_stop();
    heart_en = false;
    for (int k = 0; k < 24; k++) {
        gpio_init(ch_gpio[k]);            /* SIO input, no pull */
        if (pull == 1) gpio_pull_down(ch_gpio[k]);
        else if (pull == 2) gpio_pull_up(ch_gpio[k]);
    }
}

static bool signal_comb(uint32_t base_hz)
{
    if (base_hz == 0) { comb_active = false; return true; }
    if (base_hz > 10000000u) return false;
    engine_stop();
    heart_en = false;
    engine_claim_outputs();
    memset(comb_last, 0, sizeof(comb_last));
    comb_base = base_hz;
    comb_start = get_absolute_time();
    comb_active = true;
    return true;
}

static bool signal_walk(uint32_t toggle, uint32_t static_mask, uint32_t hz)
{
    if (hz == 0 || hz > 1000000u || (toggle & static_mask)) return false;
    engine_stop();
    heart_en = false;
    engine_claim_outputs();
    walk_toggle = toggle;
    walk_static = static_mask;
    walk_hz = hz;
    walk_start_us = (uint64_t)to_us_since_boot(get_absolute_time());
    walk_active = true;
    return true;
}

/* Call every main-loop iteration while the comb or walk engine is active. */
static void engine_service(void)
{
    if (comb_active) {
        uint32_t us = (uint32_t)absolute_time_diff_us(comb_start, get_absolute_time());
        for (int k = 0; k < 24; k++) {
            if (exclude_mask & (1u << k)) continue;
            uint64_t half_period = (500000ULL * (k + 1)) / comb_base;
            if (half_period == 0) continue;
            uint32_t want = (uint32_t)(us / half_period);
            if (want != comb_last[k]) {
                gpio_xor_mask(1u << ch_gpio[k]);
                comb_last[k] = want;
            }
        }
    }
    if (walk_active) {
        uint64_t us = (uint64_t)to_us_since_boot(get_absolute_time()) - walk_start_us;
        uint64_t period = 1000000ULL / walk_hz;
        bool level = (us % period) < (period / 2u);
        uint32_t chstate = ((level ? walk_toggle : 0u) | (walk_static & ~walk_toggle))
                           & ~exclude_mask;
        gpio_put_masked(all_pin_mask(), channels_to_gpio(chstate));
    }
}

/* ------------------------------------------------------------------ */
/* Deterministic breakpoint for GDB debug tests                        */
/* ------------------------------------------------------------------ */
__attribute__((noinline)) void breakpoint_hit(void)
{
    g_bp_hits++;
}

/* ------------------------------------------------------------------ */
/* Command handling                                                    */
/* ------------------------------------------------------------------ */
static void handle_command(uint8_t cmd)
{
    uint32_t i;

    uart_cmd_ok = true;

    switch (cmd) {
    case 'I':
        uart_str("PICOGREMLIN_TARGET_FIXTURE_V1\n");
        break;

    case 'R': {
        uart_str("counter="); uart_put_u32_dec(g_counter); uart_put_byte('\n');
        uart_str("bp_hits="); uart_put_u32_dec(g_bp_hits); uart_put_byte('\n');
        uart_str("toggle_count="); uart_put_u32_dec(g_toggle_count); uart_put_byte('\n');
        uart_str("loop_speed="); uart_put_u32_dec(g_loop_speed); uart_put_byte('\n');
        uart_str("ch_a="); uart_put_u32_dec(g_ch_a_freq); uart_put_byte(',');
        uart_put_u32_dec(g_ch_a_duty); uart_put_byte(',');
        uart_put_u32_dec(g_ch_a_phase); uart_put_byte('\n');
        uart_str("ch_b="); uart_put_u32_dec(g_ch_b_freq); uart_put_byte(',');
        uart_put_u32_dec(g_ch_b_duty); uart_put_byte(',');
        uart_put_u32_dec(g_ch_b_phase); uart_put_byte('\n');
        uart_str("fast_mode="); uart_put_byte(g_fast_mode ? '1' : '0'); uart_put_byte('\n');
        uart_str("gen_a="); uart_put_u32_dec(g_gen_a_gpio); uart_put_byte('\n');
        uart_str("gen_b="); uart_put_u32_dec(g_gen_b_gpio); uart_put_byte('\n');
        uart_str("comb="); uart_put_u32_dec(comb_base); uart_put_byte(',');
        uart_put_u32_dec(comb_active ? 1u : 0u); uart_put_byte('\n');
        uart_str("walk="); uart_put_u32_dec(walk_active ? 1u : 0u); uart_put_byte(',');
        uart_put_u32_dec(walk_hz); uart_put_byte('\n');
        uart_str("heart="); uart_put_u32_dec(heart_en ? 1u : 0u); uart_put_byte('\n');
        break;
    }

    case 'C': {  /* C ch f(4) d(2) p(2) */
        uint8_t ch = uart_getc_wait();
        uint32_t f = uart_get_u32le_wait();
        uint16_t d = uart_get_u16_wait();
        uint16_t p = uart_get_u16_wait();
        if (ch == 0) { g_ch_a_freq = f; g_ch_a_duty = d; g_ch_a_phase = p; }
        else         { g_ch_b_freq = f; g_ch_b_duty = d; g_ch_b_phase = p; }
        apply_signal_config();
        uart_put_byte('K');
        break;
    }

    case 'E': {  /* E len(4) <payload> -> echo */
        uint32_t len = uart_get_u32le_wait();
        if (len > 1048576u) { uart_put_byte('!'); break; }
        for (i = 0; i < len; i++) uart_put_byte(uart_getc_wait());
        break;
    }

    case 'P': {  /* P pat len(4) -> pattern + crc32(4) */
        uint8_t pat = uart_getc_wait();
        uint32_t len = uart_get_u32le_wait();
        if (len > 1048576u) { uart_put_byte('!'); break; }
        prng_state = 0x12345678u;   /* deterministic per command */
        uint32_t crc = 0xFFFFFFFFu;
        for (i = 0; i < len; i++) {
            uint8_t b; make_pattern(pat, i, &b);
            uart_put_byte(b);
            uint8_t one = b;
            crc = crc32_update(crc, &one, 1);
        }
        uart_put_u32le(crc ^ 0xFFFFFFFFu);
        break;
    }

    case 'S':  /* S pat len(4) -> start stream */
        stream_pat = uart_getc_wait();
        stream_len = uart_get_u32le_wait();
        if (stream_len == 0 || stream_len > 1048576u) stream_len = 1;
        stream_active = true;
        uart_put_byte('K');
        break;

    case 'X':
        stream_active = false;
        uart_put_byte('K');
        break;

    case 'B': {  /* B baud(4) -> reinit UART */
        uint32_t baud = uart_get_u32le_wait();
        if (baud < 300 || baud > 4000000u) { uart_put_byte('!'); break; }
        uart_deinit(UART_ID);
        uart_init_hw(baud);
        uart_put_byte('K');
        break;
    }

    case 'T': {  /* T n(1) size(4) -> n packets of size bytes + crc32 */
        uint32_t n = uart_getc_wait();
        uint32_t size = uart_get_u32le_wait();
        if (n == 0 || n > 255 || size == 0 || size > 65536u) { uart_put_byte('!'); break; }
        for (i = 0; i < n; i++) {
            uint32_t pcrc = 0xFFFFFFFFu;
            for (uint32_t j = 0; j < size; j++) {
                uint8_t b = (uint8_t)((i * size + j) & 0xff);
                uart_put_byte(b);
                uint8_t one = b;
                pcrc = crc32_update(pcrc, &one, 1);
            }
            uart_put_u32le(pcrc ^ 0xFFFFFFFFu);
        }
        break;
    }

    case 'Z':
        g_ch_a_freq = 1000; g_ch_a_duty = 500; g_ch_a_phase = 0;
        g_ch_b_freq = 1000; g_ch_b_duty = 500; g_ch_b_phase = 0;
        g_gen_a_gpio = CHAN_A_GPIO_DEFAULT;
        g_gen_b_gpio = CHAN_B_GPIO_DEFAULT;
        heart_en = true;
        apply_signal_config();
        uart_put_byte('K');
        break;

    case 'M': {  /* M mask(4) -> drive all 24 pins to mask as SIO outputs */
        uint32_t mask = uart_get_u32le_wait();
        signal_set_static(mask);
        uart_put_byte('K');
        break;
    }

    case 'K': {  /* K toggle(4) static(4) hz(4) -> toggle mask bits at hz */
        uint32_t toggle = uart_get_u32le_wait();
        uint32_t static_mask = uart_get_u32le_wait();
        uint32_t hz = uart_get_u32le_wait();
        if (!signal_walk(toggle, static_mask, hz)) { uart_put_byte('!'); break; }
        uart_put_byte('K');
        break;
    }

    case 'F': {  /* F base_hz(4) -> frequency comb, ch k = base/(k+1); 0=stop */
        uint32_t base_hz = uart_get_u32le_wait();
        if (!signal_comb(base_hz)) { uart_put_byte('!'); break; }
        uart_put_byte('K');
        break;
    }

    case 'G': {  /* G ch_a(1) ch_b(1) -> retarget precision generator pair */
        uint8_t a = uart_getc_wait();
        uint8_t b = uart_getc_wait();
        if (a > 23 || b > 23 || a == b ||
            (exclude_mask & ((1u << a) | (1u << b)))) { uart_put_byte('!'); break; }
        comb_active = false; walk_active = false;
        int old_a = g_gen_a_gpio, old_b = g_gen_b_gpio;
        g_gen_a_gpio = ch_gpio[a];
        g_gen_b_gpio = ch_gpio[b];
        pio_release_all();
        g_fast_mode = false;
        gpio_init(old_a); gpio_init(old_b);     /* release to high-Z input */
        apply_signal_config();
        uart_put_byte('K');
        break;
    }

    case 'N': {  /* N mask(4) -> channels that must never be driven (bit k = ch k) */
        uint32_t mask = uart_get_u32le_wait();
        exclude_mask = mask & 0xFFFFFFu;
        /* any channel that just became excluded is released to an input */
        for (int k = 0; k < 24; k++)
            if (exclude_mask & (1u << k)) gpio_set_dir(ch_gpio[k], GPIO_IN);
        uart_put_byte('K');
        break;
    }

    case 'D': {  /* D pull(1) -> all 24 pins SIO input, 0=none 1=down 2=up */
        uint8_t pull = uart_getc_wait();
        if (pull > 2) { uart_put_byte('!'); break; }
        signal_set_inputs(pull);
        uart_put_byte('K');
        break;
    }

    case 'W': {  /* W en(1) -> enable/disable the GP22 heartbeat toggle */
        uint8_t en = uart_getc_wait();
        heart_en = (en != 0);
        if (heart_en) gpio_init(g_toggle_pin), gpio_set_dir(g_toggle_pin, GPIO_OUT);
        uart_put_byte('K');
        break;
    }

    case 'A': {  /* A baud(4) pat(1) len(4) -> one-shot autonomous transmit
                  * (used for autobaud measurement on the probe side) */
        uint32_t baud = uart_get_u32le_wait();
        uint8_t pat = uart_getc_wait();
        uint32_t len = uart_get_u32le_wait();
        if (baud < 300 || baud > 4000000u || len > 1048576u) { uart_put_byte('!'); break; }
        uart_deinit(UART_ID);
        uart_init_hw(baud);
        for (i = 0; i < 1000; i++) { }          /* small settle */
        sleep_ms(300);                          /* host switches bridge to autobaud */
        prng_state = 0x12345678u;
        for (i = 0; i < len; i++) {
            uint8_t b; make_pattern(pat, i, &b);
            uart_put_byte(b);
        }
        uart_deinit(UART_ID);
        uart_init_hw(UART_BAUD_DEFAULT);        /* back to 115200 */
        uart_put_byte('K');                     /* at 115200 */
        break;
    }

    default:
        uart_put_byte('!');
        break;
    }

    /* A truncated command must never wedge the main loop: drain any leftover
     * bytes and resume. */
    if (!uart_cmd_ok) {
        while (uart_is_readable(UART_ID)) (void)uart_getc(UART_ID);
        uart_cmd_ok = true;
    }
}

/* ------------------------------------------------------------------ */
/* Main                                                                */
/* ------------------------------------------------------------------ */
int main(void)
{
    set_sys_clock_khz(200000, true);
    uart_init_hw(UART_BAUD_DEFAULT);

    gpio_init(g_toggle_pin);
    gpio_set_dir(g_toggle_pin, GPIO_OUT);

    apply_signal_config();

    absolute_time_t last_toggle = get_absolute_time();
    absolute_time_t last_speed = get_absolute_time();
    uint32_t last_counter = 0;

    while (true) {
        g_counter++;

        if (uart_is_readable(UART_ID)) {
            uint8_t cmd = (uint8_t)uart_getc(UART_ID);
            handle_command(cmd);
        }

        if (stream_active) {
            uint8_t b; make_pattern(stream_pat, (uint32_t)(g_counter % stream_len), &b);
            uart_put_byte(b);
        }

        engine_service();

        if (heart_en && absolute_time_diff_us(last_toggle, get_absolute_time()) >= (int64_t)g_toggle_period_ms * 1000) {
            g_toggle_state = !g_toggle_state;
            gpio_put(g_toggle_pin, g_toggle_state);
            g_toggle_count++;
            last_toggle = get_absolute_time();
        }

        if ((g_counter & 0x000FFFFFu) == 0) breakpoint_hit();  /* ~ every 1M iters */

        if (absolute_time_diff_us(last_speed, get_absolute_time()) >= 1000000) {
            g_loop_speed = g_counter - last_counter;
            last_counter = g_counter;
            last_speed = get_absolute_time();
        }
    }
}