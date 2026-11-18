/*
 * MCT 431 project, ESP32 B: the inspection station.
 *
 * Link to the gateway: Wi-Fi (station mode) and a TCP connection to the gateway's
 * port 5000, messages in 4-byte length-prefix frames, compact JSON inside.
 *
 * This file is the only part that needs ESP-IDF: Wi-Fi start-up, the clock, the
 * LED, and the evidence log on the serial console. The connection handling is in
 * link.c and the protocol rules and the inspection logic are in station.c.
 *
 * Unlike ESP32 A, this board's USB serial port is free, so it carries a readable
 * log of every message (direction, content, and what the station decided). Open
 * "ESP-IDF: Monitor Device" to watch it.
 *
 * Before building, set WIFI_SSID, WIFI_PASSWORD and GATEWAY_IP in config.h.
 */
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#include "config.h"
#include "link.h"
#include "station.h"

#define LED_GPIO  2                     /* blue LED on many DevKit boards; -1 if yours has none */

static station_t s_station;
static link_t    s_link;
static volatile bool s_ip_ready;        /* set by the Wi-Fi event handler */

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* ---- evidence log ----------------------------------------------------- */

/* One line per message or decision: time since boot in ms, direction, content. */
static void log_cb(const char *tag, const char *text, size_t len, void *ctx)
{
    (void)ctx;
    printf("[%10lu ms] %-3s %.*s\n", (unsigned long)now_ms(), tag, (int)len, text);
}

/* Progress lines (Wi-Fi and connection), tagged NET, in the same format as the evidence log. */
static void net_say(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

static void net_say(const char *fmt, ...)
{
    char line[128];
    va_list ap;

    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    printf("[%10lu ms] NET %s\n", (unsigned long)now_ms(), line);
}

static void note_cb(const char *text)
{
    net_say("%s", text);
}

/* ---- LED -------------------------------------------------------------- */

static void led_setup(void)
{
#if LED_GPIO >= 0
    gpio_reset_pin((gpio_num_t)LED_GPIO);
    gpio_set_direction((gpio_num_t)LED_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level((gpio_num_t)LED_GPIO, 0);
#endif
}

/* off = no connection to the gateway, steady on = connected and idle, fast blink = inspecting */
static void idle_cb(void *user)
{
#if LED_GPIO >= 0
    const station_t *st = (const station_t *)user;
    int level = 0;

    switch (station_state(st)) {
    case STATION_IDLE:
        level = 1;
        break;
    case STATION_INSPECTING:
        level = (int)((now_ms() / 100u) & 1u);
        break;
    default:
        break;
    }
    gpio_set_level((gpio_num_t)LED_GPIO, level);
#else
    (void)user;
#endif
}

/* ---- environment for link.c ------------------------------------------- */

static void sleep_cb(uint32_t ms)
{
    TickType_t t = pdMS_TO_TICKS(ms);

    vTaskDelay(t == 0 ? 1 : t);         /* never 0: the idle task has to get its turn */
}

static bool net_ready_cb(void)
{
    return s_ip_ready;
}

/* ---- Wi-Fi ------------------------------------------------------------ */

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        net_say("Wi-Fi started, joining \"%s\"", WIFI_SSID);
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *d = (const wifi_event_sta_disconnected_t *)data;

        s_ip_ready = false;             /* link.c notices, closes the TCP connection and waits */
        net_say("Wi-Fi not connected to \"%s\" (reason %d), retrying", WIFI_SSID, (int)d->reason);
        esp_wifi_connect();             /* and keep trying to rejoin the network */
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = (const ip_event_got_ip_t *)data;

        s_ip_ready = true;
        net_say("Wi-Fi joined, board IP " IPSTR ", router " IPSTR, IP2STR(&e->ip_info.ip), IP2STR(&e->ip_info.gw));
    }
}

static void wifi_start(void)
{
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    wifi_config_t wc;
    esp_err_t e = nvs_flash_init();      /* the Wi-Fi driver keeps its calibration data here */

    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        e = nvs_flash_init();
    }
    ESP_ERROR_CHECK(e);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL));

    memset(&wc, 0, sizeof(wc));
    strncpy((char *)wc.sta.ssid, WIFI_SSID, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, WIFI_PASSWORD, sizeof(wc.sta.password) - 1);
    wc.sta.threshold.authmode = (strlen(WIFI_PASSWORD) == 0) ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;
    wc.sta.pmf_cfg.capable = true;
    wc.sta.pmf_cfg.required = false;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));     /* no power saving: it adds delay to every message */
}

void app_main(void)
{
    const link_env_t env = {
        .now_ms = now_ms,
        .sleep_ms = sleep_cb,
        .net_ready = net_ready_cb,
        .idle = idle_cb,
        .user = &s_station,
        .note = note_cb,
    };

    net_say("ESP32 B %s starting: Wi-Fi \"%s\", gateway %s:%d", FW_VERSION, WIFI_SSID, GATEWAY_IP, GATEWAY_PORT);
    if (strcmp(WIFI_SSID, "CHANGE_ME") == 0) {
        net_say("WARNING: config.h still has the placeholder Wi-Fi name. Edit it, then build and flash again.");
    }
    led_setup();
    station_init(&s_station, NULL, log_cb, NULL);
    link_init(&s_link, &s_station, GATEWAY_IP, GATEWAY_PORT, &env);
    wifi_start();

    for (;;) {
        link_step(&s_link);
    }
}
