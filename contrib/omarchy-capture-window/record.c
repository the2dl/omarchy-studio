// omarchy-capture-window -- record ONE window's own pixels.
//
// The frame path is the point: Hyprland renders the window's surface tree into a
// GBM buffer we hand it, that buffer's DRM PRIME fd is imported straight into
// VAAPI, and the encoder reads it there. Nothing is ever copied to the CPU, which
// is the difference between 44.8fps and display rate (see README).
//
// Flag vocabulary deliberately mirrors gpu-screen-recorder, because the recorder
// script already branches on kms|portal and this is meant to be a third value
// rather than a second way of doing everything.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <gbm.h>
#include <wayland-client.h>
#include <libavcodec/avcodec.h>
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavformat/avformat.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_drm.h>
#include <libavutil/opt.h>

#include "hyprland-toplevel-export-v1-client-protocol.h"
#include "linux-dmabuf-v1-client-protocol.h"

#define NBUF 3   // one being rendered into, one in flight, one free

// ---------------------------------------------------------------- options

static const char *o_out, *o_codec = "auto", *o_render = "/dev/dri/renderD128";
static uint32_t o_handle;
static int o_fps = 60, o_cursor = 1, o_quality = 23;
static const char *o_ts;          // gsr-compatible first-frame sidecar

// ---------------------------------------------------------------- wayland

static struct wl_display *dpy;
static struct zwp_linux_dmabuf_v1 *dmabuf;
static struct hyprland_toplevel_export_manager_v1 *mgr;
static struct gbm_device *gbm;
static int drm_fd = -1;

struct buf {
    struct gbm_bo *bo;
    struct wl_buffer *wb;
    int busy;
};
static struct buf bufs[NBUF];
static int nbufs, have_params, allocated;
static uint32_t FMT, W, H;

// ---------------------------------------------------------------- ffmpeg

static AVFormatContext *ofmt;
static AVStream *vstream;
static AVCodecContext *enc;
static AVBufferRef *drm_dev, *va_dev, *drm_frames;
static AVFilterGraph *graph;
static AVFilterContext *fsrc, *fsink;
static AVFrame *drm_frame, *filt_frame;
static AVPacket *pkt;

static volatile sig_atomic_t stop_now;
static int64_t frames_in, frames_out;
static int64_t first_frame_mono_us, first_frame_real_us;
static int have_first;

