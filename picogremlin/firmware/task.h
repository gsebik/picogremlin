/*
 * Minimal FreeRTOS task.h shim for the merged (no-FreeRTOS) build — see
 * FreeRTOS.h in this directory for the rationale.
 *
 * debugprobe/src/probe_config.h does `#include "FreeRTOS.h"` followed by
 * `#include "task.h"`; without this file, that second include fails (no real
 * FreeRTOS is linked in this build). Everything probe_config.h actually uses
 * from task.h (vTaskSuspendAll/xTaskResumeAll, inside `#if false` blocks) is
 * never compiled, so this shim only needs to exist — TaskHandle_t itself is
 * already provided by FreeRTOS.h.
 */
#ifndef TASK_SHIM_H
#define TASK_SHIM_H
#include "FreeRTOS.h"
#endif /* TASK_SHIM_H */
