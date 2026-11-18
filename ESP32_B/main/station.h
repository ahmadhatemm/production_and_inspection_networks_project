#ifndef STATION_H
#define STATION_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "config.h"
#include "jsonmini.h"

/* The inspection station: the protocol rules and the inspection logic of ESP32 B.
 *
 * This file has no ESP-IDF and no socket code in it. Time is passed in
 * (milliseconds), messages leave through a callback, and received messages
 * are handed in one body at a time, so the same code runs on the board and
 * in the PC tests.
 *
 * What ESP32 B does (Interface Contract v1, "Gateway <-> ESP32 B: TCP"):
 *   CONFIG    -> validates N, K and inspect_time_ms, answers ACK, applies them,
 *                then reports completion with CONFIG_DONE
 *   JOB_REQ   -> answers ACK (acceptance), waits inspect_time_ms, computes
 *                FAIL when (part_id + K) mod N == 0 (otherwise PASS) and
 *                reports completion with JOB_RESULT
 *   HEARTBEAT -> sent every second, expected every second */

typedef enum {
    STATION_DISCONNECTED = 0,   /* no TCP connection to the gateway          */
    STATION_IDLE,               /* connected, no job in progress             */
    STATION_INSPECTING          /* connected, one job in progress            */
} station_state_t;

/* Called with one message body (compact JSON, no length prefix, not NUL-terminated). */
typedef void (*station_tx_fn)(const uint8_t *body, size_t len, void *ctx);

/* Evidence log. tag is "TX" (sent), "RX" (received) or "EVT" (what the station decided).
 * text is not NUL-terminated. */
typedef void (*station_log_fn)(const char *tag, const char *text, size_t len, void *ctx);

typedef struct {
    uint32_t connects;
    uint32_t disconnects;
    uint32_t link_timeouts;         /* nothing valid from the gateway for 3000 ms          */
    uint32_t frame_errors;          /* framing broke (announced length over the limit)     */
    uint32_t malformed;             /* body is not a JSON object with a string "type"      */
    uint32_t bad_fields;            /* a known message with a missing or invalid field that cannot be answered */
    uint32_t unexpected_type;       /* a message type the gateway should never send        */
    uint32_t heartbeats_rx;
    uint32_t cmds_accepted;
    uint32_t cmds_rejected;
    uint32_t cmd_duplicates;
    uint32_t jobs_accepted;
    uint32_t jobs_rejected;
    uint32_t job_duplicates;
    uint32_t jobs_completed;
    uint32_t jobs_abandoned;        /* in progress when the link was lost                  */
    uint32_t verdict_pass;
    uint32_t verdict_fail;
    uint32_t results_acked;
    uint32_t results_rejected;      /* the gateway answered ACK "rejected" to a JOB_RESULT */
    uint32_t result_retries;
    uint32_t results_failed;        /* no ACK after all tries                              */
    uint32_t cfgdone_acked;
    uint32_t cfgdone_rejected;
    uint32_t cfgdone_retries;
    uint32_t cfgdone_failed;
    uint32_t stale_acks;            /* an ACK that matches nothing we are waiting for      */
    uint32_t outbox_overflow;
} station_stats_t;

typedef enum {
    JOB_RUNNING = 1,
    JOB_DONE,
    JOB_ABANDONED
} job_state_t;

/* One accepted job, kept to recognise a repeat of the same job_id. */
typedef struct {
    bool        used;
    uint32_t    job_id;
    uint32_t    part_id;
    job_state_t state;
    bool        fail;               /* the verdict, valid when state is JOB_DONE           */
    uint32_t    n;                  /* the parameters this job was accepted with            */
    uint32_t    k;
} job_record_t;

/* One accepted CONFIG, kept to recognise a repeat of the same cmd_id. */
typedef struct {
    bool     used;
    uint32_t cmd_id;
    uint32_t n;
    uint32_t k;
    uint32_t inspect_ms;
} cfg_record_t;

typedef enum {
    OUT_RESULT = 1,                 /* a JOB_RESULT waiting for its ACK  */
    OUT_CFGDONE                     /* a CONFIG_DONE waiting for its ACK */
} out_kind_t;

typedef struct {
    bool       used;
    out_kind_t kind;
    uint32_t   id;                  /* job_id or cmd_id */
    uint8_t    tries;
    uint32_t   deadline_ms;
    uint16_t   len;
    uint8_t    body[STATION_MSG_MAX];   /* the exact bytes, so a retry is identical */
} outbox_t;

typedef struct {
    station_tx_fn  tx;
    station_log_fn log;
    void          *ctx;

    bool     link_up;
    bool     drop_request;          /* the station wants the connection closed               */
    uint32_t last_rx_ms;
    uint32_t next_hb_ms;
    uint32_t hb_seq;

    /* the inspection rule parameters, set by CONFIG (never hard-coded) */
    bool     configured;
    uint32_t n;
    uint32_t k;
    uint32_t inspect_ms;

    /* the one job in progress */
    bool     busy;
    uint32_t job_deadline_ms;
    uint8_t  job_slot;              /* index into jobs[] */

    job_record_t jobs[STATION_HISTORY];
    uint8_t      jobs_next;
    cfg_record_t cfgs[STATION_HISTORY];
    uint8_t      cfgs_next;
    outbox_t     out[STATION_OUTBOX];

    json_obj_t      scratch;        /* parse buffer, kept here to save stack */
    station_stats_t stats;
} station_t;

void station_init(station_t *s, station_tx_fn tx, station_log_fn log, void *ctx);

/* The TCP connection to the gateway is up: sends HELLO. */
void station_on_connect(station_t *s, uint32_t now_ms);

/* The TCP connection is gone (closed by either side). Safe state: the job in
 * progress is abandoned and nothing more is sent. Calling it twice is harmless. */
void station_on_disconnect(station_t *s, uint32_t now_ms);

/* One whole message body from the gateway. */
void station_on_message(station_t *s, const uint8_t *body, size_t len, uint32_t now_ms);

/* The framing broke: the caller has to close the connection. */
void station_note_frame_error(station_t *s);

/* Call often (every few ms): heartbeats, inspection timer, ACK time-outs, link watchdog. */
void station_tick(station_t *s, uint32_t now_ms);

/* True when the station wants the connection closed (the link watchdog fired).
 * The caller closes the socket and then calls station_on_disconnect(). */
bool station_drop_requested(const station_t *s);

station_state_t station_state(const station_t *s);

/* The inspection rule: true = FAIL, false = PASS. n must be at least 1. */
bool station_verdict_fail(uint32_t part_id, uint32_t n, uint32_t k);

#endif
