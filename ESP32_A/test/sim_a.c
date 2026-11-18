/* ESP32 A on a PC: the real station.c, atr.c, prodmsg.c, crc16.c and slip.c, with the serial port replaced
 * by a TCP server. The gateway opens it as a serial port:   python gateway.py --serial socket://127.0.0.1:7000
 *
 *   make -f test.mk sim
 *   ./sim_a 7000 [drop_rx_every [drop_tx_every]]
 *
 * drop_rx_every N: throw away every Nth frame that arrives from the gateway (0 = never)
 * drop_tx_every N: throw away every Nth frame this station sends             (0 = never)
 * Both are for testing the retry and duplicate rules with the real station code. Heartbeats are never dropped,
 * so only the traffic under test is affected.
 *
 * When the gateway disconnects, the simulator waits for the next connection, and the station keeps its state,
 * like a board that stays powered while the cable is unplugged.
 */
#define _GNU_SOURCE   /* clock_gettime and MSG_NOSIGNAL under -std=c11 */
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "../main/atr.h"
#include "../main/prodmsg.h"
#include "../main/slip.h"
#include "../main/station.h"

static int      s_client = -1;
static unsigned s_drop_rx, s_drop_tx, s_rx_count, s_tx_count;

static uint32_t now_ms(void)
{
    struct timespec t;

    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint32_t)((uint64_t)t.tv_sec * 1000u + (uint64_t)t.tv_nsec / 1000000u);
}

static int is_heartbeat(const uint8_t *frame, size_t len)
{
    return len == ATR_PROD_FRAME_LEN && frame[ATR_HEADER_LEN] == PM_HEARTBEAT;
}

static void link_tx(const uint8_t *frame, size_t len, void *ctx)
{
    uint8_t out[2 * ATR_MAX_FRAME_LEN + 2];
    size_t n;

    (void)ctx;
    if (s_drop_tx && !is_heartbeat(frame, len) && ++s_tx_count % s_drop_tx == 0) {
        fprintf(stderr, "[sim_a] dropped a frame to the gateway\n");
        return;
    }
    n = slip_encode(frame, len, out, sizeof(out));
    if (s_client >= 0 && n > 0) {
        if (send(s_client, out, n, MSG_NOSIGNAL) < 0) {
            close(s_client);
            s_client = -1;
        }
    }
}

int main(int argc, char **argv)
{
    int port = argc > 1 ? atoi(argv[1]) : 7000;
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    int yes = 1;
    struct sockaddr_in addr;
    static station_t st;
    static uint8_t packet[ATR_MAX_FRAME_LEN];
    slip_decoder_t dec;

    s_drop_rx = argc > 2 ? (unsigned)atoi(argv[2]) : 0;
    s_drop_tx = argc > 3 ? (unsigned)atoi(argv[3]) : 0;
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons((uint16_t)port);
    if (bind(listener, (struct sockaddr *)&addr, sizeof(addr)) < 0 || listen(listener, 1) < 0) {
        perror("sim_a: bind/listen");
        return 1;
    }
    fprintf(stderr, "[sim_a] listening on 127.0.0.1:%d\n", port);
    station_init(&st, link_tx, NULL, now_ms());
    slip_decoder_init(&dec, packet, sizeof(packet));

    for (;;) {
        fd_set rd;
        struct timeval tv = { 0, 5000 };
        int maxfd = listener;

        FD_ZERO(&rd);
        FD_SET(listener, &rd);
        if (s_client >= 0) {
            FD_SET(s_client, &rd);
            if (s_client > maxfd) {
                maxfd = s_client;
            }
        }
        if (select(maxfd + 1, &rd, NULL, NULL, &tv) > 0) {
            if (FD_ISSET(listener, &rd)) {
                int c = accept(listener, NULL, NULL);
                int one = 1;

                if (c >= 0) {
                    if (s_client >= 0) {
                        close(s_client);
                    }
                    s_client = c;
                    setsockopt(c, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
                    slip_decoder_init(&dec, packet, sizeof(packet));
                    fprintf(stderr, "[sim_a] gateway connected\n");
                }
            }
            if (s_client >= 0 && FD_ISSET(s_client, &rd)) {
                uint8_t buf[256];
                ssize_t n = recv(s_client, buf, sizeof(buf), 0);

                if (n <= 0) {
                    close(s_client);
                    s_client = -1;
                    fprintf(stderr, "[sim_a] gateway disconnected\n");
                } else {
                    for (ssize_t i = 0; i < n; i++) {
                        size_t len = 0;
                        slip_result_t r = slip_decode_byte(&dec, buf[i], &len);

                        if (r == SLIP_PACKET) {
                            if (s_drop_rx && !is_heartbeat(packet, len) && ++s_rx_count % s_drop_rx == 0) {
                                fprintf(stderr, "[sim_a] dropped a frame from the gateway\n");
                                continue;
                            }
                            station_on_packet(&st, packet, len, now_ms());
                        } else if (r == SLIP_DROPPED) {
                            station_note_slip_drop(&st);
                        }
                    }
                }
            }
        }
        station_tick(&st, now_ms());
    }
}
