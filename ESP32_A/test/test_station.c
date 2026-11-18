/* PC tests for the code of ESP32 A. No board needed.
 *
 *   make -C test        builds with warnings as errors and the address/UB sanitizers, then runs
 *
 * A fake gateway feeds frames to the station and a fake clock drives it in 5 ms steps. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../main/atr.h"
#include "../main/crc16.h"
#include "../main/prodmsg.h"
#include "../main/slip.h"
#include "../main/station.h"

static int g_fail;
static int g_checks;

#define CHECK(cond) do { \
    g_checks++; \
    if (!(cond)) { g_fail++; printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); } \
} while (0)

/* ---- fake wire and clock ---------------------------------------------- */

typedef struct { uint8_t d[ATR_MAX_FRAME_LEN]; size_t n; } wire_frame_t;

#define MAX_TX 4000
static wire_frame_t g_tx[MAX_TX];
static int          g_ntx;
static uint32_t     g_now;
static int          g_gw_alive;         /* the fake gateway sends a heartbeat every second */
static uint32_t     g_next_gw_hb;
static uint32_t     g_gw_seq;

static void capture(const uint8_t *frame, size_t n, void *ctx)
{
    (void)ctx;
    if (g_ntx < MAX_TX && n <= ATR_MAX_FRAME_LEN) {
        memcpy(g_tx[g_ntx].d, frame, n);
        g_tx[g_ntx].n = n;
        g_ntx++;
    }
}

static prod_msg_t tx_msg(int i)
{
    atr_t a;
    prod_msg_t m;

    memset(&m, 0, sizeof(m));
    CHECK(i >= 0 && i < g_ntx);
    CHECK(atr_parse(g_tx[i].d, g_tx[i].n, &a) == ATR_OK);
    CHECK(a.test_id == ATR_TEST_ID_PRODUCTION);
    CHECK(a.payload_len == ATR_PROD_PAYLOAD_LEN);
    prod_decode(a.payload, &m);
    return m;
}

static uint32_t tx_seq_of(int i)
{
    atr_t a;
    CHECK(atr_parse(g_tx[i].d, g_tx[i].n, &a) == ATR_OK);
    return a.seq;
}

static int count_type(int from, uint8_t type)
{
    int c = 0;
    for (int i = from; i < g_ntx; i++) {
        if (tx_msg(i).type == type) {
            c++;
        }
    }
    return c;
}

static void gw_raw(station_t *s, const uint8_t *frame, size_t n)
{
    station_on_packet(s, frame, n, g_now);
}

static void gw_send(station_t *s, uint8_t type, uint32_t id, uint8_t arg, uint32_t va, uint32_t vb)
{
    const prod_msg_t m = { .type = type, .id = id, .arg = arg, .value_a = va, .value_b = vb };
    uint8_t payload[ATR_PROD_PAYLOAD_LEN];
    uint8_t frame[ATR_PROD_FRAME_LEN];
    size_t n;

    prod_encode(payload, &m);
    n = atr_build(frame, sizeof(frame), ATR_TEST_ID_PRODUCTION, g_gw_seq++, payload, ATR_PROD_PAYLOAD_LEN);
    CHECK(n == ATR_PROD_FRAME_LEN);
    gw_raw(s, frame, n);
}

static void run_to(station_t *s, uint32_t t)
{
    while ((int32_t)(g_now - t) < 0) {                              /* signed compare: survives the clock wrap */
        g_now += 5;
        if (g_gw_alive && (int32_t)(g_now - g_next_gw_hb) >= 0) {
            gw_send(s, PM_HEARTBEAT, 0, 0, 0, 0);
            g_next_gw_hb = g_now + 1000;
        }
        station_tick(s, g_now);
    }
}

static void setup(station_t *s, uint32_t start)
{
    g_ntx = 0;
    g_now = start;
    g_gw_alive = 1;
    g_next_gw_hb = start + 1000;
    g_gw_seq = 0;
    station_init(s, capture, NULL, start);
}

/* send a command and return the index of its first answer frame */
static int cmd(station_t *s, uint32_t id, uint8_t code, uint32_t va, uint32_t vb)
{
    int first = g_ntx;
    gw_send(s, PM_CMD, id, code, va, vb);
    return first;
}

