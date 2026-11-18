#ifndef CONFIG_H
#define CONFIG_H

/* Every tunable number of ESP32 B, in one place.
 * Values marked "contract" come from Interface Contract v1. If the team changes
 * the contract, change them here.
 * Values marked "EDIT" must be set for your own network before you build. */

/* ---- Network (EDIT) ----------------------------------------------------- */
#define WIFI_SSID        "WE_18D593"     /* the gateway's hotspot name                    */
#define WIFI_PASSWORD    "828f25b3"     /* its password; "" for an open network          */
#define GATEWAY_IP       "192.168.100.12"   /* the gateway's fixed address on that network   */
#define GATEWAY_PORT     5000            /* contract: gateway listens on TCP port 5000    */

/* ---- Identity (contract: HELLO) ----------------------------------------- */
#define DEVICE_NAME      "esp32_b"
#define FW_VERSION       "1.0.0"

/* ---- Reliability (contract) --------------------------------------------- */
#define STATION_ACK_TIMEOUT_MS   500    /* wait this long for the gateway's ACK            */
#define STATION_MAX_TRIES        4      /* first send + 3 retries, same bytes every time   */
#define STATION_HEARTBEAT_MS     1000   /* send a heartbeat this often                     */
#define STATION_LINK_TIMEOUT_MS  3000   /* nothing valid from the gateway for this long = link lost */
#define STATION_HISTORY          32     /* remembered job_id values and, separately, cmd_id values */

/* ---- Field limits (contract) -------------------------------------------- */
#define STATION_INSPECT_MIN_MS   10
#define STATION_INSPECT_MAX_MS   60000

/* ---- Framing (contract) ------------------------------------------------- */
#define STATION_MAX_BODY         1024   /* largest TCP message body in bytes               */

/* ---- Implementation limits (not in the contract) ------------------------ */
#define STATION_MSG_MAX          192    /* largest message this station builds              */
#define STATION_OUTBOX           4      /* messages waiting for an ACK at the same time     */
#define STATION_LOG_HEARTBEATS   1      /* 0 = leave heartbeats out of the log              */

/* ---- Connection handling (not in the contract) -------------------------- */
#define LINK_RECONNECT_MS        1000   /* wait this long between connection attempts       */
#define LINK_CONNECT_TIMEOUT_MS  3000   /* give up one connection attempt after this long   */
#define LINK_POLL_MS             5      /* the main loop wakes at least this often          */

#endif
