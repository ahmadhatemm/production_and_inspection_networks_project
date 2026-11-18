#include "slip.h"

size_t slip_encode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap)
{
    size_t n = 0;

    if (out_cap < 2) {
        return 0;
    }
    out[n++] = SLIP_END;            /* leading END flushes any line noise */
    for (size_t i = 0; i < len; i++) {
        if (in[i] == SLIP_END || in[i] == SLIP_ESC) {
            if (n + 3 > out_cap) {  /* 2 bytes + room for the closing END */
                return 0;
            }
            out[n++] = SLIP_ESC;
            out[n++] = (in[i] == SLIP_END) ? SLIP_ESC_END : SLIP_ESC_ESC;
        } else {
            if (n + 2 > out_cap) {
                return 0;
            }
            out[n++] = in[i];
        }
    }
    out[n++] = SLIP_END;
    return n;
}

void slip_decoder_init(slip_decoder_t *d, uint8_t *buf, size_t cap)
{
    d->buf = buf;
    d->cap = cap;
    d->len = 0;
    d->in_escape = false;
    d->bad = false;
}

slip_result_t slip_decode_byte(slip_decoder_t *d, uint8_t byte, size_t *packet_len)
{
    if (byte == SLIP_END) {
        slip_result_t r = SLIP_NONE;

        if (d->bad) {
            r = SLIP_DROPPED;
        } else if (d->len > 0) {    /* two ENDs in a row give an empty packet: ignore */
            *packet_len = d->len;
            r = SLIP_PACKET;
        }
        d->len = 0;
        d->in_escape = false;
        d->bad = false;
        return r;
    }

    if (d->bad) {
        return SLIP_NONE;           /* skip everything until the next END */
    }

    if (d->in_escape) {
        d->in_escape = false;
        if (byte == SLIP_ESC_END) {
            byte = SLIP_END;
        } else if (byte == SLIP_ESC_ESC) {
            byte = SLIP_ESC;
        } else {
            d->bad = true;          /* ESC must be followed by ESC_END or ESC_ESC */
            return SLIP_NONE;
        }
    } else if (byte == SLIP_ESC) {
        d->in_escape = true;
        return SLIP_NONE;
    }

    if (d->len >= d->cap) {
        d->bad = true;              /* longer than any valid frame */
        return SLIP_NONE;
    }
    d->buf[d->len++] = byte;
    return SLIP_NONE;
}
