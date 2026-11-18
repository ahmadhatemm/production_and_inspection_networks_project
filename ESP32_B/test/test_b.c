/* PC tests for the code of ESP32 B. No board needed.
 *
 *   make -f test.mk        builds with warnings as errors and the address/UB sanitizers, then runs
 *
 * A fake gateway feeds messages to the station and a fake clock drives it in 5 ms steps. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../main/jsonmini.h"
#include "../main/station.h"
#include "../main/tcpframe.h"

static int g_fail;
static int g_checks;

#define CHECK(cond) do { \
    g_checks++; \
    if (!(cond)) { g_fail++; printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); } \
} while (0)

#define CHECK_STR(got, want) do { \
    g_checks++; \
    if (strcmp((got), (want)) != 0) { g_fail++; \
        printf("FAIL %s:%d\n   got : %s\n   want: %s\n", __FILE__, __LINE__, (got), (want)); } \
} while (0)

/* ---- fake wire, clock, log -------------------------------------------- */

#define MAX_TX 600
static char     g_tx[MAX_TX][STATION_MSG_MAX + 1];
static int      g_ntx;
static int      g_nlog_tx, g_nlog_rx, g_nlog_evt;
static char     g_last_tx_log[STATION_MSG_MAX + 1];
static uint32_t g_now;
static int      g_gw_alive;             /* the fake gateway sends a heartbeat every second */
static uint32_t g_next_gw_hb;

static void capture(const uint8_t *body, size_t n, void *ctx)
{
    (void)ctx;
    if (g_ntx < MAX_TX && n <= STATION_MSG_MAX) {
        memcpy(g_tx[g_ntx], body, n);
        g_tx[g_ntx][n] = '\0';
        g_ntx++;
    }
}

static void logger(const char *tag, const char *text, size_t len, void *ctx)
{
    (void)ctx;
    if (strcmp(tag, "TX") == 0) {
        g_nlog_tx++;
        if (len <= STATION_MSG_MAX) {
            memcpy(g_last_tx_log, text, len);
            g_last_tx_log[len] = '\0';
        }
    } else if (strcmp(tag, "RX") == 0) {
        g_nlog_rx++;
    } else {
        g_nlog_evt++;
    }
}

static void rx(station_t *s, const char *json)
{
    station_on_message(s, (const uint8_t *)json, strlen(json), g_now);
}

static void run_to(station_t *s, uint32_t t)
{
    while ((int32_t)(g_now - t) < 0) {                  /* signed compare: survives the clock wrap */
        g_now += 5;
        if (g_gw_alive && (int32_t)(g_now - g_next_gw_hb) >= 0) {
            rx(s, "{\"type\":\"HEARTBEAT\",\"seq\":1}");
            g_next_gw_hb = g_now + 1000;
        }
        station_tick(s, g_now);
    }
}

static void setup(station_t *s, uint32_t start, int connect)
{
    g_ntx = 0;
    g_nlog_tx = g_nlog_rx = g_nlog_evt = 0;
    g_now = start;
    g_gw_alive = 1;
    g_next_gw_hb = start + 1000;
    station_init(s, capture, logger, NULL);
    if (connect) {
        station_on_connect(s, g_now);
    }
}

static int count_containing(int from, const char *needle)
{
    int c = 0;
    for (int i = from; i < g_ntx; i++) {
        if (strstr(g_tx[i], needle) != NULL) {
            c++;
        }
    }
    return c;
}

/* index of the first TX at or after from that contains needle, or -1 */
static int find_tx(int from, const char *needle)
{
    for (int i = from; i < g_ntx; i++) {
        if (strstr(g_tx[i], needle) != NULL) {
            return i;
        }
    }
    return -1;
}

static const char *CONFIG_18 = "{\"type\":\"CONFIG\",\"cmd_id\":18,\"N\":5,\"K\":2,\"inspect_time_ms\":500}";

static void configure(station_t *s)
{
    rx(s, CONFIG_18);
    rx(s, "{\"type\":\"ACK\",\"ref\":\"CONFIG_DONE\",\"cmd_id\":18,\"status\":\"accepted\"}");
}

/* ---- JSON reader ------------------------------------------------------ */

