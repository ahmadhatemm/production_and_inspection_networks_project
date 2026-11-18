#include <string.h>

#include "prodmsg.h"

static void put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static uint32_t get_u32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

void prod_encode(uint8_t out[ATR_PROD_PAYLOAD_LEN], const prod_msg_t *m)
{
    out[0] = m->type;
    put_u32(&out[1], m->id);
    out[5] = m->arg;
    put_u32(&out[6], m->value_a);
    put_u32(&out[10], m->value_b);
    out[14] = 0;
    out[15] = 0;
}

void prod_decode(const uint8_t in[ATR_PROD_PAYLOAD_LEN], prod_msg_t *m)
{
    m->type = in[0];
    m->id = get_u32(&in[1]);
    m->arg = in[5];
    m->value_a = get_u32(&in[6]);
    m->value_b = get_u32(&in[10]);
}