static void expect_accepted(int first, uint32_t id, uint8_t done)
{
    CHECK(g_ntx == first + 2);
    if (g_ntx == first + 2) {
        prod_msg_t a = tx_msg(first), d = tx_msg(first + 1);
        CHECK(a.type == PM_CMD_ACK && a.id == id && a.arg == PM_CMDACK_ACCEPTED);
        CHECK(d.type == PM_CMD_DONE && d.id == id && d.arg == done);
    }
}

static void expect_rejected(int first, uint32_t id)
{
    CHECK(g_ntx == first + 1);
    if (g_ntx == first + 1) {
        prod_msg_t a = tx_msg(first);
        CHECK(a.type == PM_CMD_ACK && a.id == id && a.arg == PM_CMDACK_REJECTED);
    }
}

/* ---- the tests -------------------------------------------------------- */

static void test_codec(void)
{
    /* values computed independently in Python (binascii.crc_hqx) */
    static const uint8_t part1[26] = {
        0x00,0x00, 0x00,0x00,0x00,0x00, 0x00,0x10,
        0x01, 0x00,0x00,0x00,0x01, 0x00, 0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00, 0x00,0x00,
        0x5a,0xda };
    static const uint8_t start_cmd[26] = {
        0x00,0x00, 0x00,0x00,0x00,0x05, 0x00,0x10,
        0x03, 0x00,0x00,0x00,0x64, 0x01, 0x00,0x00,0x00,0x64, 0x00,0x00,0x00,0x03, 0x00,0x00,
        0xc2,0x37 };
    uint8_t payload[ATR_PROD_PAYLOAD_LEN];
    uint8_t frame[ATR_PROD_FRAME_LEN];
    prod_msg_t m, back;
    atr_t a;

    CHECK(crc16_ccitt((const uint8_t *)"123456789", 9, CRC16_INIT) == 0x29B1);
    CHECK(crc16_ccitt((const uint8_t *)"", 0, CRC16_INIT) == 0xFFFF);

    m = (prod_msg_t){ .type = PM_PART, .id = 1, .arg = 0, .value_a = 0, .value_b = 0 };
    prod_encode(payload, &m);
    CHECK(atr_build(frame, sizeof(frame), 0, 0, payload, 16) == 26);
    CHECK(memcmp(frame, part1, 26) == 0);

    m = (prod_msg_t){ .type = PM_CMD, .id = 100, .arg = PM_CMD_START, .value_a = 100, .value_b = 3 };
    prod_encode(payload, &m);
    CHECK(atr_build(frame, sizeof(frame), 0, 5, payload, 16) == 26);
    CHECK(memcmp(frame, start_cmd, 26) == 0);

    CHECK(atr_parse(frame, 26, &a) == ATR_OK);
    CHECK(a.test_id == 0 && a.seq == 5 && a.payload_len == 16);
    prod_decode(a.payload, &back);
    CHECK(back.type == PM_CMD && back.id == 100 && back.arg == 1 && back.value_a == 100 && back.value_b == 3);

    /* every field uses its full width, big-endian */
    m = (prod_msg_t){ .type = 0xAA, .id = 0x01020304, .arg = 0xBB, .value_a = 0x0A0B0C0D, .value_b = 0xF1F2F3F4 };
    prod_encode(payload, &m);
    CHECK(payload[0] == 0xAA && payload[1] == 1 && payload[4] == 4 && payload[5] == 0xBB);
    CHECK(payload[6] == 0x0A && payload[9] == 0x0D && payload[10] == 0xF1 && payload[13] == 0xF4);
    CHECK(payload[14] == 0 && payload[15] == 0);
    CHECK(atr_build(frame, sizeof(frame), 0xFFFF, 0xFFFFFFFFu, payload, 16) == 26);
    CHECK(atr_parse(frame, 26, &a) == ATR_OK && a.test_id == 0xFFFF && a.seq == 0xFFFFFFFFu);

    /* errors */
    CHECK(atr_parse(frame, 9, &a) == ATR_TOO_SHORT);
    frame[12] ^= 0x01;
    CHECK(atr_parse(frame, 26, &a) == ATR_BAD_CRC);
    frame[12] ^= 0x01;
    CHECK(atr_parse(frame, 25, &a) == ATR_BAD_CRC);
    CHECK(atr_build(frame, 25, 0, 0, payload, 16) == 0);            /* buffer too small */
    CHECK(atr_build(frame, sizeof(frame), 0, 0, payload, 129) == 0); /* payload too long */
}

