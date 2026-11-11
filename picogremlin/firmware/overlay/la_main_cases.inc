                    case 0x40: // PG_INFO — PicoGremlin extras (upstream's 0-6 stay stock)
                    {
                        char pgmsg[96];
                        sprintf(pgmsg, "PICOGREMLIN:@PROBE_VERSION@\nSTREAM:1\nANALOG:3,250000,1\n");
                        sendResponse(pgmsg, fromWiFi);
#ifdef PROBE_CDC_UART
                        sprintf(pgmsg, "BRIDGE_TX:%d\n",
                                (gpio_get_function(PROBE_UART_TX) == GPIO_FUNC_UART) ? 1 : 0);
                        sendResponse(pgmsg, fromWiFi);
#endif
                        break;
                    }

                    case 0x47: // STREAM_EDGE — PIO change-detect, RAM-only capture + diagnostics
                    {
                        CAPTURE_REQUEST* sreq = (CAPTURE_REQUEST*)&messageBuffer[3];
                        uint8_t  ch_count = sreq->channelCount;
                        uint8_t* ch_ptr   = (uint8_t*)&sreq->channels;
                        uint32_t div      = sreq->frequency ? sreq->frequency : 1u;  /* PIO clkdiv */
                        uint32_t want     = sreq->postSamples ? sreq->postSamples : 256u;
                        if (want > pg_edge_max()) want = pg_edge_max();
                        uint32_t* ebuf = pg_edge_buf();

                        if (!pg_edge_start(ch_ptr, ch_count, div)) {
                            sendResponse("STREAM_ERROR\n", fromWiFi);
                            break;
                        }
                        LED_OFF();

                        /* Capture into RAM only — no USB traffic inside the loop, so the
                           measurement is not perturbed by transport or host timing. */
                        uint32_t n = 0, dup = 0, single = 0, multi = 0, maxbits = 0, invalid = 0;
                        uint32_t smask = (pg_edge_span() >= 32u) ? 0u : ((1u << (32u - pg_edge_span())) - 1u);
                        uint32_t prev = 0, last_ts = pg_edge_now(), t_start = last_ts;
                        bool have_prev = false;
                        while (n < want) {
                            uint32_t st;
                            if (!pg_edge_pop(&st)) {
                                if ((uint32_t)(pg_edge_now() - t_start) > 400000000u) break; /* ~2 s cap */
                                continue;
                            }
                            uint32_t ts = pg_edge_now();
                            uint32_t dt = ts - last_ts;
                            last_ts = ts;
                            if (have_prev) {
                                uint32_t x = st ^ prev;
                                unsigned nb = (unsigned)__builtin_popcount(x);
                                if (nb == 0) dup++;
                                else if (nb == 1) single++;
                                else { multi++; if (nb > maxbits) maxbits = nb; }
                            }
                            if (st & smask) invalid++;   /* bits below the span field must be zero */
                            ebuf[n * 2]     = dt;
                            ebuf[n * 2 + 1] = st;
                            prev = st; have_prev = true; n++;
                        }

                        uint32_t fifo_lvl = pg_edge_fifo_level();
                        uint32_t fdbg     = pg_edge_fdebug();
                        pg_edge_stop();
                        LED_ON();

                        uint32_t hdr[13] = { 0x45444731u /*'EDG1'*/, n, dup, single, multi,
                                             maxbits, invalid, fdbg, fifo_lvl, 0u, div,
                                             pg_edge_lo(), pg_edge_span() };
                        cdc_transfer((uint8_t*)hdr, sizeof(hdr));
                        cdc_transfer((uint8_t*)ebuf, n * 8);
                        sendResponse("EDGE_DONE\n", fromWiFi);
                        break;
                    }

                    case 0x48: // STREAM_EDGE_LIVE — PIO change-detect, framed USB streaming + counters
                    {
                        CAPTURE_REQUEST* sreq = (CAPTURE_REQUEST*)&messageBuffer[3];
                        uint8_t  ch_count = sreq->channelCount;
                        uint8_t* ch_ptr   = (uint8_t*)&sreq->channels;
                        uint32_t div      = sreq->frequency ? sreq->frequency : 1u;

                        if (!pg_edge_start(ch_ptr, ch_count, div)) {
                            sendResponse("STREAM_ERROR\n", fromWiFi);
                            break;
                        }
                        sendResponse("STREAM_EDGE_STARTED\n", fromWiFi);
                        LED_OFF();

                        bool streaming = true;
                        uint8_t stop_state = 0;
                        uint32_t seq = 0;
                        uint32_t total = 0, dup = 0, single = 0, multi = 0, maxbits = 0, invalid = 0;
                        uint32_t smask = (pg_edge_span() >= 32u) ? 0u : ((1u << (32u - pg_edge_span())) - 1u);
                        uint32_t prev = 0, last_ts = pg_edge_now(), last_flush = last_ts;
                        bool have_prev = false;
                        static uint32_t ev[64];
                        uint32_t n = 0;

                        while (streaming) {
                            if (!la_bridge_connected()) break;
                            uint32_t st;
                            while (pg_edge_pop(&st)) {
                                uint32_t ts = pg_edge_now();
                                uint32_t dt = ts - last_ts;
                                last_ts = ts;
                                if (have_prev) {
                                    uint32_t x = st ^ prev;
                                    unsigned nb = (unsigned)__builtin_popcount(x);
                                    if (nb == 0) dup++;
                                    else if (nb == 1) single++;
                                    else { multi++; if (nb > maxbits) maxbits = nb; }
                                }
                                if (st & smask) invalid++;
                                ev[n * 2] = dt; ev[n * 2 + 1] = st;
                                prev = st; have_prev = true; n++; total++;
                                if (n >= 64) break;
                            }
                            uint32_t now = pg_edge_now();
                            if (n >= 64 || (n > 0 && (now - last_flush) > 20000u)
                                         || (now - last_flush) > 2000000u) {   /* idle keepalive ~10 ms */
                                uint32_t hdr[2] = { seq, n };
                                cdc_transfer((uint8_t*)hdr, 8);
                                if (n > 0) cdc_transfer((uint8_t*)ev, n * 8);
                                seq++; n = 0; last_flush = now;
                            }

#ifdef MERGED_BUILD
                            {
                                int c;
                                while ((c = la_bridge_getc()) >= 0) {
                                    uint8_t b = (uint8_t)c;
                                    if      (b == 0x55)                     stop_state = 1;
                                    else if (stop_state == 1 && b == 0xAA) stop_state = 2;
                                    else if (stop_state == 2 && b == 0x42) { streaming = false; break; }
                                    else                                     stop_state = 0;
                                }
                            }
#else
                            while (getchar_timeout_us(0) != PICO_ERROR_TIMEOUT) { }
#endif
                        }

                        uint32_t fifo_lvl = pg_edge_fifo_level();
                        uint32_t fdbg     = pg_edge_fdebug();
                        pg_edge_stop();
                        LED_ON();
                        sleep_ms(2);   /* let the stop frame's trailing 0xAA 0x55 land before draining */
#ifdef MERGED_BUILD
                        while (la_bridge_getc() >= 0) { }
#else
                        while (getchar_timeout_us(0) != PICO_ERROR_TIMEOUT) { }
#endif
                        {
                            char sum[160];
                            sprintf(sum, "EDGE_SUMMARY total=%u dup=%u single=%u multi=%u maxbits=%u invalid=%u fdebug=%08x fifo=%u lo=%u span=%u\n",
                                    (unsigned)total, (unsigned)dup, (unsigned)single, (unsigned)multi,
                                    (unsigned)maxbits, (unsigned)invalid, (unsigned)fdbg, (unsigned)fifo_lvl,
                                    (unsigned)pg_edge_lo(), (unsigned)pg_edge_span());
                            sendResponse(sum, fromWiFi);
                        }
                        sendResponse("STREAM_STOPPED\n", fromWiFi);
                        break;
                    }

                    case 0x45: // STREAM_DELTA — send only level changes (+ periodic keepalive)
                    case 0x46: // STREAM_DELTA_TRIGGERED — arm on an edge, then delta-stream
                    {
                        bool triggered = (messageBuffer[2] == 10u);
                        CAPTURE_REQUEST* sreq = (CAPTURE_REQUEST*)&messageBuffer[3];
                        uint32_t halfSamples = sreq->postSamples;
                        if (halfSamples == 0) halfSamples = 512;
                        uint8_t  cap_mode = sreq->captureMode;
                        uint8_t  ch_count = sreq->channelCount;
                        uint8_t* ch_ptr   = (uint8_t*)&sreq->channels;
                        uint32_t cap_freq = sreq->frequency;
                        uint32_t bps = (cap_mode == 0) ? 1u : (cap_mode == 1) ? 2u : 4u;
                        const uint32_t KEEPALIVE = 4096;   /* re-anchor the state at least this often */

                        if (!pg_stream_start(cap_freq, halfSamples, ch_ptr, ch_count, (CHANNEL_MODE)cap_mode,
                                             triggered ? (int)sreq->trigger : -1,
                                             triggered ? sreq->inverted : 0)) {
                            sendResponse("STREAM_ERROR\n", fromWiFi);
                            break;
                        }
                        sendResponse("STREAM_CONT_STARTED\n", fromWiFi);
                        LED_OFF();

                        #define PG_RD(b, i) ((bps == 1) ? (uint32_t)(b)[i] : \
                            (bps == 2) ? (uint32_t)((b)[2*(i)] | ((b)[2*(i)+1] << 8)) : \
                            (uint32_t)((b)[4*(i)] | ((b)[4*(i)+1] << 8) | ((b)[4*(i)+2] << 16) | ((b)[4*(i)+3] << 24)))

                        bool streaming = true;
                        uint8_t stop_state = 0;
                        uint32_t seq = 0;
                        uint32_t prevRaw = 0;
                        bool have_prev = false;

                        while (streaming) {
                            if (!la_bridge_connected()) break;
                            uint32_t ready = pg_stream_ready();
                            for (uint32_t which = 0; which < 2 && streaming; which++) {
                                if (!(ready & (1u << which))) continue;
                                uint8_t* buf = pg_stream_half_ptr(which);
                                uint32_t n = pg_stream_half_samples();
                                cdc_transfer((uint8_t*)&seq, 4);
                                uint32_t last = 0, pv = prevRaw;
                                bool hv = have_prev;
                                for (uint32_t i = 0; i < n; i++) {
                                    uint32_t w = PG_RD(buf, i);
                                    if (!hv || w != pv || (i - last) >= KEEPALIVE) {
                                        uint32_t e[2] = { i, w };
                                        cdc_transfer((uint8_t*)e, 8);
                                        last = i; pv = w; hv = true;
                                    }
                                }
                                uint32_t end[2] = { 0xFFFFFFFFu, 0xFFFFFFFFu };
                                cdc_transfer((uint8_t*)end, 8);
                                prevRaw = pv; have_prev = true;
                                seq++;
                                pg_stream_clear(1u << which);
                            }

#ifdef MERGED_BUILD
                            {
                                int c;
                                while ((c = la_bridge_getc()) >= 0) {
                                    uint8_t b = (uint8_t)c;
                                    if      (b == 0x55)                     stop_state = 1;
                                    else if (stop_state == 1 && b == 0xAA) stop_state = 2;
                                    else if (stop_state == 2 && b == 0x42) { streaming = false; break; }
                                    else                                     stop_state = 0;
                                }
                            }
#else
                            while (getchar_timeout_us(0) != PICO_ERROR_TIMEOUT) { }
#endif
                        }

                        #undef PG_RD
                        pg_stream_stop();
                        LED_ON();
                        sleep_ms(2);
