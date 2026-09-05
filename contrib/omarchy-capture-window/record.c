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

#include <poll.h>
#include <pthread.h>
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

#define NBUF 4   // rendering into one, one queued, one encoding, one spare
// A window hidden for a minute should not cost a minute of repeated frames in one
// burst. Past this the take simply has a shorter gap than real time.
#define MAX_GAP_FILL 300

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

// The fd, the descriptor and the AVFrame all live as long as the buffer does.
//
// THE BUG THIS SHAPE EXISTS FOR: the first attempt built a descriptor per frame and
// closed the fd as soon as av_buffersrc_add_frame_flags returned. VAAPI imports the
// object later, when the filter graph actually maps it, and by then the fd was gone --
// which surfaces as "Failed to create surface from DRM object: 2 (resource allocation
// failed)". That was read as "this driver cannot import BGRA" and very nearly bought
// an EGL implementation nobody needed.
enum { B_FREE = 0, B_CAPTURING, B_FILLED, B_ENCODING };

struct buf {
    struct gbm_bo *bo;
    struct wl_buffer *wb;
    int fd;
    AVDRMFrameDescriptor desc;
    AVFrame *frame;
    int state;
    int64_t pts;
};
static struct buf bufs[NBUF];
static int nbufs, have_params, allocated;
static uint32_t FMT, W, H;
static AVBufferRef *drm_dev, *drm_frames;

// ---------------------------------------------------------------- ffmpeg

static AVFormatContext *ofmt;
static AVStream *vstream;
static AVCodecContext *enc;
static AVBufferRef *va_dev;
static AVFilterGraph *graph;
static AVFilterContext *fsrc, *fsink;
static AVFrame *filt_frame;
// The last frame handed to the encoder, kept so a gap can be filled with it.
static AVFrame *held;
static int64_t last_sent = -1;
static AVPacket *pkt;

static volatile sig_atomic_t stop_now;
static int consec_fail;

// The encode runs on its own thread. Not for CPU parallelism -- this loop is idle
// ~99% of the time either way -- but because avcodec_send_frame ends in a vaSync that
// blocks until the VCN has finished the frame. On the Wayland thread that wait was
// the last serial link: the compositor could not be asked for the next frame until
// the previous one had finished ENCODING, on a different engine that was otherwise
// free to run in parallel.
static pthread_t enc_thread;
static pthread_mutex_t q_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t q_cond = PTHREAD_COND_INITIALIZER;
static int enc_running;
static int64_t dropped;

// A FIFO of buffer indices, not a scan for "the first one marked FILLED".
//
// Scanning by array index hands the encoder whichever buffer sits lowest in the
// array, which is arrival order only by accident. It reordered frames within a few
// hundred milliseconds and the muxer rejected the take outright: "Application provided
// invalid, non monotonically increasing dts to muxer in stream 0: 2560 >= 2304".
static int q[NBUF], qhead, qtail, qcount;
static int64_t frames_in, frames_out;
static int64_t first_frame_mono_us, first_frame_real_us, frame_mono_us, last_pts = -1;
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
static void encode_bo(struct buf *b, int64_t pts) {
    b->frame->pts = pts;
    int r = av_buffersrc_add_frame_flags(fsrc, b->frame, AV_BUFFERSRC_FLAG_KEEP_REF);
    if (r < 0) die("buffersrc", r);

    while (1) {
        r = av_buffersink_get_frame(fsink, filt_frame);
        if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return;
        if (r < 0) die("buffersink", r);

        // CONSTANT FRAME RATE, by repeating the last frame across any gap.
        //
        // Placing frames at their true times and leaving holes produced an honest
        // VFR file -- and an unusable one. probe.fps prefers avg_frame_rate, which
        // for a gapped stream is the AVERAGE (510/11 = 46.4) rather than the grid the
        // frames actually sit on (r_frame_rate = 60). That number becomes the
        // bundle's Timebase, and every frame index downstream -- cuts, zoom, the
        // cursor track -- is computed against it. A repeat is also what a dropped
        // frame looks like to a viewer: the picture did not change.
        if (held && last_sent >= 0 && pts > last_sent + 1) {
            int64_t gap = pts - last_sent - 1;
            if (gap > MAX_GAP_FILL) gap = MAX_GAP_FILL;   // a long hide, not a drop
            for (int64_t g = 0; g < gap; g++) {
                av_frame_unref(filt_frame);
                if (av_frame_ref(filt_frame, held) < 0) break;
                filt_frame->pts = last_sent + 1 + g;
                drain(0);
            }
            last_sent += gap;
            av_frame_unref(filt_frame);
            if (av_buffersink_get_frame(fsink, filt_frame) < 0) return;
        }

        filt_frame->pts = pts;
        av_frame_unref(held);
        av_frame_ref(held, filt_frame);
        last_sent = pts;
        drain(0);
        av_frame_unref(filt_frame);
    }
}

