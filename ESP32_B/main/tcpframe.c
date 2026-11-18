#include <string.h>

#include "tcpframe.h"

size_t tcpframe_encode(const uint8_t *body, size_t len, uint8_t *out, size_t out_cap)
{
    if (len > TCPFRAME_MAX_BODY || TCPFRAME_HDR_LEN + len > out_cap) {
        return 0;
    }
    out[0] = (uint8_t)(len >> 24);
    out[1] = (uint8_t)(len >> 16);
    out[2] = (uint8_t)(len >> 8);
    out[3] = (uint8_t)len;
    if (len > 0) {
        memcpy(&out[TCPFRAME_HDR_LEN], body, len);
    }
    return TCPFRAME_HDR_LEN + len;
}

void tcpframe_decoder_init(tcpframe_decoder_t *d, uint8_t *buf, size_t cap)
{
    d->buf = buf;
    d->cap = cap;
    tcpframe_decoder_reset(d);
}

void tcpframe_decoder_reset(tcpframe_decoder_t *d)
{
    d->hdr_got = 0;
    d->want = 0;
    d->got = 0;
    d->failed = false;
}

tcpframe_result_t tcpframe_feed(tcpframe_decoder_t *d, const uint8_t *data, size_t len,
                                size_t *used, size_t *body_len)
{
    size_t i = 0;

    *used = 0;
    *body_len = 0;
    if (d->failed) {
        return TCPFRAME_ERROR;
    }

    while (i < len) {
        if (d->hdr_got < TCPFRAME_HDR_LEN) {
            d->hdr[d->hdr_got++] = data[i++];
            if (d->hdr_got == TCPFRAME_HDR_LEN) {
                uint32_t n = ((uint32_t)d->hdr[0] << 24) | ((uint32_t)d->hdr[1] << 16) |
                             ((uint32_t)d->hdr[2] << 8) | (uint32_t)d->hdr[3];

                if (n > TCPFRAME_MAX_BODY || n > d->cap) {
                    d->failed = true;
                    *used = i;
                    return TCPFRAME_ERROR;
                }
                d->want = n;
                d->got = 0;
                if (n == 0) {                       /* an empty body is a complete message */
                    d->hdr_got = 0;
                    *used = i;
                    return TCPFRAME_MESSAGE;
                }
            }
        } else {
            size_t need = d->want - d->got;
            size_t avail = len - i;
            size_t take = need < avail ? need : avail;

            memcpy(&d->buf[d->got], &data[i], take);
            d->got += take;
            i += take;
            if (d->got == d->want) {
                *body_len = d->want;
                d->hdr_got = 0;                     /* ready for the next header */
                d->want = 0;
                d->got = 0;
                *used = i;
                return TCPFRAME_MESSAGE;
            }
        }
    }
    *used = i;
    return TCPFRAME_NEED_MORE;
}
