#ifndef CRC16_H
#define CRC16_H

#include <stddef.h>
#include <stdint.h>

/* CRC-16/CCITT-FALSE: polynomial 0x1021, start value 0xFFFF, MSB first,
 * no final XOR. Check value: crc16_ccitt("123456789", 9, CRC16_INIT) == 0x29B1 */
#define CRC16_INIT 0xFFFFu

uint16_t crc16_ccitt(const uint8_t *data, size_t len, uint16_t crc);

#endif
