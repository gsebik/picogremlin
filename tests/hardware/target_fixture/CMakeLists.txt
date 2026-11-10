cmake_minimum_required(VERSION 3.13)

# PicoGremlin hardware-test target fixture.
# Builds a plain RP2350 application for the Pico 2 / Pico 2 W target wired to
# the PicoGremlin probe. This is NOT the PicoGremlin firmware; it must be
# programmed to the TARGET over SWD through the PicoGremlin probe only.

include(pico_sdk_import.cmake)

project(target_fixture C CXX ASM)
pico_sdk_init()

add_executable(target_fixture
    fixture.c
)

target_include_directories(target_fixture PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}
)

# UART0 is used directly by the fixture; disable stdio so the default
# Pico SDK stdio-uart (GP0/GP1) cannot interfere.
pico_enable_stdio_uart(target_fixture 0)
pico_enable_stdio_usb(target_fixture 0)

target_link_libraries(target_fixture
    pico_stdlib
    hardware_uart
    hardware_pio
    hardware_clocks
    hardware_gpio
)

pico_generate_pio_header(target_fixture
    ${CMAKE_CURRENT_LIST_DIR}/siggen.pio)

pico_add_extra_outputs(target_fixture)