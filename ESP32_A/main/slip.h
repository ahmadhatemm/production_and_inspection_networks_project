#ifndef SLIP_H
#define SLIP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* RFC 1055 special bytes */
#define SLIP_END     0xC0
#define SLIP_ESC     0xDB
#define SLIP_ESC_END 0xDC
#define SLIP_ESC_ESC 0xDD

/* Writes END + escaped packet + END into out.
 * Returns the number of bytes written, or 0 if out_cap is too small. */
size_t slip_encode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap);

typedef enum {
    SLIP_NONE,      /* byte taken, no packet finished yet                   */
    SLIP_PACKET,    /* END seen: buf holds a packet of *packet_len bytes    */
    SLIP_DROPPED    /* END seen, but the packet was too long or had a bad   */
                    /* escape sequence, so it was thrown away               */
} slip_result_t;

typedef struct {
    uint8_t *buf;
    size_t   cap;
    size_t   len;
    bool     in_escape;
    bool     bad;
} slip_decoder_t;

void slip_decoder_init(slip_decoder_t *d, uint8_t *buf, size_t cap);

/* Feed one received byte. When SLIP_PACKET is returned, use the packet in
 * d->buf before feeding the next byte. */
slip_result_t slip_decode_byte(slip_decoder_t *d, uint8_t byte, size_t *packet_len);

#endif
