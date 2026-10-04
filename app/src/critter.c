/* Drawing the agent's critter from its persona (see plugin/persona.py for the format). */
#include "app.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *FRAME_NAMES[FRAME_COUNT] = {"idle", "blink", "talk", "think", "happy", "sleep"};

static uint32_t parse_hex(const char *s)
{
    if (!s || s[0] != '#' || strlen(s) < 7) return 0;
    return (uint32_t)strtoul(s + 1, NULL, 16) & 0xFFFFFF;
}

static uint32_t palette(const cJSON *pal, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(pal, key);
    return cJSON_IsString(v) ? parse_hex(v->valuestring) : 0;
}

bool critter_load(critter_t *c, const cJSON *persona)
{
    const cJSON *sprite = cJSON_GetObjectItemCaseSensitive(persona, "sprite");
    const cJSON *frames = cJSON_GetObjectItemCaseSensitive(sprite, "frames");
    const cJSON *pal = cJSON_GetObjectItemCaseSensitive(persona, "palette");
    const cJSON *legend = cJSON_GetObjectItemCaseSensitive(persona, "legend");
    const cJSON *w = cJSON_GetObjectItemCaseSensitive(sprite, "w");
    const cJSON *h = cJSON_GetObjectItemCaseSensitive(sprite, "h");
    if (!cJSON_IsObject(frames) || !cJSON_IsObject(pal) || !cJSON_IsNumber(w) || !cJSON_IsNumber(h)) return false;
    if (w->valueint < 4 || w->valueint > CRITTER_MAX || h->valueint < 4 || h->valueint > CRITTER_MAX) return false;

    critter_t next;
    memset(&next, 0, sizeof next);
    next.w = w->valueint;
    next.h = h->valueint;
    const cJSON *name = cJSON_GetObjectItemCaseSensitive(persona, "name");
    const cJSON *species = cJSON_GetObjectItemCaseSensitive(persona, "species");
    snprintf(next.name, sizeof next.name, "%s", cJSON_IsString(name) ? name->valuestring : "Hermes");
    snprintf(next.species, sizeof next.species, "%s", cJSON_IsString(species) ? species->valuestring : "");
    next.body = palette(pal, "body");
    next.shade = palette(pal, "shade");
    next.outline = palette(pal, "outline");
    next.accent = palette(pal, "accent");

    /* Legend character -> colour, resolved once. */
    uint32_t colour[128];
    bool solid[128];
    memset(solid, 0, sizeof solid);
    const cJSON *entry;
    cJSON_ArrayForEach(entry, legend) {
        if (!entry->string || !entry->string[0] || (unsigned char)entry->string[0] > 127) continue;
        if (!cJSON_IsString(entry)) continue; /* null means see-through */
        const cJSON *value = cJSON_GetObjectItemCaseSensitive(pal, entry->valuestring);
        if (!cJSON_IsString(value)) continue;
        colour[(unsigned char)entry->string[0]] = parse_hex(value->valuestring);
        solid[(unsigned char)entry->string[0]] = true;
    }

    const cJSON *idle = cJSON_GetObjectItemCaseSensitive(frames, "idle");
    for (int f = 0; f < FRAME_COUNT; f++) {
        const cJSON *rows = cJSON_GetObjectItemCaseSensitive(frames, FRAME_NAMES[f]);
        if (!cJSON_IsArray(rows)) rows = idle;
        if (!cJSON_IsArray(rows)) return false;
        int y = 0;
        const cJSON *row;
        cJSON_ArrayForEach(row, rows) {
            if (y >= next.h || !cJSON_IsString(row)) break;
            const char *text = row->valuestring;
            for (int x = 0; x < next.w && text[x]; x++) {
                unsigned char ch = (unsigned char)text[x];
                if (ch < 128 && solid[ch]) next.px[f][y * CRITTER_MAX + x] = 0xFF000000u | colour[ch];
            }
            y++;
        }
    }
    next.loaded = true;
    *c = next;
    return true;
}

typedef struct {
    int scale;
    lv_draw_buf_t *buf;
} canvas_info_t;

static void canvas_deleted(lv_event_t *e)
{
    canvas_info_t *info = lv_event_get_user_data(e);
    if (info->buf) lv_draw_buf_destroy(info->buf);
    lv_free(info);
}

lv_obj_t *critter_create(lv_obj_t *parent, int scale)
{
    lv_obj_t *canvas = lv_canvas_create(parent);
    canvas_info_t *info = lv_malloc(sizeof *info);
    info->scale = scale;
    info->buf = lv_draw_buf_create(16 * scale, 16 * scale, LV_COLOR_FORMAT_ARGB8888, 0);
    lv_canvas_set_draw_buf(canvas, info->buf);
    lv_canvas_fill_bg(canvas, lv_color_black(), LV_OPA_TRANSP);
    lv_obj_set_user_data(canvas, info);
    lv_obj_add_event_cb(canvas, canvas_deleted, LV_EVENT_DELETE, info);
    lv_obj_remove_flag(canvas, LV_OBJ_FLAG_CLICKABLE);
    return canvas;
}

void critter_draw(lv_obj_t *canvas, const critter_t *c, critter_frame_t frame)
{
    canvas_info_t *info = lv_obj_get_user_data(canvas);
    if (!info || !info->buf) return;
    int scale = info->scale;
    int side = 16 * scale;
    uint8_t *data = info->buf->data;
    uint32_t stride = info->buf->header.stride;
    memset(data, 0, (size_t)stride * (size_t)side);
    if (c->loaded) {
        /* Sprites smaller than 16 are centred; larger ones are cropped. */
        int ox = (16 - (c->w < 16 ? c->w : 16)) / 2, oy = (16 - (c->h < 16 ? c->h : 16)) / 2;
        for (int y = 0; y < c->h && y < 16; y++) {
            for (int x = 0; x < c->w && x < 16; x++) {
                uint32_t argb = c->px[frame][y * CRITTER_MAX + x];
                if (!(argb >> 24)) continue;
                for (int dy = 0; dy < scale; dy++) {
                    uint8_t *p = data + (size_t)((y + oy) * scale + dy) * stride + (size_t)((x + ox) * scale) * 4;
                    for (int dx = 0; dx < scale; dx++, p += 4) {
                        p[0] = (uint8_t)(argb & 0xFF);         /* blue */
                        p[1] = (uint8_t)((argb >> 8) & 0xFF);  /* green */
                        p[2] = (uint8_t)((argb >> 16) & 0xFF); /* red */
                        p[3] = 0xFF;
                    }
                }
            }
        }
    }
    lv_obj_invalidate(canvas);
}