static void test_slip_roundtrip(void)
{
    uint8_t payload[ATR_PROD_PAYLOAD_LEN];
    uint8_t frame[ATR_PROD_FRAME_LEN];
    uint8_t wire[2 * ATR_MAX_FRAME_LEN + 2];
    uint8_t packet[ATR_MAX_FRAME_LEN];
    prod_msg_t m = { .type = PM_PART, .id = 0xC0DBC0DBu, .arg = 0xDB, .value_a = 0xC0C0C0C0u, .value_b = 0xDBDBDBDBu };
    slip_decoder_t dec;
    size_t n, len = 0, got = 0;
    int packets = 0;
    atr_t a;

    prod_encode(payload, &m);
    CHECK(atr_build(frame, sizeof(frame), 0, 0xC0DB0000u, payload, 16) == 26);
    n = slip_encode(frame, 26, wire, sizeof(wire));
    CHECK(n > 28);                                                   /* escaping made it longer */
    slip_decoder_init(&dec, packet, sizeof(packet));
    for (size_t i = 0; i < n; i++) {
        if (slip_decode_byte(&dec, wire[i], &len) == SLIP_PACKET) {
            packets++;
            got = len;
        }
    }
    CHECK(packets == 1 && got == 26 && memcmp(packet, frame, 26) == 0);
    CHECK(atr_parse(packet, got, &a) == ATR_OK);
}

static void test_heartbeat(void)
{
    station_t s;

    setup(&s, 0);
    g_gw_alive = 0;
    station_tick(&s, 0);
    CHECK(g_ntx == 1 && tx_msg(0).type == PM_HEARTBEAT && tx_msg(0).id == 0 && tx_msg(0).arg == 0);
    CHECK(tx_seq_of(0) == 0);
    run_to(&s, 995);
    CHECK(g_ntx == 1);
    run_to(&s, 1000);
    CHECK(g_ntx == 2 && tx_msg(1).type == PM_HEARTBEAT && tx_msg(1).id == 1 && tx_seq_of(1) == 1);
    run_to(&s, 2000);
    CHECK(g_ntx == 3 && tx_msg(2).id == 2);
}

static void test_normal_batch(void)
{
    station_t s;
    int first;

    setup(&s, 0);
    run_to(&s, 1000);
    first = cmd(&s, 100, PM_CMD_START, 100, 3);                     /* cycle 100 ms, 3 parts */
    expect_accepted(first, 100, PM_DONE_RUNNING);
    CHECK(s.state == STATION_RUNNING && s.target == 3 && s.cycle_ms == 100);

    first = g_ntx;
    run_to(&s, 1095);
    CHECK(count_type(first, PM_PART) == 0);                          /* first part only after one cycle */
    run_to(&s, 1100);
    CHECK(count_type(first, PM_PART) == 1);
    CHECK(tx_msg(g_ntx - 1).type == PM_PART && tx_msg(g_ntx - 1).id == 1);

    run_to(&s, 1110);
    gw_send(&s, PM_PART_ACK, 1, PM_ACK_ACCEPTED, 0, 0);
    run_to(&s, 1195);
    CHECK(s.produced == 1);
    run_to(&s, 1200);
    CHECK(s.produced == 2 && tx_msg(g_ntx - 1).id == 2);             /* one part every 100 ms */
    gw_send(&s, PM_PART_ACK, 2, PM_ACK_ACCEPTED, 0, 0);
    run_to(&s, 1300);
    CHECK(s.produced == 3 && tx_msg(g_ntx - 1).id == 3);
    CHECK(!s.finished);
    gw_send(&s, PM_PART_ACK, 3, PM_ACK_ACCEPTED, 0, 0);
    CHECK(s.finished && s.stats.batches_done == 1 && !s.pending);
    run_to(&s, 2000);
    CHECK(s.produced == 3);                                          /* no part 4 */
    CHECK(count_type(first, PM_PART) == 3);
    CHECK(s.stats.parts_sent == 3 && s.stats.parts_acked == 3 && s.stats.retries == 0);

    /* with no loss every frame has the next seq */
    for (int i = 0; i < g_ntx; i++) {
        CHECK(tx_seq_of(i) == (uint32_t)i);
    }
}

