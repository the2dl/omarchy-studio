// Milestone 1: the same capture with NO readback.
//
// spike.c measured 44.8fps at 5076x2768 because every frame was copied out of the
// GPU into shared memory -- 56MB a frame, ~2.5GB/s of readback, which is the whole
// cost. Here the compositor renders straight into a GBM buffer we hand it, and the
// frame never leaves the GPU. That is also the buffer an encoder wants: a DRM PRIME
// fd that maps into VAAPI without a copy.
//
// Still the measurement, not the recorder: the question is whether this reaches the
// 60fps the KMS path already gives us, because that decides whether window capture
// can be offered at the same quality as everything else.
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <gbm.h>
#include <wayland-client.h>
#include "hyprland-toplevel-export-v1-client-protocol.h"
#include "linux-dmabuf-v1-client-protocol.h"

#define NBUF 2   // ping-pong: request the next frame while the last one encodes

static struct zwp_linux_dmabuf_v1 *dmabuf;
static struct hyprland_toplevel_export_manager_v1 *mgr;
static struct gbm_device *gbm;

static uint32_t g_handle, FMT, W, H;
static struct { struct gbm_bo *bo; struct wl_buffer *wb; } bufs[NBUF];
static int nbufs, cur, have_params, running = 1, frames, failures, allocated;
static double deadline;

