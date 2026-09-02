#!/bin/bash
# SPIKE 8 / ROUTE 1 -- ffmpeg single-open camera fan-out.
#
# One ffmpeg process opens /dev/video0 ONCE and produces:
#   cam.mp4        h264_vaapi recording (the file the editor composites later)
#   cam.tsv        mkvtimestamp_v2 sidecar: per-frame CLOCK_REALTIME in ms
#   a live self-view in mpv, fed the camera's NATIVE MJPEG with zero re-encode
#
# Verified on this machine 2026-09-02. Zero new packages.
#
# Notes proven by the spike:
#  - the preview branch is wrapped in ffmpeg's `fifo` pseudo-muxer so that a
#    dying preview CANNOT kill the recording (a plain `| mpv` pipe does).
#  - `-ts abs -copyts` puts true wallclock in the pts, so frame 0's capture time
#    is `ffprobe -show_entries format=start_time`. No gsr .ts file needed.
#  - device open -> first frame took 661 ms in one measured run, so you must NOT
#    use the launcher's own wallclock as the anchor.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
DEV=${DEV:-/dev/video0}
W=${W:-1280}
H=${H:-720}
FPS=${FPS:-30}
SECS=${SECS:-10}
OUT=${OUT:-$DIR/cam.mp4}
TSV=${TSV:-$DIR/cam.tsv}
FIFO=${FIFO:-$DIR/.camprev.fifo}

rm -f "$FIFO"; mkfifo "$FIFO"
trap 'rm -f "$FIFO"' EXIT

ffmpeg -hide_banner -loglevel warning -y \
  -vaapi_device /dev/dri/renderD128 \
  -copyts -f v4l2 -ts abs -input_format mjpeg -video_size "${W}x${H}" -framerate "$FPS" -i "$DEV" \
  -map 0:v -vf 'format=nv12,hwupload' -c:v h264_vaapi -b:v 6M -frames:v $((FPS * SECS)) "$OUT" \
  -map 0:v -c:v copy -frames:v $((FPS * SECS)) -f mkvtimestamp_v2 "$TSV" \
  -map 0:v -c:v copy -frames:v $((FPS * SECS)) \
    -f fifo -fifo_format mpjpeg -queue_size 30 -drop_pkts_on_overflow 1 \
    -attempt_recovery 1 -recover_any_error 1 -max_recovery_attempts 100 -recovery_wait_time 1 \
    "$FIFO" &
FF=$!

# Self-view. NOTE: mpv could not open the FIFO directly in testing; relay with
# cat, which also lets the preview be restarted without touching the recorder.
sleep 0.5
( cat "$FIFO" | mpv --demuxer-lavf-format=mpjpeg --profile=low-latency \
    --untimed --no-cache --cache=no --title=WebcamOverlay \
    --wayland-app-id=WebcamOverlay --no-border --no-audio --no-osc --osd-level=0 \
    --really-quiet fd://0 ) &>/dev/null &

wait "$FF"
echo "recording : $OUT"
echo "frame-0 wallclock: $(ffprobe -v error -show_entries format=start_time -of csv=p=0 "$OUT")"
echo "sidecar   : $TSV ($(wc -l <"$TSV") lines)"