static void test_duplicate_and_restart(void)
{
    station_t s;
    int first;

    setup(&s, 0);
    run_to(&s, 1000);
    cmd(&s, 100, PM_CMD_START, 100, 1);
    run_to(&s, 1100);
    gw_send(&s, PM_PART_ACK, 1, PM_ACK_ACCEPTED, 0, 0);
    CHECK(s.finished);

    /* the same command again: answered again, not executed again */
    first = cmd(&s, 100, PM_CMD_START, 100, 1);
    expect_accepted(first, 100, PM_DONE_RUNNING);
    CHECK(s.stats.duplicate_cmds == 1 && s.stats.cmds_accepted == 1);
    CHECK(s.finished && s.produced == 1);

    /* a new command may start the next batch, because the finished one counts as idle */
    first = cmd(&s, 101, PM_CMD_START, 50, 2);
    expect_accepted(first, 101, PM_DONE_RUNNING);
    CHECK(!s.finished && s.produced == 0 && s.target == 2 && s.cycle_ms == 50);
    run_to(&s, g_now + 50);
    CHECK(s.produced == 1 && tx_msg(g_ntx - 1).type == PM_PART && tx_msg(g_ntx - 1).id == 1);   /* ids restart at 1 */
}

static void test_queue_full(void)
{
    station_t s;
    int first;
    uint8_t original[ATR_PROD_FRAME_LEN];

    setup(&s, 0);
    run_to(&s, 1000);
    cmd(&s, 1, PM_CMD_START, 50, 2);
    run_to(&s, 1050);
    CHECK(s.pending && s.pending_part_id == 1);
    memcpy(original, g_tx[g_ntx - 1].d, sizeof(original));

    /* five QUEUE_FULL answers: more than the 4 tries a silent gateway would allow */
    first = g_ntx;
    for (int k = 0; k < 5; k++) {
        gw_send(&s, PM_PART_ACK, 1, PM_ACK_QUEUE_FULL, 0, 0);
        CHECK(s.pending_hold);
        run_to(&s, g_now + 195);
        CHECK(count_type(first, PM_PART) == k);                      /* not yet */
        run_to(&s, g_now + 5);
        CHECK(count_type(first, PM_PART) == k + 1);                  /* re-sent after 200 ms */
        CHECK(!s.pending_hold);
        CHECK(g_tx[g_ntx - 1].n == sizeof(original) && memcmp(g_tx[g_ntx - 1].d, original, sizeof(original)) == 0);
    }
    CHECK(s.stats.queue_full == 5 && s.stats.parts_failed == 0 && s.state == STATION_RUNNING);
    CHECK(s.produced == 1);                                          /* production is blocked meanwhile */

    gw_send(&s, PM_PART_ACK, 1, PM_ACK_ACCEPTED, 0, 0);
    CHECK(!s.pending && s.produced == 1);
    run_to(&s, g_now + 5);
    CHECK(s.produced == 2);                                          /* next part is due long ago: sent at once */
    run_to(&s, g_now + 20);
    CHECK(s.produced == 2);                                          /* but no burst */
}

static void test_retries(void)
{
    station_t s;
    int first;
    uint32_t t0;

    setup(&s, 0);
    run_to(&s, 1000);
    cmd(&s, 1, PM_CMD_START, 100, 5);
    run_to(&s, 1100);
    t0 = g_now;
    first = g_ntx - 1;
    CHECK(tx_msg(first).type == PM_PART);
    run_to(&s, t0 + 495);
    CHECK(count_type(first, PM_PART) == 1);
    run_to(&s, t0 + 500);
    CHECK(count_type(first, PM_PART) == 2);
    run_to(&s, t0 + 1000);
    CHECK(count_type(first, PM_PART) == 3);
    run_to(&s, t0 + 1500);
    CHECK(count_type(first, PM_PART) == 4);
    CHECK(s.state == STATION_RUNNING);
    run_to(&s, t0 + 1995);
    CHECK(s.state == STATION_RUNNING);
    run_to(&s, t0 + 2000);
    CHECK(s.state == STATION_IDLE && s.stats.parts_failed == 1 && s.stats.retries == 3);
    CHECK(count_type(first, PM_PART) == 4);

    /* all four sends are the same bytes */
    int seen = 0;
    for (int i = first; i < g_ntx; i++) {
        if (tx_msg(i).type == PM_PART) {
            CHECK(memcmp(g_tx[i].d, g_tx[first].d, ATR_PROD_FRAME_LEN) == 0);
            seen++;
        }
    }
    CHECK(seen == 4);
    run_to(&s, t0 + 4000);
    CHECK(count_type(first, PM_PART) == 4 && s.produced == 1);       /* stopped producing */
}

