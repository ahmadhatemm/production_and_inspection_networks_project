#ifndef JSONMINI_H
#define JSONMINI_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* A small reader for the flat JSON objects of the TCP link, for example
 *
 *     {"type":"JOB_REQ","job_id":28,"part_id":8}
 *
 * It is strict about what it needs and tolerant about the rest:
 *   - the text must be exactly one JSON object (anything after it is an error);
 *   - strings, integers, true/false/null, nested objects and arrays are all
 *     accepted, but nested values are skipped, not stored;
 *   - a key that appears twice is an error (the meaning would be ambiguous);
 *   - a field this firmware does not know is simply ignored.
 *
 * It does no allocation and keeps no pointers into the text. */

#define JSON_MAX_FIELDS  16
#define JSON_KEY_MAX     24
#define JSON_STR_MAX     48

typedef enum {
    JV_STRING,
    JV_INT,         /* a number without fraction or exponent that fits in int64 (or was clamped, see below) */
    JV_OTHER        /* decimal number, true, false, null, object, array */
} json_vtype_t;

typedef struct {
    char         key[JSON_KEY_MAX + 1];
    json_vtype_t type;
    bool         truncated;             /* string longer than JSON_STR_MAX: only the start is kept */
    char         str[JSON_STR_MAX + 1];
    int64_t      num;                   /* integers beyond int64 are clamped to INT64_MIN / INT64_MAX */
} json_field_t;

typedef struct {
    json_field_t f[JSON_MAX_FIELDS];
    size_t       n;
} json_obj_t;

/* Returns true if text[0..len) is one valid JSON object. On false the contents
 * of *o are not meaningful. */
bool json_parse_object(const char *text, size_t len, json_obj_t *o);

typedef enum {
    JF_OK = 0,
    JF_MISSING,         /* no such key                                          */
    JF_WRONG_TYPE,      /* the key is there but is not a string / not an integer */
    JF_OUT_OF_RANGE     /* an integer, but negative or above 4294967295          */
} json_get_t;

/* *out points into o and stays valid as long as o does. A string that was cut
 * short counts as the wrong type. */
json_get_t json_get_str(const json_obj_t *o, const char *key, const char **out);

json_get_t json_get_u32(const json_obj_t *o, const char *key, uint32_t *out);

#endif
