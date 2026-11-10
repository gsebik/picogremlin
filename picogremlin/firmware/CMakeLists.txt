# PicoGremlin firmware — build.
#
# Pulls sources from the two sibling upstream trees:
#   ../../debugprobe/src                            (CMSIS-DAP, UART bridge, SWD PIO)
#   ../../logicanalyzer/Firmware/LogicAnalyzer_V2   (capture engine + LA loop)
# and overlays the PicoGremlin integration layer in THIS directory.

cmake_minimum_required(VERSION 3.13)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# --- Target board ---------------------------------------------------------------
# Actual hardware: Raspberry Pi Pico 2 W (RP2350A, 26 header GPIO).
# GP0/GP1 are the LA complex-trigger loopback; GP23/29 are SWCLK/SWDIO and
# GP24/25 are UART1. The remaining LA channels are GP2..22 and GP26..28.
if(NOT PICO_BOARD)
    set(PICO_BOARD pico2_w CACHE STRING "Board type")
endif()
if(NOT PICO_BOARD STREQUAL "pico2_w")
    message(FATAL_ERROR
        "PicoGremlin supports only PICO_BOARD=pico2_w (RP2350A Pico 2 W); "
        "received PICO_BOARD=${PICO_BOARD}")
endif()

include(pico_sdk_import.cmake)
project(picogremlin C CXX ASM)
pico_sdk_init()

# --- Required SDK capability check ----------------------------------------------
# The runtime-reset path (usb_descriptors.c / reset_driver.c / main.c) depends on
# the pico_usb_reset library (public header <pico/usb_reset.h>), first shipped in
# pico-sdk 2.3.0. Older SDKs (<= 2.2.0, incl. the pico VS Code extension's managed
# 2.1.1/2.2.0) keep the reset constants in <pico/usb_reset_interface.h> and the
# TUD_RPI_RESET_* macros private inside stdio_usb_descriptors.c, so the build
# otherwise dies with a confusing "fatal error: pico/usb_reset.h: No such file or
# directory". Fail early with actionable guidance instead.
if(NOT TARGET pico_usb_reset)
    message(FATAL_ERROR
        "PicoGremlin requires a pico-sdk providing the 'pico_usb_reset' library "
        "(public header <pico/usb_reset.h>) -- pico-sdk 2.3.0 or newer.\n"
        "  PICO_SDK_PATH           = ${PICO_SDK_PATH}\n"
        "  PICO_SDK_VERSION_STRING = ${PICO_SDK_VERSION_STRING}\n"
        "Clone the 2.3.1 tag (README section 2) and point PICO_SDK_PATH at it:\n"
        "  git clone --recurse-submodules --branch 2.3.1 https://github.com/raspberrypi/pico-sdk ~/pico-sdk\n"
        "  export PICO_SDK_PATH=~/pico-sdk\n"
        "2.3.1 also needs picotool 2.3.0; if configure complains, add:\n"
        "  -Dpicotool_DIR=<pico-install>/picotool/2.3.0/picotool")
endif()

set(DEBUGPROBE_ROOT ${CMAKE_CURRENT_LIST_DIR}/../../debugprobe)
set(DEBUGPROBE_SRC  ${DEBUGPROBE_ROOT}/src)
set(CMSIS_DAP_SRC   ${DEBUGPROBE_ROOT}/CMSIS_DAP/CMSIS/DAP/Firmware/Source)
set(CMSIS_DAP_INC   ${DEBUGPROBE_ROOT}/CMSIS_DAP/CMSIS/DAP/Firmware/Include)
set(CMSIS_CORE_INC  ${DEBUGPROBE_ROOT}/CMSIS_DAP/CMSIS/Core/Include)
set(LA_SRC          ${CMAKE_CURRENT_LIST_DIR}/../../logicanalyzer/Firmware/LogicAnalyzer_V2)

