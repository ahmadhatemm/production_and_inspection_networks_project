#include "crc16.h"

uint16_t crc16_ccitt(const uint8_t *data, size_t len, uint16_t crc)
{
    for (size_t i = 0; i < len; i++) {
        crc = (uint16_t)(crc ^ ((unsigned)data[i] << 8));
        for (int bit = 0; bit < 8; bit++) {
            if (crc & 0x8000u) {
                crc = (uint16_t)(((unsigned)crc << 1) ^ 0x1021u);
            } else {
                crc = (uint16_t)((unsigned)crc << 1);
            }
        }
    }
    return crc;
}
