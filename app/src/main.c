/* Hermlet app: start a display, then let the UI follow the local hermlet service.
 *
 *   hermlet-app                         a window (or the whole screen on a bare console)
 *   hermlet-app --size 480x320          a particular window size
 *   hermlet-app --fullscreen
 *   hermlet-app --socket PATH           a service somewhere other than ~/.config/hermlet
 *   hermlet-app --attach FILE           open with a file ready to send (repeatable)
 *   hermlet-app --no-spawn              do not start the service (`hermlet run`); by default the app does
 *   hermlet-app --screenshot out.bmp --after 1500 [--screen history|settings|attach|keyboard|meet|transcript:ID]
 *                                       draw, save a picture, exit (for docs and tests)
 */
#include "app.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifdef HERMLET_SDL
#include <SDL2/SDL.h>
#endif
#ifdef HERMLET_FBDEV
#include <fcntl.h>
#include <linux/input.h>
#include <sys/ioctl.h>
#endif

static uint32_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

static void poll_timer(lv_timer_t *t) { ipc_poll(); }
static void tick_timer(lv_timer_t *t) { ui_tick(); }
static void reveal_timer(lv_timer_t *t) { ui_reveal_tick(); }

static bool save_bmp(const char *path)
{
    lv_draw_buf_t *shot = lv_snapshot_take(lv_screen_active(), LV_COLOR_FORMAT_ARGB8888);
    if (!shot) return false;
    uint32_t w = shot->header.w, h = shot->header.h, stride = shot->header.stride;
    FILE *f = fopen(path, "wb");
    if (!f) {
        lv_draw_buf_destroy(shot);
        return false;
    }
    uint32_t size = 54 + w * h * 4;
    uint8_t head[54] = {'B', 'M'};
    int32_t top_down = -(int32_t)h;
    memcpy(head + 2, &size, 4);
    head[10] = 54;
    head[14] = 40;
    memcpy(head + 18, &w, 4);
    memcpy(head + 22, &top_down, 4);
    head[26] = 1;
    head[28] = 32;
    fwrite(head, 1, sizeof head, f);
    for (uint32_t y = 0; y < h; y++) fwrite(shot->data + (size_t)y * stride, 4, w, f);
    fclose(f);
    lv_draw_buf_destroy(shot);
    return true;
}

#ifdef HERMLET_SDL
static int sdl_watch(void *user, SDL_Event *event)
{
    if (event->type == SDL_DROPFILE && event->drop.file) {
        ui_add_attachment(event->drop.file); /* a file dragged onto the window */
        SDL_free(event->drop.file);
    }
    return 0;
}

static void start_display(int width, int height, bool fullscreen)
{
    SDL_Init(SDL_INIT_VIDEO);
    if (fullscreen) {
        SDL_DisplayMode mode;
        if (SDL_GetCurrentDisplayMode(0, &mode) == 0) {
            width = mode.w;
            height = mode.h;
        }
    }
    lv_display_t *disp = lv_sdl_window_create(width, height);
    lv_sdl_window_set_title(disp, "Hermlet");
    if (fullscreen) {
        SDL_Window *window = SDL_RenderGetWindow(lv_sdl_window_get_renderer(disp));
        if (window) SDL_SetWindowFullscreen(window, SDL_WINDOW_FULLSCREEN_DESKTOP);
        SDL_ShowCursor(SDL_DISABLE);
    }
    lv_group_set_default(lv_group_create());
    lv_sdl_mouse_create();
    lv_indev_t *wheel = lv_sdl_mousewheel_create();
    lv_indev_t *keyboard = lv_sdl_keyboard_create();
    lv_indev_set_group(keyboard, lv_group_get_default());
    lv_indev_set_group(wheel, lv_group_get_default());
    SDL_EventState(SDL_DROPFILE, SDL_ENABLE);
    SDL_AddEventWatch(sdl_watch, NULL);
}
#endif

#ifdef HERMLET_FBDEV
#define TEST_BIT(bits, bit) ((bits)[(bit) / 8] & (1 << ((bit) % 8)))

/* Every pointer (touchscreen, mouse) and keyboard under /dev/input becomes an LVGL input. */
static void start_inputs(void)
{
    for (int i = 0; i < 32; i++) {
        char path[64];
        snprintf(path, sizeof path, "/dev/input/event%d", i);
        int fd = open(path, O_RDONLY | O_NONBLOCK);
        if (fd < 0) continue;
        unsigned char ev[EV_MAX / 8 + 1] = {0}, keys[KEY_MAX / 8 + 1] = {0};
        ioctl(fd, EVIOCGBIT(0, sizeof ev), ev);
        ioctl(fd, EVIOCGBIT(EV_KEY, sizeof keys), keys);
        close(fd);
        if (TEST_BIT(ev, EV_ABS) || TEST_BIT(ev, EV_REL)) {
            lv_evdev_create(LV_INDEV_TYPE_POINTER, path);
        } else if (TEST_BIT(ev, EV_KEY) && TEST_BIT(keys, KEY_A)) {
            lv_indev_t *keyboard = lv_evdev_create(LV_INDEV_TYPE_KEYPAD, path);
            if (keyboard) lv_indev_set_group(keyboard, lv_group_get_default());
        }
    }
}

