#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "station.h"

#define UL(x) ((unsigned long)(x))

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

/* ---- log and send ----------------------------------------------------- */

static void log_text(station_t *s, const char *tag, const char *text, size_t len)
{
    if (s->log != NULL) {
        s->log(tag, text, len, s->ctx);
    }
}

static void evt(station_t *s, const char *fmt, ...) __attribute__((format(printf, 2, 3)));

static void evt(station_t *s, const char *fmt, ...)
{
    char b[160];
    va_list ap;
    int n;

    va_start(ap, fmt);
    n = vsnprintf(b, sizeof(b), fmt, ap);
    va_end(ap);
    if (n < 0) {
        return;
    }
    if ((size_t)n >= sizeof(b)) {
        n = (int)sizeof(b) - 1;
    }
    log_text(s, "EVT", b, (size_t)n);
}

static void send_body(station_t *s, const char *text, size_t len, bool is_heartbeat)
{
    if (!s->link_up || s->tx == NULL || len == 0) {
        return;
    }
    if (!is_heartbeat || STATION_LOG_HEARTBEATS) {
        log_text(s, "TX", text, len);
    }
    s->tx((const uint8_t *)text, len, s->ctx);
}

/* ---- message builders: compact JSON, field order as in the contract ---- */

static size_t finish(int n, size_t cap)
{
    return (n > 0 && (size_t)n < cap) ? (size_t)n : 0;
}

static size_t build_hello(char *b, size_t cap)
{
    return finish(snprintf(b, cap, "{\"type\":\"HELLO\",\"device\":\"%s\",\"fw\":\"%s\"}",
                           DEVICE_NAME, FW_VERSION), cap);
}

static size_t build_heartbeat(char *b, size_t cap, uint32_t seq)
{
    return finish(snprintf(b, cap, "{\"type\":\"HEARTBEAT\",\"seq\":%lu}", UL(seq)), cap);
}

/* reason == NULL builds an "accepted" ACK, otherwise a "rejected" one. */
static size_t build_ack(char *b, size_t cap, const char *ref, const char *idname, uint32_t id,
                        const char *reason)
{
    if (reason == NULL) {
        return finish(snprintf(b, cap,
                               "{\"type\":\"ACK\",\"ref\":\"%s\",\"%s\":%lu,\"status\":\"accepted\"}",
                               ref, idname, UL(id)), cap);
    }
    return finish(snprintf(b, cap,
                           "{\"type\":\"ACK\",\"ref\":\"%s\",\"%s\":%lu,\"status\":\"rejected\",\"reason\":\"%s\"}",
                           ref, idname, UL(id), reason), cap);
}

static size_t build_cfgdone(char *b, size_t cap, uint32_t cmd_id, uint32_t n, uint32_t k, uint32_t ms)
{
    return finish(snprintf(b, cap,
                           "{\"type\":\"CONFIG_DONE\",\"cmd_id\":%lu,\"N\":%lu,\"K\":%lu,\"inspect_time_ms\":%lu}",
                           UL(cmd_id), UL(n), UL(k), UL(ms)), cap);
}

static size_t build_result(char *b, size_t cap, uint32_t job_id, uint32_t part_id, bool fail,
                           uint32_t n, uint32_t k)
{
    return finish(snprintf(b, cap,
                           "{\"type\":\"JOB_RESULT\",\"job_id\":%lu,\"part_id\":%lu,\"verdict\":\"%s\",\"N\":%lu,\"K\":%lu}",
                           UL(job_id), UL(part_id), fail ? "FAIL" : "PASS", UL(n), UL(k)), cap);
}

/* ---- the inspection rule ---------------------------------------------- */

bool station_verdict_fail(uint32_t part_id, uint32_t n, uint32_t k)
{
    /* 64-bit sum: part_id + K can pass 2^32 */
    return (((uint64_t)part_id + (uint64_t)k) % (uint64_t)n) == 0u;
}

/* ---- records and the outbox ------------------------------------------- */

static job_record_t *find_job(station_t *s, uint32_t job_id)
{
    for (size_t i = 0; i < STATION_HISTORY; i++) {
        if (s->jobs[i].used && s->jobs[i].job_id == job_id) {
            return &s->jobs[i];
        }
    }
    return NULL;
}

