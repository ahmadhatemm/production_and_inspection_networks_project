#ifndef STATION_H
#define STATION_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "atr.h"
#include "config.h"

/* The production station: the state machine and the protocol rules of ESP32 A.
 *
 * This file has no ESP-IDF code in it. Time is passed in (milliseconds) and
 * frames leave through a callback, so the same code runs on the board and in
 * the PC tests (test/test_station.c). */

typedef enum {
    STATION_IDLE = 0,
    STATION_RUNNING,
    STATION_PAUSED
} station_state_t;

/* Called with one complete ATR frame, not yet SLIP-encoded. */
typedef void (*station_tx_fn)(const uint8_t *frame, size_t len, void *ctx);

typedef struct {
    uint32_t frames_ok;         /* ATR frames with a good CRC and length           */
    uint32_t crc_errors;
    uint32_t short_frames;
    uint32_t length_errors;
    uint32_t slip_drops;        /* packets thrown away by the SLIP decoder         */
    uint32_t wrong_test_id;     /* good frame, but not production traffic          */
    uint32_t bad_payload_len;   /* production frame whose payload is not 16 bytes  */
    uint32_t unexpected_type;   /* message type that the gateway should never send */
    uint32_t parts_sent;
    uint32_t parts_acked;
    uint32_t retries;           /* part re-sent after an ACK timeout               */
    uint32_t queue_full;        /* QUEUE_FULL answers received                     */
    uint32_t parts_failed;      /* part never acknowledged after all tries         */
    uint32_t stale_acks;        /* PART_ACK for a part that is not in flight       */
    uint32_t bad_acks;          /* PART_ACK with an unknown arg                    */
    uint32_t cmds_accepted;
    uint32_t cmds_rejected;
    uint32_t duplicate_cmds;    /* same cmd_id seen again, not executed again      */
    uint32_t link_lost;
    uint32_t batches_done;
} station_stats_t;

typedef struct {
    bool     used;
    uint32_t cmd_id;
    uint8_t  ack_arg;           /* PM_CMDACK_ACCEPTED or PM_CMDACK_REJECTED        */
    uint8_t  done_arg;          /* PM_DONE_*, 0 when the command was rejected      */
} cmd_record_t;

typedef struct {
    station_tx_fn tx;
    void         *tx_ctx;

    station_state_t state;
    bool     finished;          /* every part of the batch is produced and acknowledged */

    /* batch */
    uint32_t target;
    uint32_t cycle_ms;
    uint32_t produced;          /* parts made so far; the last part_id given       */
    uint32_t next_part_ms;      /* when the next part is due                       */

    /* the one part that is waiting for the gateway's answer */
    bool     pending;
    bool     pending_hold;      /* gateway said QUEUE_FULL: waiting to try again   */
    uint32_t pending_part_id;
    uint32_t pending_deadline_ms;
    uint8_t  pending_tries;
    uint8_t  pending_frame[ATR_PROD_FRAME_LEN];

    /* link */
    uint32_t tx_seq;            /* ATR seq of the next frame we create             */
    uint32_t hb_counter;
    uint32_t next_hb_ms;
    bool     link_up;
    uint32_t last_rx_ms;

    cmd_record_t history[STATION_CMD_HISTORY];
    uint8_t      history_next;

    station_stats_t stats;
} station_t;

void station_init(station_t *s, station_tx_fn tx, void *tx_ctx, uint32_t now_ms);

/* One complete ATR frame, SLIP already removed. */
void station_on_packet(station_t *s, const uint8_t *raw, size_t raw_len, uint32_t now_ms);

/* The SLIP decoder threw a packet away. */
void station_note_slip_drop(station_t *s);

/* Call often (every few ms): heartbeats, time-outs, retries, new parts. */
void station_tick(station_t *s, uint32_t now_ms);

#endif