static double now_s(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static int64_t mono_us(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000 + t.tv_nsec / 1000;
}
static int64_t real_us(void) {
    struct timespec t; clock_gettime(CLOCK_REALTIME, &t);
    return (int64_t)t.tv_sec * 1000000 + t.tv_nsec / 1000;
}

static void die(const char *what, int err) {
    char msg[256] = "";
    if (err) av_strerror(err, msg, sizeof msg);
    fprintf(stderr, "omarchy-capture-window: %s%s%s\n", what, err ? ": " : "", msg);
    exit(1);
}

// ---------------------------------------------------------------- encode

static void drain(int flush) {
    int r = avcodec_send_frame(enc, flush ? NULL : filt_frame);
    if (r < 0 && r != AVERROR(EAGAIN)) die("send_frame", r);
    while (1) {
        r = avcodec_receive_packet(enc, pkt);
        if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return;
        if (r < 0) die("receive_packet", r);
        av_packet_rescale_ts(pkt, enc->time_base, vstream->time_base);
        pkt->stream_index = vstream->index;
        r = av_interleaved_write_frame(ofmt, pkt);
        if (r < 0) die("write_frame", r);
        av_packet_unref(pkt);
        frames_out++;
    }
}

// Wrap a GBM bo as a DRM PRIME AVFrame, push it through the GPU filter chain
// (hwmap into VAAPI, convert to nv12) and encode whatever comes out.
static void encode_bo(struct gbm_bo *bo, int64_t pts) {
    void *map_data = NULL;
    uint32_t stride = 0;
    void *src = gbm_bo_map(bo, 0, 0, W, H, GBM_BO_TRANSFER_READ, &stride, &map_data);
    if (!src) die("gbm_bo_map", 0);

    drm_frame->data[0] = src;
    drm_frame->linesize[0] = (int)stride;
    drm_frame->pts = pts;
    int r = av_buffersrc_add_frame_flags(fsrc, drm_frame,
                                         AV_BUFFERSRC_FLAG_KEEP_REF);
    gbm_bo_unmap(bo, map_data);
    if (r < 0) die("buffersrc", r);

    while (1) {
        r = av_buffersink_get_frame(fsink, filt_frame);
        if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return;
        if (r < 0) die("buffersink", r);
        filt_frame->pts = pts;
        drain(0);
        av_frame_unref(filt_frame);
    }
}

// ---------------------------------------------------------------- capture

static void shoot(void);

static void on_dmabuf(void *u, struct hyprland_toplevel_export_frame_v1 *f,
                      uint32_t fmt, uint32_t w, uint32_t h) {
    (void)u; (void)f;
    FMT = fmt; W = w; H = h; have_params = 1;
}
static void on_buffer(void *u, struct hyprland_toplevel_export_frame_v1 *f,
                      uint32_t a, uint32_t b, uint32_t c, uint32_t d) {
    (void)u;(void)f;(void)a;(void)b;(void)c;(void)d;
}
static void on_damage(void *u, struct hyprland_toplevel_export_frame_v1 *f,
                      uint32_t a, uint32_t b, uint32_t c, uint32_t d) {
    (void)u;(void)f;(void)a;(void)b;(void)c;(void)d;
}
static void on_flags(void *u, struct hyprland_toplevel_export_frame_v1 *f, uint32_t x) {
    (void)u;(void)f;(void)x;
}

static int cur;
static void setup_pipeline(void);

static void on_buffer_done(void *u, struct hyprland_toplevel_export_frame_v1 *f) {
    (void)u;
    if (!have_params) { hyprland_toplevel_export_frame_v1_destroy(f); stop_now = 1; return; }
    if (!allocated) { setup_pipeline(); allocated = 1; }
    hyprland_toplevel_export_frame_v1_copy(f, bufs[cur].wb, 1);
}

static void on_ready(void *u, struct hyprland_toplevel_export_frame_v1 *f,
                     uint32_t hi, uint32_t lo, uint32_t nsec) {
    (void)u;
    if (!have_first) {
        // The compositor's own stamp for this frame, which is CLOCK_MONOTONIC. Pair
        // it with a REALTIME sample taken now: the sidecar carries both so the event
        // tracks can be lined up against the video the way gsr's does.
        // ready() carries tv_sec split across two u32s plus tv_nsec -- SECONDS, not
        // a microsecond count. Treating the pair as microseconds put the anchor at
        // 36us past the epoch and would have placed every event track at the wrong
        // end of the recording.
        first_frame_mono_us = ((int64_t)hi << 32 | lo) * 1000000 + (int64_t)nsec / 1000;
        if (first_frame_mono_us <= 0) first_frame_mono_us = mono_us();
        first_frame_real_us = real_us();
        have_first = 1;
    }
    encode_bo(bufs[cur].bo, frames_in++);
    cur = (cur + 1) % nbufs;
    hyprland_toplevel_export_frame_v1_destroy(f);
    if (!stop_now) shoot();
}

static void on_failed(void *u, struct hyprland_toplevel_export_frame_v1 *f) {
    (void)u;
    // The window is hidden, on another workspace, or gone. Repeating the last frame
    // keeps the timeline honest -- the recording continues, showing what the window
    // last showed, rather than stalling or ending.
    hyprland_toplevel_export_frame_v1_destroy(f);
    if (allocated && have_first) encode_bo(bufs[(cur + nbufs - 1) % nbufs].bo, frames_in++);
    if (!stop_now) shoot();
}

static const struct hyprland_toplevel_export_frame_v1_listener frame_l = {
    .buffer = on_buffer, .damage = on_damage, .flags = on_flags,
    .ready = on_ready, .failed = on_failed,
    .linux_dmabuf = on_dmabuf, .buffer_done = on_buffer_done,
};

static void shoot(void) {
    struct hyprland_toplevel_export_frame_v1 *f =
        hyprland_toplevel_export_manager_v1_capture_toplevel(mgr, o_cursor, o_handle);
    hyprland_toplevel_export_frame_v1_add_listener(f, &frame_l, NULL);
}

static void reg(void *u, struct wl_registry *r, uint32_t name, const char *i, uint32_t v) {
    (void)u; (void)v;
    if (!strcmp(i, zwp_linux_dmabuf_v1_interface.name))
        dmabuf = wl_registry_bind(r, name, &zwp_linux_dmabuf_v1_interface, 3);
    else if (!strcmp(i, hyprland_toplevel_export_manager_v1_interface.name))
        mgr = wl_registry_bind(r, name, &hyprland_toplevel_export_manager_v1_interface, 1);
}
static void reg_gone(void *u, struct wl_registry *r, uint32_t n) { (void)u;(void)r;(void)n; }
static const struct wl_registry_listener reg_l = { reg, reg_gone };

static void params_created(void *u, struct zwp_linux_buffer_params_v1 *p, struct wl_buffer *b) {
    (void)u;(void)p;(void)b;
}
static void params_failed(void *u, struct zwp_linux_buffer_params_v1 *p) { (void)u;(void)p; }
static const struct zwp_linux_buffer_params_v1_listener params_l = {
    params_created, params_failed,
};

// ---------------------------------------------------------------- setup

static void alloc_buffers(void) {
    for (int i = 0; i < NBUF; i++) {
        // LINEAR is not a default worth arguing with. GBM picks a tiled/DCC-compressed
        // layout for RENDERING|SCANOUT, and VAAPI refuses to import that: "Failed to
        // create surface from DRM object: 2 (resource allocation failed)". A linear
        // buffer costs the compositor some render bandwidth and buys an import that
        // works, which is the whole pipeline.
        struct gbm_bo *bo = gbm_bo_create(gbm, W, H, FMT,
                                          GBM_BO_USE_RENDERING | GBM_BO_USE_LINEAR);
        if (!bo) bo = gbm_bo_create(gbm, W, H, FMT, GBM_BO_USE_LINEAR);
        if (!bo) die("gbm_bo_create", 0);
        int fd = gbm_bo_get_fd(bo);
        uint64_t mod = gbm_bo_get_modifier(bo);
        struct zwp_linux_buffer_params_v1 *p = zwp_linux_dmabuf_v1_create_params(dmabuf);
        zwp_linux_buffer_params_v1_add_listener(p, &params_l, NULL);
        zwp_linux_buffer_params_v1_add(p, fd, 0, gbm_bo_get_offset(bo, 0),
                                       gbm_bo_get_stride(bo),
                                       (uint32_t)(mod >> 32), (uint32_t)mod);
        bufs[i].wb = zwp_linux_buffer_params_v1_create_immed(p, W, H, FMT, 0);
        zwp_linux_buffer_params_v1_destroy(p);
        close(fd);
        bufs[i].bo = bo;
        nbufs++;
    }
}

static void setup_pipeline(void) {
    int r;
    alloc_buffers();

    r = av_hwdevice_ctx_create(&drm_dev, AV_HWDEVICE_TYPE_DRM, o_render, NULL, 0);
    if (r < 0) die("drm hwdevice", r);
    r = av_hwdevice_ctx_create_derived(&va_dev, AV_HWDEVICE_TYPE_VAAPI, drm_dev, 0);
    if (r < 0) die("vaapi hwdevice", r);

    drm_frames = av_hwframe_ctx_alloc(drm_dev);
    if (!drm_frames) die("drm frames ctx", 0);
    AVHWFramesContext *fc = (AVHWFramesContext *)drm_frames->data;
    fc->format = AV_PIX_FMT_DRM_PRIME;
    fc->sw_format = AV_PIX_FMT_BGRA;
    fc->width = W; fc->height = H;
    r = av_hwframe_ctx_init(drm_frames);
    if (r < 0) die("drm frames init", r);

    // hwmap into VAAPI, then scale_vaapi for the BGRA -> NV12 the encoder wants.
    // Both stages run on the GPU; the pixels never touch the CPU.
    graph = avfilter_graph_alloc();
    // Allocated then configured then initialised, in that order and not the usual
    // avfilter_graph_create_filter one-shot: a hardware pix_fmt on the buffer source
    // is rejected unless hw_frames_ctx is already set, and the one-shot initialises
    // before there is anywhere to set it ("Setting BufferSourceContext.pix_fmt to a
    // HW format requires hw_frames_ctx to be non-NULL").
    char args[256];
    snprintf(args, sizeof args,
             "video_size=%ux%u:pix_fmt=%d:time_base=1/%d:pixel_aspect=1/1",
             W, H, AV_PIX_FMT_BGRA, o_fps);
    r = avfilter_graph_create_filter(&fsrc, avfilter_get_by_name("buffer"), "in",
                                     args, NULL, graph);
    if (r < 0) die("buffer filter", r);

    r = avfilter_graph_create_filter(&fsink, avfilter_get_by_name("buffersink"), "out",
                                     NULL, NULL, graph);
    if (r < 0) die("buffersink", r);

    // Built by hand rather than parsed from a string. hwupload needs its device
    // reference set BEFORE it is initialised, and avfilter_graph_parse_ptr
    // initialises as it parses -- "A hardware device reference is required to upload
    // frames to", with no opportunity to supply one. Allocating each filter, setting
    // the device, then initialising is the only order that works.
    AVFilterContext *up = avfilter_graph_alloc_filter(graph,
                              avfilter_get_by_name("hwupload"), "up");
    if (!up) die("hwupload alloc", 0);
    up->hw_device_ctx = av_buffer_ref(va_dev);
    r = avfilter_init_str(up, NULL);
    if (r < 0) die("hwupload init", r);

    AVFilterContext *sc = avfilter_graph_alloc_filter(graph,
                              avfilter_get_by_name("scale_vaapi"), "sc");
    if (!sc) die("scale_vaapi alloc", 0);
    r = avfilter_init_str(sc, "format=nv12");
    if (r < 0) die("scale_vaapi init", r);

    if ((r = avfilter_link(fsrc, 0, up, 0)) < 0) die("link src->up", r);
    if ((r = avfilter_link(up, 0, sc, 0)) < 0) die("link up->sc", r);
    if ((r = avfilter_link(sc, 0, fsink, 0)) < 0) die("link sc->sink", r);

    r = avfilter_graph_config(graph, NULL);
    if (r < 0) die("graph config", r);

    const char *cname = o_codec;
    if (!strcmp(cname, "auto"))
        cname = (W > 4096 || H > 4096) ? "hevc_vaapi" : "h264_vaapi";
    const AVCodec *codec = avcodec_find_encoder_by_name(cname);
    if (!codec) die("no such encoder", 0);
    enc = avcodec_alloc_context3(codec);
    enc->width = W; enc->height = H;
    enc->pix_fmt = AV_PIX_FMT_VAAPI;
    enc->time_base = (AVRational){1, o_fps};
    enc->framerate = (AVRational){o_fps, 1};
    enc->color_range = AVCOL_RANGE_JPEG;   // full range, as the rest of the pipeline
    enc->hw_frames_ctx = av_buffer_ref(av_buffersink_get_hw_frames_ctx(fsink));
    av_opt_set_int(enc->priv_data, "qp", o_quality, 0);

    r = avformat_alloc_output_context2(&ofmt, NULL, NULL, o_out);
    if (r < 0) die("output context", r);
    vstream = avformat_new_stream(ofmt, NULL);
    if (ofmt->oformat->flags & AVFMT_GLOBALHEADER)
        enc->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    r = avcodec_open2(enc, codec, NULL);
    if (r < 0) die("open encoder", r);
    r = avcodec_parameters_from_context(vstream->codecpar, enc);
    if (r < 0) die("stream params", r);
    vstream->time_base = enc->time_base;
    r = avio_open(&ofmt->pb, o_out, AVIO_FLAG_WRITE);
    if (r < 0) die("avio_open", r);
    r = avformat_write_header(ofmt, NULL);
    if (r < 0) die("write_header", r);

    drm_frame = av_frame_alloc();
    drm_frame->format = AV_PIX_FMT_BGRA;
    drm_frame->width = W; drm_frame->height = H;
    filt_frame = av_frame_alloc();
    pkt = av_packet_alloc();
}

static void on_signal(int s) { (void)s; stop_now = 1; }

int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-w") && i + 1 < argc)
            o_handle = (uint32_t)(strtoull(argv[++i], NULL, 0) & 0xFFFFFFFFULL);
        else if (!strcmp(argv[i], "-o") && i + 1 < argc) o_out = argv[++i];
        else if (!strcmp(argv[i], "-f") && i + 1 < argc) o_fps = atoi(argv[++i]);
        else if (!strcmp(argv[i], "-k") && i + 1 < argc) o_codec = argv[++i];
        else if (!strcmp(argv[i], "-q") && i + 1 < argc) o_quality = atoi(argv[++i]);
        else if (!strcmp(argv[i], "-cursor") && i + 1 < argc)
            o_cursor = !strcmp(argv[++i], "yes");
        else if (!strcmp(argv[i], "-write-first-frame-ts") && i + 1 < argc) o_ts = argv[++i];
        else {
            fprintf(stderr,
                "usage: omarchy-capture-window -w 0xADDRESS -o out.mp4 [-f 60] "
                "[-k auto|h264_vaapi|hevc_vaapi] [-q QP] [-cursor yes|no] "
                "[-write-first-frame-ts FILE]\n");
            return 2;
        }
    }
    if (!o_handle || !o_out) { fprintf(stderr, "need -w and -o\n"); return 2; }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGHUP, on_signal);

    drm_fd = open(o_render, O_RDWR | O_CLOEXEC);
    if (drm_fd < 0) die("open render node", 0);
    gbm = gbm_create_device(drm_fd);
    if (!gbm) die("gbm_create_device", 0);

    dpy = wl_display_connect(NULL);
    if (!dpy) die("no wayland display", 0);
    struct wl_registry *r = wl_display_get_registry(dpy);
    wl_registry_add_listener(r, &reg_l, NULL);
    wl_display_roundtrip(dpy);
    if (!mgr) die("compositor has no hyprland_toplevel_export_manager_v1", 0);
    if (!dmabuf) die("compositor has no zwp_linux_dmabuf_v1", 0);

    double t0 = now_s();
    shoot();
    while (!stop_now && wl_display_dispatch(dpy) != -1) { }

    if (allocated) {
        drain(1);
        av_write_trailer(ofmt);
        avio_closep(&ofmt->pb);
    }
    double el = now_s() - t0;

    if (o_ts && have_first) {
        // gsr's two-column sidecar, read by omarchy_studio.capture.read_gsr_ts.
        FILE *f = fopen(o_ts, "w");
        if (f) {
            fprintf(f, "%ld\n%ld\n", (long)first_frame_mono_us, (long)first_frame_real_us);
            fclose(f);
        }
    }
    fprintf(stderr, "{\"frames_in\":%lld,\"frames_out\":%lld,\"seconds\":%.3f,"
                    "\"fps\":%.2f,\"w\":%u,\"h\":%u}\n",
            (long long)frames_in, (long long)frames_out, el,
            el > 0 ? frames_out / el : 0.0, W, H);
    return 0;
}
