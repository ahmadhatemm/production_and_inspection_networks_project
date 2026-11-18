#ifndef ATR_H
#define ATR_H

#include <stddef.h>
#include <stdint.h>

/* The Application Test Record, as in the project description. All multi-byte
 * integers are big-endian.
 *
 *   test_id (2) | seq (4) | payload_len (2) | payload (payload_len) | crc16 (2)
 *
 * crc16 is CRC-16/CCITT-FALSE over the header and the payload. */
#define ATR_HEADER_LEN      8
#define ATR_CRC_LEN         2
#define ATR_OVERHEAD        (ATR_HEADER_LEN + ATR_CRC_LEN)
#define ATR_MAX_PAYLOAD     128                           /* benchmark sizes are 16, 64 and 128 */
#define ATR_MAX_FRAME_LEN   (ATR_OVERHEAD + ATR_MAX_PAYLOAD)

/* Production traffic: test_id 0 and a fixed 16-byte payload (see prodmsg.h) */
#define ATR_TEST_ID_PRODUCTION  0
#define ATR_PROD_PAYLOAD_LEN    16
#define ATR_PROD_FRAME_LEN      (ATR_OVERHEAD + ATR_PROD_PAYLOAD_LEN)   /* 26 */

typedef struct {
    uint16_t       test_id;
    uint32_t       seq;
    uint16_t       payload_len;
    const uint8_t *payload;     /* points into the buffer that was parsed */
} atr_t;

typedef enum {
    ATR_OK = 0,
    ATR_TOO_SHORT,
    ATR_BAD_CRC,
    ATR_BAD_LENGTH
} atr_status_t;

/* Returns the number of bytes written to out, or 0 if the payload is longer
 * than ATR_MAX_PAYLOAD or out_cap is too small. */
size_t atr_build(uint8_t *out, size_t out_cap, uint16_t test_id, uint32_t seq,
                 const uint8_t *payload, uint16_t payload_len);

atr_status_t atr_parse(const uint8_t *raw, size_t raw_len, atr_t *a);

#endif