static void test_json(void)
{
    json_obj_t o;
    const char *str;
    uint32_t u;
    const char *m1 = "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}";

    CHECK(json_parse_object(m1, strlen(m1), &o));
    CHECK(json_get_str(&o, "type", &str) == JF_OK && strcmp(str, "JOB_REQ") == 0);
    CHECK(json_get_u32(&o, "job_id", &u) == JF_OK && u == 28);
    CHECK(json_get_u32(&o, "part_id", &u) == JF_OK && u == 8);
    CHECK(json_get_u32(&o, "nope", &u) == JF_MISSING);
    CHECK(json_get_u32(&o, "type", &u) == JF_WRONG_TYPE);
    CHECK(json_get_str(&o, "job_id", &str) == JF_WRONG_TYPE);
    CHECK(strlen(m1) == 42);                                    /* the contract's example message */

    /* whitespace, empty object, key order */
    {
        const char *w = " \t\r\n{ \"a\" : 1 , \"b\" :\"x\" }\n ";
        CHECK(json_parse_object(w, strlen(w), &o) && o.n == 2);
    }
    CHECK(json_parse_object("{}", 2, &o) && o.n == 0);

    /* integers */
    {
        const char *t = "{\"a\":0,\"b\":4294967295,\"c\":4294967296,\"d\":-1,\"e\":1.5,\"f\":1e3,\"g\":-0,"
                        "\"h\":99999999999999999999999,\"i\":\"7\",\"j\":true,\"k\":null,\"l\":-99999999999999999999}";
        CHECK(json_parse_object(t, strlen(t), &o));
        CHECK(json_get_u32(&o, "a", &u) == JF_OK && u == 0);
        CHECK(json_get_u32(&o, "b", &u) == JF_OK && u == 4294967295u);
        CHECK(json_get_u32(&o, "c", &u) == JF_OUT_OF_RANGE);
        CHECK(json_get_u32(&o, "d", &u) == JF_OUT_OF_RANGE);
        CHECK(json_get_u32(&o, "e", &u) == JF_WRONG_TYPE);      /* 1.5 is not an integer */
        CHECK(json_get_u32(&o, "f", &u) == JF_WRONG_TYPE);      /* neither is 1e3 */
        CHECK(json_get_u32(&o, "g", &u) == JF_OK && u == 0);
        CHECK(json_get_u32(&o, "h", &u) == JF_OUT_OF_RANGE);    /* beyond int64 */
        CHECK(json_get_u32(&o, "i", &u) == JF_WRONG_TYPE);      /* a string is not a number */
        CHECK(json_get_u32(&o, "j", &u) == JF_WRONG_TYPE);
        CHECK(json_get_u32(&o, "k", &u) == JF_WRONG_TYPE);
        CHECK(json_get_u32(&o, "l", &u) == JF_OUT_OF_RANGE);
    }

    /* strings and escapes */
    {
        const char *t = "{\"a\":\"q\\\"b\\\\c\\/d\\n\\u0041\\u00e9\"}";
        CHECK(json_parse_object(t, strlen(t), &o));
        CHECK(json_get_str(&o, "a", &str) == JF_OK && strcmp(str, "q\"b\\c/d\nA?") == 0);
        CHECK(!json_parse_object("{\"a\":\"\\u0000\"}", 14, &o));   /* NUL would end the string early */
        CHECK(!json_parse_object("{\"a\":\"\\x\"}", 10, &o));
        CHECK(!json_parse_object("{\"a\":\"\\u12G4\"}", 14, &o));
        CHECK(!json_parse_object("{\"a\":\"ab\ncd\"}", 13, &o));    /* raw control character */
        CHECK(!json_parse_object("{\"a\":\"abc}", 10, &o));         /* no closing quote */
    }

    /* long strings are cut, and then count as the wrong type */
    {
        char t[200];
        snprintf(t, sizeof(t), "{\"type\":\"%0100d\"}", 7);
        CHECK(json_parse_object(t, strlen(t), &o));
        CHECK(json_get_str(&o, "type", &str) == JF_WRONG_TYPE);
    }

    /* nested values are skipped; unknown fields are ignored */
    {
        const char *t = "{\"x\":{\"a\":[1,2,{\"b\":null}],\"c\":\"}\"},\"y\":[],\"type\":\"HEARTBEAT\",\"z\":{}}";
        CHECK(json_parse_object(t, strlen(t), &o));
        CHECK(json_get_str(&o, "type", &str) == JF_OK && strcmp(str, "HEARTBEAT") == 0);
        CHECK(json_get_u32(&o, "x", &u) == JF_WRONG_TYPE);
    }

    /* things that must be refused */
    CHECK(!json_parse_object("", 0, &o));
    CHECK(!json_parse_object("[]", 2, &o));
    CHECK(!json_parse_object("\"x\"", 3, &o));
    CHECK(!json_parse_object("{", 1, &o));
    CHECK(!json_parse_object("{\"a\":1", 6, &o));
    CHECK(!json_parse_object("{\"a\":1,}", 8, &o));             /* trailing comma */
    CHECK(!json_parse_object("{\"a\" 1}", 7, &o));
    CHECK(!json_parse_object("{a:1}", 5, &o));
    CHECK(!json_parse_object("{\"a\":1}x", 8, &o));             /* garbage after the object */
    CHECK(!json_parse_object("{\"a\":1}{\"b\":2}", 15, &o));
    CHECK(!json_parse_object("{\"a\":01}", 8, &o));             /* leading zero */
    CHECK(!json_parse_object("{\"a\":-}", 7, &o));
    CHECK(!json_parse_object("{\"a\":1.}", 8, &o));
    CHECK(!json_parse_object("{\"a\":1e}", 8, &o));
    CHECK(!json_parse_object("{\"a\":tru}", 9, &o));
    CHECK(!json_parse_object("{\"a\":1,\"a\":2}", 13, &o));     /* duplicate key */
    CHECK(!json_parse_object("{\"a\":\"x\"", 8, &o));

    /* a key longer than any key in use is ignored, not an error; a 17th field is an error */
    {
        const char *t = "{\"this_key_is_far_too_long_to_matter\":1,\"job_id\":5}";
        CHECK(json_parse_object(t, strlen(t), &o) && o.n == 1);
        CHECK(json_get_u32(&o, "job_id", &u) == JF_OK && u == 5);
    }
    {
        char t[400] = "{";
        for (int i = 0; i < 16; i++) {
            char f[48];
            snprintf(f, sizeof(f), "%s\"k%d\":%d", i ? "," : "", i, i);
            strcat(t, f);
        }
        strcat(t, "}");
        CHECK(json_parse_object(t, strlen(t), &o) && o.n == 16);
        t[strlen(t) - 1] = '\0';
        strcat(t, ",\"k16\":16}");
        CHECK(!json_parse_object(t, strlen(t), &o));
    }

    /* nesting deeper than the skip limit is refused rather than recursing without end */
    {
        char t[200];
        int n = 0;
        n += snprintf(t + n, sizeof(t) - (size_t)n, "{\"a\":");
        for (int i = 0; i < 20; i++) {
            t[n++] = '[';
        }
        for (int i = 0; i < 20; i++) {
            t[n++] = ']';
        }
        t[n++] = '}';
        CHECK(!json_parse_object(t, (size_t)n, &o));
    }
}

/* ---- length-prefix framing -------------------------------------------- */

