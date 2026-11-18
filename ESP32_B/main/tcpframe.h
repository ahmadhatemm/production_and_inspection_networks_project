#ifndef TCPFRAME_H
#define TCPFRAME_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Length-prefix framing for the TCP link (Interface Contract v1):
 *
 *   4-byte big-endian length N, then N bytes of body (compact UTF-8 JSON)
 *
 * TCP is a byte stream, so one recv() can hold half a message or three messages.
 * The decoder below collects bytes until a whole body is there. */

#define TCPFRAME_HDR_LEN   4
#define TCPFRAME_MAX_BODY  1024     /* contract: maximum body 1024 bytes */

/* Writes the 4-byte length and the body into out.
 * Returns the number of bytes written (4 + len), or 0 if len is over
 * TCPFRAME_MAX_BODY or out_cap is too small. */
size_t tcpframe_encode(const uint8_t *body, size_t len, uint8_t *out, size_t out_cap);

typedef enum {
    TCPFRAME_NEED_MORE,     /* all bytes taken, no message finished yet                  */
    TCPFRAME_MESSAGE,       /* a whole body is in buf (body_len bytes)                    */
    TCPFRAME_ERROR          /* announced length is over the limit: the stream is lost     */
} tcpframe_result_t;

typedef struct {
    uint8_t *buf;           /* where bodies are collected, at least TCPFRAME_MAX_BODY bytes */
    size_t   cap;
    uint8_t  hdr[TCPFRAME_HDR_LEN];
    size_t   hdr_got;
    size_t   want;          /* body length announced by the header */
    size_t   got;           /* body bytes collected so far         */
    bool     failed;
} tcpframe_decoder_t;

void tcpframe_decoder_init(tcpframe_decoder_t *d, uint8_t *buf, size_t cap);

/* Forget a half-received message (used after a reconnect). */
void tcpframe_decoder_reset(tcpframe_decoder_t *d);

/* Feed received bytes. Stops after the first finished message or error and
 * reports in *used how many of the bytes it took, so the caller calls again
 * with the rest:
 *
 *     while (len > 0) {
 *         r = tcpframe_feed(d, p, len, &used, &body_len);
 *         ... use d->buf[0..body_len) if r == TCPFRAME_MESSAGE ...
 *         p += used; len -= used;
 *     }
 *
 * The body in d->buf is valid until the next call. After TCPFRAME_ERROR the
 * decoder stays failed: there is no way to find the next message boundary in
 * a TCP stream, so the connection has to be closed. */
tcpframe_result_t tcpframe_feed(tcpframe_decoder_t *d, const uint8_t *data, size_t len,
                                size_t *used, size_t *body_len);

#endif
