/* The local socket: newline-delimited JSON to and from `hermlet run`. */
#include "app.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif

static char g_path[256];
static int g_fd = -1;
static ipc_message_cb g_cb;
static char *g_buf;
static size_t g_len, g_cap;
static uint64_t g_next_try; /* milliseconds on the steady clock */
static bool g_path_given, g_keep;
static char g_service[600], g_log[300];
static uint64_t g_first_spawn, g_last_spawn, g_last_search;

static void keep_alive(void);

static uint64_t steady_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}

const char *ipc_socket_path(void) { return g_path; }
bool ipc_connected(void) { return g_fd >= 0; }

static void resolve_path(const char *given)
{
    const char *home = getenv("HERMLET_HOME");
    if (given && *given) {
        snprintf(g_path, sizeof g_path, "%s", given);
        g_path_given = true;
    } else if (home && *home) {
        snprintf(g_path, sizeof g_path, "%s/hermlet.sock", home);
    } else {
        const char *xdg = getenv("XDG_CONFIG_HOME");
        const char *user_home = getenv("HOME");
        if (xdg && *xdg) snprintf(g_path, sizeof g_path, "%s/hermlet/hermlet.sock", xdg);
        else snprintf(g_path, sizeof g_path, "%s/.config/hermlet/hermlet.sock", user_home ? user_home : ".");
    }
}

void ipc_init(const char *socket_path, ipc_message_cb on_message)
{
    resolve_path(socket_path);
    g_cb = on_message;
}

static void drop(void)
{
    if (g_fd >= 0) close(g_fd);
    g_fd = -1;
    g_len = 0;
    g_next_try = steady_ms() + 1000;
    cJSON *gone = cJSON_CreateObject();
    cJSON_AddStringToObject(gone, "type", "_disconnected");
    if (g_cb) g_cb(gone);
    cJSON_Delete(gone);
}

static bool try_connect(void)
{
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    if (strlen(g_path) >= sizeof addr.sun_path) return false;
    strcpy(addr.sun_path, g_path);
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return false;
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        close(fd);
        return false;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
#ifdef SO_NOSIGPIPE
    int on = 1;
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, sizeof on);
#endif
    g_fd = fd;
    return true;
}

void ipc_poll(void)
{
    if (g_fd < 0) {
        if (steady_ms() < g_next_try) return;
        if (!try_connect()) {
            if (g_keep) keep_alive();
            /* While a service we started is coming up, look often so the app opens the moment it is. */
            g_next_try = steady_ms() + (g_first_spawn ? 250 : 2000);
            return;
        }
        g_first_spawn = g_last_spawn = 0;
    }
    for (;;) {
        if (g_cap - g_len < 4096) {
            g_cap = g_cap ? g_cap * 2 : 16384;
            g_buf = realloc(g_buf, g_cap);
            if (!g_buf) abort();
        }
        ssize_t n = read(g_fd, g_buf + g_len, g_cap - g_len - 1);
        if (n > 0) {
            g_len += (size_t)n;
            continue;
        }
        if (n == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
            drop();
            return;
        }
        break;
    }
    size_t start = 0;
    for (size_t i = 0; i < g_len; i++) {
        if (g_buf[i] != '\n') continue;
        g_buf[i] = '\0';
        cJSON *msg = cJSON_Parse(g_buf + start);
        if (msg) {
            if (g_cb) g_cb(msg);
            cJSON_Delete(msg);
        }
        start = i + 1;
        if (g_fd < 0) return; /* the callback's op failed and dropped the connection */
    }
    if (start) {
        memmove(g_buf, g_buf + start, g_len - start);
        g_len -= start;
    }
}

bool ipc_send(cJSON *op)
{
    bool ok = false;
    char *text = cJSON_PrintUnformatted(op);
    cJSON_Delete(op);
    if (text && g_fd >= 0) {
        size_t len = strlen(text), sent = 0;
        text[len] = '\n'; /* overwrite the terminator: the length is tracked by hand from here */
        len += 1;
        int flags = fcntl(g_fd, F_GETFL, 0);
        fcntl(g_fd, F_SETFL, flags & ~O_NONBLOCK); /* ops are small; a blocking write keeps them whole */
        while (sent < len) {
#ifdef MSG_NOSIGNAL
            ssize_t n = send(g_fd, text + sent, len - sent, MSG_NOSIGNAL);
#else
            ssize_t n = write(g_fd, text + sent, len - sent);
#endif
            if (n <= 0) break;
            sent += (size_t)n;
        }
        fcntl(g_fd, F_SETFL, flags);
        ok = sent == len;
        if (!ok) drop();
    }
    free(text);
    return ok;
}

/* -- starting the service ----------------------------------------------------------------
 *
 * Opening the app is the whole of "running Hermlet": when nothing answers on the socket the app
 * starts `hermlet run` itself, detached so it keeps the connection to Hermes after the app closes.
 */

