/*
 * Minimal FreeRTOS type shim for the merged (no-FreeRTOS) build.
 *
 * The upstream debugprobe headers (cdc_uart.h, tusb_edpt_handler.h) declare
 * FreeRTOS handle types even though the merged build replaces the
 * FreeRTOS-dependent .c files entirely. This shim satisfies the type
 * declarations so those headers can be included without pulling in FreeRTOS.
 *
 * Nothing in this shim is ever called at runtime.
 */
#ifndef FREERTOS_SHIM_H
#define FREERTOS_SHIM_H

typedef void *TaskHandle_t;
typedef void *SemaphoreHandle_t;
typedef unsigned int TickType_t;

/* Suppress any remaining FreeRTOS macro usage in headers. */
#define xSemaphoreCreateMutex()     ((SemaphoreHandle_t)0)
#define xSemaphoreGive(x)           ((void)(x))
#define vSemaphoreDelete(x)         ((void)(x))
#define xTaskGetTickCount()         (0u)

#endif /* FREERTOS_SHIM_H */