static double now_s(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

static void reg(void *d, struct wl_registry *r, uint32_t name, const char *i, uint32_t v) {
    (void)d; (void)v;
    if (!strcmp(i, zwp_linux_dmabuf_v1_interface.name))
        dmabuf = wl_registry_bind(r, name, &zwp_linux_dmabuf_v1_interface, 3);
    else if (!strcmp(i, hyprland_toplevel_export_manager_v1_interface.name))
        mgr = wl_registry_bind(r, name, &hyprland_toplevel_export_manager_v1_interface, 1);
}
static void reg_gone(void *d, struct wl_registry *r, uint32_t n) { (void)d;(void)r;(void)n; }
static const struct wl_registry_listener reg_l = { reg, reg_gone };

// zwp_linux_buffer_params_v1 answers created/failed; we create synchronously with
// create_immed instead, so these exist only to satisfy the listener.
static void params_created(void *d, struct zwp_linux_buffer_params_v1 *p, struct wl_buffer *b) {
    (void)d;(void)p;(void)b;
}
static void params_failed(void *d, struct zwp_linux_buffer_params_v1 *p) { (void)d;(void)p; }
static const struct zwp_linux_buffer_params_v1_listener params_l = {
    params_created, params_failed,
};

static int alloc_bufs(void) {
    for (int i = 0; i < NBUF; i++) {
        // SCANOUT|RENDERING asks for a layout the compositor can render into and a
        // GPU can sample -- the same constraints an encoder's import wants.
        struct gbm_bo *bo = gbm_bo_create(gbm, W, H, FMT,
                                          GBM_BO_USE_RENDERING | GBM_BO_USE_SCANOUT);
        if (!bo) bo = gbm_bo_create(gbm, W, H, FMT, GBM_BO_USE_RENDERING);
        if (!bo) { fprintf(stderr, "gbm_bo_create failed for %ux%u fmt %#x\n", W, H, FMT); return -1; }
        int fd = gbm_bo_get_fd(bo);
        if (fd < 0) { fprintf(stderr, "gbm_bo_get_fd failed\n"); return -1; }
        uint64_t mod = gbm_bo_get_modifier(bo);
        struct zwp_linux_buffer_params_v1 *p = zwp_linux_dmabuf_v1_create_params(dmabuf);
        zwp_linux_buffer_params_v1_add_listener(p, &params_l, NULL);
        zwp_linux_buffer_params_v1_add(p, fd, 0, gbm_bo_get_offset(bo, 0),
                                       gbm_bo_get_stride(bo),
                                       (uint32_t)(mod >> 32), (uint32_t)(mod & 0xFFFFFFFF));
        bufs[i].wb = zwp_linux_buffer_params_v1_create_immed(p, W, H, FMT, 0);
        zwp_linux_buffer_params_v1_destroy(p);
        close(fd);
        bufs[i].bo = bo;
        if (!bufs[i].wb) { fprintf(stderr, "create_immed failed\n"); return -1; }
        nbufs++;
    }
    return 0;
}

static void shoot(void);

static void on_dmabuf(void *d, struct hyprland_toplevel_export_frame_v1 *f,
                      uint32_t format, uint32_t w, uint32_t h) {
    (void)d; (void)f;
    FMT = format; W = w; H = h; have_params = 1;
}
static void on_buffer(void *d, struct hyprland_toplevel_export_frame_v1 *f,
                      uint32_t fo, uint32_t w, uint32_t h, uint32_t s) {
    (void)d;(void)f;(void)fo;(void)w;(void)h;(void)s;   // shm offered too; ignored
}
static void on_damage(void *d, struct hyprland_toplevel_export_frame_v1 *f,
                      uint32_t x, uint32_t y, uint32_t w, uint32_t h) {
    (void)d;(void)f;(void)x;(void)y;(void)w;(void)h;
}
static void on_flags(void *d, struct hyprland_toplevel_export_frame_v1 *f, uint32_t fl) {
    (void)d;(void)f;(void)fl;
}

static void on_buffer_done(void *d, struct hyprland_toplevel_export_frame_v1 *f) {
    (void)d;
    if (!have_params) { fprintf(stderr, "no linux_dmabuf params offered\n");
                        hyprland_toplevel_export_frame_v1_destroy(f); running = 0; return; }
    if (!allocated) {
        if (alloc_bufs() < 0) { running = 0; return; }
        allocated = 1;
    }
    hyprland_toplevel_export_frame_v1_copy(f, bufs[cur].wb, 1);
}

static void on_ready(void *d, struct hyprland_toplevel_export_frame_v1 *f,
                     uint32_t hi, uint32_t lo, uint32_t ns) {
    (void)d;(void)hi;(void)lo;(void)ns;
    frames++;
    cur = (cur + 1) % nbufs;
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
    if (argc < 3) { fprintf(stderr, "usage: spike_dmabuf <0xADDRESS> <seconds>\n"); return 2; }
    g_handle = (uint32_t)(strtoull(argv[1], NULL, 0) & 0xFFFFFFFFULL);
    double secs = atof(argv[2]);

    int drm = open("/dev/dri/renderD128", O_RDWR | O_CLOEXEC);
    if (drm < 0) { perror("renderD128"); return 1; }
    gbm = gbm_create_device(drm);
    if (!gbm) { fprintf(stderr, "gbm_create_device failed\n"); return 1; }

    struct wl_display *dpy = wl_display_connect(NULL);
    if (!dpy) { fprintf(stderr, "no wayland display\n"); return 1; }
    struct wl_registry *r = wl_display_get_registry(dpy);
    wl_registry_add_listener(r, &reg_l, NULL);
    wl_display_roundtrip(dpy);
    if (!mgr || !dmabuf) {
        fprintf(stderr, "missing %s\n", mgr ? "zwp_linux_dmabuf_v1" : "toplevel export manager");
        return 1;
    }

    double t0 = now_s();
    deadline = t0 + secs;
    shoot();
    while (running && wl_display_dispatch(dpy) != -1) { }
    double el = now_s() - t0;
    fprintf(stderr, "{\"path\":\"dmabuf\",\"frames\":%d,\"failed\":%d,\"seconds\":%.3f,"
                    "\"fps\":%.2f,\"w\":%u,\"h\":%u,\"fourcc\":\"%c%c%c%c\",\"buffers\":%d}\n",
            frames, failures, el, el > 0 ? frames / el : 0.0, W, H,
            FMT & 0xff, (FMT >> 8) & 0xff, (FMT >> 16) & 0xff, (FMT >> 24) & 0xff, nbufs);
    return 0;
}