static void test_pause_resume_stop(void)
{
    station_t s;
    int first;
    uint32_t t;

    setup(&s, 0);
    run_to(&s, 1000);
    cmd(&s, 1, PM_CMD_START, 100, 10);
    run_to(&s, 1100);
    gw_send(&s, PM_PART_ACK, 1, PM_ACK_ACCEPTED, 0, 0);

    run_to(&s, 1150);
    first = cmd(&s, 2, PM_CMD_PAUSE, 0, 0);
    expect_accepted(first, 2, PM_DONE_PAUSED);
    CHECK(s.state == STATION_PAUSED);
    first = g_ntx;
    run_to(&s, 3000);
    CHECK(count_type(first, PM_PART) == 0 && s.produced == 1);       /* nothing new while paused */

    t = g_now;
    first = cmd(&s, 3, PM_CMD_RESUME, 0, 0);
    expect_accepted(first, 3, PM_DONE_RUNNING);
    first = g_ntx;
    run_to(&s, t + 95);
    CHECK(count_type(first, PM_PART) == 0);
    run_to(&s, t + 100);
    CHECK(count_type(first, PM_PART) == 1 && s.produced == 2);

    /* paused with a part in flight: its retry and its ACK are still handled */
    first = cmd(&s, 4, PM_CMD_PAUSE, 0, 0);
    expect_accepted(first, 4, PM_DONE_PAUSED);
    first = g_ntx;
    run_to(&s, g_now + 500);
    CHECK(count_type(first, PM_PART) == 1 && s.stats.retries == 1);
    gw_send(&s, PM_PART_ACK, 2, PM_ACK_ACCEPTED, 0, 0);
    CHECK(!s.pending && s.state == STATION_PAUSED);
    run_to(&s, g_now + 1000);
    CHECK(s.produced == 2);

    /* stop */
    first = cmd(&s, 5, PM_CMD_STOP, 0, 0);
    expect_accepted(first, 5, PM_DONE_STOPPED);
    CHECK(s.state == STATION_IDLE && !s.pending);
    gw_send(&s, PM_PART_ACK, 2, PM_ACK_ACCEPTED, 0, 0);
    CHECK(s.stats.stale_acks == 1);
    first = cmd(&s, 6, PM_CMD_STOP, 0, 0);                           /* nothing to stop */
    expect_rejected(first, 6);
    first = cmd(&s, 7, PM_CMD_RESUME, 0, 0);
    expect_rejected(first, 7);
}

static void test_stop_while_part_in_flight(void)
{
    station_t s;
    int first;

    setup(&s, 0);
    run_to(&s, 1000);
    cmd(&s, 1, PM_CMD_START, 100, 5);
    run_to(&s, 1100);
    CHECK(s.pending);
    first = cmd(&s, 2, PM_CMD_STOP, 0, 0);
    expect_accepted(first, 2, PM_DONE_STOPPED);
    CHECK(!s.pending);
    first = g_ntx;
    run_to(&s, 3000);
    CHECK(count_type(first, PM_PART) == 0);                          /* the abandoned part is not re-sent */
}

