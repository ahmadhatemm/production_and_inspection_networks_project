#ifndef PRODMSG_H
#define PRODMSG_H

#include <stdbool.h>
#include <stdint.h>

#include "atr.h"

/* The 16-byte production payload carried inside an ATR record
 * (Interface Contract v1, "Production payload"). Multi-byte fields big-endian.
 *
 *   offset  size  field
 *        0     1  msg_type
 *        1     4  id        part_id for part messages, cmd_id for commands, counter for heartbeats
 *        5     1  arg       see the tables below
 *        6     4  value_a   Start command: cycle_time_ms, otherwise 0
 *       10     4  value_b   Start command: target_parts, otherwise 0
 *       14     2  reserved  always 0 */

/* msg_type */
#define PM_PART       0x01   /* A -> gateway: a new part, id = part_id       */
#define PM_PART_ACK   0x02   /* gateway -> A: answer to PART, id = part_id   */
#define PM_CMD        0x03   /* gateway -> A: command, id = cmd_id           */
#define PM_CMD_ACK    0x04   /* A -> gateway: command accepted or rejected   */
#define PM_CMD_DONE   0x05   /* A -> gateway: command carried out            */
#define PM_HEARTBEAT  0x06   /* both directions, id = counter                */

/* arg of PM_PART_ACK */
#define PM_ACK_ACCEPTED    1
#define PM_ACK_QUEUE_FULL  2

/* arg of PM_CMD */
#define PM_CMD_START   1
#define PM_CMD_PAUSE   2
#define PM_CMD_RESUME  3
#define PM_CMD_STOP    4

/* arg of PM_CMD_ACK */
#define PM_CMDACK_ACCEPTED  1
#define PM_CMDACK_REJECTED  2

/* arg of PM_CMD_DONE: the state ESP32 A is in now */
#define PM_DONE_RUNNING  1
#define PM_DONE_PAUSED   2
#define PM_DONE_STOPPED  3

typedef struct {
    uint8_t  type;
    uint32_t id;
    uint8_t  arg;
    uint32_t value_a;
    uint32_t value_b;
} prod_msg_t;

/* Writes exactly ATR_PROD_PAYLOAD_LEN bytes to out. */
void prod_encode(uint8_t out[ATR_PROD_PAYLOAD_LEN], const prod_msg_t *m);

/* Reads exactly ATR_PROD_PAYLOAD_LEN bytes. The reserved bytes are ignored. */
void prod_decode(const uint8_t in[ATR_PROD_PAYLOAD_LEN], prod_msg_t *m);

#endif
