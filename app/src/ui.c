/* The screens.
 *
 * The app is a place to go to rather than a chat log: the critter and what it
 * is doing sit to one side, the exchange happening now is set large, and what
 * came before recedes above it. Typing anywhere goes into the prompt; the
 * microphone and files are one tap away.
 *
 * Everything is rebuilt from a small model (the conversation, the service
 * state and the "look") whenever the look or the critter changes, so every
 * colour, face and size is decided in one place: apply_look().
 */
#include "app.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_CHAT 200
#define MAX_PENDING 8
#define MAX_ROWS 40
#define MAX_SWATCHES 16
#define MAX_TTF 24

enum { ROLE_USER, ROLE_AGENT, ROLE_NOTE };

typedef struct {
    int role;
    char *text;
} entry_t;

static struct {
    /* What the service told us. */
    bool have_hello;
    char state[16], mode[16], device[64], url[160], link_error[200], session_title[96], status[160];
    char pair_code[24], pair_command[160], inbox[200];
    bool busy, mic_available, speaker_available, recording;
    int mic_level;
    critter_t critter;
    entry_t chat[MAX_CHAT];
    int chat_len;
    char *pending[MAX_PENDING];
    int pending_len;
    /* The look (docs/local-api.md): shared settings kept on Hermes. */
    char theme_pref[8], font_kind[300], greeting[96], corners[8], critter_size[8], accent_hex[8];
    bool fun, speak, has_accent, has_bg, has_surface, has_text;
    uint32_t look_accent, look_bg, look_surface, look_text;
    int text_px, fresh_min;
    /* Naming and colouring the critter. */
    bool persona_chosen, meet_skipped;
    struct { char name[16]; char hex[8]; uint32_t rgb; } swatches[MAX_SWATCHES];
    int swatch_count;
    critter_t preview;
    char meet_colour[8];
    /* History browser. */
    struct { char id[64], source[16], title[96]; double last_active; bool current; } rows[MAX_ROWS];
    int row_count;
    char viewing_id[64];
    /* The reply being written out. */
    char *reveal;
    size_t reveal_len, reveal_shown;
    bool reveal_final;
    /* Presentation, all derived in apply_look(). */
    bool dark, small, wide, touch, settings_open;
    uint32_t bg, surface, text, muted, line, accent, on_accent, past, agent_box;
    int radius, stage_w;
    const lv_font_t *font, *font_small, *font_reply, *font_big, *font_head, *font_label;
    critter_frame_t frame;
    int tick;
    time_t happy_until, toast_until;
    char open_screen[96];
    char prompt_id[64];
    char draft[1024];
    /* Setting up: where Hermes is. */
    bool editing_address;
    char setup_url[160], setup_name[64];
    int service_status;
} S;

static struct {
    lv_obj_t *root, *critter, *name, *status, *stage, *stream_label, *input, *send, *send_label, *mic, *mic_icon, *level, *pending_row;
    lv_obj_t *gate_user, *gate_pass, *gate_error, *gate_url, *gate_name;
    lv_obj_t *overlay, *overlay_list, *overlay_search, *prompt, *prompt_input, *toast, *keyboard, *hero;
    lv_obj_t *meet_critter, *meet_name, *swatch[MAX_SWATCHES];
} W;

static struct { char path[300]; int size; lv_font_t *font; bool pinned; } g_ttf[MAX_TTF];

static void build(void);
static void show_history(void);
static void show_meet(void);
static void settings_clicked(lv_event_t *e);

/* -- small helpers -------------------------------------------------------------- */

static const char *jstr(const cJSON *obj, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return cJSON_IsString(v) && v->valuestring ? v->valuestring : "";
}

static bool jbool(const cJSON *obj, const char *key)
{
    return cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(obj, key));
}

static void copy(char *dst, size_t size, const char *src)
{
    snprintf(dst, size, "%s", src ? src : "");
}

static bool is(const char *a, const char *b) { return strcmp(a, b) == 0; }

static lv_color_t col(uint32_t rgb) { return lv_color_hex(rgb); }

static uint32_t mix(uint32_t a, uint32_t b, int percent_b)
{
    uint32_t out = 0;
    for (int shift = 16; shift >= 0; shift -= 8) {
        int ca = (int)((a >> shift) & 0xFF), cb = (int)((b >> shift) & 0xFF);
        out |= (uint32_t)((ca * (100 - percent_b) + cb * percent_b) / 100) << shift;
    }
    return out;
}

static bool hex_colour(const cJSON *obj, const char *key, uint32_t *out)
{
    const char *text = jstr(obj, key);
    if (text[0] != '#' || strlen(text) != 7) return false;
    *out = (uint32_t)strtoul(text + 1, NULL, 16) & 0xFFFFFF;
    return true;
}

static void op_simple(const char *name)
{
    cJSON *op = cJSON_CreateObject();
    cJSON_AddStringToObject(op, "op", name);
    ipc_send(op);
}

static void chat_push(int role, const char *text)
{
    if (!text || !*text) return;
    if (S.chat_len == MAX_CHAT) {
        free(S.chat[0].text);
        memmove(&S.chat[0], &S.chat[1], sizeof(entry_t) * (MAX_CHAT - 1));
        S.chat_len--;
    }
    S.chat[S.chat_len].role = role;
    S.chat[S.chat_len].text = strdup(text);
    S.chat_len++;
}

static void chat_clear(void)
{
    for (int i = 0; i < S.chat_len; i++) free(S.chat[i].text);
    S.chat_len = 0;
}

static void chat_load(const cJSON *chat)
{
    chat_clear();
    const cJSON *m;
    cJSON_ArrayForEach(m, chat) {
        const char *text = jstr(m, "text");
        const cJSON *files = cJSON_GetObjectItemCaseSensitive(m, "files");
        int role = is(jstr(m, "role"), "user") ? ROLE_USER : ROLE_AGENT;
        if (cJSON_IsArray(files) && cJSON_GetArraySize(files) > 0) {
            char line[512];
            int n = snprintf(line, sizeof line, "%s%s", text, *text ? "\n" : "");
            const cJSON *f;
            cJSON_ArrayForEach(f, files) {
                if (n < (int)sizeof line - 8) n += snprintf(line + n, sizeof line - (size_t)n, LV_SYMBOL_FILE " %s  ", jstr(f, "name"));
            }
            chat_push(role, line);
        } else {
            chat_push(role, text);
        }
    }
}

/* -- the look: fonts, colours, sizes ------------------------------------------------ */

static const struct { int px; const lv_font_t *font; } BUILTIN[] = {
    {12, &lv_font_montserrat_12}, {14, &lv_font_montserrat_14}, {16, &lv_font_montserrat_16},
    {18, &lv_font_montserrat_18}, {20, &lv_font_montserrat_20}, {22, &lv_font_montserrat_22},
    {24, &lv_font_montserrat_24}, {26, &lv_font_montserrat_26}, {28, &lv_font_montserrat_28},
};
#define BUILTIN_COUNT (sizeof BUILTIN / sizeof BUILTIN[0])

static size_t builtin_index(int px)
{
    size_t best = 0;
    for (size_t i = 0; i < BUILTIN_COUNT; i++) {
        if (BUILTIN[i].px <= px) best = i;
    }
    return best;
}

static const char *first_readable(const char *const *paths)
{
    for (; *paths; paths++) {
        if (access(*paths, R_OK) == 0) return *paths;
    }
    return NULL;
}

static const lv_font_t *ttf(const char *path, int px, bool pinned);

/* The built-in face covers plain English and the icons. Everything else an agent writes (curly
 * quotes, accents, other alphabets) comes from a font already on the device, when there is one. */
static const lv_font_t *builtin(int px)
{
    static const char *const sans[] = {
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf", NULL};
    static lv_font_t copies[BUILTIN_COUNT];
    static bool made[BUILTIN_COUNT];
    size_t i = builtin_index(px);
    if (!made[i]) {
        copies[i] = *BUILTIN[i].font;
        copies[i].fallback = ttf(first_readable(sans), BUILTIN[i].px, true);
        made[i] = true;
    }
    return &copies[i];
}

/* A TrueType face from the device's own fonts, at a size; NULL when it cannot be had. */
static const lv_font_t *ttf(const char *path, int px, bool pinned)
{
    if (!path) return NULL;
    int free_slot = -1;
    for (int i = 0; i < MAX_TTF; i++) {
        if (g_ttf[i].font && g_ttf[i].size == px && is(g_ttf[i].path, path)) return g_ttf[i].font;
        if (!g_ttf[i].font && free_slot < 0) free_slot = i;
    }
    if (free_slot < 0) return NULL; /* every slot holds a face that may still be on screen */
    char lv_path[320];
    snprintf(lv_path, sizeof lv_path, "A:%s", path);
    lv_font_t *font = lv_tiny_ttf_create_file(lv_path, px);
    if (!font) return NULL;
    if (!pinned) font->fallback = BUILTIN[builtin_index(px)].font; /* the icons live in the built-in face */
    copy(g_ttf[free_slot].path, sizeof g_ttf[0].path, path);
    g_ttf[free_slot].size = px;
    g_ttf[free_slot].font = font;
    g_ttf[free_slot].pinned = pinned;
    return font;
}

/* Faces from an earlier look are let go once nothing on screen uses them (right after a rebuild). */
static void ttf_release_unused(void)
{
    const lv_font_t *live[] = {S.font, S.font_small, S.font_reply, S.font_big, S.font_head, S.font_label};
    for (int i = 0; i < MAX_TTF; i++) {
        bool used = false;
        for (size_t j = 0; j < sizeof live / sizeof live[0]; j++) used = used || live[j] == g_ttf[i].font;
        if (g_ttf[i].font && !used && !g_ttf[i].pinned) {
            lv_tiny_ttf_destroy(g_ttf[i].font);
            g_ttf[i].font = NULL;
        }
    }
}

static const lv_font_t *face(int px)
{
    static const char *const mono[] = {
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf", "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
        "/System/Library/Fonts/Monaco.ttf", "/System/Library/Fonts/Supplemental/Andale Mono.ttf", NULL};
    static const char *const serif[] = {
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSerif.ttf", "/usr/share/fonts/dejavu/DejaVuSerif.ttf",
        "/System/Library/Fonts/Supplemental/Georgia.ttf", "/System/Library/Fonts/Supplemental/Times New Roman.ttf", NULL};
    const lv_font_t *found = NULL;
    if (is(S.font_kind, "pixel")) return px >= 14 ? &lv_font_unscii_16 : &lv_font_unscii_8;
    if (is(S.font_kind, "mono")) found = ttf(first_readable(mono), px, false);
    else if (is(S.font_kind, "serif")) found = ttf(first_readable(serif), px, false);
    else if (S.font_kind[0] == '/') found = ttf(access(S.font_kind, R_OK) == 0 ? S.font_kind : NULL, px, false);
    return found ? found : builtin(px);
}

static bool wants_dark(void)
{
    const char *forced = getenv("HERMLET_THEME"); /* this one screen only: a kiosk, or a picture for the docs */
    if (forced && is(forced, "dark")) return true;
    if (forced && is(forced, "light")) return false;
    if (is(S.theme_pref, "dark")) return true;
    if (is(S.theme_pref, "light")) return false;
    time_t now = time(NULL);
    struct tm local;
    localtime_r(&now, &local);
    return local.tm_hour < 7 || local.tm_hour >= 19; /* light by day, dark in the evening */
}

static void apply_look(void)
{
    lv_display_t *disp = lv_display_get_default();
    int width = lv_display_get_horizontal_resolution(disp), height = lv_display_get_vertical_resolution(disp);
    S.small = width < 520 || height < 360;
    S.wide = width >= 640 && height >= 360;
    S.dark = wants_dark();

    uint32_t bg = S.dark ? 0x1d1a21 : 0xf6efe6, surface = S.dark ? 0x2a2530 : 0xfffaf2, text = S.dark ? 0xf3ece4 : 0x2b2530;
    if (!S.fun) { /* the plain look is cooler and flatter */
        bg = S.dark ? 0x16181c : 0xf7f8fa;
        surface = S.dark ? 0x21252b : 0xffffff;
        text = S.dark ? 0xe9ecf1 : 0x1c2026;
    }
    S.bg = S.has_bg ? S.look_bg : bg;
    S.text = S.has_text ? S.look_text : text;
    S.surface = S.has_surface ? S.look_surface : (S.has_bg ? mix(S.bg, S.text, 8) : surface);
    S.muted = mix(S.text, S.bg, 42);
    S.past = mix(S.text, S.bg, 28);
    S.line = mix(S.bg, S.text, 16);
    S.agent_box = 0; /* set below, once the accent is known */
    S.accent = S.has_accent ? S.look_accent : (S.fun && S.critter.loaded && S.critter.body ? S.critter.body : 0x5f7fd9);
    unsigned r = (S.accent >> 16) & 0xFF, g = (S.accent >> 8) & 0xFF, b = S.accent & 0xFF;
    S.on_accent = (r * 299 + g * 587 + b * 114) / 1000 > 150 ? 0x1d1a21 : 0xffffff;
    S.radius = is(S.corners, "square") ? 0 : is(S.corners, "soft") ? 7 : 16;
    S.agent_box = mix(S.bg, S.accent, S.dark ? 20 : 16);

    int base = S.text_px > 0 ? S.text_px : 16;
    if (S.small && base > 12) base -= 2;
    if (height >= 1000) base += 6; /* a monitor is read from further away than a 5 inch panel */
    else if (height >= 680) base += 3;
    S.font = face(base);
    S.font_small = face(base - 2 < 10 ? 10 : base - 2);
    S.font_reply = face(S.small ? base + 2 : base + 4);
    S.font_big = face(S.small ? base + 4 : base + 8);
    bool pixel_heads = S.fun && !is(S.font_kind, "mono") && !is(S.font_kind, "serif") && S.font_kind[0] != '/';
    S.font_head = pixel_heads ? (S.small ? &lv_font_unscii_8 : &lv_font_unscii_16) : S.font_big;
    S.font_label = pixel_heads ? &lv_font_unscii_8 : S.font_small;
    lv_display_set_theme(disp, lv_theme_default_init(disp, col(S.accent), col(S.accent), S.dark, S.font));
}

static void take_look(const cJSON *look)
{
    if (!cJSON_IsObject(look)) return;
    const cJSON *size = cJSON_GetObjectItemCaseSensitive(look, "text_size");
    copy(S.theme_pref, sizeof S.theme_pref, *jstr(look, "theme") ? jstr(look, "theme") : "auto");
    copy(S.font_kind, sizeof S.font_kind, *jstr(look, "font") ? jstr(look, "font") : "rounded");
    copy(S.corners, sizeof S.corners, *jstr(look, "corners") ? jstr(look, "corners") : "round");
    copy(S.critter_size, sizeof S.critter_size, *jstr(look, "critter_size") ? jstr(look, "critter_size") : "large");
    copy(S.greeting, sizeof S.greeting, jstr(look, "greeting"));
    copy(S.accent_hex, sizeof S.accent_hex, jstr(look, "accent"));
    S.fun = !cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(look, "fun"));
    S.speak = jbool(look, "speak");
    S.has_accent = hex_colour(look, "accent", &S.look_accent);
    S.has_bg = hex_colour(look, "background", &S.look_bg);
    S.has_surface = hex_colour(look, "surface", &S.look_surface);
    S.has_text = hex_colour(look, "text", &S.look_text);
    const cJSON *fresh = cJSON_GetObjectItemCaseSensitive(look, "fresh_after_min");
    S.fresh_min = cJSON_IsNumber(fresh) ? fresh->valueint : 20;
    if (cJSON_IsNumber(size)) S.text_px = size->valueint;
    else S.text_px = is(jstr(look, "text_size"), "small") ? 14 : is(jstr(look, "text_size"), "large") ? 20 : 16;
}

