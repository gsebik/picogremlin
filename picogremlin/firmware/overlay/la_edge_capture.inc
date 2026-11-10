
#include "hardware/structs/timer.h"
#include "hardware/gpio.h"

/* ── PIO change-detector edge stream (CMD=0x47) ───────────────────────── */
static PIO  pgEdgePio;
static uint pgEdgeSm;
static uint pgEdgeOff;
static bool pgEdgeActive;

#define PG_EDGE_MAX 512
static uint32_t pgEdgeBuf[PG_EDGE_MAX * 2];   /* [dt, state] pairs, RAM only */

static uint8_t pgEdgeLo;      /* lowest selected channel (offset from INPUT_PIN_BASE) */
static uint8_t pgEdgeSpan;    /* contiguous pins read; state occupies bits (32-span)..31 */

static uint16_t pgEdgeIns[10];
static pio_program_t pgEdgeProg;
static uint8_t pgEdgeWrapTarget;
static uint8_t pgEdgeWrap;

static void pg_edge_build_program(uint8_t span) {
    pgEdgeIns[0] = (uint16_t)pio_encode_mov(pio_isr, pio_null);
    pgEdgeIns[1] = (uint16_t)pio_encode_in(pio_pins, span);
    pgEdgeIns[2] = (uint16_t)pio_encode_mov(pio_y, pio_isr);
    pgEdgeIns[3] = (uint16_t)pio_encode_mov(pio_isr, pio_null);   /* wrap_target */
    pgEdgeIns[4] = (uint16_t)pio_encode_in(pio_pins, span);
    pgEdgeIns[5] = (uint16_t)pio_encode_mov(pio_x, pio_isr);
    pgEdgeIns[6] = (uint16_t)pio_encode_jmp_x_ne_y(8);
    pgEdgeIns[7] = (uint16_t)pio_encode_jmp(3);
    pgEdgeIns[8] = (uint16_t)pio_encode_mov(pio_y, pio_x);
    pgEdgeIns[9] = (uint16_t)pio_encode_push(false, false);       /* push noblock */
    pgEdgeWrapTarget = 3;
    pgEdgeWrap = 9;
    pgEdgeProg.instructions = pgEdgeIns;
    pgEdgeProg.length = 10;
    pgEdgeProg.origin = -1;
    pgEdgeProg.pio_version = 1;
#if PICO_PIO_VERSION > 0
    pgEdgeProg.used_gpio_ranges = 0x3;
#endif
}

bool pg_edge_start(const uint8_t* capturePins, uint8_t capturePinCount, uint32_t div) {
    if (capturePinCount < 1 || capturePinCount > 16) return false;
    if (div == 0) div = 1;

    /* Read ONLY the selected contiguous span so unselected pins never produce events. */
    uint8_t lo = 0xFF, hi = 0;
    for (uint8_t i = 0; i < capturePinCount; i++) {
        uint8_t ch = capturePins[i];
        if (ch < lo) lo = ch;
        if (ch > hi) hi = ch;
    }
    if (hi > 15) return false;                 /* only the 16 readable lines exist */
    uint8_t span = (uint8_t)(hi - lo + 1);
    uint8_t in_base = (uint8_t)(INPUT_PIN_BASE + lo);
    pgEdgeLo = lo;
    pgEdgeSpan = span;

    pg_edge_build_program(span);

    pgEdgePio = pio2;
    pgEdgeSm = pio_claim_unused_sm(pgEdgePio, true);
    pio_sm_clear_fifos(pgEdgePio, pgEdgeSm);
    pio_sm_restart(pgEdgePio, pgEdgeSm);
    pgEdgeOff = pio_add_program(pgEdgePio, &pgEdgeProg);

    for (uint8_t i = 0; i < span; i++) {
        pio_gpio_init(pgEdgePio, in_base + i);
        pio_sm_set_consecutive_pindirs(pgEdgePio, pgEdgeSm, in_base + i, 1, false);
        gpio_pull_up(in_base + i);   /* hold unconnected inputs steady (no drive) */
    }

    pio_sm_config c = pio_get_default_sm_config();
    sm_config_set_in_pins(&c, in_base);
    sm_config_set_in_shift(&c, true, false, 0);   /* shift right, autopush OFF */
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_RX);
    sm_config_set_clkdiv(&c, (float)div);
    sm_config_set_wrap(&c, pgEdgeOff + pgEdgeWrapTarget, pgEdgeOff + pgEdgeWrap);
    pio_sm_init(pgEdgePio, pgEdgeSm, pgEdgeOff, &c);

    /* Clear sticky FDEBUG for this SM before the run: RXSTALL = bit sm (blocking push
       stalled on a full FIFO OR a nonblocking push discarded data), RXUNDER = bit sm+8. */
    pgEdgePio->fdebug = (1u << pgEdgeSm) | (1u << (pgEdgeSm + 8));

    /* TIMER1 counts clk_sys cycles -> ~5 ns ticks at 200 MHz (TIMER0 stays on us). */
    timer1_hw->source = 1;

    pio_sm_set_enabled(pgEdgePio, pgEdgeSm, true);
    pgEdgeActive = true;
    return true;
}

bool pg_edge_pop(uint32_t* state) {
    if (!pgEdgeActive || pio_sm_is_rx_fifo_empty(pgEdgePio, pgEdgeSm)) return false;
    *state = pio_sm_get(pgEdgePio, pgEdgeSm);
    return true;
}

uint32_t pg_edge_now(void) { return timer1_hw->timerawl; }
int pg_edge_off(void) { return (int)pgEdgeOff; }
uint32_t pg_edge_fifo_level(void) { return pio_sm_get_rx_fifo_level(pgEdgePio, pgEdgeSm); }
uint32_t pg_edge_pc(void) { return pio_sm_get_pc(pgEdgePio, pgEdgeSm); }
uint32_t pg_edge_fdebug(void) { return pgEdgePio->fdebug; }
uint32_t* pg_edge_buf(void) { return pgEdgeBuf; }
uint32_t pg_edge_max(void) { return PG_EDGE_MAX; }
uint32_t pg_edge_lo(void) { return pgEdgeLo; }
uint32_t pg_edge_span(void) { return pgEdgeSpan; }

void pg_edge_stop(void) {
    if (!pgEdgeActive) return;
    pio_sm_set_enabled(pgEdgePio, pgEdgeSm, false);
    pio_remove_program(pgEdgePio, &pgEdgeProg, pgEdgeOff);
    pio_sm_unclaim(pgEdgePio, pgEdgeSm);
    pgEdgeActive = false;
}