// ---------------------------------------------------------------- capture

static void shoot(void);

static void *encoder_main(void *u) {
    (void)u;
    for (;;) {
        pthread_mutex_lock(&q_lock);
        while (qcount == 0) {
            if (!enc_running) { pthread_mutex_unlock(&q_lock); return NULL; }
            pthread_cond_wait(&q_cond, &q_lock);
        }
        int i = q[qhead];
        qhead = (qhead + 1) % NBUF;
        qcount--;
        bufs[i].state = B_ENCODING;
        int64_t pts = bufs[i].pts;
        pthread_mutex_unlock(&q_lock);

        encode_bo(&bufs[i], pts);

        pthread_mutex_lock(&q_lock);
        bufs[i].state = B_FREE;
        pthread_cond_broadcast(&q_cond);
        pthread_mutex_unlock(&q_lock);
    }
}

// Hand this buffer to the encoder and take a free one for the next capture. Returns
// the index to render into, or -1 when every buffer is busy -- in which case the
// frame is dropped rather than waited for, because stalling here stalls the
// compositor too and a dropped frame is a repeat, not a hole.
static int hand_off(int filled, int64_t pts) {
    pthread_mutex_lock(&q_lock);
    if (filled >= 0) {
        bufs[filled].pts = pts;
        bufs[filled].state = B_FILLED;
        q[qtail] = filled;
        qtail = (qtail + 1) % NBUF;
        qcount++;
        pthread_cond_broadcast(&q_cond);
    }
    // Backpressure, not a retry loop. Asking the compositor for another frame while
    // every buffer is busy and immediately throwing it away spun hot -- 41k "drops"
    // in six seconds, which was the retry rate, not a frame rate. Waiting for a
    // buffer means the next frame is simply not REQUESTED until there is somewhere to
    // put it, so capture paces itself to the encoder.
    int next = -1;
    for (;;) {
        for (int k = 0; k < nbufs; k++)
            if (bufs[k].state == B_FREE) { next = k; bufs[k].state = B_CAPTURING; break; }
        if (next >= 0 || stop_now) break;
        struct timespec deadline;
        clock_gettime(CLOCK_REALTIME, &deadline);
        deadline.tv_nsec += 100 * 1000000L;
        if (deadline.tv_nsec >= 1000000000L) { deadline.tv_sec++; deadline.tv_nsec -= 1000000000L; }
        if (pthread_cond_timedwait(&q_cond, &q_lock, &deadline) == ETIMEDOUT) {
            dropped++;      // the encoder is wedged, not merely busy
            break;
        }
    }
    pthread_mutex_unlock(&q_lock);
    return next;
}

static void on_dmabuf(void *u, struct hyprland_toplevel_export_frame_v1 *f,
                      uint32_t fmt, uint32_t w, uint32_t h) {
    (void)u; (void)f;
    FMT = fmt; W = w; H = h; have_params = 1;
}
static void on_buffer(void *u, struct hyprland_toplevel_export_frame_v1 *f,
                      uint32_t fmt, uint32_t w, uint32_t h, uint32_t stride) {
    (void)u;(void)f;(void)fmt;(void)w;(void)h;(void)stride;   // shm offered too; unused
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
    if (!allocated) {
        setup_pipeline();
        allocated = 1;
        enc_running = 1;
        if (pthread_create(&enc_thread, NULL, encoder_main, NULL) != 0)
            die("pthread_create", 0);
        cur = hand_off(-1, 0);
    }
    if (cur < 0) {
        // Nothing free: skip this one and ask again. The compositor paces the retry,
        // so this does not spin.
        hyprland_toplevel_export_frame_v1_destroy(f);
        cur = hand_off(-1, 0);
        if (!stop_now) shoot();
        return;
    }
    hyprland_toplevel_export_frame_v1_copy(f, bufs[cur].wb, 1);
}