/* -- widget builders -------------------------------------------------------------- */

static lv_obj_t *box(lv_obj_t *parent, lv_flex_flow_t flow)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_flex_flow(o, flow);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    return o;
}

static lv_obj_t *label(lv_obj_t *parent, const char *text, const lv_font_t *font, uint32_t colour)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_text(l, text);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, col(colour), 0);
    return l;
}

static lv_obj_t *wrapped(lv_obj_t *parent, const char *text, const lv_font_t *font, uint32_t colour)
{
    lv_obj_t *l = label(parent, text, font, colour);
    lv_obj_set_width(l, lv_pct(100));
    lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
    return l;
}

static lv_obj_t *button(lv_obj_t *parent, const char *text, bool filled, lv_event_cb_t cb, void *user)
{
    lv_obj_t *b = lv_button_create(parent);
    int pad = S.small ? 8 : 11;
    lv_obj_set_style_bg_color(b, col(filled ? S.accent : S.surface), 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(b, S.radius > 12 ? 12 : S.radius, 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_set_style_border_width(b, filled ? 0 : 1, 0);
    lv_obj_set_style_border_color(b, col(S.line), 0);
    lv_obj_set_style_pad_hor(b, pad + 3, 0);
    lv_obj_set_style_pad_ver(b, pad, 0);
    lv_obj_set_style_min_width(b, S.small ? 40 : 46, 0);
    if (S.touch) { /* a finger needs more room than a pointer */
        lv_obj_set_style_min_width(b, S.small ? 48 : 60, 0);
        lv_obj_set_style_min_height(b, S.small ? 44 : 56, 0);
        lv_obj_set_style_pad_hor(b, pad + 6, 0);
    }
    lv_obj_t *l = label(b, text, S.touch && !S.small ? S.font_reply : S.font, filled ? S.on_accent : S.text);
    lv_obj_center(l);
    if (cb) lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user);
    /* Buttons stay out of the keyboard group, so whatever is typed always lands in a text box. */
    lv_group_remove_obj(b);
    return b;
}

static lv_obj_t *field(lv_obj_t *parent, const char *placeholder, bool secret)
{
    lv_obj_t *ta = lv_textarea_create(parent);
    lv_textarea_set_one_line(ta, true);
    lv_textarea_set_placeholder_text(ta, placeholder);
    lv_textarea_set_password_mode(ta, secret);
    lv_obj_set_style_bg_color(ta, col(S.surface), 0);
    lv_obj_set_style_text_color(ta, col(S.text), 0);
    lv_obj_set_style_text_font(ta, S.font, 0);
    lv_obj_set_style_border_color(ta, col(S.line), 0);
    lv_obj_set_style_border_color(ta, col(S.accent), LV_STATE_FOCUSED);
    lv_obj_set_style_border_width(ta, 1, 0);
    lv_obj_set_style_radius(ta, S.radius > 14 ? 14 : S.radius, 0);
    lv_obj_set_style_text_color(ta, col(S.muted), LV_PART_TEXTAREA_PLACEHOLDER);
    if (S.touch) lv_obj_set_style_pad_ver(ta, S.small ? 11 : 16, 0);
    return ta;
}

static void opa_step(void *obj, int32_t value) { lv_obj_set_style_opa(obj, (lv_opa_t)value, 0); }

static void hop_step(void *obj, int32_t value) { lv_obj_set_style_translate_y(obj, value, 0); }

/* New things arrive gently: they fade in while rising into place. */
static void appear(lv_obj_t *obj)
{
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, obj);
    lv_anim_set_values(&a, 0, 255);
    lv_anim_set_duration(&a, 320);
    lv_anim_set_exec_cb(&a, opa_step);
    lv_anim_start(&a);
    lv_anim_init(&a);
    lv_anim_set_var(&a, obj);
    lv_anim_set_values(&a, 22, 0);
    lv_anim_set_duration(&a, 380);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_anim_set_exec_cb(&a, hop_step);
    lv_anim_start(&a);
}

static void hop(lv_obj_t *obj)
{
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, obj);
    lv_anim_set_values(&a, 0, -8);
    lv_anim_set_duration(&a, 160);
    lv_anim_set_playback_duration(&a, 160);
    lv_anim_set_repeat_count(&a, 2);
    lv_anim_set_exec_cb(&a, hop_step);
    lv_anim_start(&a);
}

static void scroll_to_end(lv_obj_t *list)
{
    lv_obj_update_layout(list);
    lv_obj_scroll_to_y(list, LV_COORD_MAX, LV_ANIM_OFF);
}

