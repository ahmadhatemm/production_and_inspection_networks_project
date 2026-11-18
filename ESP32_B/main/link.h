#ifndef LINK_H
#define LINK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "station.h"
#include "tcpframe.h"

/* The TCP connection of ESP32 B to the gateway and the loop that drives the station.
 *
 * ESP32 B is the TCP client: it connects to the gateway (which owns the Wi-Fi
 * network and the fixed address), sends the station's messages with the 4-byte
 * length prefix and cuts the received byte stream back into messages.
 *
 * This file builds both for the ESP32 (lwIP sockets) and for a PC (POSIX
 * sockets), so the same connection code can be tested against a fake gateway.
 * Everything that differs between the two (clock, sleeping, "is Wi-Fi up")
 * comes in through link_env_t. */

typedef struct {
    uint32_t (*now_ms)(void);               /* milliseconds, free-running, may wrap          */
    void     (*sleep_ms)(uint32_t ms);      /* give the CPU away for about ms                */
    bool     (*net_ready)(void);            /* true when the network (Wi-Fi) is up           */
    void     (*idle)(void *user);           /* called once per loop, for the LED and the like */
    void      *user;
    void     (*note)(const char *text);     /* optional (may be NULL): one line of connection progress for the log */
} link_env_t;

typedef struct {
    station_t  *st;
    const char *ip;                         /* the gateway, as text such as "192.168.4.1" */
    uint16_t    port;
    link_env_t  env;

    int         sock;                       /* -1 when not connected                      */
    bool        broken;                     /* a send failed: close at the next chance    */
    bool        attempted;                  /* at least one connection attempt was made   */
    uint32_t    last_attempt_ms;
    tcpframe_decoder_t dec;
    uint8_t     rx_body[TCPFRAME_MAX_BODY];
    uint8_t     rx_raw[512];
    uint8_t     tx_buf[TCPFRAME_HDR_LEN + TCPFRAME_MAX_BODY];
} link_t;

/* Also hooks the station's message output up to this connection. */
void link_init(link_t *l, station_t *st, const char *ip, uint16_t port, const link_env_t *env);

/* One pass of the loop: connect if needed, wait up to LINK_POLL_MS for data,
 * hand received messages to the station, run its timers, close the connection
 * if something went wrong. Call it forever. */
void link_step(link_t *l);

#endif
