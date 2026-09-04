// Milestone 0: can this machine capture ONE window's own pixels at 60fps?
//
// hyprland_toplevel_export_v1 hands us the window's surface tree rendered alone --
// no occluding windows, no dim_special, no borders. That is the thing a rectangle
// capture can never give us, and the reason this path exists at all.
//
// This is the MEASUREMENT, not the recorder. It takes the cheap route deliberately:
// wl_shm buffers, which cost the compositor a readPixels per frame. If the iGPU
// holds 60 here it will hold it on the dmabuf path too; if it does not, dmabuf
// stops being an optimisation and becomes a requirement. Either way we learn it
// before committing to an encoder pipeline.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include "hyprland-toplevel-export-v1-client-protocol.h"

static struct wl_shm *shm;
static struct hyprland_toplevel_export_manager_v1 *mgr;

static uint32_t g_handle, fmt, W, H, STRIDE;
static struct wl_buffer *buf;
static void *pix;
static int have_params, running = 1, frames, failures;
static double deadline;
static FILE *raw;

static double now_s(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

static void reg(void *d, struct wl_registry *r, uint32_t name,
                const char *iface, uint32_t ver) {
    (void)d; (void)ver;
    if (!strcmp(iface, wl_shm_interface.name))
        shm = wl_registry_bind(r, name, &wl_shm_interface, 1);
    else if (!strcmp(iface, hyprland_toplevel_export_manager_v1_interface.name))
        mgr = wl_registry_bind(r, name,
                               &hyprland_toplevel_export_manager_v1_interface, 1);
}
static void reg_gone(void *d, struct wl_registry *r, uint32_t n) { (void)d;(void)r;(void)n; }
static const struct wl_registry_listener reg_l = { reg, reg_gone };

static void shoot(void);

static void on_buffer(void *d, struct hyprland_toplevel_export_frame_v1 *f,
                      uint32_t format, uint32_t w, uint32_t h, uint32_t stride) {
    (void)d; (void)f;
    fmt = format; W = w; H = h; STRIDE = stride;
    have_params = 1;
}

static void on_damage(void *d, struct hyprland_toplevel_export_frame_v1 *f,
                      uint32_t x, uint32_t y, uint32_t w, uint32_t h) {
    (void)d;(void)f;(void)x;(void)y;(void)w;(void)h;
}
static void on_flags(void *d, struct hyprland_toplevel_export_frame_v1 *f, uint32_t fl) {
    (void)d;(void)f;(void)fl;
}
static void on_dmabuf(void *d, struct hyprland_toplevel_export_frame_v1 *f,
                      uint32_t format, uint32_t w, uint32_t h) {
    (void)d;(void)f;(void)format;(void)w;(void)h;
}

static void on_buffer_done(void *d, struct hyprland_toplevel_export_frame_v1 *f) {
    (void)d;
    if (!have_params) { hyprland_toplevel_export_frame_v1_destroy(f); running = 0; return; }
    if (!buf) {
        // One buffer, reused: the spike is measuring the compositor's cost, and a
        // ping-pong pair would only hide it.
        size_t sz = (size_t)STRIDE * H;
        int fd = memfd_create("spike", MFD_CLOEXEC);
        if (fd < 0 || ftruncate(fd, sz) < 0) { perror("memfd"); exit(1); }
        pix = mmap(NULL, sz, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, sz);
        buf = wl_shm_pool_create_buffer(pool, 0, W, H, STRIDE, fmt);
        wl_shm_pool_destroy(pool);
        close(fd);
    }
    // ignore_damage = 1: a recorder wants a frame per tick, not a frame per change.
    hyprland_toplevel_export_frame_v1_copy(f, buf, 1);
}

static void on_ready(void *d, struct hyprland_toplevel_export_frame_v1 *f,
                     uint32_t hi, uint32_t lo, uint32_t nsec) {
    (void)d; (void)hi; (void)lo; (void)nsec;
    frames++;
    if (raw && frames <= 2) fwrite(pix, 1, (size_t)STRIDE * H, raw);
    hyprland_toplevel_export_frame_v1_destroy(f);
    if (now_s() < deadline) shoot(); else running = 0;
}

static void on_failed(void *d, struct hyprland_toplevel_export_frame_v1 *f) {
    (void)d;
    failures++;
    hyprland_toplevel_export_frame_v1_destroy(f);
    if (now_s() < deadline) shoot(); else running = 0;
}

static const struct hyprland_toplevel_export_frame_v1_listener frame_l = {
    .buffer = on_buffer, .damage = on_damage, .flags = on_flags,
    .ready = on_ready, .failed = on_failed,
    .linux_dmabuf = on_dmabuf, .buffer_done = on_buffer_done,
};

static void shoot(void) {
    struct hyprland_toplevel_export_frame_v1 *f =
        hyprland_toplevel_export_manager_v1_capture_toplevel(mgr, 1, g_handle);
    hyprland_toplevel_export_frame_v1_add_listener(f, &frame_l, NULL);
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: spike <0xADDRESS> <seconds> [raw.out]\n");
        return 2;
    }
    unsigned long long addr = strtoull(argv[1], NULL, 0);
    g_handle = (uint32_t)(addr & 0xFFFFFFFFULL);   // xdph truncates the same way
    double secs = atof(argv[2]);
    if (argc > 3) raw = fopen(argv[3], "wb");

    struct wl_display *dpy = wl_display_connect(NULL);
    if (!dpy) { fprintf(stderr, "no wayland display\n"); return 1; }
    struct wl_registry *r = wl_display_get_registry(dpy);
    wl_registry_add_listener(r, &reg_l, NULL);
    wl_display_roundtrip(dpy);
    if (!mgr || !shm) {
        fprintf(stderr, "missing %s\n", mgr ? "wl_shm" : "hyprland_toplevel_export_manager_v1");
        return 1;
    }

    double t0 = now_s();
    deadline = t0 + secs;
    shoot();
    while (running && wl_display_dispatch(dpy) != -1) { }
    double el = now_s() - t0;

    fprintf(stderr, "{\"frames\":%d,\"failed\":%d,\"seconds\":%.3f,\"fps\":%.2f,"
                    "\"w\":%u,\"h\":%u,\"stride\":%u,\"format\":%u}\n",
            frames, failures, el, el > 0 ? frames / el : 0.0, W, H, STRIDE, fmt);
    if (raw) fclose(raw);
    return 0;
}