static void on_ready(void *u, struct hyprland_toplevel_export_frame_v1 *f,
                     uint32_t hi, uint32_t lo, uint32_t nsec) {
    (void)u;
    frame_mono_us = ((int64_t)hi << 32 | lo) * 1000000 + (int64_t)nsec / 1000;
    if (frame_mono_us <= 0) frame_mono_us = mono_us();
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
    // Ask for the NEXT frame before encoding this one. Encoding first left the
    // compositor idle for the whole map/upload/encode and made the buffer ring
    // pointless -- capture and encode were strictly alternating. Requesting first
    // means the compositor renders frame N+1 into another buffer while frame N is
    // still being encoded, which is the only thing NBUF was ever for.
    int done = cur;
    hyprland_toplevel_export_frame_v1_destroy(f);
    consec_fail = 0;
    // pts from the compositor's own stamp for THIS frame, not a running counter.
    //
    // `pts = frames_in++` on a 1/60 time base says every frame is 1/60s after the
    // last one, which is only true if nothing is ever dropped. It never held: a 4.02s
    // take that captured 94 frames became a 1.55s file playing 2.6x too fast, and any
    // recording that dropped anything was silently sped up in proportion. Placing each
    // frame at its real time leaves gaps where frames were missed, which is what a
    // gap IS, and the file's duration matches the take.
    int64_t rel = frame_mono_us - first_frame_mono_us;
    if (rel < 0) rel = 0;
    int64_t pts = (rel * o_fps + 500000) / 1000000;
    if (pts <= last_pts) pts = last_pts + 1;   // strictly increasing, as the muxer needs
    last_pts = pts;
    frames_in++;
    cur = hand_off(done, pts);
    if (!stop_now) shoot();
}

// A window that never yields a frame, or stops yielding for good, must not leave the
// recorder spinning. Asking for a bad address used to loop on `failed` forever with no
// output and no error -- the process simply never returned.
#define FAIL_NEVER_STARTED 120   // ~2s at 60Hz: no first frame means no such window
#define FAIL_GAVE_UP       600   // ~10s: it was there and is not coming back