static void test_tcpframe(void)
{
    uint8_t out[TCPFRAME_HDR_LEN + TCPFRAME_MAX_BODY];
    uint8_t buf[TCPFRAME_MAX_BODY];
    tcpframe_decoder_t d;
    const char *m = "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}";
    size_t n, used, bl;
    tcpframe_result_t r;

    /* the contract's example: a 42-byte message is sent as 00 00 00 2A + the 42 bytes */
    n = tcpframe_encode((const uint8_t *)m, strlen(m), out, sizeof(out));
    CHECK(n == 46 && out[0] == 0 && out[1] == 0 && out[2] == 0 && out[3] == 0x2A);
    CHECK(memcmp(&out[4], m, 42) == 0);

    CHECK(tcpframe_encode((const uint8_t *)m, strlen(m), out, 45) == 0);        /* buffer too small */
    CHECK(tcpframe_encode((const uint8_t *)m, 1025, out, sizeof(out)) == 0);    /* body too long    */
    memset(buf, 'x', sizeof(buf));
    CHECK(tcpframe_encode(buf, 1024, out, sizeof(out)) == 1028);
    CHECK(out[0] == 0 && out[1] == 0 && out[2] == 4 && out[3] == 0);
    CHECK(tcpframe_encode(buf, 0, out, sizeof(out)) == 4);

    /* byte by byte */
    tcpframe_decoder_init(&d, buf, sizeof(buf));
    n = tcpframe_encode((const uint8_t *)m, strlen(m), out, sizeof(out));
    for (size_t i = 0; i < n; i++) {
        r = tcpframe_feed(&d, &out[i], 1, &used, &bl);
        CHECK(used == 1);
        if (i + 1 < n) {
            CHECK(r == TCPFRAME_NEED_MORE);
        } else {
            CHECK(r == TCPFRAME_MESSAGE && bl == 42 && memcmp(buf, m, 42) == 0);
        }
    }

    /* three messages in one chunk, cut at every possible place */
    {
        uint8_t chunk[200];
        size_t total = 0;
        const char *msgs[3] = { "{\"a\":1}", "{\"type\":\"HEARTBEAT\",\"seq\":5}", "{}" };

        for (int i = 0; i < 3; i++) {
            total += tcpframe_encode((const uint8_t *)msgs[i], strlen(msgs[i]), &chunk[total], sizeof(chunk) - total);
        }
        for (size_t cut = 0; cut <= total; cut++) {
            int got = 0;
            const uint8_t *parts[2] = { chunk, chunk + cut };
            size_t lens[2] = { cut, total - cut };

            tcpframe_decoder_init(&d, buf, sizeof(buf));
            for (int p = 0; p < 2; p++) {
                const uint8_t *q = parts[p];
                size_t left = lens[p];

                while (left > 0) {
                    r = tcpframe_feed(&d, q, left, &used, &bl);
                    q += used;
                    left -= used;
                    if (r == TCPFRAME_MESSAGE) {
                        CHECK(bl == strlen(msgs[got]) && memcmp(buf, msgs[got], bl) == 0);
                        got++;
                    }
                    CHECK(r != TCPFRAME_ERROR);
                }
            }
            CHECK(got == 3);
        }
    }

    /* an empty body is a message of length 0 */
    {
        const uint8_t z[4] = { 0, 0, 0, 0 };
        tcpframe_decoder_init(&d, buf, sizeof(buf));
        r = tcpframe_feed(&d, z, 4, &used, &bl);
        CHECK(r == TCPFRAME_MESSAGE && bl == 0 && used == 4);
    }

    /* 1024 is allowed, 1025 is not, and a failed decoder stays failed until reset */
    {
        uint8_t big[TCPFRAME_HDR_LEN + 1024];
        const uint8_t over[4] = { 0, 0, 4, 1 };
        const uint8_t huge[4] = { 0xFF, 0xFF, 0xFF, 0xFF };

        n = tcpframe_encode(buf, 1024, big, sizeof(big));
        tcpframe_decoder_init(&d, buf, sizeof(buf));
        r = tcpframe_feed(&d, big, n, &used, &bl);
        CHECK(r == TCPFRAME_MESSAGE && bl == 1024 && used == n);

        r = tcpframe_feed(&d, over, 4, &used, &bl);
        CHECK(r == TCPFRAME_ERROR);
        r = tcpframe_feed(&d, big, n, &used, &bl);
        CHECK(r == TCPFRAME_ERROR && used == 0);
        tcpframe_decoder_reset(&d);
        r = tcpframe_feed(&d, big, n, &used, &bl);
        CHECK(r == TCPFRAME_MESSAGE);
        r = tcpframe_feed(&d, huge, 4, &used, &bl);
        CHECK(r == TCPFRAME_ERROR);
    }

    /* a half message is forgotten by reset (a new connection starts clean) */
    {
        tcpframe_decoder_init(&d, buf, sizeof(buf));
        r = tcpframe_feed(&d, out, 10, &used, &bl);
        CHECK(r == TCPFRAME_NEED_MORE);
        tcpframe_decoder_reset(&d);
        r = tcpframe_feed(&d, out, 46, &used, &bl);
        CHECK(r == TCPFRAME_MESSAGE && bl == 42);
    }
}

/* ---- the inspection rule ---------------------------------------------- */

static void test_verdict(void)
{
    CHECK(station_verdict_fail(8, 5, 2));                       /* contract example: (8+2) mod 5 = 0 -> FAIL */
    CHECK(!station_verdict_fail(9, 5, 2));
    CHECK(!station_verdict_fail(7, 5, 2));
    CHECK(station_verdict_fail(3, 5, 2));
    CHECK(station_verdict_fail(1, 1, 0));                       /* N = 1: everything fails */
    CHECK(station_verdict_fail(123456, 1, 99));
    CHECK(station_verdict_fail(5, 5, 0));                       /* K = 0 */
    CHECK(!station_verdict_fail(4, 5, 0));
    CHECK(station_verdict_fail(0, 5, 0));
    /* the sum passes 2^32: 4294967295 + 4294967295 = 8589934590 = 6 * 1431655765, so FAIL.
     * A 32-bit sum would wrap to 4294967294, and 4294967294 mod 6 = 2 would give PASS. */
    CHECK(station_verdict_fail(4294967295u, 6, 4294967295u));
    CHECK(!station_verdict_fail(4294967295u, 7, 4294967295u));  /* 8589934590 mod 7 = 6 */
    CHECK(station_verdict_fail(4294967295u, 4294967295u, 0));
    /* every part from 1 to 200 against a plain loop */
    for (uint32_t n = 1; n <= 9; n++) {
        for (uint32_t k = 0; k <= 9; k++) {
            for (uint32_t p = 1; p <= 200; p++) {
                CHECK(station_verdict_fail(p, n, k) == (((p + k) % n) == 0));
            }
        }
    }
}

/* ---- the station ------------------------------------------------------ */