# Keep the submodules at public upstream commits.  The small, PicoGremlin-specific
# changes are stored as patches in this repository and applied only to copies in
# the build directory; building must never dirty either submodule.
find_program(PATCH_EXECUTABLE patch REQUIRED)
find_package(Git REQUIRED)
set(MERGED_OVERLAY_DIR ${CMAKE_CURRENT_BINARY_DIR}/picogremlin-overlays)
set(DEBUGPROBE_OVERLAY_SRC ${MERGED_OVERLAY_DIR}/debugprobe/src)
set(LA_OVERLAY_SRC ${MERGED_OVERLAY_DIR}/logicanalyzer/Firmware/LogicAnalyzer_V2)
file(MAKE_DIRECTORY ${DEBUGPROBE_OVERLAY_SRC} ${LA_OVERLAY_SRC})

function(require_submodule_commit SUBMODULE_PATH EXPECTED_COMMIT)
    execute_process(
        COMMAND ${GIT_EXECUTABLE} -C ${SUBMODULE_PATH} rev-parse HEAD
        OUTPUT_VARIABLE ACTUAL_COMMIT
        OUTPUT_STRIP_TRAILING_WHITESPACE
        RESULT_VARIABLE GIT_RESULT
    )
    if (NOT GIT_RESULT EQUAL 0 OR NOT ACTUAL_COMMIT STREQUAL EXPECTED_COMMIT)
        message(FATAL_ERROR
            "PicoGremlin overlay expects ${SUBMODULE_PATH} at ${EXPECTED_COMMIT}; "
            "update its patch and this pinned commit together.")
    endif()
endfunction()

# The patches below are intentionally valid for only these reviewed upstream bases.
require_submodule_commit(${DEBUGPROBE_ROOT} 262f9625d75a2bad8d76cc228ee5e21ca3a64d3a)
require_submodule_commit(${CMAKE_CURRENT_LIST_DIR}/../../logicanalyzer 4e88df784e4879c1f180456a72d71910dd15faf1)

function(copy_source_as_lf INPUT_FILE OUTPUT_FILE)
    file(READ ${INPUT_FILE} CONTENTS)
    string(REPLACE "\r\n" "\n" CONTENTS "${CONTENTS}")
    file(WRITE ${OUTPUT_FILE} "${CONTENTS}")
endfunction()

foreach (FILE probe.c probe.pio sw_dp_pio.c)
    copy_source_as_lf(${DEBUGPROBE_SRC}/${FILE} ${DEBUGPROBE_OVERLAY_SRC}/${FILE})
endforeach()
foreach (FILE LogicAnalyzer.c LogicAnalyzer_Capture.c LogicAnalyzer_Board_Settings.h)
    copy_source_as_lf(${LA_SRC}/${FILE} ${LA_OVERLAY_SRC}/${FILE})
endforeach()
foreach (PATCH_NAME debugprobe-picogremlin.patch logicanalyzer-picogremlin.patch)
    configure_file(${CMAKE_CURRENT_LIST_DIR}/patches/${PATCH_NAME}
                   ${MERGED_OVERLAY_DIR}/${PATCH_NAME} COPYONLY)
endforeach()

execute_process(
    COMMAND ${PATCH_EXECUTABLE} --batch --ignore-whitespace --fuzz=0 --strip=1
            --input ${MERGED_OVERLAY_DIR}/debugprobe-picogremlin.patch
    WORKING_DIRECTORY ${MERGED_OVERLAY_DIR}/debugprobe
    RESULT_VARIABLE DEBUGPROBE_PATCH_RESULT
)
if (NOT DEBUGPROBE_PATCH_RESULT EQUAL 0)
    message(FATAL_ERROR "Could not apply the PicoGremlin debugprobe integration patch")
endif()
execute_process(
    COMMAND ${PATCH_EXECUTABLE} --batch --ignore-whitespace --fuzz=0 --strip=1
            --input ${MERGED_OVERLAY_DIR}/logicanalyzer-picogremlin.patch
    WORKING_DIRECTORY ${MERGED_OVERLAY_DIR}/logicanalyzer
    RESULT_VARIABLE LOGICANALYZER_PATCH_RESULT
)
if (NOT LOGICANALYZER_PATCH_RESULT EQUAL 0)
    message(FATAL_ERROR "Could not apply the PicoGremlin LogicAnalyzer integration patch")