#ifdef MERGED_BUILD
                        while (la_bridge_getc() >= 0) { }
#else
                        while (getchar_timeout_us(0) != PICO_ERROR_TIMEOUT) { }
#endif
                        sendResponse("STREAM_STOPPED\n", fromWiFi);
                        break;
                    }

                    case 0x43: // STREAM_CONTINUOUS — double-buffered, PIO never stops
                    case 0x44: // STREAM_CONT_TRIGGERED — arm on an edge, then stream gaplessly
                    {
                        bool triggered = (messageBuffer[2] == 8u);
                        CAPTURE_REQUEST* sreq = (CAPTURE_REQUEST*)&messageBuffer[3];
                        uint32_t halfSamples = sreq->postSamples;
                        if (halfSamples == 0) halfSamples = 512;
                        uint8_t  cap_mode = sreq->captureMode;
                        uint8_t  ch_count = sreq->channelCount;
                        uint8_t* ch_ptr   = (uint8_t*)&sreq->channels;
                        uint32_t cap_freq = sreq->frequency;

                        if (!pg_stream_start(cap_freq, halfSamples, ch_ptr, ch_count,
                                             (CHANNEL_MODE)cap_mode,
                                             triggered ? (int)sreq->trigger : -1,
                                             triggered ? sreq->inverted : 0)) {
                            sendResponse("STREAM_ERROR\n", fromWiFi);
                            break;
                        }
                        sendResponse("STREAM_CONT_STARTED\n", fromWiFi);
                        LED_OFF();

                        bool streaming = true;
                        uint8_t stop_state = 0;
                        while (streaming) {
                            if (!la_bridge_connected()) break;

                            uint32_t ready = pg_stream_ready();
                            for (uint32_t which = 0; which < 2 && streaming; which++) {
                                if (!(ready & (1u << which))) continue;
                                uint32_t bytes = pg_stream_half_bytes();
                                cdc_transfer((uint8_t*)&bytes, 4);
                                cdc_transfer(pg_stream_half_ptr(which), bytes);
                                pg_stream_clear(1u << which);
                            }

#ifdef MERGED_BUILD
                            {
                                int c;
                                while ((c = la_bridge_getc()) >= 0) {
                                    uint8_t b = (uint8_t)c;
                                    if      (b == 0x55)                     stop_state = 1;
                                    else if (stop_state == 1 && b == 0xAA) stop_state = 2;
                                    else if (stop_state == 2 && b == 0x42) { streaming = false; break; }
                                    else                                     stop_state = 0;
                                }
                            }
#else
                            while (getchar_timeout_us(0) != PICO_ERROR_TIMEOUT) { }
#endif
                        }

                        pg_stream_stop();
                        LED_ON();
                        sleep_ms(2);
#ifdef MERGED_BUILD
                        while (la_bridge_getc() >= 0) { }
#else
                        while (getchar_timeout_us(0) != PICO_ERROR_TIMEOUT) { }
#endif
                        sendResponse("STREAM_STOPPED\n", fromWiFi);
                        break;
                    }

                    case 0x49: // ANALOG_CAPTURE — ADC one-shot (GP26/27/28 + temp)
                    {
                        CAPTURE_REQUEST* areq = (CAPTURE_REQUEST*)&messageBuffer[3];
                        uint8_t  nch = areq->channelCount;
                        uint8_t* ach = (uint8_t*)&areq->channels;
                        uint32_t rate = areq->frequency ? areq->frequency : 10000u;
                        uint32_t pre  = areq->preSamples;
                        uint32_t post = areq->postSamples ? areq->postSamples : 1000u;
                        bool eight = (areq->captureMode == 1u);
                        /* triggerType 3 = capture now; 0 = level crossing on
                           the ADC input given in `trigger` */
                        int trig = (areq->triggerType == 3u) ? -1 : (int)areq->trigger;

                        if (!pg_adc_capture_start(rate, pre, post, ach, nch, eight, trig,
                                                  areq->triggerValue, areq->inverted != 0)) {
                            sendResponse("ANALOG_ERROR\n", fromWiFi);
                            break;
                        }
                        LED_OFF();
                        if (!pg_adc_capture_wait()) {
                            pg_adc_capture_abort();
                            LED_ON();
                            sendResponse("ANALOG_ERROR\n", fromWiFi);
                            break;
                        }
                        LED_ON();
                        {
                            char amsg[64];
                            uint32_t values = (pre + post) * nch;
                            uint32_t ratePerChan = pg_adc_rate();
                            uint8_t width = eight ? 1u : 2u;
                            if (eight) pg_adc_pack8_inplace(pg_adc_buffer(), values);
                            sprintf(amsg, "ANALOG_STARTED %u\n", (unsigned)ratePerChan);
                            sendResponse(amsg, fromWiFi);
                            cdc_transfer((uint8_t*)&values, 4);
                            cdc_transfer((uint8_t*)pg_adc_buffer(), values * width);
                            uint8_t stamps = 0;
                            cdc_transfer(&stamps, 1);
                        }
                        break;
                    }

                    case 0x4A: // ANALOG_STREAM — gapless ADC stream (GP26/27/28 + temp)
                    {
                        CAPTURE_REQUEST* areq = (CAPTURE_REQUEST*)&messageBuffer[3];
                        uint32_t halfSamples = areq->postSamples;
                        if (halfSamples == 0) halfSamples = 512;
                        uint8_t  nch = areq->channelCount;
                        uint8_t* ach = (uint8_t*)&areq->channels;
                        bool eight = (areq->captureMode == 1u);

                        if (!pg_adc_stream_start(areq->frequency, halfSamples, ach, nch, eight)) {
                            sendResponse("ANALOG_ERROR\n", fromWiFi);
                            break;
                        }
                        {
                            char amsg[64];
                            sprintf(amsg, "ANALOG_STREAM_STARTED %u\n", (unsigned)pg_adc_rate());
                            sendResponse(amsg, fromWiFi);
                        }
                        LED_OFF();

                        bool streaming = true;
                        uint8_t stop_state = 0;
                        while (streaming) {
                            if (!la_bridge_connected()) break;

                            uint32_t ready = pg_stream_ready();
                            for (uint32_t which = 0; which < 2 && streaming; which++) {
                                if (!(ready & (1u << which))) continue;
                                uint32_t n = pg_stream_half_samples();
                                uint8_t* buf = pg_stream_half_ptr(which);
                                uint32_t bytes = eight ? n : n * 2u;
                                if (eight) pg_adc_pack8_inplace(buf, n);
                                cdc_transfer((uint8_t*)&bytes, 4);
                                cdc_transfer(buf, bytes);
                                pg_stream_clear(1u << which);
                            }

#ifdef MERGED_BUILD
                            {
                                int c;
                                while ((c = la_bridge_getc()) >= 0) {
                                    uint8_t b = (uint8_t)c;
                                    if      (b == 0x55)                     stop_state = 1;
                                    else if (stop_state == 1 && b == 0xAA) stop_state = 2;
                                    else if (stop_state == 2 && b == 0x42) { streaming = false; break; }
                                    else                                     stop_state = 0;
                                }
                            }
#else
                            while (getchar_timeout_us(0) != PICO_ERROR_TIMEOUT) { }
#endif
                        }

                        pg_adc_stream_stop();
                        LED_ON();
                        sleep_ms(2);
#ifdef MERGED_BUILD
                        while (la_bridge_getc() >= 0) { }
#else
                        while (getchar_timeout_us(0) != PICO_ERROR_TIMEOUT) { }
#endif
                        sendResponse("STREAM_STOPPED\n", fromWiFi);
                        break;
                    }