static void test_rejections(void)
{
    station_t s;
    int first;

    setup(&s, 0);
    run_to(&s, 1000);

    first = cmd(&s, 10, PM_CMD_START, 5, 3);                         /* cycle below 10 ms */
    expect_rejected(first, 10);
    first = cmd(&s, 11, PM_CMD_START, 60001, 3);
    expect_rejected(first, 11);
    first = cmd(&s, 12, PM_CMD_START, 100, 0);
    expect_rejected(first, 12);
    first = cmd(&s, 13, PM_CMD_START, 100, 10001);
    expect_rejected(first, 13);
    first = cmd(&s, 14, PM_CMD_PAUSE, 0, 0);                         /* nothing running */
    expect_rejected(first, 14);
    first = cmd(&s, 15, 9, 0, 0);                                    /* unknown command code */
    expect_rejected(first, 15);
    CHECK(s.state == STATION_IDLE);

    first = cmd(&s, 16, PM_CMD_START, 10, 10000);                    /* limits themselves are allowed */
    expect_accepted(first, 16, PM_DONE_RUNNING);
    first = cmd(&s, 17, PM_CMD_START, 100, 3);                       /* already running */
    expect_rejected(first, 17);
    first = cmd(&s, 18, PM_CMD_RESUME, 0, 0);                        /* not paused */
    expect_rejected(first, 18);
    CHECK(s.target == 10000 && s.cycle_ms == 10);                    /* the rejected Start changed nothing */

    first = cmd(&s, 10, PM_CMD_START, 5, 3);                         /* repeat of a rejected command */
    expect_rejected(first, 10);
    CHECK(s.stats.duplicate_cmds == 1);
    CHECK(s.stats.cmds_accepted == 1 && s.stats.cmds_rejected == 8);
}

static void test_command_history(void)
{
    station_t s;
    int first;

    setup(&s, 0);
    run_to(&s, 1000);
    for (uint32_t id = 1000; id < 1016; id++) {                      /* 16 different commands fill the memory */
        first = cmd(&s, id, PM_CMD_PAUSE, 0, 0);
        expect_rejected(first, id);
    }
    CHECK(s.stats.cmds_rejected == 16 && s.stats.duplicate_cmds == 0);
    cmd(&s, 1000, PM_CMD_PAUSE, 0, 0);                               /* still remembered */
    CHECK(s.stats.duplicate_cmds == 1);
    cmd(&s, 2000, PM_CMD_PAUSE, 0, 0);                               /* a 17th pushes out the oldest */
    CHECK(s.stats.cmds_rejected == 17);
    cmd(&s, 1000, PM_CMD_PAUSE, 0, 0);                               /* forgotten: treated as new */
    CHECK(s.stats.duplicate_cmds == 1 && s.stats.cmds_rejected == 18);
    cmd(&s, 1015, PM_CMD_PAUSE, 0, 0);                               /* recent: still a duplicate */
    CHECK(s.stats.duplicate_cmds == 2);
}

static void test_link_lost(void)
{
    station_t s;
    int first;
    uint32_t last;

    setup(&s, 0);
    run_to(&s, 1000);
    cmd(&s, 1, PM_CMD_START, 2000, 50);                              /* first part only at 3000 ms */
    run_to(&s, 1500);
    CHECK(s.state == STATION_RUNNING && s.link_up);

    g_gw_alive = 0;                                                  /* the gateway goes silent */
    last = s.last_rx_ms;
    run_to(&s, last + 2995);
    CHECK(s.state == STATION_RUNNING && s.link_up && s.pending);     /* a part is in flight when the link dies */
    run_to(&s, last + 3000);
    CHECK(s.state == STATION_IDLE && !s.link_up && s.stats.link_lost == 1 && !s.pending);

    first = g_ntx;
    run_to(&s, last + 6000);
    CHECK(count_type(first, PM_PART) == 0);
    CHECK(count_type(first, PM_HEARTBEAT) >= 2);                     /* still announces itself */
    CHECK(s.stats.link_lost == 1);                                   /* counted once */

    g_gw_alive = 1;                                                  /* the gateway is back */
    gw_send(&s, PM_HEARTBEAT, 0, 0, 0, 0);
    CHECK(s.link_up);
    first = cmd(&s, 2, PM_CMD_START, 100, 3);
    expect_accepted(first, 2, PM_DONE_RUNNING);
}

