#include <string.h>

#include "jsonmini.h"

#define SKIP_DEPTH_MAX 8

static const char *skip_ws(const char *p, const char *end)
{
    while (p < end && (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')) {
        p++;
    }
    return p;
}

static int hex_val(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

/* *pp points at the opening quote. On success *pp is just after the closing quote.
 * At most cap characters are stored in out (which must have room for cap + 1);
 * longer strings set *trunc. out may be NULL to throw the string away. */
static bool parse_string(const char **pp, const char *end, char *out, size_t cap, bool *trunc)
{
    const char *p = *pp + 1;
    size_t n = 0;

    *trunc = false;
    while (p < end) {
        unsigned char c = (unsigned char)*p;
        char ch;

        if (c == '"') {
            if (out != NULL) {
                out[n] = '\0';
            }
            *pp = p + 1;
            return true;
        }
        if (c < 0x20) {                 /* a raw control character is not allowed inside a string */
            return false;
        }
        if (c == '\\') {
            p++;
            if (p >= end) {
                return false;
            }
            switch (*p) {
            case '"':  ch = '"';  break;
            case '\\': ch = '\\'; break;
            case '/':  ch = '/';  break;
            case 'b':  ch = '\b'; break;
            case 'f':  ch = '\f'; break;
            case 'n':  ch = '\n'; break;
            case 'r':  ch = '\r'; break;
            case 't':  ch = '\t'; break;
            case 'u': {
                int code = 0;

                if (end - p < 5) {
                    return false;
                }
                for (int i = 1; i <= 4; i++) {
                    int h = hex_val(p[i]);
                    if (h < 0) {
                        return false;
                    }
                    code = (code << 4) | h;
                }
                if (code == 0) {        /* would end the string early for C code */
                    return false;
                }
                ch = code < 0x80 ? (char)code : '?';
                p += 4;
                break;
            }
            default:
                return false;
            }
        } else {
            ch = (char)c;
        }
        p++;
        if (out != NULL && n < cap) {
            out[n++] = ch;
        } else if (n < cap) {
            n++;
        } else {
            *trunc = true;
        }
    }
    return false;                       /* no closing quote */
}

static bool is_digit(char c)
{
    return c >= '0' && c <= '9';
}

/* Number grammar of RFC 8259. *is_int is true when there is no fraction or exponent. */
static bool parse_number(const char **pp, const char *end, bool *is_int, int64_t *val)
{
    const char *p = *pp;
    bool neg = false;
    bool overflow = false;
    uint64_t acc = 0;

    if (p < end && *p == '-') {
        neg = true;
        p++;
    }
    if (p >= end || !is_digit(*p)) {
        return false;
    }
    if (*p == '0') {
        p++;                            /* a leading zero cannot be followed by more digits */
    } else {
        while (p < end && is_digit(*p)) {
            unsigned d = (unsigned)(*p - '0');

            if (acc > (UINT64_MAX - d) / 10u) {
                overflow = true;
            } else {
                acc = acc * 10u + d;
            }
            p++;
        }
    }
    *is_int = true;
    if (p < end && *p == '.') {
        p++;
        if (p >= end || !is_digit(*p)) {
            return false;
        }
        while (p < end && is_digit(*p)) {
            p++;
        }
        *is_int = false;
    }
    if (p < end && (*p == 'e' || *p == 'E')) {
        p++;
        if (p < end && (*p == '+' || *p == '-')) {
            p++;
        }
        if (p >= end || !is_digit(*p)) {
            return false;
        }
        while (p < end && is_digit(*p)) {
            p++;
        }
        *is_int = false;
    }

    if (neg) {
        if (overflow || acc > (uint64_t)INT64_MAX + 1u) {
            *val = INT64_MIN;
        } else if (acc == (uint64_t)INT64_MAX + 1u) {
            *val = INT64_MIN;
        } else {
            *val = -(int64_t)acc;
        }
    } else {
        *val = (overflow || acc > (uint64_t)INT64_MAX) ? INT64_MAX : (int64_t)acc;
    }
    *pp = p;
    return true;
}

static bool match_literal(const char **pp, const char *end, const char *lit)
{
    size_t n = strlen(lit);

    if ((size_t)(end - *pp) < n || memcmp(*pp, lit, n) != 0) {
        return false;
    }
    *pp += n;
    return true;
}

/* Skips one JSON value of any kind. */
static bool skip_value(const char **pp, const char *end, int depth)
{
    const char *p = *pp;
    bool trunc;
    bool is_int;
    int64_t v;

    if (depth > SKIP_DEPTH_MAX || p >= end) {
        return false;
    }
    switch (*p) {
    case '"':
        if (!parse_string(&p, end, NULL, 0, &trunc)) {
            return false;
        }
        break;
    case 't':
        if (!match_literal(&p, end, "true")) {
            return false;
        }
        break;
    case 'f':
        if (!match_literal(&p, end, "false")) {
            return false;
        }
        break;
    case 'n':
        if (!match_literal(&p, end, "null")) {
            return false;
        }
        break;
    case '{':
        p = skip_ws(p + 1, end);
        if (p < end && *p == '}') {
            p++;
            break;
        }
        for (;;) {
            p = skip_ws(p, end);
            if (p >= end || *p != '"' || !parse_string(&p, end, NULL, 0, &trunc)) {
                return false;
            }
            p = skip_ws(p, end);
            if (p >= end || *p != ':') {
                return false;
            }
            p = skip_ws(p + 1, end);
            if (!skip_value(&p, end, depth + 1)) {
                return false;
            }
            p = skip_ws(p, end);
            if (p < end && *p == ',') {
                p++;
                continue;
            }
            if (p < end && *p == '}') {
                p++;
                break;
            }
            return false;
        }
        break;
    case '[':
        p = skip_ws(p + 1, end);
        if (p < end && *p == ']') {
            p++;
            break;
        }
        for (;;) {
            p = skip_ws(p, end);
            if (!skip_value(&p, end, depth + 1)) {
                return false;
            }
            p = skip_ws(p, end);
            if (p < end && *p == ',') {
                p++;
                continue;
            }
            if (p < end && *p == ']') {
                p++;
                break;
            }
            return false;
        }
        break;
    default:
        if (!parse_number(&p, end, &is_int, &v)) {
            return false;
        }
        break;
    }
    *pp = p;
    return true;
}

static json_field_t *find_field(const json_obj_t *o, const char *key)
{
    for (size_t i = 0; i < o->n; i++) {
        if (strcmp(o->f[i].key, key) == 0) {
            return (json_field_t *)&o->f[i];
        }
    }
    return NULL;
}

bool json_parse_object(const char *text, size_t len, json_obj_t *o)
{
    const char *p = text;
    const char *end = text + len;

    o->n = 0;
    p = skip_ws(p, end);
    if (p >= end || *p != '{') {
        return false;
    }
    p = skip_ws(p + 1, end);
    if (p < end && *p == '}') {
        p++;
    } else {
        for (;;) {
            char key[JSON_KEY_MAX + 1];
            bool key_trunc;
            json_field_t tmp;
            bool store = true;

            p = skip_ws(p, end);
            if (p >= end || *p != '"' || !parse_string(&p, end, key, JSON_KEY_MAX, &key_trunc)) {
                return false;
            }
            p = skip_ws(p, end);
            if (p >= end || *p != ':') {
                return false;
            }
            p = skip_ws(p + 1, end);
            if (p >= end) {
                return false;
            }

            memset(&tmp, 0, sizeof(tmp));
            memcpy(tmp.key, key, sizeof(tmp.key));
            if (*p == '"') {
                tmp.type = JV_STRING;
                if (!parse_string(&p, end, tmp.str, JSON_STR_MAX, &tmp.truncated)) {
                    return false;
                }
            } else if (*p == '-' || is_digit(*p)) {
                bool is_int;
                int64_t v;

                if (!parse_number(&p, end, &is_int, &v)) {
                    return false;
                }
                tmp.type = is_int ? JV_INT : JV_OTHER;
                tmp.num = v;
            } else {
                tmp.type = JV_OTHER;
                if (!skip_value(&p, end, 1)) {
                    return false;
                }
            }

            if (key_trunc) {
                store = false;          /* longer than any key we use: cannot be needed */
            } else if (find_field(o, tmp.key) != NULL) {
                return false;           /* duplicate key */
            }
            if (store) {
                if (o->n >= JSON_MAX_FIELDS) {
                    return false;
                }
                o->f[o->n++] = tmp;
            }

            p = skip_ws(p, end);
            if (p < end && *p == ',') {
                p++;
                continue;
            }
            if (p < end && *p == '}') {
                p++;
                break;
            }
            return false;
        }
    }
    p = skip_ws(p, end);
    return p == end;
}

json_get_t json_get_str(const json_obj_t *o, const char *key, const char **out)
{
    const json_field_t *f = find_field(o, key);

    if (f == NULL) {
        return JF_MISSING;
    }
    if (f->type != JV_STRING || f->truncated) {
        return JF_WRONG_TYPE;
    }
    *out = f->str;
    return JF_OK;
}

json_get_t json_get_u32(const json_obj_t *o, const char *key, uint32_t *out)
{
    const json_field_t *f = find_field(o, key);

    if (f == NULL) {
        return JF_MISSING;
    }
    if (f->type != JV_INT) {
        return JF_WRONG_TYPE;
    }
    if (f->num < 0 || f->num > (int64_t)UINT32_MAX) {
        return JF_OUT_OF_RANGE;
    }
    *out = (uint32_t)f->num;
    return JF_OK;
}