static void test_connect_and_heartbeat(void)
{
    station_t s;

    setup(&s, 1000, 0);
    CHECK(station_state(&s) == STATION_DISCONNECTED);
    station_tick(&s, g_now);
    CHECK(g_ntx == 0);                                          /* nothing is sent without a connection */

    station_on_connect(&s, g_now);
    CHECK(g_ntx == 1);
    CHECK_STR(g_tx[0], "{\"type\":\"HELLO\",\"device\":\"esp32_b\",\"fw\":\"1.0.0\"}");
    CHECK(station_state(&s) == STATION_IDLE);

    g_gw_alive = 0;
    station_tick(&s, g_now);
    CHECK(g_ntx == 2);
    CHECK_STR(g_tx[1], "{\"type\":\"HEARTBEAT\",\"seq\":0}");
    run_to(&s, 1000 + 995);
    CHECK(g_ntx == 2);
    run_to(&s, 1000 + 1000);
    CHECK(g_ntx == 3);
    CHECK_STR(g_tx[2], "{\"type\":\"HEARTBEAT\",\"seq\":1}");
    run_to(&s, 1000 + 2000);
    CHECK(g_ntx == 4);
    CHECK_STR(g_tx[3], "{\"type\":\"HEARTBEAT\",\"seq\":2}");
    CHECK(g_nlog_tx == 4);                                      /* every sent message is logged */
    CHECK_STR(g_last_tx_log, g_tx[3]);
}

static void test_config(void)
{
    station_t s;
    int first;

    setup(&s, 0, 1);
    run_to(&s, 100);
    first = g_ntx;
    rx(&s, CONFIG_18);
    CHECK(g_ntx == first + 2);
    CHECK_STR(g_tx[first], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":18,\"status\":\"accepted\"}");
    CHECK_STR(g_tx[first + 1], "{\"type\":\"CONFIG_DONE\",\"cmd_id\":18,\"N\":5,\"K\":2,\"inspect_time_ms\":500}");
    CHECK(s.configured && s.n == 5 && s.k == 2 && s.inspect_ms == 500);
    CHECK(s.stats.cmds_accepted == 1);

    /* the gateway's ACK of CONFIG_DONE ends the waiting */
    rx(&s, "{\"type\":\"ACK\",\"ref\":\"CONFIG_DONE\",\"cmd_id\":18,\"status\":\"accepted\"}");
    CHECK(s.stats.cfgdone_acked == 1);
    first = g_ntx;
    run_to(&s, g_now + 3000);
    CHECK(count_containing(first, "CONFIG_DONE") == 0);

    /* the same ACK again matches nothing */
    rx(&s, "{\"type\":\"ACK\",\"ref\":\"CONFIG_DONE\",\"cmd_id\":18,\"status\":\"accepted\"}");
    CHECK(s.stats.stale_acks == 1);
}

static void test_config_invalid(void)
{
    station_t s;
    int first;

    setup(&s, 0, 1);
    first = g_ntx;
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":1,\"K\":2,\"inspect_time_ms\":500}");                 /* N missing */
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":1,\"status\":\"rejected\",\"reason\":\"malformed\"}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":2,\"N\":\"5\",\"K\":2,\"inspect_time_ms\":500}");     /* N a string */
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":2,\"status\":\"rejected\",\"reason\":\"malformed\"}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":3,\"N\":5.5,\"K\":2,\"inspect_time_ms\":500}");       /* N not an integer */
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":3,\"status\":\"rejected\",\"reason\":\"malformed\"}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":4,\"N\":5,\"inspect_time_ms\":500}");                 /* K missing */
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":4,\"status\":\"rejected\",\"reason\":\"malformed\"}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":5,\"N\":5,\"K\":2}");                                 /* time missing */
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":5,\"status\":\"rejected\",\"reason\":\"malformed\"}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":6,\"N\":0,\"K\":2,\"inspect_time_ms\":500}");         /* N below 1 */
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":6,\"status\":\"rejected\",\"reason\":\"out_of_range\"}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":7,\"N\":-1,\"K\":2,\"inspect_time_ms\":500}");
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":7,\"status\":\"rejected\",\"reason\":\"out_of_range\"}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":8,\"N\":5,\"K\":-3,\"inspect_time_ms\":500}");        /* K below 0 */
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":8,\"status\":\"rejected\",\"reason\":\"out_of_range\"}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":9,\"N\":5,\"K\":2,\"inspect_time_ms\":9}");           /* time below 10 */
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":9,\"status\":\"rejected\",\"reason\":\"out_of_range\"}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":10,\"N\":5,\"K\":2,\"inspect_time_ms\":60001}");     /* time above 60000 */
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":10,\"status\":\"rejected\",\"reason\":\"out_of_range\"}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":11,\"N\":4294967296,\"K\":2,\"inspect_time_ms\":500}");  /* N above 2^32-1 */
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":11,\"status\":\"rejected\",\"reason\":\"out_of_range\"}");

    CHECK(!s.configured);                                       /* none of them was applied */
    CHECK(s.stats.cmds_rejected == 11 && s.stats.cmds_accepted == 0);
    CHECK(count_containing(first, "CONFIG_DONE") == 0);         /* a rejected command is never "done" */

    /* no usable cmd_id: nothing to answer */
    first = g_ntx;
    rx(&s, "{\"type\":\"CONFIG\",\"N\":5,\"K\":2,\"inspect_time_ms\":500}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":-4,\"N\":5,\"K\":2,\"inspect_time_ms\":500}");
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":\"x\",\"N\":5,\"K\":2,\"inspect_time_ms\":500}");
    CHECK(g_ntx == first && s.stats.bad_fields == 3);

    /* limits themselves are allowed: K = 0, the smallest and largest time, N = 1, N = 2^32-1, K = 2^32-1 */
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":20,\"N\":1,\"K\":0,\"inspect_time_ms\":10}");
    CHECK(s.configured && s.n == 1 && s.k == 0 && s.inspect_ms == 10);
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":21,\"N\":4294967295,\"K\":4294967295,\"inspect_time_ms\":60000}");
    CHECK(s.n == 4294967295u && s.k == 4294967295u && s.inspect_ms == 60000);
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"CONFIG_DONE\",\"cmd_id\":21,\"N\":4294967295,\"K\":4294967295,\"inspect_time_ms\":60000}");

    /* an invalid command after a valid one leaves the valid parameters in place */
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":22,\"N\":0,\"K\":2,\"inspect_time_ms\":500}");
    CHECK(s.n == 4294967295u && s.k == 4294967295u && s.inspect_ms == 60000);
}