endif()

# Blast mode owns only dmaPingPong0. The upstream StopCapture() unconditionally
# clears dmaPingPong1 as well, which can be a reclaimed autobaud channel after a
# blast capture. Keep this checked overlay transform separate from the upstream
# patch because the surrounding function is shared by several capture paths.
file(READ ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c LA_CAPTURE_DMA_OVERLAY)
set(LA_BLAST_DMA_OLD [=[        hw_clear_bits(&dma_hw->ch[dmaPingPong0].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
        hw_clear_bits(&dma_hw->ch[dmaPingPong1].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);]=])
set(LA_BLAST_DMA_NEW [=[        hw_clear_bits(&dma_hw->ch[dmaPingPong0].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
        if(lastCaptureType != CAPTURE_TYPE_BLAST)
            hw_clear_bits(&dma_hw->ch[dmaPingPong1].al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);]=])
string(FIND "${LA_CAPTURE_DMA_OVERLAY}" "${LA_BLAST_DMA_OLD}" LA_BLAST_DMA_POS)
string(FIND "${LA_CAPTURE_DMA_OVERLAY}" "${LA_BLAST_DMA_NEW}" LA_BLAST_DMA_NEW_POS)
if (LA_BLAST_DMA_POS EQUAL -1 AND LA_BLAST_DMA_NEW_POS EQUAL -1)
    message(FATAL_ERROR "Could not locate the checked blast DMA teardown anchor")
endif()
if (NOT LA_BLAST_DMA_POS EQUAL -1)
    string(REPLACE "${LA_BLAST_DMA_OLD}" "${LA_BLAST_DMA_NEW}"
                   LA_CAPTURE_DMA_OVERLAY "${LA_CAPTURE_DMA_OVERLAY}")
    file(WRITE ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c "${LA_CAPTURE_DMA_OVERLAY}")
endif()

# The blast capture path grants the capture DMA absolute bus priority over BOTH
# CPUs (bus_ctrl_hw->priority = DMA_W|DMA_R). In the standalone LA that is free —
# the CPU is otherwise idle. In the merged probe+LA build core0 services USB +
# CMSIS-DAP from SRAM (copy_to_ram), so absolute DMA priority starves core0 for
# the whole capture: OpenOCD floods with I/O errors, and the priority even leaks
# if the capture is cancelled before blast_capture_completed() runs. Fence the
# write behind !MERGED_BUILD so the merged build keeps core0 responsive. Kept as
# a checked overlay transform (the priority write is deep inside configureBlastDMA,
# not on a stable patch boundary).
file(READ ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c LA_CAPTURE_PRIO_OVERLAY)
set(LA_PRIO_OLD [==[    //Full priority to the DMA
    bus_ctrl_hw->priority = BUSCTRL_BUS_PRIORITY_DMA_W_BITS | BUSCTRL_BUS_PRIORITY_DMA_R_BITS;]==])
set(LA_PRIO_NEW [==[    //Full priority to the DMA (standalone LA only). In the merged probe+LA
    //build core0 services USB + CMSIS-DAP from SRAM; absolute DMA bus priority
    //starves it and freezes the probe for the whole capture, so skip it here.
#ifndef MERGED_BUILD
    bus_ctrl_hw->priority = BUSCTRL_BUS_PRIORITY_DMA_W_BITS | BUSCTRL_BUS_PRIORITY_DMA_R_BITS;
#endif]==])
string(FIND "${LA_CAPTURE_PRIO_OVERLAY}" "${LA_PRIO_OLD}" LA_PRIO_POS)
string(FIND "${LA_CAPTURE_PRIO_OVERLAY}" "${LA_PRIO_NEW}" LA_PRIO_NEW_POS)
if (LA_PRIO_POS EQUAL -1 AND LA_PRIO_NEW_POS EQUAL -1)
    message(FATAL_ERROR "Could not locate the capture DMA bus-priority anchor")
endif()
if (NOT LA_PRIO_POS EQUAL -1)
    string(REPLACE "${LA_PRIO_OLD}" "${LA_PRIO_NEW}"
                   LA_CAPTURE_PRIO_OVERLAY "${LA_CAPTURE_PRIO_OVERLAY}")
    file(WRITE ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c "${LA_CAPTURE_PRIO_OVERLAY}")
endif()

# Streaming (CMD 0x41/0x42) stop hygiene. The CMD=0x41 stream loop scans raw bytes for the
# 0x55 0xAA 0x42 stop sequence; a framed stop (la_frame(0x42) = 55 AA 42 AA 55) leaves
# the trailing terminator (AA 55) in the rx ring. On return to the framed command
# parser the leftover 0x55 perpetually desyncs it, wedging ALL later LA commands
# until reboot. Drain the rx ring (after a short settle so late terminator bytes
# have arrived) when the stream loop exits, so the parser restarts clean. Checked
# overlay transform: the stop site is deep in the (patched) CMD=0x41 handler.
file(READ ${LA_OVERLAY_SRC}/LogicAnalyzer.c LA_STREAM_OVERLAY)
set(LA_STREAM_OLD [==[                        LED_ON();
                        sendResponse("STREAM_STOPPED\n", fromWiFi);]==])
set(LA_STREAM_NEW [==[                        LED_ON();
                        // Drain bytes left after the stop frame (e.g. the frame
                        // terminator) so they cannot desync the command parser.
                        sleep_ms(2);
#ifdef MERGED_BUILD
                        while (la_bridge_getc() >= 0) { }
#else
                        while (getchar_timeout_us(0) != PICO_ERROR_TIMEOUT) { }
#endif
                        sendResponse("STREAM_STOPPED\n", fromWiFi);]==])
string(FIND "${LA_STREAM_OVERLAY}" "${LA_STREAM_OLD}" LA_STREAM_OLD_POS)
string(FIND "${LA_STREAM_OVERLAY}" "${LA_STREAM_NEW}" LA_STREAM_NEW_POS)
if (LA_STREAM_OLD_POS EQUAL -1 AND LA_STREAM_NEW_POS EQUAL -1)
    message(FATAL_ERROR "Could not locate the CMD=0x41 stream-stop anchor")
endif()
if (NOT LA_STREAM_OLD_POS EQUAL -1)
    string(REPLACE "${LA_STREAM_OLD}" "${LA_STREAM_NEW}"
                   LA_STREAM_OVERLAY "${LA_STREAM_OVERLAY}")
    file(WRITE ${LA_OVERLAY_SRC}/LogicAnalyzer.c "${LA_STREAM_OVERLAY}")
endif()

# ── One-shot capture latency: the stock pending-capture loop sleeps 1 s twice
#    per iteration, so a finished capture is only noticed ~2 s later and the
#    cancel check is sluggish. Poll every 20 ms instead.
file(READ ${LA_OVERLAY_SRC}/LogicAnalyzer.c LA_CAPTURE_POLL)
set(LA_POLL_OLD1 [=[LED_OFF();
                sleep_ms(1000);

                //Check for cancel request]=])
set(LA_POLL_NEW1 [=[LED_OFF();
                sleep_ms(20);

                //Check for cancel request]=])
set(LA_POLL_OLD2 [=[check_fast_interrupt();
                    #endif
                    sleep_ms(1000);]=])
set(LA_POLL_NEW2 [=[check_fast_interrupt();
                    #endif
                    sleep_ms(20);]=])
string(FIND "${LA_CAPTURE_POLL}" "${LA_POLL_OLD1}" LA_POLL_POS1)
string(FIND "${LA_CAPTURE_POLL}" "${LA_POLL_OLD2}" LA_POLL_POS2)
if (LA_POLL_POS1 EQUAL -1 OR LA_POLL_POS2 EQUAL -1)
    message(FATAL_ERROR "Could not locate the pending-capture sleeps for the latency patch")
endif()
string(REPLACE "${LA_POLL_OLD1}" "${LA_POLL_NEW1}" LA_CAPTURE_POLL "${LA_CAPTURE_POLL}")
string(REPLACE "${LA_POLL_OLD2}" "${LA_POLL_NEW2}" LA_CAPTURE_POLL "${LA_CAPTURE_POLL}")
file(WRITE ${LA_OVERLAY_SRC}/LogicAnalyzer.c "${LA_CAPTURE_POLL}")

# ── CMD=0x43 continuous stream: double-buffered DMA, the PIO never stops ─────
# CMD=0x41 stops the PIO between batches, so the USB link is idle while each block
# transfers (measured ~50 ms per 10 KB -> ~50% coverage). CMD=0x43 keeps PIO + DMA
# free-running into two chained half-buffers; core1 only sends whichever half
# just completed, so sampling is gapless while the host drains a half faster
# than the DMA fills it. Implemented as checked overlay transforms (appending a
# translation unit of capture code + a new command case) because it is new code,
# not a small edit to an existing hunk.
file(READ ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c LA_CONT_CAPTURE)
string(FIND "${LA_CONT_CAPTURE}" "pg_stream_start" LA_CONT_ALREADY)
if (LA_CONT_ALREADY EQUAL -1)
    # The appended translation unit lives in overlay/la_stream_capture.inc so the
    # integration C stays reviewable outside this build script.
    file(READ ${CMAKE_CURRENT_LIST_DIR}/overlay/la_stream_capture.inc PG_STREAM_CAPTURE_CODE)
    file(APPEND ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c "${PG_STREAM_CAPTURE_CODE}")
endif()

# ── CMD=0x47 PIO change-detector edge stream ─────────────────────────────────
# The PIO compares the 16-bit input word against the previous one (JMP X!=Y) and
# pushes a new state only on a change, so core 1 never scans unchanged samples.
# Core 1 drains the RX FIFO and reads TIMER1 (switched to clk_sys cycles, ~5 ns
# ticks) per event. Poll is 5 PIO cycles (~40 MHz at 200 MHz).
file(READ ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c LA_EDGE_CAPTURE)
string(FIND "${LA_EDGE_CAPTURE}" "pg_edge_start" LA_EDGE_ALREADY)
if (LA_EDGE_ALREADY EQUAL -1)
    # CMD=0x47/0x48 PIO edge-stream code (overlay/la_edge_capture.inc).
    file(READ ${CMAKE_CURRENT_LIST_DIR}/overlay/la_edge_capture.inc LA_EDGE_CAPTURE_CODE)
    file(APPEND ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c "${LA_EDGE_CAPTURE_CODE}")
endif()

# ── CMD=0x49/0x4A ADC analog capture ─────────────────────────────────────────
# Three ADC inputs on GP26..GP28 (the same pins as LA digital channels 21..23)
# plus the on-chip temperature sensor. One-shot captures reuse captureBuffer:
# the merged build has no spare SRAM for another multi-100 kB buffer.
file(READ ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c LA_ANALOG_CAPTURE)
string(FIND "${LA_ANALOG_CAPTURE}" "pg_adc_capture_start" LA_ANALOG_ALREADY)
if (LA_ANALOG_ALREADY EQUAL -1)
    file(READ ${CMAKE_CURRENT_LIST_DIR}/overlay/la_analog_capture.inc PG_ANALOG_CAPTURE_CODE)
    file(APPEND ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c "${PG_ANALOG_CAPTURE_CODE}")
endif()

file(READ ${LA_OVERLAY_SRC}/LogicAnalyzer.c LA_CONT_MAIN)
string(FIND "${LA_CONT_MAIN}" "case 0x43: // STREAM_CONTINUOUS" LA_CONT_CASE_ALREADY)
if (LA_CONT_CASE_ALREADY EQUAL -1)
    set(LA_CONT_PROTO_OLD "bool StartCaptureImmediate(uint32_t freq, uint32_t length, const uint8_t* capturePins, uint8_t capturePinCount, CHANNEL_MODE captureMode);")
    set(LA_CONT_PROTO_NEW "${LA_CONT_PROTO_OLD}
bool pg_stream_start(uint32_t freq, uint32_t halfSamples, const uint8_t* capturePins, uint8_t capturePinCount, CHANNEL_MODE captureMode, int triggerChannel, uint8_t inverted);
void pg_stream_stop(void);
uint32_t pg_stream_ready(void);
void pg_stream_clear(uint32_t mask);
uint32_t pg_stream_half_bytes(void);
uint32_t pg_stream_half_samples(void);
uint8_t* pg_stream_half_ptr(uint32_t which);
bool pg_edge_start(const uint8_t* capturePins, uint8_t capturePinCount, uint32_t div);
void pg_edge_stop(void);
bool pg_edge_pop(uint32_t* state);
uint32_t pg_edge_now(void);
int pg_edge_off(void);
uint32_t pg_edge_fifo_level(void);
uint32_t pg_edge_pc(void);
uint32_t pg_edge_fdebug(void);
uint32_t* pg_edge_buf(void);
uint32_t pg_edge_max(void);
uint32_t pg_edge_lo(void);
uint32_t pg_edge_span(void);
bool pg_adc_capture_start(uint32_t rate, uint32_t pre, uint32_t post, const uint8_t* channels, uint8_t count, bool eightBit, int triggerChannel, uint16_t triggerLevel, bool inverted);
bool pg_adc_capture_wait(void);
void pg_adc_capture_abort(void);
uint8_t* pg_adc_buffer(void);
uint32_t pg_adc_rate(void);
void pg_adc_pack8_inplace(uint8_t* buf, uint32_t values);
bool pg_adc_stream_start(uint32_t rate, uint32_t halfSamples, const uint8_t* channels, uint8_t count, bool eightBit);
void pg_adc_stream_stop(void);")
    string(FIND "${LA_CONT_MAIN}" "${LA_CONT_PROTO_OLD}" LA_CONT_PROTO_POS)
    if (LA_CONT_PROTO_POS EQUAL -1)
        message(FATAL_ERROR "Could not locate the StartCaptureImmediate prototype for the CMD=0x43 stream")
    endif()
    string(REPLACE "${LA_CONT_PROTO_OLD}" "${LA_CONT_PROTO_NEW}" LA_CONT_MAIN "${LA_CONT_MAIN}")

    # The inserted command cases live in overlay/la_main_cases.inc (C fragment;
    # @PROBE_VERSION@ is substituted below from probe/version.h).
    file(READ ${CMAKE_CURRENT_LIST_DIR}/overlay/la_main_cases.inc LA_CONT_CASE7)
    file(STRINGS ${CMAKE_CURRENT_LIST_DIR}/probe/version.h LA_VERSION_LINE REGEX "#define PROBE_VERSION")
    string(REGEX MATCH "\"([^\"]+)\"" LA_VERSION_MATCH "${LA_VERSION_LINE}")
    string(REPLACE "@PROBE_VERSION@" "${CMAKE_MATCH_1}" LA_CONT_CASE7 "${LA_CONT_CASE7}")

    set(LA_CONT_CASE_ANCHOR "case 0x42: // STREAM_STOP")
    string(FIND "${LA_CONT_MAIN}" "${LA_CONT_CASE_ANCHOR}" LA_CONT_ANCHOR_POS)
    if (LA_CONT_ANCHOR_POS EQUAL -1)
        message(FATAL_ERROR "Could not locate the CMD=0x42 stream-stop case for the CMD=0x43 insertion")
    endif()
    string(REPLACE "${LA_CONT_CASE_ANCHOR}" "${LA_CONT_CASE7}${LA_CONT_CASE_ANCHOR}" LA_CONT_MAIN "${LA_CONT_MAIN}")

    file(WRITE ${LA_OVERLAY_SRC}/LogicAnalyzer.c "${LA_CONT_MAIN}")
endif()

# Fail closed if a patch hunk was accepted by GNU patch but did not produce the
# intended A+ overlay. These checks protect the PIO ownership boundary.
file(READ ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c LA_CAPTURE_CHECK)
file(READ ${LA_OVERLAY_SRC}/LogicAnalyzer.c LA_MAIN_CHECK)
foreach (REQUIRED_TEXT
    "capturePIO = LA_CAPTURE_PIO_FAST;"
    "triggerPIO = pio0;"
    "captureProgram = &FAST_CAPTURE_program;"
    "captureProgram = &COMPLEX_CAPTURE_program;"
    "captureProgram = &BLAST_CAPTURE_program;"
    "captureProgram = &NEGATIVE_CAPTURE_MEASUREBURSTS_program;"
    "captureProgram = &NEGATIVE_CAPTURE_program;"
    "captureProgram = &POSITIVE_CAPTURE_MEASUREBURSTS_program;"
    "captureProgram = &POSITIVE_CAPTURE_program;"
    "pio_remove_program(capturePIO, captureProgram, captureOffset)"
    "lastCaptureType != CAPTURE_TYPE_BLAST")
    string(FIND "${LA_CAPTURE_CHECK}" "${REQUIRED_TEXT}" REQUIRED_POS)
    if (REQUIRED_POS EQUAL -1)
        message(FATAL_ERROR "A+ LA capture overlay is missing: ${REQUIRED_TEXT}")
    endif()
endforeach()
string(FIND "${LA_MAIN_CHECK}" "if(bufferPos >= sizeof(messageBuffer))" MAIN_BOUNDS_POS)
if (MAIN_BOUNDS_POS EQUAL -1)
    message(FATAL_ERROR "LA parser bounds check is missing from the generated overlay")
endif()
string(FIND "${LA_CAPTURE_CHECK}" "//Full priority to the DMA (standalone LA only)" LA_PRIO_GUARD_POS)
if (LA_PRIO_GUARD_POS EQUAL -1)
    message(FATAL_ERROR "Merged-build DMA bus-priority guard is missing from the generated overlay")
endif()
string(FIND "${LA_MAIN_CHECK}" "cannot desync the command parser" LA_STREAM_DRAIN_POS)
if (LA_STREAM_DRAIN_POS EQUAL -1)
    message(FATAL_ERROR "Streaming stop-drain fix is missing from the generated overlay")
endif()
string(FIND "${LA_CAPTURE_CHECK}" "pg_stream_start" LA_CONT_CAP_POS)
if (LA_CONT_CAP_POS EQUAL -1)
    message(FATAL_ERROR "CMD=0x43 continuous-stream capture code is missing from the generated overlay")
endif()
string(FIND "${LA_MAIN_CHECK}" "case 0x43: // STREAM_CONTINUOUS" LA_CONT_CASE_POS)
if (LA_CONT_CASE_POS EQUAL -1)
    message(FATAL_ERROR "CMD=0x43 continuous-stream handler is missing from the generated overlay")
endif()
string(FIND "${LA_MAIN_CHECK}" "pg_stream_half_ptr" LA_CONT_PROTO_POS)
if (LA_CONT_PROTO_POS EQUAL -1)
    message(FATAL_ERROR "CMD=0x43 stream prototypes are missing from the generated overlay")
endif()
string(FIND "${LA_CAPTURE_CHECK}" "pg_adc_capture_start" LA_ANALOG_CAP_POS)
if (LA_ANALOG_CAP_POS EQUAL -1)
    message(FATAL_ERROR "CMD=0x49/0x4A analog capture code is missing from the generated overlay")
endif()
string(FIND "${LA_MAIN_CHECK}" "case 0x49: // ANALOG_CAPTURE" LA_ANALOG_CASE_POS)
if (LA_ANALOG_CASE_POS EQUAL -1)
    message(FATAL_ERROR "CMD=0x49 analog capture handler is missing from the generated overlay")
endif()

add_executable(picogremlin
    # ---- PicoGremlin integration layer (this dir) ----
    main.c
    usb_descriptors.c
    la_bridge.c
    cdc_uart.c          # polling UART bridge (replaces FreeRTOS-based upstream cdc_uart.c)
    autobaud.c           # polling PIO/DMA autobaud state machine
    reset_driver.c       # application-owned runtime-reset class driver

    # ---- probe sources (patched build overlay + upstream debugprobe) ----
    ${DEBUGPROBE_OVERLAY_SRC}/probe.c
    ${DEBUGPROBE_SRC}/get_serial.c
    ${DEBUGPROBE_OVERLAY_SRC}/sw_dp_pio.c
    ${DEBUGPROBE_SRC}/probe_config.c
    # tusb_edpt_handler.c dropped: upstream endpoint/reset driver is not used;
    # DAP and the reset class are handled by main.c/reset_driver.c.

    # ---- CMSIS-DAP core (from debugprobe/CMSIS_DAP) ----
    ${CMSIS_DAP_SRC}/DAP.c
    ${CMSIS_DAP_SRC}/DAP_vendor.c
    ${CMSIS_DAP_SRC}/JTAG_DP.c
    ${CMSIS_DAP_SRC}/SWO.c
    # ---- logic analyzer (patched build overlay + upstream capture engine) ----
    ${LA_OVERLAY_SRC}/LogicAnalyzer.c
    ${LA_OVERLAY_SRC}/LogicAnalyzer_Capture.c
    # WiFi/LED feature files excluded for v1 (USE_CYGW_WIFI / WS2812_LED not defined).
    # If the linker reports missing symbols, add Shared_Buffers.c / Event_Machine.c.
)

# PicoGremlin dir is first so its tusb_config.h / board_merged_config.h shadow upstreams.
target_include_directories(picogremlin PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}
    ${DEBUGPROBE_SRC}
    ${DEBUGPROBE_ROOT}/include
    ${CMSIS_DAP_INC}
    ${CMSIS_CORE_INC}
    ${LA_SRC}
)

# Do NOT link the default stdio-uart driver. Its binary-info block would
# advertise GP0/GP1 as "UART0 TX/RX", but on PicoGremlin those pins are the
# logic analyzer's hardware-jumpered complex-trigger loopback, not a console.
# All host I/O goes through TinyUSB (CDC0 UART bridge / CDC1 LA) instead.
pico_enable_stdio_uart(picogremlin 0)
pico_enable_stdio_usb(picogremlin 0)

# PIO programs from both upstream trees.
pico_generate_pio_header(picogremlin ${DEBUGPROBE_OVERLAY_SRC}/probe.pio)
pico_generate_pio_header(picogremlin ${LA_SRC}/LogicAnalyzer.pio)
pico_generate_pio_header(picogremlin ${DEBUGPROBE_SRC}/autobaud.pio)

target_compile_definitions(picogremlin PRIVATE
    MERGED_BUILD=1              # activates #ifdef MERGED_BUILD paths in LogicAnalyzer.c
    MERGED_BOARD_PICO2W=1       # selects Pico 2 W (RP2350A) pin map in board_merged_config.h
    BUILD_PICO_2=1              # selects RP2350 board settings in LogicAnalyzer_Board_Settings.h
    # TURBO_MODE=1              # uncomment for 400 MHz — validate stability on RP2350A first
    FIRMWARE_VERSION="V6_0"     # LA protocol version string (from upstream CMakeLists)
    # The SDK driver is registered by main.c; this application owns CDC callbacks
    # and the combined MS OS 2.0 descriptor.
    PICO_ENABLE_USB_RESET_VIA_VENDOR_INTERFACE=1
    PICO_ENABLE_USB_RESET_VIA_BAUD_RATE=0
    PICO_USB_RESET_INCLUDE_DEFAULT_APP_DRIVER_CB=0
    PICO_USB_RESET_SUPPORT_MS_OS_20_DESCRIPTOR=0
)

target_link_libraries(picogremlin
    pico_stdlib
    pico_multicore
    pico_unique_id
    tinyusb_device
    tinyusb_board
    hardware_pio
    hardware_dma
    hardware_adc
    hardware_irq
    hardware_clocks
    hardware_flash
    hardware_vreg
    hardware_exception
    pico_usb_reset
    cmsis_core                  # LA capture includes <RP2350.h> for BUILD_PICO_2
)

# Run from RAM: both upstreams overclock + need deterministic capture timing.
pico_set_binary_type(picogremlin copy_to_ram)
pico_add_extra_outputs(picogremlin)
