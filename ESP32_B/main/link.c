#include <errno.h>
#include <stdio.h>
#include <string.h>

#ifdef ESP_PLATFORM
/* ESP32: lwIP sockets. Its own function names are used for select, ioctl and close,
 * because ESP-IDF routes the plain names through its file system layer. */
#include "lwip/sockets.h"
#define NET_SELECT  lwip_select
#define NET_IOCTL   lwip_ioctl
#define NET_CLOSE   lwip_close
#define NET_NOSIGNAL 0
#else
/* PC: POSIX sockets. */
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/ioctl.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>
#define NET_SELECT  select
#define NET_IOCTL   ioctl
#define NET_CLOSE   close
#define NET_NOSIGNAL MSG_NOSIGNAL           /* a send to a closed socket must not kill the program */
#endif

#include "link.h"

static uint32_t time_since(uint32_t now, uint32_t then)
{
    return (uint32_t)(now - then);
}

static bool would_block(int e)
{
    return e == EWOULDBLOCK || e == EAGAIN;
}

static void set_timeval(struct timeval *tv, uint32_t ms)
{
    tv->tv_sec = (long)(ms / 1000u);
    tv->tv_usec = (long)((ms % 1000u) * 1000u);
}

/* ---- sockets ---------------------------------------------------------- */

/* Opens a non-blocking TCP connection and waits at most timeout_ms for it to complete. */
static int sock_connect(const char *ip, uint16_t port, uint32_t timeout_ms, int *why)
{
    struct sockaddr_in addr;
    struct timeval tv;
    fd_set wfds;
    int on = 1;
    int err = 0;
    socklen_t errlen = sizeof(err);
    int s;

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    *why = 0;
    if (inet_pton(AF_INET, ip, &addr.sin_addr) != 1) {
        *why = EINVAL;                      /* GATEWAY_IP is not a valid address */
        return -1;
    }

    s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s < 0) {
        *why = errno;
        return -1;
    }
    if (NET_IOCTL(s, FIONBIO, &on) != 0) {
        *why = errno;
        NET_CLOSE(s);
        return -1;
    }

    if (connect(s, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        if (errno != EINPROGRESS) {
            *why = errno;
            NET_CLOSE(s);
            return -1;
        }
        FD_ZERO(&wfds);
        FD_SET(s, &wfds);
        set_timeval(&tv, timeout_ms);
        if (NET_SELECT(s + 1, NULL, &wfds, NULL, &tv) <= 0) {      /* timed out or failed */
            *why = ETIMEDOUT;
            NET_CLOSE(s);
            return -1;
        }
        if (getsockopt(s, SOL_SOCKET, SO_ERROR, &err, &errlen) != 0) {
            *why = errno;
            NET_CLOSE(s);
            return -1;
        }
        if (err != 0) {
            *why = err;                     /* for example ECONNREFUSED: the PC answered, but nothing listens */
            NET_CLOSE(s);
            return -1;
        }
    }

    /* Messages are small: send each one at once instead of waiting to fill a packet. */
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &on, sizeof(on));
    return s;
}

/* Sends all len bytes or reports failure. */
static bool sock_send_all(int s, const uint8_t *data, size_t len)
{
    while (len > 0) {
        int n = (int)send(s, data, len, NET_NOSIGNAL);

        if (n > 0) {
            data += n;
            len -= (size_t)n;
        } else if (n < 0 && would_block(errno)) {      /* send buffer full: wait until it can take more */
            struct timeval tv;
            fd_set wfds;

            FD_ZERO(&wfds);
            FD_SET(s, &wfds);
            set_timeval(&tv, 1000);
            if (NET_SELECT(s + 1, NULL, &wfds, NULL, &tv) <= 0) {
                return false;
            }
        } else {
            return false;
        }
    }
    return true;
}

/* ---- the station's way out -------------------------------------------- */