static bool runnable(const char *path)
{
    struct stat st;
    return stat(path, &st) == 0 && S_ISREG(st.st_mode) && access(path, X_OK) == 0;
}

static bool own_dir(char *out, size_t size)
{
#if defined(__linux__)
    ssize_t n = readlink("/proc/self/exe", out, size - 1);
    if (n <= 0) return false;
    out[n] = '\0';
#elif defined(__APPLE__)
    uint32_t len = (uint32_t)size;
    if (_NSGetExecutablePath(out, &len) != 0) return false;
#else
    (void)size;
    return false;
#endif
    char *slash = strrchr(out, '/');
    if (!slash) return false;
    *slash = '\0';
    return true;
}

/* The `hermlet` command. An app opened from a desktop menu often has a PATH without ~/.local/bin,
 * so the places the installer and a source checkout put it are tried by name first. */
static bool find_service(void)
{
    const char *given = getenv("HERMLET_SERVICE"), *home = getenv("HOME"), *path = getenv("PATH");
    char dir[400];
    if (given && *given) {
        snprintf(g_service, sizeof g_service, "%s", given);
        if (runnable(g_service)) return true;
        g_service[0] = '\0';
        return false;
    }
    if (own_dir(dir, sizeof dir)) {
        snprintf(g_service, sizeof g_service, "%s/hermlet", dir);
        if (runnable(g_service)) return true;
        snprintf(g_service, sizeof g_service, "%s/../../.venv/bin/hermlet", dir); /* app/build in a checkout */
        if (runnable(g_service)) return true;
    }
    if (home && *home) {
        snprintf(g_service, sizeof g_service, "%s/.local/bin/hermlet", home);
        if (runnable(g_service)) return true;
        snprintf(g_service, sizeof g_service, "%s/.local/share/hermlet/venv/bin/hermlet", home);
        if (runnable(g_service)) return true;
    }
    while (path && *path) {
        size_t len = strcspn(path, ":");
        if (len && len < sizeof dir) {
            snprintf(g_service, sizeof g_service, "%.*s/hermlet", (int)len, path);
            if (runnable(g_service)) return true;
        }
        path += len;
        if (*path == ':') path++;
    }
    g_service[0] = '\0';
    return false;
}

static void socket_dir(char *out, size_t size)
{
    snprintf(out, size, "%s", g_path);
    char *slash = strrchr(out, '/');
    if (slash) *slash = '\0';
    else snprintf(out, size, ".");
}

static void spawn(void)
{
    char dir[256];
    socket_dir(dir, sizeof dir);
    char parent[256];
    snprintf(parent, sizeof parent, "%s", dir);
    char *slash = strrchr(parent, '/');
    if (slash && slash != parent) {
        *slash = '\0';
        mkdir(parent, 0755);
    }
    mkdir(dir, 0700);
    snprintf(g_log, sizeof g_log, "%s/service.log", dir);

    pid_t pid = fork();
    if (pid < 0) return;
    if (pid > 0) {
        waitpid(pid, NULL, 0); /* the middle process leaves at once; do not keep it as a zombie */
        return;
    }
    /* Child: detach fully so the service outlives the app. */
    if (fork() != 0) _exit(0);
    setsid();
    int in = open("/dev/null", O_RDONLY);
    int out = open(g_log, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (out < 0) out = open("/dev/null", O_WRONLY);
    if (in >= 0) dup2(in, 0);
    if (out >= 0) {
        dup2(out, 1);
        dup2(out, 2);
    }
    for (int fd = 3; fd < 256; fd++) close(fd); /* the display, the socket: none of it is the service's */
    if (g_path_given) setenv("HERMLET_HOME", dir, 1); /* answer on the socket this app was pointed at */
    execl(g_service, "hermlet", "run", (char *)NULL);
    _exit(127);
}

static void keep_alive(void)
{
    uint64_t now = steady_ms();
    if (!g_service[0]) {
        if (g_last_search && now - g_last_search < 5000) return;
        g_last_search = now;
        if (!find_service()) return;
    }
    if (g_last_spawn && now - g_last_spawn < 15000) return; /* a second copy would only find the first and leave */
    spawn();
    g_last_spawn = now;
    if (!g_first_spawn) g_first_spawn = now;
}

void ipc_keep_service(bool on)
{
    g_keep = on;
    if (on && !ipc_connected()) g_next_try = 0;
}

service_status_t ipc_service_status(void)
{
    if (!g_keep) return SERVICE_OFF;
    if (!g_service[0]) return g_last_search ? SERVICE_MISSING : SERVICE_STARTING;
    if (g_first_spawn && steady_ms() - g_first_spawn > 20000) return SERVICE_STUCK;
    return SERVICE_STARTING;
}

const char *ipc_service_log(void) { return g_log; }
