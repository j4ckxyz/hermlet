/* Hermlet app: shared declarations.
 *
 * The app is a face for the `hermlet run` service on the same machine. It
 * holds no connection to Hermes itself: it reads events from the local socket
 * and writes ops to it (docs/local-api.md), so it stays small and starts fast.
 */
#ifndef HERMLET_APP_H
#define HERMLET_APP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cJSON.h"
#include "lvgl.h"

/* -- ipc.c: the local socket ------------------------------------------------ */

typedef void (*ipc_message_cb)(const cJSON *msg);

void ipc_init(const char *socket_path, ipc_message_cb on_message);
bool ipc_connected(void);
/* Read whatever arrived and hand each JSON line to the callback. Reconnects on its own. */
void ipc_poll(void);
/* Send one op. Takes ownership of `op` (always frees it). */
bool ipc_send(cJSON *op);
const char *ipc_socket_path(void);
/* Whether the app starts `hermlet run` itself whenever nothing is listening (it does by default),
 * so opening the app is all a person has to do. */
void ipc_keep_service(bool on);

typedef enum {
    SERVICE_OFF,      /* the app was told not to start it */
    SERVICE_STARTING, /* started; waiting for its socket */
    SERVICE_MISSING,  /* no `hermlet` command on this machine */
    SERVICE_STUCK     /* started a while ago and still not answering */
} service_status_t;

service_status_t ipc_service_status(void);
/* Where a service started by the app writes what it has to say. */
const char *ipc_service_log(void);

/* -- critter.c: the agent's pixel companion ---------------------------------- */

typedef enum {
    FRAME_IDLE, FRAME_BLINK, FRAME_TALK, FRAME_THINK, FRAME_HAPPY, FRAME_SLEEP, FRAME_COUNT
} critter_frame_t;

#define CRITTER_MAX 32

typedef struct {
    bool loaded;
    int w, h;
    char name[32];
    char species[16];
    uint32_t body, shade, outline, accent; /* 0xRRGGBB */
    /* One ARGB pixel per cell, per frame; alpha 0 is see-through. */
    uint32_t px[FRAME_COUNT][CRITTER_MAX * CRITTER_MAX];
} critter_t;

bool critter_load(critter_t *c, const cJSON *persona);
/* A canvas showing the critter at `scale` screen pixels per sprite pixel. */
lv_obj_t *critter_create(lv_obj_t *parent, int scale);
void critter_draw(lv_obj_t *canvas, const critter_t *c, critter_frame_t frame);

/* -- ui.c: the screens -------------------------------------------------------- */

void ui_init(const char *open_screen);
void ui_on_message(const cJSON *msg);
/* Called a few times a second: animation, toasts, clock-driven theme, connection state. */
void ui_tick(void);
/* Called every frame or two: writes the reply out a few characters at a time. */
void ui_reveal_tick(void);
/* A file dropped onto the window, or chosen some other way. */
void ui_add_attachment(const char *path);

#endif