/* One thing said. `current` is the exchange happening now, set large; the rest recede. */
static lv_obj_t *say(lv_obj_t *list, int role, const char *text, bool current)
{
    if (role == ROLE_NOTE) {
        lv_obj_t *n = wrapped(list, text, S.font_small, S.muted);
        lv_obj_set_style_text_align(n, LV_TEXT_ALIGN_CENTER, 0);
        return n;
    }
    /* What you said sits in a plain box; what the agent said in one tinted with the accent.
     * Both run down the page in the order they happened. */
    bool mine = role == ROLE_USER;
    int pad_h = S.small ? 10 : current ? 18 : 14, pad_v = S.small ? 7 : current ? 13 : 9;
    lv_obj_t *card = lv_obj_create(list);
    lv_obj_remove_style_all(card);
    lv_obj_set_size(card, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(card, col(mine ? S.surface : S.agent_box), 0);
    lv_obj_set_style_bg_opa(card, current ? LV_OPA_COVER : LV_OPA_60, 0);
    lv_obj_set_style_border_width(card, mine ? 1 : 0, 0);
    lv_obj_set_style_border_color(card, col(S.line), 0);
    lv_obj_set_style_radius(card, S.radius, 0);
    lv_obj_set_style_pad_hor(card, pad_h, 0);
    lv_obj_set_style_pad_ver(card, pad_v, 0);
    if (mine && list == W.stage) lv_obj_set_style_margin_top(card, current ? 10 : 4, 0);
    lv_obj_t *l = label(card, text, current ? (mine ? S.font_big : S.font_reply) : S.font, current ? S.text : S.past);
    lv_obj_set_style_max_width(l, (S.stage_w > 120 ? S.stage_w : lv_display_get_horizontal_resolution(NULL)) - 2 * pad_h - 4, 0);
    lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_line_space(l, current ? 6 : 3, 0);
    return l;
}

/* The box a spoken line sits in (or the line itself, for a note). */
static lv_obj_t *box_of(lv_obj_t *line)
{
    lv_obj_t *parent = lv_obj_get_parent(line);
    return parent == W.stage || !parent ? line : parent;
}

/* A plain block for sheets that show someone else's conversation. */
static lv_obj_t *bubble(lv_obj_t *list, int role, const char *text)
{
    lv_obj_t *row = box(list, LV_FLEX_FLOW_ROW);
    lv_obj_set_width(row, lv_pct(100));
    lv_obj_set_height(row, LV_SIZE_CONTENT);
    lv_obj_set_flex_align(row, role == ROLE_USER ? LV_FLEX_ALIGN_END : LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_t *b = lv_obj_create(row);
    lv_obj_remove_style_all(b);
    lv_obj_set_size(b, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(b, col(role == ROLE_USER ? S.accent : S.surface), 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(b, S.radius, 0);
    lv_obj_set_style_pad_hor(b, S.small ? 10 : 14, 0);
    lv_obj_set_style_pad_ver(b, S.small ? 7 : 10, 0);
    lv_obj_t *l = label(b, text, S.font, role == ROLE_USER ? S.on_accent : S.text);
    lv_obj_set_style_max_width(l, lv_display_get_horizontal_resolution(NULL) * (S.small ? 80 : 72) / 100, 0);
    lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_line_space(l, 4, 0);
    return l;
}

/* -- toasts ----------------------------------------------------------------------- */

static void toast(const char *title, const char *text, int seconds)
{
    if (W.toast) lv_obj_delete(W.toast);
    W.toast = box(lv_screen_active(), LV_FLEX_FLOW_COLUMN);
    lv_obj_set_width(W.toast, lv_pct(S.small ? 94 : 56));
    lv_obj_set_height(W.toast, LV_SIZE_CONTENT);
    lv_obj_align(W.toast, LV_ALIGN_BOTTOM_MID, 0, S.small ? -56 : -84); /* above the prompt, clear of every button */
    lv_obj_set_style_bg_color(W.toast, col(S.surface), 0);
    lv_obj_set_style_bg_opa(W.toast, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(W.toast, col(S.accent), 0);
    lv_obj_set_style_border_width(W.toast, 2, 0);
    lv_obj_set_style_radius(W.toast, S.radius > 14 ? 14 : S.radius, 0);
    lv_obj_set_style_pad_all(W.toast, 12, 0);
    lv_obj_set_style_pad_row(W.toast, 4, 0);
    if (title && *title) label(W.toast, title, S.font_label, S.muted);
    wrapped(W.toast, text, S.font, S.text);
    appear(W.toast);
    S.toast_until = time(NULL) + (seconds > 0 ? seconds : 8);
}

/* -- on-screen keyboard (touch screens) --------------------------------------------- */

static void keyboard_hide(void)
{
    if (!W.keyboard) return;
    lv_obj_add_flag(W.keyboard, LV_OBJ_FLAG_HIDDEN);
    lv_keyboard_set_textarea(W.keyboard, NULL);
    if (W.root) lv_obj_set_height(W.root, lv_pct(100));
}

static void keyboard_event(lv_event_t *e)
{
    keyboard_hide();
}

static void field_focus_event(lv_event_t *e)
{
    lv_obj_t *ta = lv_event_get_target(e);
    if (!S.touch) return;
    if (lv_event_get_code(e) == LV_EVENT_FOCUSED) {
        if (!W.keyboard) {
            W.keyboard = lv_keyboard_create(lv_screen_active());
            lv_obj_set_size(W.keyboard, lv_pct(100), lv_pct(45));
            lv_obj_add_event_cb(W.keyboard, keyboard_event, LV_EVENT_READY, NULL);
            lv_obj_add_event_cb(W.keyboard, keyboard_event, LV_EVENT_CANCEL, NULL);
        }
        lv_keyboard_set_textarea(W.keyboard, ta);
        lv_obj_remove_flag(W.keyboard, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(W.keyboard);
        if (W.root) lv_obj_set_height(W.root, lv_pct(55));
        if (W.stage) scroll_to_end(W.stage);
    } else {
        keyboard_hide();
    }
}

static void wire_field(lv_obj_t *ta)
{
    lv_obj_add_event_cb(ta, field_focus_event, LV_EVENT_FOCUSED, NULL);
    lv_obj_add_event_cb(ta, field_focus_event, LV_EVENT_DEFOCUSED, NULL);
}

/* -- sending ------------------------------------------------------------------------ */

static void refresh_pending(void);
static void refresh_status(void);

static void send_now(void)
{
    const char *text = W.input ? lv_textarea_get_text(W.input) : "";
    if ((!text || !*text) && !S.pending_len) return;
    cJSON *op = cJSON_CreateObject();
    cJSON_AddStringToObject(op, "op", "send");
    cJSON_AddStringToObject(op, "req", "send");
    cJSON_AddStringToObject(op, "text", text ? text : "");
    cJSON *files = cJSON_AddArrayToObject(op, "files");
    for (int i = 0; i < S.pending_len; i++) cJSON_AddItemToArray(files, cJSON_CreateString(S.pending[i]));
    if (!ipc_send(op)) {
        toast("", "Hermlet is not running, so that was not sent.", 6);
        return;
    }
    for (int i = 0; i < S.pending_len; i++) free(S.pending[i]);
    S.pending_len = 0;
    S.draft[0] = '\0';
    if (W.input) lv_textarea_set_text(W.input, "");
    refresh_pending();
}

static void send_clicked(lv_event_t *e)
{
    if (S.busy && !*lv_textarea_get_text(W.input) && !S.pending_len) op_simple("cancel");
    else send_now();
}

static void input_ready(lv_event_t *e) { send_now(); }

static void input_changed(lv_event_t *e)
{
    copy(S.draft, sizeof S.draft, lv_textarea_get_text(W.input));
    if (W.send_label) lv_label_set_text(W.send_label, S.busy && !S.draft[0] && !S.pending_len ? LV_SYMBOL_STOP : LV_SYMBOL_RIGHT);
}

/* A microphone drawn from four shapes: the icon font has none. */
static lv_obj_t *mic_draw(lv_obj_t *parent, int h)
{
    int head_w = h * 4 / 10, head_h = h * 6 / 10, cup_h = h * 4 / 10;
    lv_obj_t *icon = box(parent, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_layout(icon, LV_LAYOUT_NONE);
    lv_obj_set_size(icon, head_w + 10, h);
    lv_obj_center(icon);
    lv_obj_remove_flag(icon, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_t *head = lv_obj_create(icon), *cup = lv_obj_create(icon), *stem = lv_obj_create(icon), *foot = lv_obj_create(icon);
    lv_obj_t *parts[] = {head, cup, stem, foot};
    for (int i = 0; i < 4; i++) {
        lv_obj_remove_style_all(parts[i]);
        lv_obj_remove_flag(parts[i], LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_style_bg_opa(parts[i], i == 1 ? LV_OPA_TRANSP : LV_OPA_COVER, 0);
    }
    lv_obj_set_size(head, head_w, head_h);
    lv_obj_set_style_radius(head, LV_RADIUS_CIRCLE, 0);
    lv_obj_align(head, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_set_size(cup, head_w + 10, cup_h);
    lv_obj_set_style_radius(cup, cup_h, 0);
    lv_obj_set_style_border_width(cup, 2, 0);
    lv_obj_set_style_border_side(cup, LV_BORDER_SIDE_BOTTOM | LV_BORDER_SIDE_LEFT | LV_BORDER_SIDE_RIGHT, 0);
    lv_obj_align(cup, LV_ALIGN_TOP_MID, 0, head_h - cup_h * 6 / 10);
    lv_obj_set_size(stem, 2, h - (head_h + cup_h * 4 / 10) - 2);
    lv_obj_align(stem, LV_ALIGN_BOTTOM_MID, 0, -2);
    lv_obj_set_size(foot, head_w + 2, 2);
    lv_obj_align(foot, LV_ALIGN_BOTTOM_MID, 0, 0);
    return icon;
}

static void mic_paint(uint32_t colour)
{
    if (!W.mic_icon) return;
    for (uint32_t i = 0; i < lv_obj_get_child_count(W.mic_icon); i++) {
        lv_obj_t *part = lv_obj_get_child(W.mic_icon, (int32_t)i);
        lv_obj_set_style_bg_color(part, col(colour), 0);
        lv_obj_set_style_border_color(part, col(colour), 0);
    }
}

/* One tap starts listening; the next sends what was said. */
static void mic_clicked(lv_event_t *e)
{
    op_simple(S.recording ? "talk.stop" : "talk.start");
}

static void new_clicked(lv_event_t *e) { op_simple("new"); }

/* -- attachments ---------------------------------------------------------------------- */

static void pending_remove(lv_event_t *e)
{
    int index = (int)(intptr_t)lv_event_get_user_data(e);
    if (index < 0 || index >= S.pending_len) return;
    free(S.pending[index]);
    memmove(&S.pending[index], &S.pending[index + 1], sizeof(char *) * (size_t)(S.pending_len - index - 1));
    S.pending_len--;
    refresh_pending();
}

static void refresh_pending(void)
{
    if (!W.pending_row) return;
    lv_obj_clean(W.pending_row);
    if (!S.pending_len) {
        lv_obj_add_flag(W.pending_row, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    lv_obj_remove_flag(W.pending_row, LV_OBJ_FLAG_HIDDEN);
    for (int i = 0; i < S.pending_len; i++) {
        const char *base = strrchr(S.pending[i], '/');
        char text[96];
        snprintf(text, sizeof text, LV_SYMBOL_FILE " %.60s  " LV_SYMBOL_CLOSE, base ? base + 1 : S.pending[i]);
        lv_obj_t *chip = button(W.pending_row, text, false, pending_remove, (void *)(intptr_t)i);
        lv_obj_set_style_pad_ver(chip, 6, 0);
    }
    if (W.send_label) lv_label_set_text(W.send_label, LV_SYMBOL_RIGHT);
}

void ui_add_attachment(const char *path)
{
    if (!path || !*path) return;
    if (S.pending_len == MAX_PENDING) {
        toast("", "That is as many files as one message can carry.", 5);
        return;
    }
    S.pending[S.pending_len++] = strdup(path);
    refresh_pending();
}

static void overlay_close(void)
{
    if (W.overlay) lv_obj_delete(W.overlay);
    W.overlay = W.overlay_list = W.overlay_search = W.meet_critter = W.meet_name = NULL;
    memset(W.swatch, 0, sizeof W.swatch);
    S.settings_open = false;
    keyboard_hide();
    if (W.input && !S.touch) lv_group_focus_obj(W.input); /* typing goes straight back to the prompt */
}

static void overlay_close_event(lv_event_t *e) { overlay_close(); }

/* A full-screen sheet with a title and a back button; returns its content column. */
static lv_obj_t *overlay_open(const char *title, lv_obj_t **header_out)
{
    overlay_close();
    W.overlay = box(lv_screen_active(), LV_FLEX_FLOW_COLUMN);
    lv_obj_set_size(W.overlay, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(W.overlay, col(S.bg), 0);
    lv_obj_set_style_bg_opa(W.overlay, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(W.overlay, S.small ? 8 : 16, 0);
    lv_obj_set_style_pad_row(W.overlay, S.small ? 8 : 12, 0);
    lv_obj_t *header = box(W.overlay, LV_FLEX_FLOW_ROW);
    lv_obj_set_size(header, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_align(header, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(header, 8, 0);
    button(header, LV_SYMBOL_LEFT, false, overlay_close_event, NULL);
    lv_obj_t *t = label(header, title, S.font_head, S.text);
    lv_obj_set_flex_grow(t, 1);
    lv_label_set_long_mode(t, LV_LABEL_LONG_DOT);
    if (header_out) *header_out = header;
    lv_obj_t *content = box(W.overlay, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_width(content, lv_pct(100));
    lv_obj_set_flex_grow(content, 1);
    lv_obj_add_flag(content, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_scroll_dir(content, LV_DIR_VER);
    lv_obj_set_style_pad_row(content, S.small ? 6 : 10, 0);
    return content;
}

static void file_chosen(lv_event_t *e)
{
    lv_obj_t *explorer = lv_event_get_target(e);
    const char *dir = lv_file_explorer_get_current_path(explorer);
    const char *name = lv_file_explorer_get_selected_file_name(explorer);
    if (!dir || !name || !*name) return;
    char path[768];
    snprintf(path, sizeof path, "%s%s", dir[0] && dir[1] == ':' ? dir + 2 : dir, name);
    overlay_close();
    ui_add_attachment(path);
}

static void attach_clicked(lv_event_t *e)
{
    lv_obj_t *content = overlay_open("Add a file or picture", NULL);
    lv_obj_remove_flag(content, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t *explorer = lv_file_explorer_create(content);
    lv_obj_set_size(explorer, lv_pct(100), lv_pct(100));
    lv_obj_set_style_text_font(explorer, S.font, 0);
    char start[512];
    const char *home = getenv("HOME");
    snprintf(start, sizeof start, "A:%s/", home && *home ? home : "/");
    lv_file_explorer_set_sort(explorer, LV_EXPLORER_SORT_KIND);
    lv_file_explorer_open_dir(explorer, start);
    lv_obj_add_event_cb(explorer, file_chosen, LV_EVENT_VALUE_CHANGED, NULL);
}

/* -- history from every surface ------------------------------------------------------- */

static void request_sessions(const char *query)
{
    cJSON *op = cJSON_CreateObject();
    cJSON_AddStringToObject(op, "op", "sessions.list");
    cJSON_AddStringToObject(op, "req", "list");
    cJSON_AddNumberToObject(op, "limit", MAX_ROWS);
    if (query && *query) cJSON_AddStringToObject(op, "query", query);
    ipc_send(op);
}

static void request_transcript(void)
{
    cJSON *op = cJSON_CreateObject();
    cJSON_AddStringToObject(op, "op", "sessions.get");
    cJSON_AddStringToObject(op, "req", "get");
    cJSON_AddStringToObject(op, "session_id", S.viewing_id);
    cJSON_AddNumberToObject(op, "limit", 80);
    ipc_send(op);
}

static void row_clicked(lv_event_t *e)
{
    int index = (int)(intptr_t)lv_event_get_user_data(e);
    if (index < 0 || index >= S.row_count) return;
    copy(S.viewing_id, sizeof S.viewing_id, S.rows[index].id);
    request_transcript();
}

static void search_ready(lv_event_t *e)
{
    request_sessions(lv_textarea_get_text(W.overlay_search));
}

static void ago(double ts, char *out, size_t size)
{
    double seconds = difftime(time(NULL), (time_t)ts);
    if (ts <= 0) out[0] = '\0';
    else if (seconds < 90) snprintf(out, size, "just now");
    else if (seconds < 3600) snprintf(out, size, "%dm ago", (int)(seconds / 60));
    else if (seconds < 86400) snprintf(out, size, "%dh ago", (int)(seconds / 3600));
    else snprintf(out, size, "%dd ago", (int)(seconds / 86400));
}

static void fill_history(void)
{
    if (!W.overlay_list) return;
    lv_obj_clean(W.overlay_list);
    if (!S.row_count) label(W.overlay_list, "No conversations found.", S.font, S.muted);
    for (int i = 0; i < S.row_count; i++) {
        lv_obj_t *row = lv_button_create(W.overlay_list);
        lv_group_remove_obj(row);
        lv_obj_set_width(row, lv_pct(100));
        lv_obj_set_height(row, LV_SIZE_CONTENT);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_bg_color(row, col(S.rows[i].current ? mix(S.surface, S.accent, 18) : S.surface), 0);
        lv_obj_set_style_radius(row, S.radius > 14 ? 14 : S.radius, 0);
        lv_obj_set_style_shadow_width(row, 0, 0);
        lv_obj_set_style_pad_all(row, S.small ? 8 : 12, 0);
        lv_obj_set_style_pad_column(row, 10, 0);
        lv_obj_add_event_cb(row, row_clicked, LV_EVENT_CLICKED, (void *)(intptr_t)i);
        lv_obj_t *badge = label(row, S.rows[i].source, S.font_small, S.on_accent);
        lv_obj_set_style_bg_color(badge, col(S.accent), 0);
        lv_obj_set_style_bg_opa(badge, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(badge, S.radius > 8 ? 8 : S.radius, 0);
        lv_obj_set_style_pad_hor(badge, 7, 0);
        lv_obj_set_style_pad_ver(badge, 3, 0);
        lv_obj_t *title = label(row, S.rows[i].title, S.font, S.text);
        lv_obj_set_flex_grow(title, 1);
        lv_label_set_long_mode(title, LV_LABEL_LONG_DOT);
        char when[24];
        ago(S.rows[i].last_active, when, sizeof when);
        label(row, when, S.font_small, S.muted);
    }
}

static void show_history(void)
{
    lv_obj_t *content = overlay_open("Conversations", NULL);
    W.overlay_search = field(W.overlay, "Search everything you have said to Hermes", false);
    lv_obj_set_width(W.overlay_search, lv_pct(100));
    lv_obj_move_to_index(W.overlay_search, 1);
    lv_obj_add_event_cb(W.overlay_search, search_ready, LV_EVENT_READY, NULL);
    wire_field(W.overlay_search);
    if (!S.touch) lv_group_focus_obj(W.overlay_search);
    W.overlay_list = content;
    label(content, "Looking through Discord, the web, the terminal and your hermlets ...", S.font_small, S.muted);
    request_sessions(NULL);
}

static void history_clicked(lv_event_t *e) { show_history(); }

static void continue_clicked(lv_event_t *e)
{
    cJSON *op = cJSON_CreateObject();
    cJSON_AddStringToObject(op, "op", "sessions.resume");
    cJSON_AddStringToObject(op, "req", "resume");
    cJSON_AddStringToObject(op, "session_id", S.viewing_id);
    ipc_send(op);
    overlay_close();
}

static void show_transcript(const cJSON *result)
{
    const cJSON *session = cJSON_GetObjectItemCaseSensitive(result, "session");
    const char *title = jstr(session, "title");
    lv_obj_t *header = NULL;
    lv_obj_t *content = overlay_open(*title ? title : "Conversation", &header);
    button(header, "Continue here", true, continue_clicked, NULL);
    const cJSON *m;
    int shown = 0;
    cJSON_ArrayForEach(m, cJSON_GetObjectItemCaseSensitive(result, "messages")) {
        bubble(content, is(jstr(m, "role"), "user") ? ROLE_USER : ROLE_AGENT, jstr(m, "content"));
        shown++;
    }
    if (!shown) label(content, "Nothing was said in this one.", S.font, S.muted);
    scroll_to_end(content);
}

/* -- settings: every part of the look, for every device of this Hermes ---------------------- */

/* A choice is "key=value"; the value is sent as JSON true / false / null, or as text. */
static void look_chosen(lv_event_t *e)
{
    const char *choice = lv_event_get_user_data(e);
    const char *eq = strchr(choice, '=');
    if (!eq) return;
    char key[24];
    snprintf(key, sizeof key, "%.*s", (int)(eq - choice), choice);
    const char *value = eq + 1;
    cJSON *op = cJSON_CreateObject(), *changes;
    cJSON_AddStringToObject(op, "op", "look.set");
    cJSON_AddStringToObject(op, "req", "look");
    changes = cJSON_AddObjectToObject(op, "changes");
    if (is(value, "true")) cJSON_AddTrueToObject(changes, key);
    else if (is(value, "false")) cJSON_AddFalseToObject(changes, key);
    else if (is(value, "null")) cJSON_AddNullToObject(changes, key);
    else if (*value && strspn(value, "0123456789") == strlen(value)) cJSON_AddNumberToObject(changes, key, atoi(value));
    else cJSON_AddStringToObject(changes, key, value);
    ipc_send(op); /* the service answers with a `look` event, which redraws everything */
}

static lv_obj_t *choices(lv_obj_t *parent, const char *heading)
{
    label(parent, heading, S.font_label, S.muted);
    lv_obj_t *row = box(parent, LV_FLEX_FLOW_ROW_WRAP);
    lv_obj_set_size(row, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_column(row, 8, 0);
    lv_obj_set_style_pad_row(row, 8, 0);
    return row;
}

static void choice(lv_obj_t *row, const char *text, bool on, const char *key_value)
{
    button(row, text, on, look_chosen, (void *)key_value);
}

static char g_swatch_choice[MAX_SWATCHES][24];

static void set_meet(lv_event_t *e) { show_meet(); }

static void sign_out(lv_event_t *e)
{
    op_simple("signout");
    overlay_close();
}

static void change_address(lv_event_t *e)
{
    S.editing_address = true;
    copy(S.setup_url, sizeof S.setup_url, S.url);
    copy(S.setup_name, sizeof S.setup_name, S.device);
    build();
}

static void settings_clicked(lv_event_t *e)
{
    lv_obj_t *content = overlay_open("Settings", NULL);
    lv_obj_t *row;
    char line[400];
    S.settings_open = true;

    wrapped(content, "Everything here can also be asked for: \"make the accent red\", \"use a code-style font\", "
                     "\"make it look professional\". Changes reach every device of this Hermes.", S.font_small, S.muted);

    row = choices(content, "Theme");
    choice(row, "Auto (light by day)", is(S.theme_pref, "auto"), "theme=auto");
    choice(row, "Light", is(S.theme_pref, "light"), "theme=light");
    choice(row, "Dark", is(S.theme_pref, "dark"), "theme=dark");

    row = choices(content, "Style");
    choice(row, "Playful", S.fun, "fun=true");
    choice(row, "Plain", !S.fun, "fun=false");

    row = choices(content, "Accent");
    choice(row, S.fun ? "Follow the critter" : "Default", !S.has_accent, "accent=null");
    for (int i = 0; i < S.swatch_count; i++) {
        snprintf(g_swatch_choice[i], sizeof g_swatch_choice[i], "accent=%s", S.swatches[i].hex);
        lv_obj_t *b = lv_button_create(row);
        lv_group_remove_obj(b);
        lv_obj_set_size(b, S.small ? 32 : 40, S.small ? 32 : 40);
        lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(b, col(S.swatches[i].rgb), 0);
        lv_obj_set_style_shadow_width(b, 0, 0);
        lv_obj_set_style_border_color(b, col(S.text), 0);
        lv_obj_set_style_border_width(b, S.has_accent && is(S.accent_hex, S.swatches[i].hex) ? 4 : 0, 0);
        lv_obj_add_event_cb(b, look_chosen, LV_EVENT_CLICKED, g_swatch_choice[i]);
    }

    row = choices(content, "Font");
    choice(row, "Rounded", is(S.font_kind, "rounded"), "font=rounded");
    choice(row, "Code", is(S.font_kind, "mono"), "font=mono");
    choice(row, "Serif", is(S.font_kind, "serif"), "font=serif");
    choice(row, "Pixel", is(S.font_kind, "pixel"), "font=pixel");

    row = choices(content, "Text size");
    choice(row, "Small", S.text_px <= 14, "text_size=small");
    choice(row, "Medium", S.text_px > 14 && S.text_px < 20, "text_size=medium");
    choice(row, "Large", S.text_px >= 20, "text_size=large");

    row = choices(content, "Corners");
    choice(row, "Round", is(S.corners, "round"), "corners=round");
    choice(row, "Soft", is(S.corners, "soft"), "corners=soft");
    choice(row, "Square", is(S.corners, "square"), "corners=square");

    if (S.fun) {
        row = choices(content, "Critter size");
        choice(row, "Small", is(S.critter_size, "small"), "critter_size=small");
        choice(row, "Medium", is(S.critter_size, "medium"), "critter_size=medium");
        choice(row, "Large", is(S.critter_size, "large"), "critter_size=large");
    }

    row = choices(content, "Start a fresh conversation after");
    choice(row, "5 minutes away", S.fresh_min > 0 && S.fresh_min <= 5, "fresh_after_min=5");
    choice(row, "20 minutes", S.fresh_min > 5 && S.fresh_min <= 20, "fresh_after_min=20");
    choice(row, "1 hour", S.fresh_min > 20, "fresh_after_min=60");
    choice(row, "Never", S.fresh_min == 0, "fresh_after_min=0");
    wrapped(content, "Short conversations cost less: the whole conversation is sent to the model on every turn. "
                     "Older ones stay in Conversations, where any of them can be continued.", S.font_small, S.muted);

    row = choices(content, "Voice");
    choice(row, "Spoken replies on", S.speak, "speak=true");
    choice(row, "Off", !S.speak, "speak=false");
    snprintf(line, sizeof line, "%s %s",
             S.mic_available ? "Tap the microphone to talk, tap again to send." : "No microphone tool found (install alsa-utils to talk).",
             S.speaker_available ? "Spoken replies need text-to-speech set up on your Hermes." : "No audio player found for spoken replies.");
    wrapped(content, line, S.font_small, S.muted);

    if (S.critter.loaded && S.fun && is(S.state, "ready")) {
        label(content, "Your hermlet", S.font_label, S.muted);
        snprintf(line, sizeof line, "%s the %s. Every device of the same Hermes shows %s.", S.critter.name, S.critter.species, S.critter.name);
        wrapped(content, line, S.font_small, S.muted);
        button(content, "Name and colour", false, set_meet, NULL);
    }

    label(content, "This device", S.font_label, S.muted);
    snprintf(line, sizeof line, "%s\n%s\n%s", S.device, S.url,
             is(S.mode, "plugin") ? "Connected through the Hermlet plugin" : "Connected through the Hermes dashboard");
    wrapped(content, line, S.font, S.text);
    row = box(content, LV_FLEX_FLOW_ROW_WRAP);
    lv_obj_set_size(row, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_column(row, 8, 0);
    lv_obj_set_style_pad_row(row, 8, 0);
    button(row, "Change address", false, change_address, NULL);
    if (!is(S.mode, "plugin")) button(row, "Sign out", false, sign_out, NULL);
}

/* -- meet your hermlet: a name and a colour ------------------------------------------------ */

static void mark_swatches(void)
{
    for (int i = 0; i < S.swatch_count; i++) {
        if (!W.swatch[i]) continue;
        bool picked = is(S.swatches[i].hex, S.meet_colour);
        lv_obj_set_style_border_width(W.swatch[i], picked ? 4 : 0, 0);
        lv_obj_set_style_border_color(W.swatch[i], col(S.text), 0);
    }
}

static void swatch_clicked(lv_event_t *e)
{
    int index = (int)(intptr_t)lv_event_get_user_data(e);
    if (index < 0 || index >= S.swatch_count) return;
    copy(S.meet_colour, sizeof S.meet_colour, S.swatches[index].hex);
    mark_swatches();
    cJSON *op = cJSON_CreateObject(); /* the service draws the recoloured critter; nothing is saved yet */
    cJSON_AddStringToObject(op, "op", "persona.preview");
    cJSON_AddStringToObject(op, "req", "preview");
    cJSON_AddStringToObject(op, "colour", S.meet_colour);
    ipc_send(op);
}

static void suggest_clicked(lv_event_t *e)
{
    cJSON *op = cJSON_CreateObject();
    cJSON_AddStringToObject(op, "op", "persona.suggest");
    cJSON_AddStringToObject(op, "req", "suggest");
    ipc_send(op);
}

static void meet_done(lv_event_t *e)
{
    const char *name = W.meet_name ? lv_textarea_get_text(W.meet_name) : "";
    cJSON *op = cJSON_CreateObject();
    cJSON_AddStringToObject(op, "op", "persona.set");
    cJSON_AddStringToObject(op, "req", "persona");
    if (name && *name) cJSON_AddStringToObject(op, "name", name);
    if (S.meet_colour[0]) cJSON_AddStringToObject(op, "colour", S.meet_colour);
    ipc_send(op);
}

static void meet_later(lv_event_t *e)
{
    S.meet_skipped = true;
}

static void show_meet(void)
{
    lv_obj_t *header = NULL;
    lv_obj_t *content = overlay_open(S.persona_chosen ? "Your hermlet" : "Meet your hermlet", &header);
    /* Leaving by the back button counts as "not now": do not ask again until the app restarts. */
    lv_obj_add_event_cb(lv_obj_get_child(header, 0), meet_later, LV_EVENT_CLICKED, NULL);
    lv_obj_set_flex_align(content, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    S.preview = S.critter;
    S.meet_colour[0] = '\0';

    W.meet_critter = critter_create(content, S.small ? 3 : 6);
    critter_draw(W.meet_critter, &S.preview, FRAME_HAPPY);
    lv_obj_t *intro = wrapped(content, S.persona_chosen ? "Give it a new name or a new colour."
                              : S.small ? "What is your hermlet called?" : "This is your Hermes's own critter. What is it called?", S.font, S.text);
    lv_obj_set_style_text_align(intro, LV_TEXT_ALIGN_CENTER, 0);

    lv_obj_t *row = box(content, LV_FLEX_FLOW_ROW);
    lv_obj_set_size(row, lv_pct(S.small ? 100 : 70), LV_SIZE_CONTENT);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(row, 8, 0);
    W.meet_name = field(row, S.critter.name, false);
    lv_obj_set_flex_grow(W.meet_name, 1);
    lv_textarea_set_max_length(W.meet_name, 24);
    if (S.persona_chosen) lv_textarea_set_text(W.meet_name, S.critter.name);
    wire_field(W.meet_name);
    if (!S.touch) lv_group_focus_obj(W.meet_name);
    button(row, LV_SYMBOL_REFRESH " Make one up", false, suggest_clicked, NULL);

    lv_obj_t *swatches = box(content, LV_FLEX_FLOW_ROW_WRAP);
    lv_obj_set_size(swatches, lv_pct(S.small ? 100 : 70), LV_SIZE_CONTENT);
    lv_obj_set_flex_align(swatches, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(swatches, S.small ? 6 : 10, 0);
    lv_obj_set_style_pad_row(swatches, S.small ? 6 : 10, 0);
    int side = S.small ? 34 : 44;
    for (int i = 0; i < S.swatch_count; i++) {
        lv_obj_t *b = lv_button_create(swatches);
        lv_group_remove_obj(b);
        lv_obj_set_size(b, side, side);
        lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(b, col(S.swatches[i].rgb), 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
        lv_obj_set_style_shadow_width(b, 0, 0);
        lv_obj_add_event_cb(b, swatch_clicked, LV_EVENT_CLICKED, (void *)(intptr_t)i);
        W.swatch[i] = b;
    }
    mark_swatches();

    button(content, S.persona_chosen ? "Save" : "This is my hermlet", true, meet_done, NULL);
    lv_obj_t *note = wrapped(content, "Every device of this Hermes will show it, and Hermes is told its name.", S.font_small, S.muted);
    lv_obj_set_width(note, lv_pct(S.small ? 100 : 70));
    lv_obj_set_style_text_align(note, LV_TEXT_ALIGN_CENTER, 0);
}

/* -- questions from the agent ------------------------------------------------------------ */

static void prompt_close(void)
{
    if (W.prompt) lv_obj_delete(W.prompt);
    W.prompt = W.prompt_input = NULL;
    S.prompt_id[0] = '\0';
    if (W.input && !S.touch && !W.overlay) lv_group_focus_obj(W.input);
}

static void prompt_answer(const char *answer)
{
    cJSON *op = cJSON_CreateObject();
    cJSON_AddStringToObject(op, "op", "answer");
    cJSON_AddStringToObject(op, "id", S.prompt_id);
    cJSON_AddStringToObject(op, "answer", answer);
    ipc_send(op);
    prompt_close();
}

static void prompt_choice(lv_event_t *e)
{
    lv_obj_t *btn = lv_event_get_target(e);
    lv_obj_t *l = lv_obj_get_child(btn, 0);
    prompt_answer(lv_label_get_text(l));
}

static void prompt_text(lv_event_t *e)
{
    if (W.prompt_input) prompt_answer(lv_textarea_get_text(W.prompt_input));
}

static void show_prompt(const cJSON *msg)
{
    prompt_close();
    copy(S.prompt_id, sizeof S.prompt_id, jstr(msg, "id"));
    W.prompt = box(lv_screen_active(), LV_FLEX_FLOW_COLUMN);
    lv_obj_set_size(W.prompt, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(W.prompt, col(0x000000), 0);
    lv_obj_set_style_bg_opa(W.prompt, LV_OPA_60, 0);
    lv_obj_set_flex_align(W.prompt, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_add_flag(W.prompt, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_t *card = box(W.prompt, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_width(card, lv_pct(S.small ? 94 : 62));
    lv_obj_set_height(card, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(card, col(S.surface), 0);
    lv_obj_set_style_bg_opa(card, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(card, S.radius, 0);
    lv_obj_set_style_pad_all(card, S.small ? 12 : 20, 0);
    lv_obj_set_style_pad_row(card, 10, 0);
    label(card, jstr(msg, "title"), S.font_head, S.text);
    wrapped(card, jstr(msg, "text"), S.font, S.text);
    const cJSON *options = cJSON_GetObjectItemCaseSensitive(msg, "choices");
    if (cJSON_IsArray(options) && cJSON_GetArraySize(options) > 0) {
        lv_obj_t *row = box(card, LV_FLEX_FLOW_ROW_WRAP);
        lv_obj_set_size(row, lv_pct(100), LV_SIZE_CONTENT);
        lv_obj_set_style_pad_column(row, 8, 0);
        lv_obj_set_style_pad_row(row, 8, 0);
        const cJSON *c;
        int n = 0;
        cJSON_ArrayForEach(c, options) {
            if (cJSON_IsString(c)) button(row, c->valuestring, n++ == 0, prompt_choice, NULL);
        }
    } else {
        W.prompt_input = field(card, "Your answer", false);
        lv_obj_set_width(W.prompt_input, lv_pct(100));
        lv_obj_add_event_cb(W.prompt_input, prompt_text, LV_EVENT_READY, NULL);
        wire_field(W.prompt_input);
        if (!S.touch) lv_group_focus_obj(W.prompt_input);
        button(card, "Answer", true, prompt_text, NULL);
    }
    appear(card);
}

/* -- when the device is not ready: sign in, pair, wait ------------------------------------ */

static void signin_clicked(lv_event_t *e)
{
    cJSON *op = cJSON_CreateObject();
    cJSON_AddStringToObject(op, "op", "signin");
    cJSON_AddStringToObject(op, "req", "signin");
    cJSON_AddStringToObject(op, "username", lv_textarea_get_text(W.gate_user));
    cJSON_AddStringToObject(op, "password", lv_textarea_get_text(W.gate_pass));
    ipc_send(op);
    lv_label_set_text(W.gate_error, "Signing in ...");
}

static void setup_clicked(lv_event_t *e)
{
    const char *url = lv_textarea_get_text(W.gate_url);
    while (*url == ' ') url++;
    if (!*url) {
        lv_label_set_text(W.gate_error, "Type the address of your Hermes first.");
        return;
    }
    cJSON *op = cJSON_CreateObject();
    cJSON_AddStringToObject(op, "op", "setup");
    cJSON_AddStringToObject(op, "req", "setup");
    cJSON_AddStringToObject(op, "url", url);
    cJSON_AddStringToObject(op, "name", lv_textarea_get_text(W.gate_name));
    ipc_send(op);
    lv_label_set_text(W.gate_error, "Looking for it ...");
}

static void setup_typed(lv_event_t *e)
{
    /* Kept outside the widgets: the screen is rebuilt whenever the service has news. */
    if (W.gate_url) copy(S.setup_url, sizeof S.setup_url, lv_textarea_get_text(W.gate_url));
    if (W.gate_name) copy(S.setup_name, sizeof S.setup_name, lv_textarea_get_text(W.gate_name));
}

static void setup_cancelled(lv_event_t *e)
{
    S.editing_address = false;
    build();
}

static bool wants_address(void) { return ipc_connected() && (S.editing_address || is(S.state, "setup")); }

static void build_gate(lv_obj_t *parent)
{
    lv_obj_t *gate = box(parent, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_width(gate, lv_pct(100));
    lv_obj_set_flex_grow(gate, 1);
    lv_obj_set_flex_align(gate, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_row(gate, S.small ? 8 : 12, 0);
    lv_obj_set_style_pad_hor(gate, S.small ? 12 : 40, 0);
    lv_obj_add_flag(gate, LV_OBJ_FLAG_SCROLLABLE);
    int form_width = S.small ? 100 : 70;
    char line[400];
    lv_obj_t *l;
    W.gate_user = W.gate_pass = W.gate_error = W.gate_url = W.gate_name = NULL;
    if (!ipc_connected()) {
        /* The app starts the service itself (ipc.c); this is what shows for the moment that takes. */
        switch (ipc_service_status()) {
        case SERVICE_MISSING:
            label(gate, "Half installed", S.font_head, S.text);
            snprintf(line, sizeof line, "The app is here but the rest of Hermlet (the hermlet command) is not.\nRun install.sh again to put it back.");
            break;
        case SERVICE_STUCK:
            label(gate, "Still waking up ...", S.font_head, S.text);
            snprintf(line, sizeof line, "Hermlet is taking longer than it should to start. What it said is in\n%s", ipc_service_log());
            break;
        case SERVICE_OFF:
            label(gate, "Asleep", S.font_head, S.text);
            snprintf(line, sizeof line, "Hermlet's service is not running, and this app was asked not to start it.\nStart it with:  hermlet run");
            break;
        default:
            label(gate, "Waking up ...", S.font_head, S.text);
            snprintf(line, sizeof line, "Starting Hermlet on this machine.");
        }
        l = wrapped(gate, line, S.font, S.muted);
        lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    } else if (wants_address()) {
        bool first = !S.url[0];
        label(gate, first ? "Where is your Hermes?" : "Change address", S.font_head, S.text);
        if (!(S.small && S.touch)) {
            l = wrapped(gate, "The address of your Hermes dashboard: the one you open in a browser.", S.font_small, S.muted);
            lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
        }
        W.gate_url = field(gate, "hermes.your-tailnet.ts.net", false);
        lv_obj_set_width(W.gate_url, lv_pct(form_width));
        if (!S.small) label(gate, "What to call this device", S.font_small, S.muted);
        W.gate_name = field(gate, "A name for this device", false);
        lv_obj_set_width(W.gate_name, lv_pct(form_width));
        lv_textarea_set_max_length(W.gate_url, sizeof S.setup_url - 1);
        lv_textarea_set_max_length(W.gate_name, sizeof S.setup_name - 1);
        if (!S.setup_name[0]) copy(S.setup_name, sizeof S.setup_name, S.device);
        lv_textarea_set_text(W.gate_url, S.setup_url);
        lv_textarea_set_text(W.gate_name, S.setup_name);
        lv_obj_add_event_cb(W.gate_url, setup_typed, LV_EVENT_VALUE_CHANGED, NULL);
        lv_obj_add_event_cb(W.gate_name, setup_typed, LV_EVENT_VALUE_CHANGED, NULL);
        lv_obj_add_event_cb(W.gate_url, setup_clicked, LV_EVENT_READY, NULL);
        lv_obj_add_event_cb(W.gate_name, setup_clicked, LV_EVENT_READY, NULL);
        wire_field(W.gate_url);
        wire_field(W.gate_name);
        if (!S.touch) lv_group_focus_obj(W.gate_url);
        lv_obj_t *row = box(gate, LV_FLEX_FLOW_ROW);
        lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_style_pad_column(row, 8, 0);
        button(row, "Connect", true, setup_clicked, NULL);
        if (!first) button(row, "Cancel", false, setup_cancelled, NULL);
        W.gate_error = wrapped(gate, "", S.font_small, S.muted);
        lv_obj_set_style_text_align(W.gate_error, LV_TEXT_ALIGN_CENTER, 0);
    } else if (is(S.state, "signin")) {
        label(gate, "Sign in to your Hermes", S.font_head, S.text);
        l = wrapped(gate, "Use the username and password of your Hermes dashboard.\nOnly the signed-in session is kept on this device.", S.font_small, S.muted);
        lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
        W.gate_user = field(gate, "Username", false);
        lv_obj_set_width(W.gate_user, lv_pct(form_width));
        W.gate_pass = field(gate, "Password", true);
        lv_obj_set_width(W.gate_pass, lv_pct(form_width));
        lv_obj_add_event_cb(W.gate_pass, signin_clicked, LV_EVENT_READY, NULL);
        wire_field(W.gate_user);
        wire_field(W.gate_pass);
        if (!S.touch) lv_group_focus_obj(W.gate_user);
        lv_obj_t *row = box(gate, LV_FLEX_FLOW_ROW);
        lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_style_pad_column(row, 8, 0);
        button(row, "Sign in", true, signin_clicked, NULL);
        button(row, "Change address", false, change_address, NULL);
        W.gate_error = label(gate, S.url, S.font_small, S.muted);
    } else if (is(S.state, "pairing")) {
        label(gate, "Approve this device", S.font_head, S.text);
        label(gate, S.pair_code[0] ? S.pair_code : "...", builtin(28), S.accent);
        snprintf(line, sizeof line, "On the machine that runs Hermes, type:\n%s", S.pair_command[0] ? S.pair_command : "hermes hermlet pair");
        l = wrapped(gate, line, S.font, S.muted);
        lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    } else {
        label(gate, "Looking for your Hermes ...", S.font_head, S.text);
        snprintf(line, sizeof line, "%s%s%s", S.url, S.link_error[0] ? "\n" : "", S.link_error);
        l = wrapped(gate, line, S.font_small, S.muted);
        lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
        button(gate, "Change address", false, change_address, NULL);
    }
}

/* -- the main screen ---------------------------------------------------------------------- */

static void refresh_status(void)
{
    if (!W.status) return;
    const char *text;
    if (!ipc_connected()) text = "asleep";
    else if (S.recording) text = "listening ... tap the microphone to send";
    else if (S.status[0]) text = S.status;
    else if (S.busy) text = "thinking";
    else if (is(S.state, "ready")) text = S.session_title[0] ? S.session_title : (S.fun ? "here with you" : "ready");
    else if (is(S.state, "setup")) text = "new here";
    else if (is(S.state, "signin")) text = "waiting for you to sign in";
    else if (is(S.state, "pairing")) text = "waiting to be approved";
    else if (is(S.state, "connecting")) text = "looking for Hermes";
    else if (is(S.state, "offline")) text = "cannot reach Hermes; trying again";
    else text = "starting";
    lv_label_set_text(W.status, text);
    if (W.send_label) lv_label_set_text(W.send_label, S.busy && !S.draft[0] && !S.pending_len ? LV_SYMBOL_STOP : LV_SYMBOL_RIGHT);
    if (W.mic) {
        lv_obj_set_style_bg_color(W.mic, col(S.recording ? S.accent : S.surface), 0);
        mic_paint(S.recording ? S.on_accent : S.text);
    }
    if (W.input) {
        lv_textarea_set_placeholder_text(W.input, S.recording ? "Listening ... tap the microphone again to send"
                                         : S.fun && S.critter.loaded ? "Ask me anything" : "Message Hermes");
    }
    if (W.level) {
        if (S.recording) lv_obj_remove_flag(W.level, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(W.level, LV_OBJ_FLAG_HIDDEN);
        lv_bar_set_value(W.level, S.mic_level, LV_ANIM_ON);
    }
}

static const char *day_greeting(void)
{
    time_t now = time(NULL);
    struct tm local;
    localtime_r(&now, &local);
    return local.tm_hour < 5 ? "Still up?" : local.tm_hour < 12 ? "Good morning." : local.tm_hour < 18 ? "Good afternoon." : "Good evening.";
}

/* Where the reply being written goes; made on demand so the stage can be rebuilt mid-reply. */
static void ensure_stream_label(void)
{
    if (W.stream_label || !W.stage) return;
    W.stream_label = say(W.stage, ROLE_AGENT, "", true);
    appear(box_of(W.stream_label));
}

static void build_stage(lv_obj_t *parent)
{
    W.stage = box(parent, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_width(W.stage, lv_pct(100));
    lv_obj_set_flex_grow(W.stage, 1);
    lv_obj_add_flag(W.stage, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_scroll_dir(W.stage, LV_DIR_VER);
    lv_obj_set_style_pad_hor(W.stage, S.small ? 10 : 24, 0);
    lv_obj_set_style_pad_ver(W.stage, S.small ? 6 : 14, 0);
    lv_obj_set_style_pad_row(W.stage, S.small ? 6 : 10, 0);
    W.hero = NULL;
    W.stream_label = NULL;
    if (!S.chat_len && !S.reveal) {
        /* Nothing said yet: a place to arrive at. */
        char hello[120];
        bool beside_critter = S.wide && S.fun;
        lv_obj_set_flex_align(W.stage, LV_FLEX_ALIGN_CENTER, beside_critter ? LV_FLEX_ALIGN_START : LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        if (S.fun && !S.wide) {
            W.hero = critter_create(W.stage, S.small ? 5 : 8);
            critter_draw(W.hero, &S.critter, FRAME_HAPPY);
        }
        if (S.greeting[0]) snprintf(hello, sizeof hello, "%s", S.greeting);
        else if (S.fun && S.critter.loaded) snprintf(hello, sizeof hello, "Hi, I'm %s.", S.critter.name);
        else snprintf(hello, sizeof hello, "%s", day_greeting());
        lv_obj_t *head = wrapped(W.stage, hello, S.fun ? S.font_head : S.font_big, S.text);
        lv_obj_t *hint = wrapped(W.stage, S.mic_available ? "Start typing, tap the microphone, or drop a file here."
                                 : "Start typing, or drop a file here.", S.font_reply, S.muted);
        if (!beside_critter) {
            lv_obj_set_style_text_align(head, LV_TEXT_ALIGN_CENTER, 0);
            lv_obj_set_style_text_align(hint, LV_TEXT_ALIGN_CENTER, 0);
        }
        return;
    }
    /* Everything before the latest thing you said recedes; that exchange is set large. */
    int current_from = 0;
    for (int i = 0; i < S.chat_len; i++) {
        if (S.chat[i].role == ROLE_USER) current_from = i;
    }
    for (int i = 0; i < S.chat_len; i++) say(W.stage, S.chat[i].role, S.chat[i].text, i >= current_from);
    if (S.reveal) {
        W.stream_label = say(W.stage, ROLE_AGENT, "", true);
        char saved = S.reveal[S.reveal_shown];
        S.reveal[S.reveal_shown] = '\0';
        lv_label_set_text(W.stream_label, S.reveal);
        S.reveal[S.reveal_shown] = saved;
    }
}

static int critter_scale(bool rail)
{
    int width = lv_display_get_horizontal_resolution(NULL);
    if (rail) {
        int large = width >= 1000 ? 12 : width >= 760 ? 10 : 8;
        return is(S.critter_size, "small") ? large / 2 : is(S.critter_size, "medium") ? large * 3 / 4 : large;
    }
    return S.small ? (is(S.critter_size, "small") ? 2 : 3) : (is(S.critter_size, "small") ? 3 : is(S.critter_size, "medium") ? 4 : 5);
}

static void icons(lv_obj_t *parent, bool ready)
{
    if (ready) {
        button(parent, LV_SYMBOL_LIST, false, history_clicked, NULL);
        button(parent, LV_SYMBOL_PLUS, false, new_clicked, NULL);
    }
    if (ipc_connected()) button(parent, LV_SYMBOL_SETTINGS, false, settings_clicked, NULL);
}

static void build(void)
{
    apply_look();
    bool reopen_settings = S.settings_open;
    overlay_close();
    prompt_close();
    if (W.toast) { lv_obj_delete(W.toast); W.toast = NULL; }
    if (W.keyboard) { lv_obj_delete(W.keyboard); W.keyboard = NULL; }
    lv_obj_t *screen = lv_screen_active();
    lv_obj_clean(screen);
    memset(&W, 0, sizeof W);
    ttf_release_unused();
    lv_obj_set_style_bg_color(screen, col(S.bg), 0);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);

    bool ready = ipc_connected() && is(S.state, "ready") && !S.editing_address;
    bool rail = S.wide && S.fun; /* the critter gets a side of its own when there is room */
    const char *who = S.fun && S.critter.loaded ? S.critter.name : "Hermes";
    W.root = box(screen, rail ? LV_FLEX_FLOW_ROW : LV_FLEX_FLOW_COLUMN);
    lv_obj_set_size(W.root, lv_pct(100), lv_pct(100));
    S.frame = FRAME_COUNT; /* force a redraw on the next tick */

    lv_obj_t *main_col = W.root;
    int screen_w = lv_display_get_horizontal_resolution(NULL);
    S.stage_w = screen_w - 2 * (S.small ? 10 : 24) - (rail ? 16 * critter_scale(true) + 56 : 0);
    if (rail) {
        int scale = critter_scale(true);
        lv_obj_t *side = box(W.root, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_size(side, 16 * scale + 56, lv_pct(100));
        lv_obj_set_flex_align(side, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_all(side, 16, 0);
        lv_obj_set_style_pad_row(side, 8, 0);
        lv_obj_set_style_bg_color(side, col(mix(S.bg, S.accent, 7)), 0);
        lv_obj_set_style_bg_opa(side, LV_OPA_COVER, 0);
        lv_obj_t *gap = box(side, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_size(gap, 1, 12);
        W.critter = critter_create(side, scale);
        W.name = label(side, who, S.font_head, S.text);
        W.status = wrapped(side, "", S.font_small, S.muted);
        lv_obj_set_style_text_align(W.status, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_t *spring = box(side, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_width(spring, 1);
        lv_obj_set_flex_grow(spring, 1);
        lv_obj_t *row = box(side, LV_FLEX_FLOW_ROW);
        lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_style_pad_column(row, 8, 0);
        icons(row, ready);
        main_col = box(W.root, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_height(main_col, lv_pct(100));
        lv_obj_set_flex_grow(main_col, 1);
    } else {
        lv_obj_t *header = box(W.root, LV_FLEX_FLOW_ROW);
        lv_obj_set_size(header, lv_pct(100), LV_SIZE_CONTENT);
        lv_obj_set_flex_align(header, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_all(header, S.small ? 6 : 12, 0);
        lv_obj_set_style_pad_column(header, S.small ? 6 : 12, 0);
        if (S.fun) W.critter = critter_create(header, critter_scale(false));
        lv_obj_t *titles = box(header, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_height(titles, LV_SIZE_CONTENT);
        lv_obj_set_flex_grow(titles, 1);
        lv_obj_set_style_pad_row(titles, 4, 0);
        W.name = label(titles, who, S.fun ? S.font_head : S.font_reply, S.text);
        W.status = label(titles, "", S.font_small, S.muted);
        lv_obj_set_width(W.status, lv_pct(100));
        lv_label_set_long_mode(W.status, LV_LABEL_LONG_DOT);
        icons(header, ready);
    }

    if (!ready) {
        build_gate(main_col);
        refresh_status();
        return;
    }
    build_stage(main_col);

    W.pending_row = box(main_col, LV_FLEX_FLOW_ROW_WRAP);
    lv_obj_set_size(W.pending_row, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(W.pending_row, S.small ? 6 : 16, 0);
    lv_obj_set_style_pad_column(W.pending_row, 6, 0);
    lv_obj_set_style_pad_row(W.pending_row, 6, 0);

    W.level = lv_bar_create(main_col);
    lv_obj_set_size(W.level, lv_pct(100), 4);
    lv_bar_set_range(W.level, 0, 100);
    lv_obj_set_style_bg_color(W.level, col(S.line), 0);
    lv_obj_set_style_bg_color(W.level, col(S.accent), LV_PART_INDICATOR);
    lv_obj_set_style_radius(W.level, 0, 0);
    lv_obj_set_style_radius(W.level, 0, LV_PART_INDICATOR);

    /* The prompt: large, and always where typing goes. */
    lv_obj_t *bar = box(main_col, LV_FLEX_FLOW_ROW);
    lv_obj_set_size(bar, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_align(bar, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_all(bar, S.small ? 6 : 14, 0);
    lv_obj_set_style_pad_column(bar, S.small ? 6 : 10, 0);
    button(bar, LV_SYMBOL_FILE, false, attach_clicked, NULL);
    W.input = field(bar, "", false);
    lv_obj_set_flex_grow(W.input, 1);
    lv_obj_set_style_text_font(W.input, S.font_reply, 0);
    lv_obj_set_style_pad_ver(W.input, S.small ? 8 : 14, 0);
    lv_obj_set_style_border_width(W.input, 2, LV_STATE_FOCUSED);
    lv_textarea_set_max_length(W.input, 4000);
    lv_textarea_set_text(W.input, S.draft);
    lv_obj_add_event_cb(W.input, input_ready, LV_EVENT_READY, NULL);
    lv_obj_add_event_cb(W.input, input_changed, LV_EVENT_VALUE_CHANGED, NULL);
    wire_field(W.input);
    if (S.mic_available) {
        W.mic = button(bar, "", false, mic_clicked, NULL);
        lv_obj_set_style_radius(W.mic, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_size(W.mic, S.small ? 40 : 50, S.small ? 40 : 50);
        W.mic_icon = mic_draw(W.mic, S.small ? 20 : 26);
        if (S.touch) { /* on a touch screen talking is the main way in: make it the biggest target */
            int side = S.small ? 52 : 68;
            lv_obj_set_size(W.mic, side, side);
            lv_obj_set_style_border_color(W.mic, col(S.accent), 0);
            lv_obj_set_style_border_width(W.mic, 2, 0);
        }
    }
    W.send = button(bar, LV_SYMBOL_RIGHT, true, send_clicked, NULL);
    W.send_label = lv_obj_get_child(W.send, 0);
    refresh_pending();
    refresh_status();
    if (!S.touch) lv_group_focus_obj(W.input);
    if (S.chat_len || S.reveal) {
        /* Only now is the stage's final height known: land on the newest thing said. */
        lv_obj_update_layout(W.root);
        scroll_to_end(W.stage);
    }
    if (reopen_settings) settings_clicked(NULL);
}

/* -- the reply, written out smoothly -------------------------------------------------------- */

static void reveal_finish(void)
{
    if (!S.reveal) return;
    if (W.stream_label) lv_label_set_text(W.stream_label, S.reveal);
    chat_push(ROLE_AGENT, S.reveal);
    free(S.reveal);
    S.reveal = NULL;
    S.reveal_len = S.reveal_shown = 0;
    S.reveal_final = false;
    W.stream_label = NULL;
}

static void reveal_target(const char *text, bool final)
{
    size_t len = strlen(text);
    /* A reply only ever grows; if it was replaced outright, start writing it again. */
    if (S.reveal && (S.reveal_shown > len || strncmp(S.reveal, text, S.reveal_shown) != 0)) S.reveal_shown = 0;
    free(S.reveal);
    S.reveal = strdup(text);
    S.reveal_len = len;
    S.reveal_final = final;
}

void ui_reveal_tick(void)
{
    if (!S.reveal) return;
    if (!W.stage) { if (S.reveal_final) reveal_finish(); return; }
    ensure_stream_label();
    if (S.reveal_shown < S.reveal_len) {
        /* A few characters a frame, more when there is a backlog, so it never lags far behind. */
        size_t backlog = S.reveal_len - S.reveal_shown;
        size_t step = backlog / 14 + 2;
        if (step > 80) step = 80;
        size_t next = S.reveal_shown + step;
        if (next > S.reveal_len) next = S.reveal_len;
        while (next < S.reveal_len && ((unsigned char)S.reveal[next] & 0xC0) == 0x80) next++; /* whole UTF-8 characters */
        S.reveal_shown = next;
        char saved = S.reveal[next];
        S.reveal[next] = '\0';
        lv_label_set_text(W.stream_label, S.reveal);
        S.reveal[next] = saved;
        scroll_to_end(W.stage);
    } else if (S.reveal_final) {
        reveal_finish();
    }
}

/* -- events from the service ----------------------------------------------------------------- */

/* Swap the hello for the conversation the first time something is said. */
static void leave_hello(void)
{
    if (!W.stage || S.chat_len || W.stream_label) return;
    lv_obj_clean(W.stage);
    lv_obj_set_flex_align(W.stage, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    W.hero = NULL;
}

/* Replace the stage in place: what was large becomes the past, the newest exchange is set large. */
static void restage(void)
{
    if (!W.stage) return;
    lv_obj_t *parent = lv_obj_get_parent(W.stage);
    int32_t index = (int32_t)lv_obj_get_index(W.stage);
    lv_obj_delete(W.stage);
    build_stage(parent);
    lv_obj_move_to_index(W.stage, index);
    lv_obj_update_layout(W.root);
    scroll_to_end(W.stage);
}

static void add_said(int role, const char *text)
{
    if (role == ROLE_USER) {
        reveal_finish();
        chat_push(role, text);
        restage();
        if (W.stage) {
            uint32_t count = lv_obj_get_child_count(W.stage);
            if (count) appear(lv_obj_get_child(W.stage, (int32_t)count - 1));
        }
        return;
    }
    leave_hello();
    chat_push(role, text);
    if (!W.stage) return;
    appear(box_of(say(W.stage, role, text, true)));
    scroll_to_end(W.stage);
}

static void take_hello(const cJSON *msg)
{
    const cJSON *device = cJSON_GetObjectItemCaseSensitive(msg, "device");
    const cJSON *pairing = cJSON_GetObjectItemCaseSensitive(msg, "pairing");
    const cJSON *mic = cJSON_GetObjectItemCaseSensitive(msg, "mic");
    S.have_hello = true;
    copy(S.state, sizeof S.state, jstr(msg, "state"));
    copy(S.mode, sizeof S.mode, jstr(msg, "mode"));
    copy(S.device, sizeof S.device, jstr(device, "name"));
    copy(S.url, sizeof S.url, jstr(msg, "url"));
    copy(S.link_error, sizeof S.link_error, jstr(msg, "error"));
    copy(S.status, sizeof S.status, jstr(msg, "status"));
    copy(S.session_title, sizeof S.session_title, jstr(msg, "session_title"));
    copy(S.inbox, sizeof S.inbox, jstr(msg, "inbox"));
    copy(S.pair_code, sizeof S.pair_code, jstr(pairing, "code"));
    copy(S.pair_command, sizeof S.pair_command, jstr(pairing, "command"));
    S.busy = jbool(msg, "busy");
    S.mic_available = jbool(mic, "available");
    S.recording = jbool(mic, "recording");
    S.speaker_available = jbool(msg, "speaker");
    take_look(cJSON_GetObjectItemCaseSensitive(msg, "look"));
    critter_load(&S.critter, cJSON_GetObjectItemCaseSensitive(msg, "persona"));
    S.persona_chosen = jbool(msg, "persona_chosen");
    S.swatch_count = 0;
    const cJSON *swatch;
    cJSON_ArrayForEach(swatch, cJSON_GetObjectItemCaseSensitive(msg, "swatches")) {
        if (S.swatch_count == MAX_SWATCHES) break;
        const char *hex = jstr(swatch, "hex");
        if (hex[0] != '#' || strlen(hex) != 7) continue;
        copy(S.swatches[S.swatch_count].name, sizeof S.swatches[0].name, jstr(swatch, "name"));
        copy(S.swatches[S.swatch_count].hex, sizeof S.swatches[0].hex, hex);
        S.swatches[S.swatch_count].rgb = (uint32_t)strtoul(hex + 1, NULL, 16);
        S.swatch_count++;
    }
    free(S.reveal);
    S.reveal = NULL;
    chat_load(cJSON_GetObjectItemCaseSensitive(msg, "chat"));
    build();
    const cJSON *prompt = cJSON_GetObjectItemCaseSensitive(msg, "prompt");
    if (cJSON_IsObject(prompt)) show_prompt(prompt);
    bool ready = is(S.state, "ready");
    if (is(S.open_screen, "meet")) show_meet();
    else if (!strncmp(S.open_screen, "meet:", 5)) {
        /* For pictures and tests: open the screen, tap that colour, ask for a made-up name. */
        show_meet();
        for (int i = 0; i < S.swatch_count; i++) {
            if (is(S.swatches[i].name, S.open_screen + 5) && W.swatch[i]) lv_obj_send_event(W.swatch[i], LV_EVENT_CLICKED, NULL);
        }
        suggest_clicked(NULL);
    } else if (ready && S.fun && !S.persona_chosen && !S.meet_skipped && !S.open_screen[0]) show_meet();
    /* For pictures and tests: type an address into the setup form, and with connect: press Connect. */
    if (!strncmp(S.open_screen, "address:", 8) || !strncmp(S.open_screen, "connect:", 8)) {
        bool press = S.open_screen[0] == 'c';
        char typed[sizeof S.setup_url];
        copy(typed, sizeof typed, S.open_screen + 8);
        S.open_screen[0] = '\0';
        if (!W.gate_url) change_address(NULL);
        if (W.gate_url) {
            lv_textarea_set_text(W.gate_url, typed);
            if (press) setup_clicked(NULL);
        }
        return;
    } else if (is(S.open_screen, "change-address") && S.url[0]) {
        S.open_screen[0] = '\0';
        change_address(NULL);
        return;
    }
    if (is(S.open_screen, "history")) show_history();
    else if (is(S.open_screen, "settings")) settings_clicked(NULL);
    else if (is(S.open_screen, "attach")) attach_clicked(NULL);
    else if (is(S.open_screen, "keyboard") && W.input) {
        S.touch = true; /* show the on-screen keyboard as a touch screen would */
        lv_obj_send_event(W.input, LV_EVENT_FOCUSED, NULL);
    } else if (!strncmp(S.open_screen, "transcript:", 11)) {
        copy(S.viewing_id, sizeof S.viewing_id, S.open_screen + 11);
        request_transcript();
    }
    S.open_screen[0] = '\0';
}

static void take_result(const cJSON *msg)
{
    const char *req = jstr(msg, "req");
    bool ok = jbool(msg, "ok");
    if (!ok) {
        if ((is(req, "signin") || is(req, "setup")) && W.gate_error) lv_label_set_text(W.gate_error, jstr(msg, "error"));
        else toast("", jstr(msg, "error"), 8);
        return;
    }
    if (is(req, "setup")) { /* the address was taken; what happens next arrives as link events */
        S.editing_address = false;
        S.setup_url[0] = '\0';
        build();
        return;
    }
    if (is(req, "list")) {
        S.row_count = 0;
        const cJSON *row;
        cJSON_ArrayForEach(row, cJSON_GetObjectItemCaseSensitive(msg, "sessions")) {
            if (S.row_count == MAX_ROWS) break;
            copy(S.rows[S.row_count].id, sizeof S.rows[0].id, jstr(row, "id"));
            copy(S.rows[S.row_count].source, sizeof S.rows[0].source, jstr(row, "source"));
            copy(S.rows[S.row_count].title, sizeof S.rows[0].title, jstr(row, "title"));
            const cJSON *active = cJSON_GetObjectItemCaseSensitive(row, "last_active");
            S.rows[S.row_count].last_active = cJSON_IsNumber(active) ? active->valuedouble : 0;
            S.rows[S.row_count].current = jbool(row, "current");
            S.row_count++;
        }
        fill_history();
    } else if (is(req, "get")) {
        show_transcript(msg);
    } else if (is(req, "suggest")) {
        if (W.meet_name) lv_textarea_set_text(W.meet_name, jstr(msg, "name"));
    } else if (is(req, "preview")) {
        if (W.meet_critter && critter_load(&S.preview, cJSON_GetObjectItemCaseSensitive(msg, "persona")))
            critter_draw(W.meet_critter, &S.preview, FRAME_HAPPY);
    } else if (is(req, "persona")) {
        const cJSON *persona = cJSON_GetObjectItemCaseSensitive(msg, "persona");
        const char *memory = jstr(msg, "memory");
        char line[220];
        S.persona_chosen = S.meet_skipped = true;
        overlay_close();
        if (critter_load(&S.critter, persona)) build();
        if (is(memory, "saved")) snprintf(line, sizeof line, "Hermes knows too: its notes now mention %s.", S.critter.name);
        else if (is(memory, "full")) snprintf(line, sizeof line, "Saved. Hermes's notes are full, so tell it yourself: \"my hermlet is called %s\".", S.critter.name);
        else if (is(memory, "off")) snprintf(line, sizeof line, "Saved. Hermes has its memory turned off, so it was not told.");
        else snprintf(line, sizeof line, "Saved, but Hermes could not be told its name.");
        toast(S.critter.name, line, 8);
        if (W.critter && S.fun) hop(W.critter);
    }
}

void ui_on_message(const cJSON *msg)
{
    const char *type = jstr(msg, "type");
    if (is(type, "hello")) { take_hello(msg); return; }
    if (is(type, "_disconnected")) { S.busy = S.recording = false; S.status[0] = '\0'; build(); return; }
    if (!S.have_hello) return;
    if (is(type, "result")) { take_result(msg); return; }

    if (is(type, "link")) {
        bool was_ready = is(S.state, "ready");
        copy(S.state, sizeof S.state, jstr(msg, "state"));
        copy(S.link_error, sizeof S.link_error, jstr(msg, "error"));
        if (!is(S.state, "ready")) { S.busy = false; S.status[0] = '\0'; }
        /* A form someone is typing in stays put while the connection comes and goes behind it. */
        bool typing = (W.gate_url && wants_address()) || (W.gate_user && !is(S.state, "offline") && !is(S.state, "setup"));
        if (was_ready != is(S.state, "ready") || (!is(S.state, "ready") && !typing)) build();
    } else if (is(type, "pairing")) {
        copy(S.pair_code, sizeof S.pair_code, jstr(msg, "code"));
        copy(S.pair_command, sizeof S.pair_command, jstr(msg, "command"));
        build();
    } else if (is(type, "persona")) {
        const cJSON *persona = cJSON_GetObjectItemCaseSensitive(msg, "persona");
        bool meeting = W.meet_critter != NULL; /* do not pull the naming screen out from under the person */
        if (jbool(persona, "chosen")) S.persona_chosen = true;
        if (critter_load(&S.critter, persona) && !meeting) build();
    } else if (is(type, "look")) {
        /* Someone changed the look: here, on another device, or the agent on request. */
        take_look(cJSON_GetObjectItemCaseSensitive(msg, "look"));
        build();
    } else if (is(type, "user")) {
        const cJSON *files = cJSON_GetObjectItemCaseSensitive(msg, "files");
        char line[1200];
        int n = snprintf(line, sizeof line, "%s", jstr(msg, "text"));
        const cJSON *f;
        cJSON_ArrayForEach(f, files) {
            if (n < (int)sizeof line - 8) n += snprintf(line + n, sizeof line - (size_t)n, "%s" LV_SYMBOL_FILE " %s", n ? "\n" : "", jstr(f, "name"));
        }
        add_said(ROLE_USER, line);
    } else if (is(type, "transcript")) {
        add_said(ROLE_USER, jstr(msg, "text"));
    } else if (is(type, "turn.start")) {
        S.busy = true;
        reveal_finish();
    } else if (is(type, "status")) {
        copy(S.status, sizeof S.status, jstr(msg, "text"));
    } else if (is(type, "uploading")) {
        copy(S.status, sizeof S.status, "sending your files");
    } else if (is(type, "reply.delta")) {
        leave_hello();
        reveal_target(jstr(msg, "text"), false);
    } else if (is(type, "reply")) {
        if (jbool(msg, "interim")) {
            reveal_finish();
            add_said(ROLE_AGENT, jstr(msg, "text"));
        } else {
            leave_hello();
            reveal_target(jstr(msg, "text"), true); /* written out even when it arrived all at once */
        }
    } else if (is(type, "turn.end")) {
        S.busy = false;
        S.status[0] = '\0';
        if (S.reveal) S.reveal_final = true;
        if (is(jstr(msg, "outcome"), "success")) {
            S.happy_until = time(NULL) + 2;
            if (W.critter && S.fun) hop(W.critter);
        }
    } else if (is(type, "file")) {
        char line[400];
        snprintf(line, sizeof line, LV_SYMBOL_DOWNLOAD " %s%s%s\nsaved to %s", jstr(msg, "name"),
                 *jstr(msg, "caption") ? ": " : "", jstr(msg, "caption"), jstr(msg, "path"));
        reveal_finish();
        add_said(ROLE_AGENT, line);
    } else if (is(type, "notice")) {
        const cJSON *ttl = cJSON_GetObjectItemCaseSensitive(msg, "ttl_s");
        toast("", jstr(msg, "text"), cJSON_IsNumber(ttl) ? ttl->valueint : 8);
    } else if (is(type, "card")) {
        const cJSON *ttl = cJSON_GetObjectItemCaseSensitive(msg, "ttl_s");
        toast(jstr(msg, "title"), jstr(msg, "body"), cJSON_IsNumber(ttl) && ttl->valueint > 0 ? ttl->valueint : 30);
    } else if (is(type, "prompt")) {
        show_prompt(msg);
    } else if (is(type, "prompt.close")) {
        if (is(S.prompt_id, jstr(msg, "id"))) prompt_close();
    } else if (is(type, "chat")) {
        free(S.reveal);
        S.reveal = NULL;
        chat_load(cJSON_GetObjectItemCaseSensitive(msg, "chat"));
        copy(S.session_title, sizeof S.session_title, jstr(msg, "session_title"));
        build();
    } else if (is(type, "session")) {
        copy(S.session_title, sizeof S.session_title, jstr(msg, "title"));
    } else if (is(type, "mic")) {
        const cJSON *level = cJSON_GetObjectItemCaseSensitive(msg, "level");
        S.recording = is(jstr(msg, "state"), "recording");
        S.mic_level = cJSON_IsNumber(level) ? (int)(level->valuedouble * 100) : 0;
    }
    refresh_status();
}

/* -- the heartbeat ---------------------------------------------------------------------------- */

void ui_tick(void)
{
    S.tick++;
    time_t now = time(NULL);
    if (W.toast && now >= S.toast_until) { lv_obj_delete(W.toast); W.toast = NULL; }
    if (S.have_hello && wants_dark() != S.dark) build(); /* the day turned, or the evening came */
    if (!ipc_connected() && (int)ipc_service_status() != S.service_status) {
        S.service_status = (int)ipc_service_status();
        build();
    }

    critter_frame_t frame;
    if (!ipc_connected() || !is(S.state, "ready")) frame = FRAME_SLEEP;
    else if (S.reveal && S.reveal_shown < S.reveal_len) frame = (S.tick / 2) % 2 ? FRAME_TALK : FRAME_IDLE;
    else if (S.busy) frame = FRAME_THINK;
    else if (S.recording) frame = FRAME_HAPPY;
    else if (now < S.happy_until) frame = FRAME_HAPPY;
    else frame = S.tick % 34 == 0 ? FRAME_BLINK : FRAME_IDLE;
    if (frame != S.frame) {
        S.frame = frame;
        if (W.critter) critter_draw(W.critter, &S.critter, frame);
        if (W.hero) critter_draw(W.hero, &S.critter, frame == FRAME_IDLE ? FRAME_HAPPY : frame);
    }
}

void ui_init(const char *open_screen)
{
    memset(&S, 0, sizeof S);
    memset(&W, 0, sizeof W);
    copy(S.open_screen, sizeof S.open_screen, open_screen);
    copy(S.theme_pref, sizeof S.theme_pref, "auto");
    copy(S.font_kind, sizeof S.font_kind, "rounded");
    copy(S.corners, sizeof S.corners, "round");
    copy(S.critter_size, sizeof S.critter_size, "large");
    S.fun = true;
    S.text_px = 16;
    const char *touch = getenv("HERMLET_TOUCH");
    S.touch = touch ? atoi(touch) != 0 : lv_display_get_horizontal_resolution(NULL) <= 800;
    build();
}