static cfg_record_t *find_cfg(station_t *s, uint32_t cmd_id)
{
    for (size_t i = 0; i < STATION_HISTORY; i++) {
        if (s->cfgs[i].used && s->cfgs[i].cmd_id == cmd_id) {
            return &s->cfgs[i];
        }
    }
    return NULL;
}

static outbox_t *find_out(station_t *s, out_kind_t kind, uint32_t id)
{
    for (size_t i = 0; i < STATION_OUTBOX; i++) {
        if (s->out[i].used && s->out[i].kind == kind && s->out[i].id == id) {
            return &s->out[i];
        }
    }
    return NULL;
}

/* Sends a message that needs an ACK and keeps its bytes for retries.
 * If the same message is already waiting for its ACK, nothing happens. */
static void out_add(station_t *s, out_kind_t kind, uint32_t id, const char *text, size_t len, uint32_t now)
{
    outbox_t *e;

    if (len == 0 || len > sizeof(e->body) || find_out(s, kind, id) != NULL) {
        return;
    }
    e = NULL;
    for (size_t i = 0; i < STATION_OUTBOX; i++) {
        if (!s->out[i].used) {
            e = &s->out[i];
            break;
        }
    }
    if (e == NULL) {                        /* cannot happen with one job and one command at a time */
        s->stats.outbox_overflow++;
        evt(s, "outbox full: oldest slot reused");
        e = &s->out[0];
    }
    e->used = true;
    e->kind = kind;
    e->id = id;
    e->tries = 1;
    e->deadline_ms = now + STATION_ACK_TIMEOUT_MS;
    e->len = (uint16_t)len;
    memcpy(e->body, text, len);
    send_body(s, text, len, false);
}

static void out_clear(station_t *s)
{
    for (size_t i = 0; i < STATION_OUTBOX; i++) {
        s->out[i].used = false;
    }
}

/* ---- connection ------------------------------------------------------- */

/* The safe state: forget the job in progress and everything waiting for an ACK.
 * A job or command that comes again after reconnecting is recognised by its ID. */
static void go_disconnected(station_t *s)
{
    s->link_up = false;
    s->stats.disconnects++;
    if (s->busy) {
        job_record_t *r = &s->jobs[s->job_slot];

        r->state = JOB_ABANDONED;
        s->busy = false;
        s->stats.jobs_abandoned++;
        evt(s, "job_id=%lu part_id=%lu stage=abandoned (link lost)", UL(r->job_id), UL(r->part_id));
    }
    out_clear(s);
    evt(s, "link down");
}

void station_init(station_t *s, station_tx_fn tx, station_log_fn log, void *ctx)
{
    memset(s, 0, sizeof(*s));
    s->tx = tx;
    s->log = log;
    s->ctx = ctx;
}

void station_on_connect(station_t *s, uint32_t now)
{
    char b[STATION_MSG_MAX];
    size_t n;

    s->link_up = true;
    s->drop_request = false;
    s->last_rx_ms = now;
    s->next_hb_ms = now;                    /* first heartbeat on the first tick */
    s->stats.connects++;
    out_clear(s);
    evt(s, "link up");
    n = build_hello(b, sizeof(b));
    send_body(s, b, n, false);
}

void station_on_disconnect(station_t *s, uint32_t now)
{
    (void)now;
    if (s->link_up) {
        go_disconnected(s);
    }
    s->drop_request = false;
}

void station_note_frame_error(station_t *s)
{
    s->stats.frame_errors++;
    evt(s, "framing error: announced length over %d bytes", STATION_MAX_BODY);
}

bool station_drop_requested(const station_t *s)
{
    return s->drop_request;
}

station_state_t station_state(const station_t *s)
{
    if (!s->link_up) {
        return STATION_DISCONNECTED;
    }
    return s->busy ? STATION_INSPECTING : STATION_IDLE;
}

/* ---- CONFIG ----------------------------------------------------------- */

static void send_ack(station_t *s, const char *ref, const char *idname, uint32_t id, const char *reason)
{
    char b[STATION_MSG_MAX];
    size_t n = build_ack(b, sizeof(b), ref, idname, id, reason);

    send_body(s, b, n, false);
}

static void queue_cfgdone(station_t *s, const cfg_record_t *r, uint32_t now)
{
    char b[STATION_MSG_MAX];
    size_t n = build_cfgdone(b, sizeof(b), r->cmd_id, r->n, r->k, r->inspect_ms);

    out_add(s, OUT_CFGDONE, r->cmd_id, b, n, now);
}