static void link_tx(const uint8_t *body, size_t len, void *ctx)
{
    link_t *l = (link_t *)ctx;
    size_t n;

    if (l->sock < 0 || l->broken) {
        return;
    }
    n = tcpframe_encode(body, len, l->tx_buf, sizeof(l->tx_buf));
    if (n == 0 || !sock_send_all(l->sock, l->tx_buf, n)) {
        l->broken = true;                   /* link_step closes the connection */
    }
}

void link_init(link_t *l, station_t *st, const char *ip, uint16_t port, const link_env_t *env)
{
    memset(l, 0, sizeof(*l));
    l->st = st;
    l->ip = ip;
    l->port = port;
    l->env = *env;
    l->sock = -1;
    tcpframe_decoder_init(&l->dec, l->rx_body, sizeof(l->rx_body));
    st->tx = link_tx;
    st->ctx = l;
}

static void close_connection(link_t *l)
{
    if (l->sock >= 0) {
        NET_CLOSE(l->sock);
        l->sock = -1;
    }
    l->broken = false;
    tcpframe_decoder_reset(&l->dec);
    station_on_disconnect(l->st, l->env.now_ms());
}

/* Cuts received bytes into messages and hands each to the station.
 * Returns false if the connection has to be closed. */
static bool feed_bytes(link_t *l, const uint8_t *data, size_t len)
{
    while (len > 0) {
        size_t used;
        size_t body_len;
        tcpframe_result_t r = tcpframe_feed(&l->dec, data, len, &used, &body_len);

        data += used;
        len -= used;
        if (r == TCPFRAME_ERROR) {
            station_note_frame_error(l->st);
            return false;
        }
        if (r == TCPFRAME_MESSAGE) {
            station_on_message(l->st, l->dec.buf, body_len, l->env.now_ms());
            if (l->broken || station_drop_requested(l->st)) {
                return false;
            }
        }
    }
    return true;
}

void link_step(link_t *l)
{
    uint32_t now = l->env.now_ms();

    if (!l->env.net_ready()) {              /* no Wi-Fi: nothing can work, and nothing may stay half-open */
        if (l->sock >= 0) {
            close_connection(l);
        }
        l->env.sleep_ms(20);
        return;
    }

    if (l->sock < 0) {
        int s;
        int why;

        if (l->attempted && time_since(now, l->last_attempt_ms) < LINK_RECONNECT_MS) {
            l->env.sleep_ms(10);
            return;
        }
        l->attempted = true;
        l->last_attempt_ms = now;
        s = sock_connect(l->ip, l->port, LINK_CONNECT_TIMEOUT_MS, &why);
        if (s < 0 && l->env.note != NULL) {
            char line[96];

            snprintf(line, sizeof(line), "cannot connect to %s:%u (%s), retrying", l->ip, (unsigned)l->port,
                     why == ETIMEDOUT ? "no answer" : strerror(why));
            l->env.note(line);
        }
        if (s >= 0) {
            l->sock = s;
            l->broken = false;
            tcpframe_decoder_reset(&l->dec);
            station_on_connect(l->st, l->env.now_ms());
        }
        return;
    }

    /* connected: wait for data, but never longer than LINK_POLL_MS so the timers keep running */
    {
        fd_set rfds;
        struct timeval tv;
        int ready;

        FD_ZERO(&rfds);
        FD_SET(l->sock, &rfds);
        set_timeval(&tv, LINK_POLL_MS);
        ready = NET_SELECT(l->sock + 1, &rfds, NULL, NULL, &tv);

        if (ready > 0 && FD_ISSET(l->sock, &rfds)) {
            int n = (int)recv(l->sock, l->rx_raw, sizeof(l->rx_raw), 0);

            if (n > 0) {
                if (!feed_bytes(l, l->rx_raw, (size_t)n)) {
                    close_connection(l);
                    return;
                }
            } else if (n == 0 || !would_block(errno)) {     /* the gateway closed the connection, or an error */
                close_connection(l);
                return;
            }
        } else if (ready < 0 && errno != EINTR) {
            close_connection(l);
            return;
        }
    }

    station_tick(l->st, l->env.now_ms());
    if (l->broken || station_drop_requested(l->st)) {
        close_connection(l);
    }
    l->env.idle(l->env.user);
}