static void test_job_before_config(void)
{
    station_t s;
    int first;

    setup(&s, 0, 1);
    first = g_ntx;
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":1,\"part_id\":1}");
    CHECK(g_ntx == first + 1);
    CHECK_STR(g_tx[first], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":1,\"status\":\"rejected\",\"reason\":\"not_configured\"}");
    CHECK(station_state(&s) == STATION_IDLE && !s.busy);
    run_to(&s, 2000);
    CHECK(count_containing(first, "JOB_RESULT") == 0);          /* no verdict from default parameters */
    CHECK(s.stats.jobs_rejected == 1 && s.stats.jobs_accepted == 0);

    /* the rejected job was not remembered: after CONFIG the same job_id is accepted */
    configure(&s);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":1,\"part_id\":1}");
    CHECK(s.busy && s.stats.jobs_accepted == 1);
}

static void test_contract_example(void)
{
    station_t s;
    int first;

    setup(&s, 0, 1);
    configure(&s);
    run_to(&s, 100);

    /* the example of the contract: part 8, N = 5, K = 2, so (8 + 2) mod 5 = 0 and the part fails */
    first = g_ntx;
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    CHECK(g_ntx == first + 1);
    CHECK_STR(g_tx[first], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":28,\"status\":\"accepted\"}");
    CHECK(station_state(&s) == STATION_INSPECTING);

    run_to(&s, 100 + 495);
    CHECK(count_containing(first, "JOB_RESULT") == 0);          /* not before inspect_time_ms */
    run_to(&s, 100 + 500);
    CHECK(g_ntx == first + 2);
    CHECK_STR(g_tx[first + 1], "{\"type\":\"JOB_RESULT\",\"job_id\":28,\"part_id\":8,\"verdict\":\"FAIL\",\"N\":5,\"K\":2}");
    CHECK(station_state(&s) == STATION_IDLE);
    CHECK(s.stats.verdict_fail == 1 && s.stats.verdict_pass == 0);

    rx(&s, "{\"type\":\"ACK\",\"ref\":\"JOB_RESULT\",\"job_id\":28,\"status\":\"accepted\"}");
    CHECK(s.stats.results_acked == 1);
    first = g_ntx;
    run_to(&s, g_now + 3000);
    CHECK(count_containing(first, "JOB_RESULT") == 0);          /* acknowledged: no more retries */

    /* part 9: (9 + 2) mod 5 = 1, so PASS */
    first = g_ntx;
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":29,\"part_id\":9}");
    run_to(&s, g_now + 500);
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"JOB_RESULT\",\"job_id\":29,\"part_id\":9,\"verdict\":\"PASS\",\"N\":5,\"K\":2}");
    CHECK(s.stats.verdict_pass == 1 && s.stats.jobs_completed == 2);
}

static void test_busy_and_snapshot(void)
{
    station_t s;
    int first;

    setup(&s, 0, 1);
    configure(&s);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":1,\"part_id\":8}");
    run_to(&s, 200);
    first = g_ntx;
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":2,\"part_id\":9}");                /* a second job while busy */
    CHECK_STR(g_tx[first], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":2,\"status\":\"rejected\",\"reason\":\"busy\"}");
    CHECK(s.stats.jobs_rejected == 1 && s.stats.jobs_accepted == 1);

    /* a new CONFIG while a job is in progress applies to later jobs only */
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":19,\"N\":3,\"K\":0,\"inspect_time_ms\":50}");
    CHECK(s.n == 3 && s.k == 0 && s.inspect_ms == 50);
    first = g_ntx;
    run_to(&s, 500);
    CHECK_STR(g_tx[find_tx(first, "JOB_RESULT")],
              "{\"type\":\"JOB_RESULT\",\"job_id\":1,\"part_id\":8,\"verdict\":\"FAIL\",\"N\":5,\"K\":2}");  /* old N and K */

    /* the rejected job_id 2 can be offered again now, and uses the new parameters: (9 + 0) mod 3 = 0 -> FAIL */
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":2,\"part_id\":9}");
    CHECK(s.busy);
    first = g_ntx;
    run_to(&s, g_now + 50);
    CHECK_STR(g_tx[find_tx(first, "JOB_RESULT")],
              "{\"type\":\"JOB_RESULT\",\"job_id\":2,\"part_id\":9,\"verdict\":\"FAIL\",\"N\":3,\"K\":0}");
}

static void test_duplicate_job(void)
{
    station_t s;
    int first, i1;
    char saved[STATION_MSG_MAX + 1];

    setup(&s, 0, 1);
    configure(&s);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");

    /* a repeat while the job is running: ACK again, no second inspection */
    run_to(&s, 100);
    first = g_ntx;
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    CHECK(g_ntx == first + 1);
    CHECK_STR(g_tx[first], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":28,\"status\":\"accepted\"}");
    CHECK(s.stats.jobs_accepted == 1 && s.stats.job_duplicates == 1);
    run_to(&s, 600);
    first = g_ntx;
    CHECK(s.stats.jobs_completed == 1);
    i1 = find_tx(0, "JOB_RESULT");
    CHECK(i1 >= 0);
    strcpy(saved, g_tx[i1]);

    /* a repeat after the result: ACK again and the same result again, bytes identical, still one inspection */
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    CHECK(g_ntx == first + 1);                                  /* the result is already waiting for its ACK, so only the ACK is new */
    CHECK_STR(g_tx[first], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":28,\"status\":\"accepted\"}");
    rx(&s, "{\"type\":\"ACK\",\"ref\":\"JOB_RESULT\",\"job_id\":28,\"status\":\"accepted\"}");
    first = g_ntx;
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    CHECK(g_ntx == first + 2);                                  /* ACK + the result again, with its ACK wait restarted */
    CHECK_STR(g_tx[first + 1], saved);
    CHECK(s.stats.jobs_accepted == 1 && s.stats.jobs_completed == 1 && s.stats.job_duplicates == 3);

    /* the same job_id with another part_id is a conflict, not a repeat */
    first = g_ntx;
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":9}");
    CHECK_STR(g_tx[first], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":28,\"status\":\"rejected\",\"reason\":\"id_conflict\"}");
    CHECK(s.stats.jobs_accepted == 1);
}

