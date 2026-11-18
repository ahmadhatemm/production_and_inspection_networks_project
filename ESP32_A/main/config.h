#ifndef CONFIG_H
#define CONFIG_H

/* Every tunable number of ESP32 A, in one place.
 * Values marked "contract" come from Interface Contract v1 (Reliability rules,
 * Field limits). If the team changes the contract, change them here. */

/* Link to the gateway: UART0 over the USB cable, 8N1 (contract) */
#define LINK_BAUD                  115200

/* Reliability (contract) */
#define STATION_ACK_TIMEOUT_MS     500     /* wait this long for a PART_ACK                 */
#define STATION_MAX_TRIES          4       /* first send + 3 retries, same bytes every time */
#define STATION_FULL_RETRY_MS      200     /* re-send a part every 200 ms after QUEUE_FULL  */
#define STATION_HEARTBEAT_MS       1000    /* send a heartbeat this often                   */
#define STATION_LINK_TIMEOUT_MS    3000    /* no valid frame from the gateway for this long = link lost */
#define STATION_CMD_HISTORY        16      /* remembered cmd_id values for duplicate suppression */

/* Field limits for the Start command (contract) */
#define STATION_CYCLE_MIN_MS       10
#define STATION_CYCLE_MAX_MS       60000
#define STATION_TARGET_MIN         1
#define STATION_TARGET_MAX         10000

#endif