static void test_bad_input(void)
{
    station_t s;
    uint8_t payload[ATR_PROD_PAYLOAD_LEN];
    uint8_t frame[ATR_MAX_FRAME_LEN];
    uint8_t big[64];
    prod_msg_t m = { .type = PM_CMD, .id = 1, .arg = PM_CMD_START, .value_a = 100, .value_b = 3 };
    size_t n;

    setup(&s, 0);
    run_to(&s, 100);
    prod_encode(payload, &m);

    n = atr_build(frame, sizeof(frame), 0, 0, payload, 16);
    frame[10] ^= 0x10;                                               /* bit flipped after the CRC */
    gw_raw(&s, frame, n);
    CHECK(s.stats.crc_errors == 1 && s.state == STATION_IDLE);

    gw_raw(&s, frame, 9);
    CHECK(s.stats.short_frames == 1);

    /* payload_len says 17 but 16 bytes follow, CRC correct */
    n = atr_build(frame, sizeof(frame), 0, 0, payload, 16);
    frame[7] = 17;
    {
        uint16_t crc = crc16_ccitt(frame, n - 2, CRC16_INIT);
        frame[n - 2] = (uint8_t)(crc >> 8);
        frame[n - 1] = (uint8_t)crc;
    }
    gw_raw(&s, frame, n);
    CHECK(s.stats.length_errors == 1);

    n = atr_build(frame, sizeof(frame), 7, 0, payload, 16);          /* some other test_id */
    gw_raw(&s, frame, n);
    CHECK(s.stats.wrong_test_id == 1 && s.stats.cmds_accepted == 0);

    memset(big, 0x55, sizeof(big));
    n = atr_build(frame, sizeof(frame), 0, 0, big, 64);              /* production frames carry 16 bytes */
    gw_raw(&s, frame, n);
    CHECK(s.stats.bad_payload_len == 1);

    gw_send(&s, PM_PART, 1, 0, 0, 0);                                /* messages the gateway must not send */
    gw_send(&s, PM_CMD_ACK, 1, 1, 0, 0);
    gw_send(&s, PM_CMD_DONE, 1, 1, 0, 0);
    gw_send(&s, 0x77, 1, 0, 0, 0);
    CHECK(s.stats.unexpected_type == 4 && g_ntx == 1);               /* only the first heartbeat went out */

    station_note_slip_drop(&s);
    CHECK(s.stats.slip_drops == 1);

    gw_send(&s, PM_PART_ACK, 9, 7, 0, 0);                            /* ACK for a part that is not in flight */
    CHECK(s.stats.stale_acks == 1);

    n = atr_build(frame, sizeof(frame), 0, 0, payload, 16);          /* after all that, a good Start still works */
    gw_raw(&s, frame, n);
    CHECK(s.state == STATION_RUNNING);
}

static void test_unknown_ack_arg(void)
{
    station_t s;

    setup(&s, 0);
    run_to(&s, 1000);
    cmd(&s, 1, PM_CMD_START, 100, 3);
    run_to(&s, 1100);
    gw_send(&s, PM_PART_ACK, 1, 9, 0, 0);
    CHECK(s.stats.bad_acks == 1 && s.pending);                       /* still waiting for a real answer */
}

static void test_clock_wrap(void)
{
    station_t s;
    uint32_t start = 0xFFFFFF00u;                                    /* 256 ms before the 32-bit clock wraps */
    int first;

    setup(&s, start);
    run_to(&s, start + 1000);                                        /* g_now wraps past zero here */
    CHECK(g_now < 1000);
    first = cmd(&s, 1, PM_CMD_START, 100, 50);
    expect_accepted(first, 1, PM_DONE_RUNNING);
    first = g_ntx;
    run_to(&s, g_now + 1050);
    CHECK(count_type(first, PM_PART) == 2);                          /* the part at +100 and its first retry at +600 */
    CHECK(s.stats.retries == 1 && s.state == STATION_RUNNING);       /* the second retry would be at +1100 */
    CHECK(count_type(first, PM_HEARTBEAT) >= 1);
    CHECK(s.link_up);
}

int main(void)
{
    test_codec();
    test_slip_roundtrip();
    test_heartbeat();
    test_normal_batch();
    test_duplicate_and_restart();
    test_queue_full();
    test_retries();
    test_pause_resume_stop();
    test_stop_while_part_in_flight();
    test_rejections();
    test_command_history();
    test_link_lost();
    test_bad_input();
    test_unknown_ack_arg();
    test_clock_wrap();

    printf("%d checks, %d failed\n", g_checks, g_fail);
    return g_fail == 0 ? 0 : 1;
}
