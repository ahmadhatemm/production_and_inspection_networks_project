#include <string.h>

#include "prodmsg.h"
#include "station.h"

/* ---- time ------------------------------------------------------------- */

/* now_ms wraps around after 49 days. These two comparisons stay correct across the wrap. */
static bool time_reached(uint32_t now, uint32_t deadline)
{
    return (int32_t)(now - deadline) >= 0;
}

static uint32_t time_since(uint32_t now, uint32_t then)
{
    return (uint32_t)(now - then);
}

/* ---- sending ---------------------------------------------------------- */

/* Builds one ATR frame around a production message. Returns its length. */
static size_t build_frame(station_t *s, uint8_t *out, size_t cap, const prod_msg_t *m)
{
    uint8_t payload[ATR_PROD_PAYLOAD_LEN];

    prod_encode(payload, m);
    return atr_build(out, cap, ATR_TEST_ID_PRODUCTION, s->tx_seq++, payload, ATR_PROD_PAYLOAD_LEN);
}

static void send_msg(station_t *s, uint8_t type, uint32_t id, uint8_t arg)
{
    const prod_msg_t m = { .type = type, .id = id, .arg = arg, .value_a = 0, .value_b = 0 };
    uint8_t frame[ATR_PROD_FRAME_LEN];
    size_t n = build_frame(s, frame, sizeof(frame), &m);

    if (n > 0) {
        s->tx(frame, n, s->tx_ctx);
    }
}

static void send_pending_part(station_t *s)
{
    s->tx(s->pending_frame, sizeof(s->pending_frame), s->tx_ctx);
}

/* ---- batch control ---------------------------------------------------- */

/* The safe state: stop producing and forget the part in flight. */
static void enter_idle(station_t *s)
{
    s->state = STATION_IDLE;
    s->finished = false;
    s->pending = false;
    s->pending_hold = false;
}

static void begin_batch(station_t *s, uint32_t cycle_ms, uint32_t target, uint32_t now)
{
    s->state = STATION_RUNNING;
    s->finished = false;
    s->cycle_ms = cycle_ms;
    s->target = target;
    s->produced = 0;
    s->pending = false;
    s->pending_hold = false;
    s->next_part_ms = now + cycle_ms;
}

static void make_part(station_t *s, uint32_t now)
{
    const prod_msg_t m = { .type = PM_PART, .id = s->produced + 1u, .arg = 0, .value_a = 0, .value_b = 0 };

    s->produced++;
    build_frame(s, s->pending_frame, sizeof(s->pending_frame), &m);
    s->pending = true;
    s->pending_hold = false;
    s->pending_part_id = m.id;
    s->pending_tries = 1;
    s->pending_deadline_ms = now + STATION_ACK_TIMEOUT_MS;
    s->stats.parts_sent++;

    /* Fixed rate. If the station was held up for more than one cycle, do not catch up in a burst. */
    s->next_part_ms += s->cycle_ms;
    if (time_reached(now, s->next_part_ms)) {
        s->next_part_ms = now + s->cycle_ms;
    }
    send_pending_part(s);
}

/* ---- commands --------------------------------------------------------- */

static cmd_record_t *find_cmd(station_t *s, uint32_t cmd_id)
{
    for (size_t i = 0; i < STATION_CMD_HISTORY; i++) {
        if (s->history[i].used && s->history[i].cmd_id == cmd_id) {
            return &s->history[i];
        }
    }
    return NULL;
}

static cmd_record_t *remember_cmd(station_t *s, uint32_t cmd_id)
{
    cmd_record_t *r = &s->history[s->history_next];

    s->history_next = (uint8_t)((s->history_next + 1u) % STATION_CMD_HISTORY);
    r->used = true;
    r->cmd_id = cmd_id;
    r->ack_arg = PM_CMDACK_REJECTED;
    r->done_arg = 0;
    return r;
}

/* Acceptance, then (if accepted) completion. */
static void reply_to_cmd(station_t *s, const cmd_record_t *r)
{
    send_msg(s, PM_CMD_ACK, r->cmd_id, r->ack_arg);
    if (r->done_arg != 0) {
        send_msg(s, PM_CMD_DONE, r->cmd_id, r->done_arg);
    }
}

static void execute_cmd(station_t *s, const prod_msg_t *m, cmd_record_t *r, uint32_t now)
{
    switch (m->arg) {
    case PM_CMD_START:
        /* A finished batch counts as idle: the gateway may start the next one. */
        if ((s->state == STATION_IDLE || s->finished) &&
            m->value_a >= STATION_CYCLE_MIN_MS && m->value_a <= STATION_CYCLE_MAX_MS &&
            m->value_b >= STATION_TARGET_MIN && m->value_b <= STATION_TARGET_MAX) {
            begin_batch(s, m->value_a, m->value_b, now);
            r->done_arg = PM_DONE_RUNNING;
        }
        break;
    case PM_CMD_PAUSE:
        if (s->state == STATION_RUNNING) {
            s->state = STATION_PAUSED;
            r->done_arg = PM_DONE_PAUSED;
        }
        break;
    case PM_CMD_RESUME:
        if (s->state == STATION_PAUSED) {
            s->state = STATION_RUNNING;
            s->next_part_ms = now + s->cycle_ms;
            r->done_arg = PM_DONE_RUNNING;
        }
        break;
    case PM_CMD_STOP:
        if (s->state != STATION_IDLE) {
            enter_idle(s);
            r->done_arg = PM_DONE_STOPPED;
        }
        break;
    default:
        break;
    }

    if (r->done_arg != 0) {
        r->ack_arg = PM_CMDACK_ACCEPTED;
        s->stats.cmds_accepted++;
    } else {
        s->stats.cmds_rejected++;
    }
}