static void test_result_retries(void)
{
    station_t s;
    int first, idx[4], k = 0;
    uint32_t t0;

    setup(&s, 0, 1);
    configure(&s);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    first = g_ntx;
    run_to(&s, 500);
    t0 = g_now;
    CHECK(count_containing(first, "JOB_RESULT") == 1);
    run_to(&s, t0 + 495);
    CHECK(count_containing(first, "JOB_RESULT") == 1);
    run_to(&s, t0 + 500);
    CHECK(count_containing(first, "JOB_RESULT") == 2);
    run_to(&s, t0 + 1000);
    CHECK(count_containing(first, "JOB_RESULT") == 3);
    run_to(&s, t0 + 1500);
    CHECK(count_containing(first, "JOB_RESULT") == 4);
    CHECK(s.stats.results_failed == 0);
    run_to(&s, t0 + 1995);
    CHECK(s.stats.results_failed == 0);
    run_to(&s, t0 + 2000);
    CHECK(s.stats.results_failed == 1 && s.stats.result_retries == 3);
    run_to(&s, t0 + 5000);
    CHECK(count_containing(first, "JOB_RESULT") == 4);          /* four tries in all, never a fifth */

    for (int i = first; i < g_ntx; i++) {
        if (strstr(g_tx[i], "JOB_RESULT") != NULL && k < 4) {
            idx[k++] = i;
        }
    }
    CHECK(k == 4);
    for (int i = 1; i < k; i++) {
        CHECK_STR(g_tx[idx[i]], g_tx[idx[0]]);                  /* same bytes every time */
    }

    /* the station is not stuck: the next job works */
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":29,\"part_id\":9}");
    CHECK(s.busy);
}

static void test_result_rejected_by_gateway(void)
{
    station_t s;
    int first;

    setup(&s, 0, 1);
    configure(&s);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    run_to(&s, 500);
    rx(&s, "{\"type\":\"ACK\",\"ref\":\"JOB_RESULT\",\"job_id\":28,\"status\":\"rejected\",\"reason\":\"stale\"}");
    CHECK(s.stats.results_rejected == 1 && s.stats.results_acked == 0);
    first = g_ntx;
    run_to(&s, g_now + 3000);
    CHECK(count_containing(first, "JOB_RESULT") == 0);          /* a rejection ends the retries too */
}

static void test_config_done_retries_and_duplicate(void)
{
    station_t s;
    int first;

    setup(&s, 0, 1);
    first = g_ntx;
    rx(&s, CONFIG_18);
    run_to(&s, 500);
    CHECK(count_containing(first, "CONFIG_DONE") == 2);
    run_to(&s, 1500);
    CHECK(count_containing(first, "CONFIG_DONE") == 4);
    run_to(&s, 2000);
    CHECK(s.stats.cfgdone_failed == 1 && s.stats.cfgdone_retries == 3);
    CHECK(s.configured && s.n == 5);                            /* the parameters stay applied; the command is reported as failed to be confirmed */

    /* a newer command, then a repeat of the old one: answered from memory, not applied again */
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":19,\"N\":3,\"K\":1,\"inspect_time_ms\":50}");
    CHECK(s.n == 3 && s.k == 1 && s.inspect_ms == 50);
    first = g_ntx;
    rx(&s, CONFIG_18);
    CHECK(g_ntx == first + 2);
    CHECK_STR(g_tx[first], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":18,\"status\":\"accepted\"}");
    CHECK_STR(g_tx[first + 1], "{\"type\":\"CONFIG_DONE\",\"cmd_id\":18,\"N\":5,\"K\":2,\"inspect_time_ms\":500}");
    CHECK(s.n == 3 && s.k == 1 && s.inspect_ms == 50);         /* the newer parameters are still in use */
    CHECK(s.stats.cmd_duplicates == 1 && s.stats.cmds_accepted == 2);

    /* a repeat while its CONFIG_DONE still waits for an ACK does not start a second wait */
    first = g_ntx;
    rx(&s, CONFIG_18);
    CHECK(g_ntx == first + 1);
}

static void test_history_rings(void)
{
    station_t s;
    char m[120];
    int first;

    /* jobs: 32 distinct job_ids are remembered, the 33rd pushes out the first */
    setup(&s, 0, 1);
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":1,\"N\":5,\"K\":2,\"inspect_time_ms\":10}");
    for (uint32_t j = 100; j < 133; j++) {
        snprintf(m, sizeof(m), "{\"type\":\"JOB_REQ\",\"job_id\":%lu,\"part_id\":1}", (unsigned long)j);
        rx(&s, m);
        run_to(&s, g_now + 10);
        snprintf(m, sizeof(m), "{\"type\":\"ACK\",\"ref\":\"JOB_RESULT\",\"job_id\":%lu,\"status\":\"accepted\"}", (unsigned long)j);
        rx(&s, m);
    }
    CHECK(s.stats.jobs_accepted == 33 && s.stats.job_duplicates == 0);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":132,\"part_id\":1}");                /* the newest is remembered */
    CHECK(s.stats.job_duplicates == 1 && s.stats.jobs_accepted == 33);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":101,\"part_id\":1}");                /* 101 is still there (oldest remembered) */
    CHECK(s.stats.job_duplicates == 2 && s.stats.jobs_accepted == 33);
    first = g_ntx;
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":100,\"part_id\":1}");                /* 100 was pushed out: treated as new */
    CHECK(s.stats.jobs_accepted == 34 && s.busy);
    CHECK_STR(g_tx[first], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":100,\"status\":\"accepted\"}");

    /* commands: the same, with its own ring of 32 */
    setup(&s, 0, 1);
    for (uint32_t c = 1; c <= 33; c++) {
        snprintf(m, sizeof(m), "{\"type\":\"CONFIG\",\"cmd_id\":%lu,\"N\":5,\"K\":2,\"inspect_time_ms\":500}", (unsigned long)c);
        rx(&s, m);
    }
    CHECK(s.stats.cmds_accepted == 33 && s.stats.cmd_duplicates == 0);
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":2,\"N\":5,\"K\":2,\"inspect_time_ms\":500}");
    CHECK(s.stats.cmd_duplicates == 1);
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":1,\"N\":5,\"K\":2,\"inspect_time_ms\":500}");
    CHECK(s.stats.cmds_accepted == 34 && s.stats.cmd_duplicates == 1);

    /* job ids and cmd ids are separate spaces: cmd_id 28 does not make job_id 28 a duplicate */
    setup(&s, 0, 1);
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":28,\"N\":5,\"K\":2,\"inspect_time_ms\":500}");
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    CHECK(s.busy && s.stats.job_duplicates == 0 && s.stats.cmd_duplicates == 0);
}