static void reject_cmd(station_t *s, uint32_t cmd_id, const char *reason)
{
    s->stats.cmds_rejected++;
    evt(s, "cmd_id=%lu stage=rejected reason=%s", UL(cmd_id), reason);
    send_ack(s, "CONFIG", "cmd_id", cmd_id, reason);
}

static void handle_config(station_t *s, const json_obj_t *o, uint32_t now)
{
    uint32_t cmd_id, n, k, ms;
    json_get_t gn, gk, gm;
    cfg_record_t *r;

    if (json_get_u32(o, "cmd_id", &cmd_id) != JF_OK) {
        s->stats.bad_fields++;              /* no valid id: there is nothing to put in an ACK */
        evt(s, "CONFIG without a valid cmd_id: ignored");
        return;
    }

    r = find_cfg(s, cmd_id);
    if (r != NULL) {                        /* repeat of an accepted command: answer again, do not apply again */
        s->stats.cmd_duplicates++;
        evt(s, "cmd_id=%lu stage=duplicate (answers repeated, not applied again)", UL(cmd_id));
        send_ack(s, "CONFIG", "cmd_id", cmd_id, NULL);
        queue_cfgdone(s, r, now);
        return;
    }

    gn = json_get_u32(o, "N", &n);
    gk = json_get_u32(o, "K", &k);
    gm = json_get_u32(o, "inspect_time_ms", &ms);
    if (gn == JF_MISSING || gn == JF_WRONG_TYPE || gk == JF_MISSING || gk == JF_WRONG_TYPE ||
        gm == JF_MISSING || gm == JF_WRONG_TYPE) {
        reject_cmd(s, cmd_id, "malformed");
        return;
    }
    if (gn != JF_OK || gk != JF_OK || gm != JF_OK ||
        n < 1u || ms < STATION_INSPECT_MIN_MS || ms > STATION_INSPECT_MAX_MS) {
        reject_cmd(s, cmd_id, "out_of_range");
        return;
    }

    /* valid: apply, remember the command id, answer acceptance, then completion */
    s->configured = true;
    s->n = n;
    s->k = k;
    s->inspect_ms = ms;
    r = &s->cfgs[s->cfgs_next];
    s->cfgs_next = (uint8_t)((s->cfgs_next + 1u) % STATION_HISTORY);
    r->used = true;
    r->cmd_id = cmd_id;
    r->n = n;
    r->k = k;
    r->inspect_ms = ms;
    s->stats.cmds_accepted++;

    evt(s, "cmd_id=%lu stage=accepted N=%lu K=%lu inspect_time_ms=%lu", UL(cmd_id), UL(n), UL(k), UL(ms));
    send_ack(s, "CONFIG", "cmd_id", cmd_id, NULL);
    evt(s, "cmd_id=%lu stage=completed N=%lu K=%lu inspect_time_ms=%lu", UL(cmd_id), UL(n), UL(k), UL(ms));
    queue_cfgdone(s, r, now);
}

/* ---- JOB_REQ ---------------------------------------------------------- */

static void reject_job(station_t *s, uint32_t job_id, uint32_t part_id, const char *reason)
{
    s->stats.jobs_rejected++;
    evt(s, "job_id=%lu part_id=%lu stage=rejected reason=%s", UL(job_id), UL(part_id), reason);
    send_ack(s, "JOB_REQ", "job_id", job_id, reason);
}

static void queue_result(station_t *s, const job_record_t *r, uint32_t now)
{
    char b[STATION_MSG_MAX];
    size_t n = build_result(b, sizeof(b), r->job_id, r->part_id, r->fail, r->n, r->k);

    out_add(s, OUT_RESULT, r->job_id, b, n, now);
}