static void handle_cmd(station_t *s, const prod_msg_t *m, uint32_t now)
{
    cmd_record_t *r = find_cmd(s, m->id);

    if (r != NULL) {                    /* seen before: do not run it again, repeat the answer */
        s->stats.duplicate_cmds++;
        reply_to_cmd(s, r);
        return;
    }
    r = remember_cmd(s, m->id);
    execute_cmd(s, m, r, now);
    reply_to_cmd(s, r);
}

/* ---- part answers ----------------------------------------------------- */

static void handle_part_ack(station_t *s, const prod_msg_t *m, uint32_t now)
{
    if (!s->pending || m->id != s->pending_part_id) {
        s->stats.stale_acks++;
        return;
    }
    if (m->arg == PM_ACK_ACCEPTED) {
        s->pending = false;
        s->pending_hold = false;
        s->stats.parts_acked++;
        if (s->produced >= s->target) {
            s->finished = true;
            s->stats.batches_done++;
        }
    } else if (m->arg == PM_ACK_QUEUE_FULL) {
        s->stats.queue_full++;
        s->pending_hold = true;
        s->pending_tries = 0;           /* a full queue does not use up the retries */
        s->pending_deadline_ms = now + STATION_FULL_RETRY_MS;
    } else {
        s->stats.bad_acks++;
    }
}

/* ---- input ------------------------------------------------------------ */

void station_note_slip_drop(station_t *s)
{
    s->stats.slip_drops++;
}

void station_on_packet(station_t *s, const uint8_t *raw, size_t raw_len, uint32_t now)
{
    atr_t a;
    prod_msg_t m;

    switch (atr_parse(raw, raw_len, &a)) {
    case ATR_OK:
        break;
    case ATR_TOO_SHORT:
        s->stats.short_frames++;
        return;
    case ATR_BAD_CRC:
        s->stats.crc_errors++;
        return;
    case ATR_BAD_LENGTH:
    default:
        s->stats.length_errors++;
        return;
    }
    s->stats.frames_ok++;
    s->link_up = true;                  /* a frame with a good CRC proves the gateway is there */
    s->last_rx_ms = now;

    if (a.test_id != ATR_TEST_ID_PRODUCTION) {
        s->stats.wrong_test_id++;
        return;
    }
    if (a.payload_len != ATR_PROD_PAYLOAD_LEN) {
        s->stats.bad_payload_len++;
        return;
    }
    prod_decode(a.payload, &m);

    switch (m.type) {
    case PM_PART_ACK:
        handle_part_ack(s, &m, now);
        break;
    case PM_CMD:
        handle_cmd(s, &m, now);
        break;
    case PM_HEARTBEAT:
        break;                          /* nothing to do: the frame already counted as a sign of life */
    default:
        s->stats.unexpected_type++;
        break;
    }
}

/* ---- time-driven work ------------------------------------------------- */

static void tick_pending(station_t *s, uint32_t now)
{
    if (!s->pending || !time_reached(now, s->pending_deadline_ms)) {
        return;
    }
    if (s->pending_hold) {              /* QUEUE_FULL wait is over: try the part again */
        s->pending_hold = false;
        s->pending_tries = 1;
        s->pending_deadline_ms = now + STATION_ACK_TIMEOUT_MS;
        send_pending_part(s);
    } else if (s->pending_tries < STATION_MAX_TRIES) {
        s->pending_tries++;
        s->pending_deadline_ms = now + STATION_ACK_TIMEOUT_MS;
        s->stats.retries++;
        send_pending_part(s);
    } else {                            /* nobody answers: stop rather than produce into the void */
        s->stats.parts_failed++;
        enter_idle(s);
    }
}

void station_tick(station_t *s, uint32_t now)
{
    if (time_reached(now, s->next_hb_ms)) {
        send_msg(s, PM_HEARTBEAT, s->hb_counter++, 0);
        s->next_hb_ms = now + STATION_HEARTBEAT_MS;
    }

    if (s->link_up && time_since(now, s->last_rx_ms) >= STATION_LINK_TIMEOUT_MS) {
        s->link_up = false;
        s->stats.link_lost++;
        enter_idle(s);
    }

    tick_pending(s, now);

    if (s->state == STATION_RUNNING && !s->pending && !s->finished &&
        s->produced < s->target && time_reached(now, s->next_part_ms)) {
        make_part(s, now);
    }
}

void station_init(station_t *s, station_tx_fn tx, void *tx_ctx, uint32_t now)
{
    memset(s, 0, sizeof(*s));
    s->tx = tx;
    s->tx_ctx = tx_ctx;
    s->state = STATION_IDLE;
    s->next_hb_ms = now;                /* first heartbeat on the first tick */
}