static void test_link_watchdog_and_reconnect(void)
{
    station_t s;
    int first;
    uint32_t last;

    setup(&s, 0, 1);
    configure(&s);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    g_gw_alive = 0;                                             /* the gateway goes silent */
    last = s.last_rx_ms;
    run_to(&s, last + 2995);
    CHECK(station_state(&s) == STATION_INSPECTING || station_state(&s) == STATION_IDLE);
    CHECK(s.stats.link_timeouts == 0 && !station_drop_requested(&s));
    run_to(&s, last + 3000);
    CHECK(s.stats.link_timeouts == 1 && station_drop_requested(&s));
    CHECK(station_state(&s) == STATION_DISCONNECTED);

    /* the caller closes the socket; calling on_disconnect is harmless and clears the request */
    station_on_disconnect(&s, g_now);
    CHECK(!station_drop_requested(&s) && s.stats.disconnects == 1);
    first = g_ntx;
    run_to(&s, g_now + 5000);
    CHECK(g_ntx == first);                                      /* nothing is sent while disconnected */
    rx(&s, "{\"type\":\"HEARTBEAT\",\"seq\":1}");
    CHECK(s.stats.heartbeats_rx == 0 || station_state(&s) == STATION_DISCONNECTED);    /* ignored without a connection */

    /* reconnect: HELLO again, parameters kept in RAM */
    station_on_connect(&s, g_now);
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"HELLO\",\"device\":\"esp32_b\",\"fw\":\"1.0.0\"}");
    CHECK(s.configured && s.n == 5 && s.stats.connects == 2);
    CHECK(station_state(&s) == STATION_IDLE);
}

static void test_abandoned_job(void)
{
    station_t s;
    int first;

    setup(&s, 0, 1);
    configure(&s);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    run_to(&s, 200);
    CHECK(s.busy);

    /* the TCP connection drops in the middle of the inspection */
    station_on_disconnect(&s, g_now);
    CHECK(!s.busy && s.stats.jobs_abandoned == 1);
    first = g_ntx;
    run_to(&s, g_now + 2000);
    CHECK(g_ntx == first);                                      /* the late verdict is never sent */

    station_on_connect(&s, g_now);
    first = g_ntx;
    run_to(&s, g_now + 1000);
    CHECK(count_containing(first, "JOB_RESULT") == 0);

    /* the gateway retries the job: it is not run late and not run twice, and the answer says so */
    first = g_ntx;
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    CHECK_STR(g_tx[first], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":28,\"status\":\"rejected\",\"reason\":\"abandoned\"}");
    CHECK(!s.busy && s.stats.jobs_accepted == 1);

    /* a new job works */
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":29,\"part_id\":9}");
    CHECK(s.busy);
}

static void test_pending_results_dropped_on_disconnect(void)
{
    station_t s;
    int first;

    setup(&s, 0, 1);
    configure(&s);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    run_to(&s, 500);                                            /* result sent, waiting for ACK */
    CHECK(count_containing(0, "JOB_RESULT") == 1);
    station_on_disconnect(&s, g_now);
    station_on_connect(&s, g_now);
    first = g_ntx;
    run_to(&s, g_now + 3000);
    CHECK(count_containing(first, "JOB_RESULT") == 0);          /* no stale retry on the new connection */

    /* but the gateway's repeat of the job gets the result again */
    first = g_ntx;
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    CHECK(g_ntx == first + 2);
    CHECK_STR(g_tx[first + 1], "{\"type\":\"JOB_RESULT\",\"job_id\":28,\"part_id\":8,\"verdict\":\"FAIL\",\"N\":5,\"K\":2}");
}