static void handle_job_req(station_t *s, const json_obj_t *o, uint32_t now)
{
    uint32_t job_id, part_id;
    job_record_t *r;

    if (json_get_u32(o, "job_id", &job_id) != JF_OK) {
        s->stats.bad_fields++;
        evt(s, "JOB_REQ without a valid job_id: ignored");
        return;
    }
    if (json_get_u32(o, "part_id", &part_id) != JF_OK) {
        s->stats.jobs_rejected++;
        evt(s, "job_id=%lu stage=rejected reason=malformed", UL(job_id));
        send_ack(s, "JOB_REQ", "job_id", job_id, "malformed");
        return;
    }

    r = find_job(s, job_id);
    if (r != NULL) {                        /* repeat of an accepted job: never inspect twice */
        s->stats.job_duplicates++;
        if (r->part_id != part_id) {
            reject_job(s, job_id, part_id, "id_conflict");
        } else if (r->state == JOB_ABANDONED) {
            reject_job(s, job_id, part_id, "abandoned");
        } else {
            evt(s, "job_id=%lu part_id=%lu stage=duplicate (answers repeated, not inspected again)",
                UL(job_id), UL(part_id));
            send_ack(s, "JOB_REQ", "job_id", job_id, NULL);
            if (r->state == JOB_DONE) {
                queue_result(s, r, now);
            }
        }
        return;
    }

    if (!s->configured) {                   /* no N, K or inspection time yet: nothing is hard-coded */
        reject_job(s, job_id, part_id, "not_configured");
        return;
    }
    if (s->busy) {                          /* one job at a time */
        reject_job(s, job_id, part_id, "busy");
        return;
    }

    s->job_slot = s->jobs_next;
    r = &s->jobs[s->jobs_next];
    s->jobs_next = (uint8_t)((s->jobs_next + 1u) % STATION_HISTORY);
    r->used = true;
    r->job_id = job_id;
    r->part_id = part_id;
    r->state = JOB_RUNNING;
    r->fail = false;
    r->n = s->n;                            /* the job keeps the parameters it was accepted with */
    r->k = s->k;
    s->busy = true;
    s->job_deadline_ms = now + s->inspect_ms;
    s->stats.jobs_accepted++;

    evt(s, "job_id=%lu part_id=%lu stage=accepted N=%lu K=%lu inspect_time_ms=%lu",
        UL(job_id), UL(part_id), UL(r->n), UL(r->k), UL(s->inspect_ms));
    send_ack(s, "JOB_REQ", "job_id", job_id, NULL);
}

static void complete_job(station_t *s, uint32_t now)
{
    job_record_t *r = &s->jobs[s->job_slot];

    r->fail = station_verdict_fail(r->part_id, r->n, r->k);
    r->state = JOB_DONE;
    s->busy = false;
    s->stats.jobs_completed++;
    if (r->fail) {
        s->stats.verdict_fail++;
    } else {
        s->stats.verdict_pass++;
    }
    evt(s, "job_id=%lu part_id=%lu stage=completed verdict=%s N=%lu K=%lu",
        UL(r->job_id), UL(r->part_id), r->fail ? "FAIL" : "PASS", UL(r->n), UL(r->k));
    queue_result(s, r, now);
}

/* ---- ACK from the gateway --------------------------------------------- */

static void handle_ack(station_t *s, const json_obj_t *o)
{
    const char *ref;
    const char *status;
    const char *idname;
    out_kind_t kind;
    uint32_t id;
    bool accepted;
    outbox_t *e;

    if (json_get_str(o, "ref", &ref) != JF_OK || json_get_str(o, "status", &status) != JF_OK) {
        s->stats.bad_fields++;
        evt(s, "ACK without ref or status: ignored");
        return;
    }
    if (strcmp(ref, "JOB_RESULT") == 0) {
        kind = OUT_RESULT;
        idname = "job_id";
    } else if (strcmp(ref, "CONFIG_DONE") == 0) {
        kind = OUT_CFGDONE;
        idname = "cmd_id";
    } else {
        s->stats.stale_acks++;
        evt(s, "ACK for %s: nothing is waiting for it", ref);
        return;
    }
    if (json_get_u32(o, idname, &id) != JF_OK) {
        s->stats.bad_fields++;
        evt(s, "ACK for %s without a valid %s: ignored", ref, idname);
        return;
    }
    if (strcmp(status, "accepted") == 0) {
        accepted = true;
    } else if (strcmp(status, "rejected") == 0) {
        accepted = false;
    } else {
        s->stats.bad_fields++;
        evt(s, "ACK with unknown status: ignored");
        return;
    }

    e = find_out(s, kind, id);
    if (e == NULL) {                        /* late or repeated ACK */
        s->stats.stale_acks++;
        evt(s, "ACK for %s %s=%lu: nothing is waiting for it", ref, idname, UL(id));
        return;
    }
    e->used = false;
    if (kind == OUT_RESULT) {
        if (accepted) {
            s->stats.results_acked++;
        } else {
            s->stats.results_rejected++;
        }
    } else {
        if (accepted) {
            s->stats.cfgdone_acked++;
        } else {
            s->stats.cfgdone_rejected++;
        }
    }
    evt(s, "%s %s=%lu acknowledged by the gateway (%s)", ref, idname, UL(id), accepted ? "accepted" : "rejected");
}