static void on_failed(void *u, struct hyprland_toplevel_export_frame_v1 *f) {
    (void)u;
    hyprland_toplevel_export_frame_v1_destroy(f);
    consec_fail++;
    if (!have_first) {
        if (consec_fail >= FAIL_NEVER_STARTED)
            die("that window produced no frames -- check the address with "
                "`hyprctl clients -j`", 0);
    } else if (consec_fail >= FAIL_GAVE_UP) {
        fprintf(stderr, "omarchy-capture-window: window gone; closing the file\n");
        stop_now = 1;
        return;
    }
    // Hidden, on another workspace, or mid-resize. Repeating the last frame keeps the
    // timeline honest: the recording continues showing what the window last showed
    // rather than stalling or ending early.
    // No repeat frame here any more: the buffers belong to the encoder thread now,
    // and re-submitting one it may be reading is a race for a frame nobody asked for.
    // A gap in pts is already how a missing frame is represented.
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

// ---------------------------------------------------------------- setup

static void nofree(void *o, uint8_t *d) { (void)o; (void)d; }

static void params_created(void *u, struct zwp_linux_buffer_params_v1 *p, struct wl_buffer *b) {
    (void)u;(void)p;(void)b;
}
static void params_failed(void *u, struct zwp_linux_buffer_params_v1 *p) { (void)u;(void)p; }
static const struct zwp_linux_buffer_params_v1_listener params_l = {
    params_created, params_failed,
};

// DMA-BUF, so the frame never leaves the GPU: the compositor renders into these and
// VAAPI reads the same memory. The alternative (shm, then hwupload) turns the upload
// into a tiled blit on the GFX engine, and on a 2-CU iGPU that competes with the
// compositor's own rendering -- capture drops from 58 fps to 27 with nothing else
// changed. It is a GPU cost, not a CPU one, which is why "one less memcpy" reasoning
// got it wrong.
static void alloc_buffers(void) {
    for (int i = 0; i < NBUF; i++) {
        struct gbm_bo *bo = gbm_bo_create(gbm, W, H, FMT,
                                          GBM_BO_USE_RENDERING | GBM_BO_USE_SCANOUT);
        if (!bo) bo = gbm_bo_create(gbm, W, H, FMT, GBM_BO_USE_RENDERING);
        if (!bo) die("gbm_bo_create", 0);
        int fd = gbm_bo_get_fd(bo);
        if (fd < 0) die("gbm_bo_get_fd", 0);
        uint64_t mod = gbm_bo_get_modifier(bo);

        struct zwp_linux_buffer_params_v1 *p = zwp_linux_dmabuf_v1_create_params(dmabuf);
        zwp_linux_buffer_params_v1_add_listener(p, &params_l, NULL);
        zwp_linux_buffer_params_v1_add(p, fd, 0, gbm_bo_get_offset(bo, 0),
                                       gbm_bo_get_stride(bo),
                                       (uint32_t)(mod >> 32), (uint32_t)(mod & 0xFFFFFFFF));
        bufs[i].wb = zwp_linux_buffer_params_v1_create_immed(p, W, H, FMT, 0);
        zwp_linux_buffer_params_v1_destroy(p);
        if (!bufs[i].wb) die("create_immed", 0);

        bufs[i].bo = bo;
        bufs[i].fd = fd;                      // kept open: VAAPI imports it later
        AVDRMFrameDescriptor *d = &bufs[i].desc;
        memset(d, 0, sizeof *d);
        d->nb_objects = 1;
        d->objects[0].fd = fd;
        d->objects[0].size = lseek(fd, 0, SEEK_END);
        d->objects[0].format_modifier = mod;
        d->nb_layers = 1;
        d->layers[0].format = FMT;
        d->layers[0].nb_planes = 1;
        d->layers[0].planes[0].object_index = 0;
        d->layers[0].planes[0].offset = gbm_bo_get_offset(bo, 0);
        d->layers[0].planes[0].pitch = gbm_bo_get_stride(bo);

        AVFrame *f = av_frame_alloc();
        f->format = AV_PIX_FMT_DRM_PRIME;
        f->width = W; f->height = H;
        f->hw_frames_ctx = av_buffer_ref(drm_frames);
        // Stated, not inherited. The VPP produced limited-range pixels while the
        // encoder tagged them full, which reads as washed-out blacks; the shm path
        // only looked right because its defaults happened to agree.
        f->color_range = AVCOL_RANGE_JPEG;
        f->buf[0] = av_buffer_create((uint8_t *)d, sizeof *d, nofree, NULL,
                                     AV_BUFFER_FLAG_READONLY);
        f->data[0] = (uint8_t *)d;
        bufs[i].frame = f;
        nbufs++;
    }
}

static void setup_pipeline(void) {
    int r;
    // Devices and the DRM frames context FIRST: every buffer's AVFrame takes a
    // reference to drm_frames as it is created, so allocating buffers before it
    // exists dereferences null.
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

    alloc_buffers();

    // hwmap into VAAPI, then scale_vaapi for the BGRA -> NV12 the encoder wants.
    // Both stages run on the GPU; the pixels never touch the CPU.
    graph = avfilter_graph_alloc();
    // Allocated then configured then initialised, in that order and not the usual
    // avfilter_graph_create_filter one-shot: a hardware pix_fmt on the buffer source
    // is rejected unless hw_frames_ctx is already set, and the one-shot initialises
    // before there is anywhere to set it ("Setting BufferSourceContext.pix_fmt to a
    // HW format requires hw_frames_ctx to be non-NULL").
    fsrc = avfilter_graph_alloc_filter(graph, avfilter_get_by_name("buffer"), "in");
    if (!fsrc) die("buffer alloc", 0);
    AVBufferSrcParameters *bp = av_buffersrc_parameters_alloc();
    bp->format = AV_PIX_FMT_DRM_PRIME;
    bp->width = W; bp->height = H;
    bp->time_base = (AVRational){1, o_fps};
    bp->sample_aspect_ratio = (AVRational){1, 1};
    bp->hw_frames_ctx = drm_frames;
    r = av_buffersrc_parameters_set(fsrc, bp);
    av_free(bp);
    if (r < 0) die("buffersrc params", r);
    r = avfilter_init_str(fsrc, NULL);
    if (r < 0) die("buffersrc init", r);

    r = avfilter_graph_create_filter(&fsink, avfilter_get_by_name("buffersink"), "out",
                                     NULL, NULL, graph);
    if (r < 0) die("buffersink", r);

    // Built by hand rather than parsed from a string. hwupload needs its device
    // reference set BEFORE it is initialised, and avfilter_graph_parse_ptr
    // initialises as it parses -- "A hardware device reference is required to upload
    // frames to", with no opportunity to supply one. Allocating each filter, setting
    // the device, then initialising is the only order that works.
    // hwmap, not hwupload: the buffer is already on the GPU, so VAAPI wraps it in
    // place. derive_device gets the VAAPI device from the DRM one the frames come
    // from, which is also what makes the import legal.
    AVFilterContext *up = avfilter_graph_alloc_filter(graph,
                              avfilter_get_by_name("hwmap"), "map");
    if (!up) die("hwmap alloc", 0);
    r = avfilter_init_str(up, "derive_device=vaapi");
    if (r < 0) die("hwmap init", r);

    AVFilterContext *sc = avfilter_graph_alloc_filter(graph,
                              avfilter_get_by_name("scale_vaapi"), "sc");
    if (!sc) die("scale_vaapi alloc", 0);
    r = avfilter_init_str(sc, "format=nv12:out_range=pc");
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
    enc->color_range = AVCOL_RANGE_JPEG;   // matches scale_vaapi's out_range=pc
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
    // Fragmented: a die() or a SIGKILL after this point used to leave an mp4 with no
    // moov atom, which is an unplayable file and a lost take. Fragments are readable
    // up to wherever the writing stopped.
    AVDictionary *mux = NULL;
    av_dict_set(&mux, "movflags", "frag_keyframe+empty_moov+default_base_moof", 0);
    r = avformat_write_header(ofmt, &mux);
    av_dict_free(&mux);
    if (r < 0) die("write_header", r);

    filt_frame = av_frame_alloc();
    held = av_frame_alloc();
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

    int drm_fd = open(o_render, O_RDWR | O_CLOEXEC);
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

    // A polled loop rather than wl_display_dispatch, because dispatch BLOCKS and an
    // unknown window handle produces no events at all -- not even `failed`. Asking
    // for a bad address simply hung forever, with no output and no error. The poll
    // timeout is also what makes SIGTERM land promptly instead of waiting for the
    // next frame.
    while (!stop_now) {
        while (wl_display_prepare_read(dpy) != 0)
            wl_display_dispatch_pending(dpy);
        wl_display_flush(dpy);
        struct pollfd pfd = { .fd = wl_display_get_fd(dpy), .events = POLLIN };
        int n = poll(&pfd, 1, 200);
        if (n > 0) {
            wl_display_read_events(dpy);
            wl_display_dispatch_pending(dpy);
        } else {
            wl_display_cancel_read(dpy);
            if (n < 0 && errno != EINTR) break;
        }
        if (!have_first && now_s() - t0 > 3.0)
            die("that window produced no frames in 3s -- check the address with "
                "`hyprctl clients -j`", 0);
    }

    if (allocated) {
        pthread_mutex_lock(&q_lock);
        enc_running = 0;
        pthread_cond_broadcast(&q_cond);
        pthread_mutex_unlock(&q_lock);
        pthread_join(enc_thread, NULL);
        drain(1);
        av_write_trailer(ofmt);
        avio_closep(&ofmt->pb);
    }
    double el = now_s() - t0;

    if (o_ts && have_first) {
        // gsr's two-column sidecar, read by omarchy_studio.capture.read_gsr_ts.
        // gsr's exact layout: a header naming the columns, then one tab-separated
        // row. capture.read_gsr_ts skips the header and wants both numbers on ONE
        // line -- two lines of one number each parse to nothing and the anchor is
        // lost, which puts every event track at the wrong offset.
        FILE *f = fopen(o_ts, "w");
        if (f) {
            fprintf(f, "monotonic_microsec\trealtime_microsec\n%lld\t%lld\n",
                    (long long)first_frame_mono_us, (long long)first_frame_real_us);
            fclose(f);
        }
    }
    fprintf(stderr, "{\"frames_in\":%lld,\"frames_out\":%lld,\"dropped\":%lld,"
                    "\"seconds\":%.3f,\"fps\":%.2f,\"w\":%u,\"h\":%u}\n",
            (long long)frames_in, (long long)frames_out, (long long)dropped, el,
            el > 0 ? frames_out / el : 0.0, W, H);
    return 0;
}