static void start_display(int width, int height, bool fullscreen)
{
    const char *fb = getenv("HERMLET_FB");
    lv_display_t *disp = lv_linux_fbdev_create();
    lv_linux_fbdev_set_file(disp, fb && *fb ? fb : "/dev/fb0");
    lv_group_set_default(lv_group_create());
    start_inputs();
}
#endif

#ifdef HERMLET_OFFSCREEN
static void discard(lv_display_t *disp, const lv_area_t *area, uint8_t *pixels)
{
    lv_display_flush_ready(disp); /* nothing to show it on; --screenshot reads the picture back */
}

static void start_display(int width, int height, bool fullscreen)
{
    size_t size = (size_t)width * (size_t)height * 4;
    lv_display_t *disp = lv_display_create(width, height);
    lv_display_set_buffers(disp, malloc(size), NULL, (uint32_t)size, LV_DISPLAY_RENDER_MODE_DIRECT);
    lv_display_set_flush_cb(disp, discard);
    lv_group_set_default(lv_group_create());
}
#endif

int main(int argc, char **argv)
{
    const char *socket_path = NULL, *screenshot = NULL, *screen = "", *attach[8];
    int attach_count = 0;
    int width = 800, height = 480, after_ms = 1500;
    bool fullscreen = false, spawn = true;
#if defined(__linux__) && !defined(HERMLET_OFFSCREEN)
    /* No desktop session: take the whole screen. */
    if (!getenv("DISPLAY") && !getenv("WAYLAND_DISPLAY")) fullscreen = true;
#endif
    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i], *next = i + 1 < argc ? argv[i + 1] : NULL;
        if (!strcmp(arg, "--socket") && next) socket_path = argv[++i];
        else if (!strcmp(arg, "--size") && next) sscanf(argv[++i], "%dx%d", &width, &height);
        else if (!strcmp(arg, "--fullscreen")) fullscreen = true;
        else if (!strcmp(arg, "--window")) fullscreen = false;
        else if (!strcmp(arg, "--no-spawn")) spawn = false;
        else if (!strcmp(arg, "--touch")) setenv("HERMLET_TOUCH", "1", 1);
        else if (!strcmp(arg, "--attach") && next) { if (attach_count < 8) attach[attach_count++] = argv[i + 1]; i++; }
        else if (!strcmp(arg, "--screen") && next) screen = argv[++i];
        else if (!strcmp(arg, "--screenshot") && next) screenshot = argv[++i];
        else if (!strcmp(arg, "--after") && next) after_ms = atoi(argv[++i]);
        else {
            fprintf(stderr, "usage: hermlet-app [--size WxH] [--fullscreen|--window] [--touch] [--socket PATH] [--no-spawn]\n"
                            "                   [--attach FILE]... [--screenshot FILE.bmp --after MS\n"
                            "                   --screen history|settings|attach|keyboard|meet|transcript:ID|\n"
                            "                            address:TEXT|connect:TEXT|change-address]\n");
            return strcmp(arg, "--help") && strcmp(arg, "-h") ? 2 : 0;
        }
    }

    lv_init();
    lv_tick_set_cb(now_ms);
    start_display(width, height, fullscreen);

    ipc_init(socket_path, ui_on_message);
    ui_init(screen);
    for (int i = 0; i < attach_count; i++) ui_add_attachment(attach[i]);
    ipc_keep_service(spawn);
    ipc_poll();
    lv_timer_create(poll_timer, 30, NULL);
    lv_timer_create(tick_timer, 120, NULL);
    lv_timer_create(reveal_timer, 33, NULL);
#ifdef HERMLET_SDL
    /* Runs the drop handler as SDL would, for machines with no one to drag a file. It does not
     * prove SDL delivers the event: that needs a real drag onto a real window. */
    const char *test_drop = getenv("HERMLET_TEST_DROP");
    if (test_drop && *test_drop) {
        SDL_Event drop;
        SDL_zero(drop);
        drop.type = SDL_DROPFILE;
        drop.drop.file = SDL_strdup(test_drop);
        sdl_watch(NULL, &drop);
    }
#endif

    uint32_t started = now_ms();
    for (;;) {
        uint32_t wait = lv_timer_handler();
        if (screenshot && now_ms() - started >= (uint32_t)after_ms) {
            lv_refr_now(NULL);
            if (!save_bmp(screenshot)) {
                fprintf(stderr, "hermlet-app: could not save %s\n", screenshot);
                return 1;
            }
            return 0;
        }
        usleep((wait < 5 ? 5 : wait > 20 ? 20 : wait) * 1000);
    }
}
