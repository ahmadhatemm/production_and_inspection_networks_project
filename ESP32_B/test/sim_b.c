/* ESP32 B on a PC: the real station.c, link.c, jsonmini.c and tcpframe.c behind POSIX sockets.
 * It stands in for the board so that host_check_b.py can be run without hardware.
 *
 *   make -f test.mk sim
 *   python host_check_b.py 5000       (in one terminal)
 *   ./sim_b 127.0.0.1 5000            (in another)
 */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#include "../main/link.h"
#include "../main/station.h"

static uint32_t now_ms(void)
{
    struct timespec t;

    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint32_t)((uint64_t)t.tv_sec * 1000u + (uint64_t)t.tv_nsec / 1000000u);
}

static void sleep_ms(uint32_t ms)
{
    usleep(ms * 1000u);
}

static bool net_ready(void)
{
    return true;
}

static void idle(void *user)
{
    (void)user;
}

static void log_cb(const char *tag, const char *text, size_t len, void *ctx)
{
    (void)ctx;
    fprintf(stderr, "[%10lu ms] %-3s %.*s\n", (unsigned long)now_ms(), tag, (int)len, text);
}

static station_t s_station;
static link_t    s_link;

static void note(const char *text)
{
    printf("NET %s\n", text);
    fflush(stdout);
}

int main(int argc, char **argv)
{
    const char *ip = argc > 1 ? argv[1] : "127.0.0.1";
    int port = argc > 2 ? atoi(argv[2]) : 5000;
    const link_env_t env = { now_ms, sleep_ms, net_ready, idle, NULL, note };

    station_init(&s_station, NULL, log_cb, NULL);
    link_init(&s_link, &s_station, ip, (uint16_t)port, &env);
    for (;;) {
        link_step(&s_link);
    }
}