static void test_bad_input(void)
{
    station_t s;
    int first;
    char big[STATION_MAX_BODY + 8];

    setup(&s, 0, 1);
    configure(&s);
    first = g_ntx;

    rx(&s, "not json at all");
    rx(&s, "[1,2,3]");
    rx(&s, "{}");
    rx(&s, "{\"type\":5}");
    rx(&s, "{\"typ\":\"HEARTBEAT\"}");
    rx(&s, "");
    CHECK(s.stats.malformed == 6);

    memset(big, ' ', sizeof(big));
    big[sizeof(big) - 1] = '\0';
    station_on_message(&s, (const uint8_t *)big, STATION_MAX_BODY + 1, g_now);
    CHECK(s.stats.malformed == 7);

    rx(&s, "{\"type\":\"HELLO\",\"device\":\"x\",\"fw\":\"1\"}");
    rx(&s, "{\"type\":\"JOB_RESULT\",\"job_id\":1,\"part_id\":1,\"verdict\":\"PASS\",\"N\":1,\"K\":0}");
    rx(&s, "{\"type\":\"CONFIG_DONE\",\"cmd_id\":1}");
    rx(&s, "{\"type\":\"SOMETHING_NEW\"}");
    CHECK(s.stats.unexpected_type == 4);
    CHECK(g_ntx == first);                                      /* none of this was answered */
    CHECK(s.stats.link_timeouts == 0);

    /* JOB_REQ problems */
    rx(&s, "{\"type\":\"JOB_REQ\",\"part_id\":8}");                              /* no job_id: nothing to answer */
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":-1,\"part_id\":8}");
    CHECK(g_ntx == first && s.stats.bad_fields == 2);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":5}");                                /* no part_id: rejected */
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":5,\"status\":\"rejected\",\"reason\":\"malformed\"}");
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":6,\"part_id\":\"8\"}");
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":6,\"status\":\"rejected\",\"reason\":\"malformed\"}");
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":7,\"part_id\":-2}");
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":7,\"status\":\"rejected\",\"reason\":\"malformed\"}");
    CHECK(!s.busy && s.stats.jobs_accepted == 0);

    /* ACK problems: all ignored, none of them ends or starts anything */
    first = g_ntx;
    rx(&s, "{\"type\":\"ACK\",\"ref\":\"HELLO\",\"status\":\"accepted\"}");
    rx(&s, "{\"type\":\"ACK\",\"ref\":\"JOB_RESULT\",\"job_id\":99,\"status\":\"accepted\"}");
    CHECK(s.stats.stale_acks == 2);
    rx(&s, "{\"type\":\"ACK\",\"status\":\"accepted\"}");
    rx(&s, "{\"type\":\"ACK\",\"ref\":\"JOB_RESULT\",\"status\":\"accepted\"}");
    rx(&s, "{\"type\":\"ACK\",\"ref\":\"JOB_RESULT\",\"job_id\":99,\"status\":\"maybe\"}");
    CHECK(s.stats.bad_fields == 2 + 3);
    CHECK(g_ntx == first);

    /* a message of any known kind counts as a sign of life */
    g_gw_alive = 0;
    {
        uint32_t t0 = g_now;
        run_to(&s, t0 + 2000);
        rx(&s, "{\"type\":\"SOMETHING_NEW\"}");
        run_to(&s, t0 + 4000);
        CHECK(s.stats.link_timeouts == 0 && station_state(&s) == STATION_IDLE);
        run_to(&s, t0 + 5000);
        CHECK(s.stats.link_timeouts == 1);
    }
}

static void test_frame_error_counted(void)
{
    station_t s;

    setup(&s, 0, 1);
    station_note_frame_error(&s);
    CHECK(s.stats.frame_errors == 1);
}

static void test_big_ids(void)
{
    station_t s;

    setup(&s, 0, 1);
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":4294967295,\"N\":6,\"K\":4294967295,\"inspect_time_ms\":10}");
    CHECK_STR(g_tx[g_ntx - 2], "{\"type\":\"ACK\",\"ref\":\"CONFIG\",\"cmd_id\":4294967295,\"status\":\"accepted\"}");
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":4294967295,\"part_id\":4294967295}");
    CHECK_STR(g_tx[g_ntx - 1], "{\"type\":\"ACK\",\"ref\":\"JOB_REQ\",\"job_id\":4294967295,\"status\":\"accepted\"}");
    run_to(&s, g_now + 10);
    CHECK_STR(g_tx[g_ntx - 1],
              "{\"type\":\"JOB_RESULT\",\"job_id\":4294967295,\"part_id\":4294967295,\"verdict\":\"FAIL\",\"N\":6,\"K\":4294967295}");
    {                                                           /* nothing the station builds is longer than STATION_MSG_MAX */
        size_t longest = 0;
        for (int i = 0; i < g_ntx; i++) {
            if (strlen(g_tx[i]) > longest) {
                longest = strlen(g_tx[i]);
            }
        }
        CHECK(longest < STATION_MSG_MAX && longest > 90);
    }
}

static void test_clock_wrap(void)
{
    station_t s;
    int first;

    setup(&s, 0xFFFFFF00u, 1);                                  /* 256 ms before the 32-bit clock wraps */
    configure(&s);
    run_to(&s, 0xFFFFFF00u + 100);
    first = g_ntx;
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    run_to(&s, g_now + 495);
    CHECK(g_now < 0x200);                                       /* the clock has wrapped */
    CHECK(count_containing(first, "JOB_RESULT") == 0);
    run_to(&s, g_now + 5);
    CHECK(count_containing(first, "JOB_RESULT") == 1);
    run_to(&s, g_now + 2500);                                   /* retries and the final give-up work across the wrap */
    CHECK(s.stats.result_retries == 3 && s.stats.results_failed == 1);
    CHECK(s.stats.heartbeats_rx >= 2 && s.stats.link_timeouts == 0);
}

static void test_job_between_heartbeats_keeps_time(void)
{
    station_t s;

    /* a long inspection (20 s) with the gateway sending heartbeats: no early result, no watchdog */
    setup(&s, 0, 1);
    rx(&s, "{\"type\":\"CONFIG\",\"cmd_id\":1,\"N\":5,\"K\":2,\"inspect_time_ms\":20000}");
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":1,\"part_id\":8}");
    run_to(&s, 19995);
    CHECK(s.busy && count_containing(0, "JOB_RESULT") == 0 && s.stats.link_timeouts == 0);
    run_to(&s, 20000);
    CHECK(!s.busy && count_containing(0, "JOB_RESULT") == 1);
}

static void test_logging(void)
{
    station_t s;

    setup(&s, 0, 1);
    configure(&s);
    rx(&s, "{\"type\":\"JOB_REQ\",\"job_id\":28,\"part_id\":8}");
    run_to(&s, 500);
    CHECK(g_nlog_rx >= 3 && g_nlog_tx >= 5 && g_nlog_evt >= 5);
}

int main(void)
{
    test_json();
    test_tcpframe();
    test_verdict();
    test_connect_and_heartbeat();
    test_config();
    test_config_invalid();
    test_job_before_config();
    test_contract_example();
    test_busy_and_snapshot();
    test_duplicate_job();
    test_result_retries();
    test_result_rejected_by_gateway();
    test_config_done_retries_and_duplicate();
    test_history_rings();
    test_link_watchdog_and_reconnect();
    test_abandoned_job();
    test_pending_results_dropped_on_disconnect();
    test_bad_input();
    test_frame_error_counted();
    test_big_ids();
    test_clock_wrap();
    test_job_between_heartbeats_keeps_time();
    test_logging();

    printf("%d checks, %d failed\n", g_checks, g_fail);
    return g_fail == 0 ? 0 : 1;
}
