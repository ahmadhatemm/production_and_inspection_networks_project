#include <string.h>

#include "atr.h"
#include "crc16.h"

static void put_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)(v & 0xFFu);
}

static void put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static uint16_t get_u16(const uint8_t *p)
{
    return (uint16_t)((p[0] << 8) | p[1]);
}

static uint32_t get_u32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

size_t atr_build(uint8_t *out, size_t out_cap, uint16_t test_id, uint32_t seq,
                 const uint8_t *payload, uint16_t payload_len)
{
    size_t body_len = ATR_HEADER_LEN + (size_t)payload_len;

    if (payload_len > ATR_MAX_PAYLOAD || body_len + ATR_CRC_LEN > out_cap) {
        return 0;
    }
    put_u16(&out[0], test_id);
    put_u32(&out[2], seq);
    put_u16(&out[6], payload_len);
    if (payload_len > 0) {
        memcpy(&out[ATR_HEADER_LEN], payload, payload_len);
    }
    put_u16(&out[body_len], crc16_ccitt(out, body_len, CRC16_INIT));
    return body_len + ATR_CRC_LEN;
}

atr_status_t atr_parse(const uint8_t *raw, size_t raw_len, atr_t *a)
{
    if (raw_len < ATR_OVERHEAD) {
        return ATR_TOO_SHORT;
    }

    /* Check the CRC before trusting any field, including payload_len. */
    size_t body_len = raw_len - ATR_CRC_LEN;
    if (crc16_ccitt(raw, body_len, CRC16_INIT) != get_u16(&raw[body_len])) {
        return ATR_BAD_CRC;
    }

    a->test_id = get_u16(&raw[0]);
    a->seq = get_u32(&raw[2]);
    a->payload_len = get_u16(&raw[6]);
    if (a->payload_len > ATR_MAX_PAYLOAD || (size_t)a->payload_len != body_len - ATR_HEADER_LEN) {
        return ATR_BAD_LENGTH;
    }
    a->payload = &raw[ATR_HEADER_LEN];
    return ATR_OK;
}