/* ---- input ------------------------------------------------------------ */

void station_on_message(station_t *s, const uint8_t *body, size_t len, uint32_t now)
{
    json_obj_t *o = &s->scratch;
    const char *type;

    if (!s->link_up) {
        return;
    }
    if (len > STATION_MAX_BODY || !json_parse_object((const char *)body, len, o) ||
        json_get_str(o, "type", &type) != JF_OK) {
        log_text(s, "RX", (const char *)body, len);
        s->stats.malformed++;
        evt(s, "message is not a JSON object with a \"type\": ignored");
        return;
    }
    if (strcmp(type, "HEARTBEAT") != 0 || STATION_LOG_HEARTBEATS) {
        log_text(s, "RX", (const char *)body, len);
    }
    s->last_rx_ms = now;                    /* any valid message proves the gateway is there */

    if (strcmp(type, "HEARTBEAT") == 0) {
        s->stats.heartbeats_rx++;
    } else if (strcmp(type, "CONFIG") == 0) {
        handle_config(s, o, now);
    } else if (strcmp(type, "JOB_REQ") == 0) {
        handle_job_req(s, o, now);
    } else if (strcmp(type, "ACK") == 0) {
        handle_ack(s, o);
    } else {                                /* HELLO, JOB_RESULT, CONFIG_DONE or something new */
        s->stats.unexpected_type++;
        evt(s, "unexpected message type %s: ignored", type);
    }
}

/* ---- time-driven work ------------------------------------------------- */

static void tick_outbox(station_t *s, uint32_t now)
{
    for (size_t i = 0; i < STATION_OUTBOX; i++) {
        outbox_t *e = &s->out[i];
        bool is_result = (e->kind == OUT_RESULT);

        if (!e->used || !time_reached(now, e->deadline_ms)) {
            continue;
        }
        if (e->tries < STATION_MAX_TRIES) {
            e->tries++;
            e->deadline_ms = now + STATION_ACK_TIMEOUT_MS;
            if (is_result) {
                s->stats.result_retries++;
            } else {
                s->stats.cfgdone_retries++;
            }
            evt(s, "%s %s=%lu: no ACK, retry %u of %u (same bytes)",
                is_result ? "JOB_RESULT" : "CONFIG_DONE", is_result ? "job_id" : "cmd_id",
                UL(e->id), (unsigned)(e->tries - 1u), (unsigned)(STATION_MAX_TRIES - 1u));
            send_body(s, (const char *)e->body, e->len, false);
        } else {                            /* the job or command is reported as failed, never as success */
            if (is_result) {
                s->stats.results_failed++;
            } else {
                s->stats.cfgdone_failed++;
            }
            evt(s, "%s %s=%lu stage=failed: no ACK after %u tries",
                is_result ? "JOB_RESULT" : "CONFIG_DONE", is_result ? "job_id" : "cmd_id",
                UL(e->id), (unsigned)STATION_MAX_TRIES);
            e->used = false;
        }
    }
}

void station_tick(station_t *s, uint32_t now)
{
    if (!s->link_up) {
        return;
    }

    if (time_since(now, s->last_rx_ms) >= STATION_LINK_TIMEOUT_MS) {
        s->stats.link_timeouts++;
        evt(s, "link watchdog: nothing valid from the gateway for %lu ms", UL(time_since(now, s->last_rx_ms)));
        go_disconnected(s);
        s->drop_request = true;             /* the caller closes the socket */
        return;
    }

    if (time_reached(now, s->next_hb_ms)) {
        char b[STATION_MSG_MAX];
        size_t n = build_heartbeat(b, sizeof(b), s->hb_seq++);

        send_body(s, b, n, true);
        s->next_hb_ms = now + STATION_HEARTBEAT_MS;
    }

    if (s->busy && time_reached(now, s->job_deadline_ms)) {
        complete_job(s, now);
    }

    tick_outbox(s, now);
}
