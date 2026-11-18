/*
 * MCT 431 project, ESP32 A: the production station.
 *
 * Link to the gateway: UART0 (the USB cable), 115200 8N1.
 * Received bytes go  UART driver -> SLIP decoder -> station_on_packet().
 * Every frame the station sends is SLIP-encoded here and written to UART0.
 *
 * UART0 carries binary frames, so sdkconfig.defaults turns the ESP-IDF console
 * and logs off. The ROM bootloader still prints a few text lines at every
 * reset; the gateway has to survive that (its SLIP decoder drops such junk).
 *
 * Close any serial monitor before the gateway opens the port.
 */
#include <stdbool.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "soc/uart_pins.h"      /* U0TXD_GPIO_NUM, U0RXD_GPIO_NUM */

#include "atr.h"
#include "config.h"
#include "slip.h"
#include "station.h"

#define LINK_UART   UART_NUM_0
#define RX_BUF_SIZE 2048
#define POLL_MS     5           /* the main loop wakes at least this often */
#define LED_GPIO    2           /* blue LED on many DevKit boards; -1 if yours has none */

static uint8_t   s_rx[256];
static uint8_t   s_packet[ATR_MAX_FRAME_LEN];
static uint8_t   s_tx[2 * ATR_MAX_FRAME_LEN + 2];   /* worst case: every byte escaped */
static station_t s_station;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void uart_setup(void)
{
    const uart_config_t cfg = {
        .baud_rate = LINK_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    ESP_ERROR_CHECK(uart_driver_install(LINK_UART, RX_BUF_SIZE, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(LINK_UART, &cfg));
    /* With the console off, the bootloader disconnects UART0 from its pins,
     * so name them again: TX on GPIO1, RX on GPIO3 (the USB chip's lines). */
    ESP_ERROR_CHECK(uart_set_pin(LINK_UART, U0TXD_GPIO_NUM, U0RXD_GPIO_NUM,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
}

static void led_setup(void)
{
#if LED_GPIO >= 0
    gpio_reset_pin((gpio_num_t)LED_GPIO);
    gpio_set_direction((gpio_num_t)LED_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level((gpio_num_t)LED_GPIO, 0);
#endif
}

/* LED on = the station is producing parts (state Running). */
static void led_update(void)
{
#if LED_GPIO >= 0
    gpio_set_level((gpio_num_t)LED_GPIO, s_station.state == STATION_RUNNING ? 1 : 0);
#endif
}

/* The station's way out: SLIP-encode one ATR frame and write it to the wire. */
static void link_tx(const uint8_t *frame, size_t len, void *ctx)
{
    size_t n = slip_encode(frame, len, s_tx, sizeof(s_tx));

    (void)ctx;
    if (n > 0) {
        uart_write_bytes(LINK_UART, s_tx, n);
    }
}

/* Wait up to POLL_MS for one byte, then take whatever else is already buffered.
 * Waiting for a full buffer instead would add delay to every frame. */
static int read_available(void)
{
    TickType_t wait = pdMS_TO_TICKS(POLL_MS);
    int n;
    size_t waiting = 0;

    if (wait == 0) {
        wait = 1;                       /* never poll without sleeping: it would starve the idle task */
    }
    n = uart_read_bytes(LINK_UART, s_rx, 1, wait);
    if (n <= 0) {
        return 0;
    }
    uart_get_buffered_data_len(LINK_UART, &waiting);
    if (waiting > sizeof(s_rx) - 1) {
        waiting = sizeof(s_rx) - 1;
    }
    if (waiting > 0) {
        int more = uart_read_bytes(LINK_UART, &s_rx[1], (uint32_t)waiting, 0);
        if (more > 0) {
            n += more;
        }
    }
    return n;
}

void app_main(void)
{
    slip_decoder_t dec;

    led_setup();
    uart_setup();
    station_init(&s_station, link_tx, NULL, now_ms());
    slip_decoder_init(&dec, s_packet, sizeof(s_packet));

    for (;;) {
        int n = read_available();

        for (int i = 0; i < n; i++) {
            size_t len = 0;
            slip_result_t r = slip_decode_byte(&dec, s_rx[i], &len);

            if (r == SLIP_PACKET) {
                station_on_packet(&s_station, s_packet, len, now_ms());
            } else if (r == SLIP_DROPPED) {
                station_note_slip_drop(&s_station);
            }
        }
        station_tick(&s_station, now_ms());
        led_update();
    }
}
