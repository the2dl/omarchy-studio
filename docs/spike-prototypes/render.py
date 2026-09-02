#!/usr/bin/env python3
"""
SPIKE 4 -- parameter-driven ffmpeg filtergraph renderer.

Builds ONE ffmpeg filter_complex from a parameter dict:
  * gradient / solid backdrop
  * screen video inset with padding, antialiased rounded corners, drop shadow
  * webcam overlay from a SEPARATE file: on/off, arbitrary x/y/size, circle or rounded-rect
  * auto-zoom driven by a click-event list, smootherstep easing, toggleable

Graph structure (the ordering matters -- see NOTES):

    gradients|color  (INFINITE, rate=fps)  ->  [bgi]
    [bgi] + shadow(1 frame, eof=repeat)    ->  [bg1]
    screen -> fps -> zoompan -> scale -> alphamerge(rounded mask)
           -> pad to canvas with TRANSPARENT colour                  -> [content0]
    [content0] + ring(1f) + cam            ->  [content]   (timeline == screen)
    [bg1] + [content] overlay=shortest=1   ->  [vout]

NOTES / gotchas found the hard way:
  * A 1-frame `color`/`gradients` source used as the overlay MAIN input drags its
    1/1 timebase into the output: the first version of this script produced a
    208-SECOND file from a 6.9 s clip. The fix is the shape above -- the
    length- and timebase-defining stream must be the real video, reached via
    `shortest=1` against an infinite backdrop.
  * Every mask is made in-graph from a 1-frame lavfi source + `geq`, so `geq`
    (which is a per-pixel interpreter) runs ONCE, not per frame.
  * zoom z/x/y expressions use st()/ld() accumulator registers so the cost is
    O(clicks) instead of O(clicks) copies of a 5x-duplicated smootherstep.

Usage:
  render.py --out X.mp4 [--set key=value ...] [--print-graph-only]
"""
import argparse, json, os, subprocess, sys, time

# ---------------------------------------------------------------- parameters
DEFAULTS = {
    "out_w": 1920, "out_h": 1080, "fps": 30,

    "bg_mode": "gradient",                 # "gradient" | "solid"
    "bg_c0": "#1b2340", "bg_c1": "#5a2f6b", "bg_solid": "#101014",

    "screen_pad": 72,
    "screen_radius": 22,
    "shadow": True,
    "shadow_blur": 26.0,
    "shadow_dx": 0, "shadow_dy": 18,
    "shadow_opacity": 0.6,
    "shadow_margin": 96,

    "cam_on": True,
    "cam_x": 1490, "cam_y": 690,
    "cam_size": 300,
    "cam_shape": "circle",                 # "circle" | "rounded" | "rect"
    "cam_radius": 32,
    "cam_ring": 4,
    "cam_ring_color": "#f2f2f7",

    "zoom_on": True,
    "zoom_level": 1.9,
    "zoom_in": 0.45, "zoom_hold": 1.0, "zoom_out": 0.65,
    "clicks": [],                          # [[t_sec, x_px, y_px], ...] in SOURCE coords

    "vaapi": False,
    "crf": 20, "preset": "medium",
}

def hexcol(h):
    return "0x" + h.lstrip("#")

# ----------------------------------------------------------- mask expressions
def rounded_rect_geq(w, h, r):
    """Antialiased rounded-rect coverage, ~1 px of edge softening."""
    if r <= 0:
        return "255"
    dx = f"max(max({r}-X,X-({w}-1-{r})),0)"
    dy = f"max(max({r}-Y,Y-({h}-1-{r})),0)"
    return f"clip(255*({r}-hypot({dx},{dy})+0.5),0,255)"

def circle_geq(size):
    r = size / 2.0
    return f"clip(255*({r}-hypot(X-{r}+0.5,Y-{r}+0.5)+0.5),0,255)"

# ------------------------------------------------------------ zoom expressions
#
# ffmpeg's expression parser (libavutil/eval.c) has a RECURSION BUDGET of 100.
# It is consumed by any LEFT/RIGHT-LINEAR chain: `a;b;c;...` and `a+b+c+...`
# alike. Measured on this machine: 98 semicolons OK, 100 FAIL; a 96-term `+`
# chain OK, a 128-term one FAIL.  A *balanced* nesting `((a+b)+(c+d))` is only
# log2(N) deep and survives 4096 terms.
#
# So the envelope for ONE click is a small parenthesised st()/ld() group
# (fixed depth, no duplication of the quintic), and the clicks are combined
# with balanced max/sum TREES. That lifts the ceiling from 10 clicks to
# thousands while keeping the per-frame evaluation cost linear in click count.

def _envelope(t, ti, th, to):
    """Parenthesised sub-expression evaluating to this click's 0..1 envelope."""
    S = lambda r: (f"st({r},ld({r})*ld({r})*ld({r})*(ld({r})*(ld({r})*6-15)+10))")
    return (f"(st(0,clip((time-{t:.4f})/{ti},0,1));{S(0)};"
            f"st(9,clip((time-{t + ti + th:.4f})/{to},0,1));{S(9)};"
            f"ld(0)*(1-ld(9)))")

def _tree(terms, op):
    """Balanced binary nesting: depth log2(N) instead of N."""
    if len(terms) == 1:
        return terms[0]
    m = len(terms) // 2
    a, b = _tree(terms[:m], op), _tree(terms[m:], op)
    return f"max({a},{b})" if op == "max" else f"({a}+{b})"

def zoom_exprs(p):
    if not p["zoom_on"] or not p["clicks"]:
        return None
    ti, th, to = p["zoom_in"], p["zoom_hold"], p["zoom_out"]
    envs = [_envelope(t, ti, th, to) for (t, _, _) in p["clicks"]]

    E = _tree(envs, "max")                                        # peak envelope
    den = _tree(envs, "+")                                        # sum e_i
    numx = _tree([f"({e}*{cx})" for e, (_, cx, _) in zip(envs, p["clicks"])], "+")
    numy = _tree([f"({e}*{cy})" for e, (_, _, cy) in zip(envs, p["clicks"])], "+")

    Z = p["zoom_level"] - 1.0
    z = f"1+{Z:.6f}*({E})"
    # store den once per expression so it is not evaluated twice
    cx = f"st(1,{den});if(gt(ld(1),0.0001),({numx})/ld(1),iw/2)"
    cy = f"st(1,{den});if(gt(ld(1),0.0001),({numy})/ld(1),ih/2)"
    # zoompan: x,y are the crop top-left in INPUT coords; crop is iw/zoom x ih/zoom
    x = f"st(2,({cx}));clip(ld(2)-(iw/zoom/2),0,iw-iw/zoom)"
    y = f"st(3,({cy}));clip(ld(3)-(ih/zoom/2),0,ih-ih/zoom)"
    return z, x, y

# ----------------------------------------------------------------- the graph
def build(p, screen_wh, cam_wh):
    W, H, FPS = p["out_w"], p["out_h"], p["fps"]
    sw, sh = screen_wh
    g = []

    # ---- 1. backdrop, INFINITE at the output rate --------------------------
    if p["bg_mode"] == "gradient":
        g.append(f"gradients=s={W}x{H}:c0={hexcol(p['bg_c0'])}:c1={hexcol(p['bg_c1'])}"
                 f":x0=0:y0=0:x1={W}:y1={H}:r={FPS},format=rgba[bgi]")
    else:
        g.append(f"color=c={hexcol(p['bg_solid'])}:s={W}x{H}:r={FPS},format=rgba[bgi]")

    # ---- 2. screen geometry ------------------------------------------------
    sc = min((W - 2 * p["screen_pad"]) / sw, (H - 2 * p["screen_pad"]) / sh)
    dw, dh = (int(sw * sc) // 2) * 2, (int(sh * sc) // 2) * 2
    px, py = (W - dw) // 2, (H - dh) // 2

    # ---- 3. screen chain ---------------------------------------------------
    chain = [f"[0:v]fps={FPS}", "setsar=1"]
    ze = zoom_exprs(p)
    if ze:
        z, xe, ye = ze
        chain.append(f"zoompan=z='{z}':x='{xe}':y='{ye}':d=1:s={sw}x{sh}:fps={FPS}")
    chain += [f"scale={dw}:{dh}:flags=bicubic", "format=rgba[scr]"]
    g.append(",".join(chain))

    g.append(f"color=c=black:s={dw}x{dh}:r=1:d=1,format=gray,"
             f"geq=lum='{rounded_rect_geq(dw, dh, p['screen_radius'])}'[smaskraw]")
    g.append("[smaskraw]split=2[smask_a][smask_b]")
    g.append("[scr][smask_a]alphamerge=repeatlast=1:shortest=0[scr_r]")
    # transparent canvas whose timeline == the screen stream
    g.append(f"[scr_r]pad={W}:{H}:{px}:{py}:color=black@0.0,format=rgba[content0]")

    # ---- 4. drop shadow (composited onto the BACKDROP, under the content) --
    if p["shadow"]:
        m = p["shadow_margin"]
        g.append(f"[smask_b]pad={dw+2*m}:{dh+2*m}:{m}:{m}:color=black,"
                 f"gblur=sigma={p['shadow_blur']}:steps=3[shadow_a]")
        g.append(f"color=c=black:s={dw+2*m}x{dh+2*m}:r=1:d=1,format=rgba[shadow_c]")
        g.append(f"[shadow_c][shadow_a]alphamerge,"
                 f"colorchannelmixer=aa={p['shadow_opacity']}[shadow]")
        g.append(f"[bgi][shadow]overlay=x={px - m + p['shadow_dx']}:y={py - m + p['shadow_dy']}"
                 f":eof_action=repeat:shortest=0:format=auto[bg1]")
    else:
        g.append("[smask_b]nullsink")
        g.append("[bgi]null[bg1]")

    # ---- 5. webcam, composited into the CONTENT canvas --------------------
    if p["cam_on"]:
        cs, (cw, ch) = p["cam_size"], cam_wh
        side = min(cw, ch)
        g.append(f"[1:v]fps={FPS},crop={side}:{side}:{(cw-side)//2}:{(ch-side)//2},"
                 f"scale={cs}:{cs}:flags=bicubic,setsar=1,format=rgba[camsq]")

        if p["cam_shape"] == "circle":
            mexpr = circle_geq(cs)
        elif p["cam_shape"] == "rounded":
            mexpr = rounded_rect_geq(cs, cs, p["cam_radius"])
        else:
            mexpr = "255"
        g.append(f"color=c=black:s={cs}x{cs}:r=1:d=1,format=gray,geq=lum='{mexpr}'[cmask]")

        base = "[content0]"
        if p["cam_ring"] > 0 and p["cam_shape"] != "rect":
            rs = cs + 2 * p["cam_ring"]
            rexpr = (circle_geq(rs) if p["cam_shape"] == "circle"
                     else rounded_rect_geq(rs, rs, p["cam_radius"] + p["cam_ring"]))
            g.append(f"color=c=black:s={rs}x{rs}:r=1:d=1,format=gray,geq=lum='{rexpr}'[rmask]")
            g.append(f"color=c={hexcol(p['cam_ring_color'])}:s={rs}x{rs}:r=1:d=1,format=rgba[rcol]")
            g.append("[rcol][rmask]alphamerge[ring]")
            g.append(f"[content0][ring]overlay=x={p['cam_x'] - p['cam_ring']}:"
                     f"y={p['cam_y'] - p['cam_ring']}:eof_action=repeat:shortest=0"
                     f":format=auto[content1]")
            base = "[content1]"

        g.append("[camsq][cmask]alphamerge=repeatlast=1:shortest=0[cam]")
        g.append(f"{base}[cam]overlay=x={p['cam_x']}:y={p['cam_y']}"
                 f":eof_action=repeat:shortest=0:format=auto[content]")
    else:
        g.append("[content0]null[content]")

    # ---- 6. final composite; shortest=1 makes the CONTENT define the length -
    g.append("[bg1][content]overlay=x=0:y=0:shortest=1:format=auto[vfin]")
    tail = "format=yuv420p"
    if p["vaapi"]:
        tail = "format=nv12,hwupload"
    g.append(f"[vfin]{tail}[vout]")
    return ";".join(g), dict(screen_box=[px, py, dw, dh])

# --------------------------------------------------------------------- driver
def count_chains(graph):
    """Top-level ';' only -- expression semicolons live inside quotes/parens."""
    n, depth, q = 1, 0, False
    for c in graph:
        if c == "'": q = not q
        elif q: continue
        elif c == "(": depth += 1
        elif c == ")": depth -= 1
        elif c == ";" and depth == 0: n += 1
    return n

def probe(path):
    o = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "json", path],
                       capture_output=True, text=True).stdout
    s = json.loads(o)["streams"][0]
    return int(s["width"]), int(s["height"])

def load_params(args):
    p = dict(DEFAULTS)
    if args.params_json:
        p.update(json.load(open(args.params_json)))
    for kv in args.set:
        k, v = kv.split("=", 1)
        if k not in p:
            sys.exit(f"unknown param {k}")
        cur = p[k]
        if isinstance(cur, bool):    p[k] = v.lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):   p[k] = int(float(v))
        elif isinstance(cur, float): p[k] = float(v)
        elif isinstance(cur, list):  p[k] = json.loads(v)
        else:                        p[k] = v
    return p

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen", default="screen_1080.mp4")
    ap.add_argument("--cam", default="media_webcam_raw.mp4")
    ap.add_argument("--out", default="out.mp4")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--params-json")
    ap.add_argument("--graph-out")
    ap.add_argument("--print-graph-only", action="store_true")
    ap.add_argument("--frames", type=int, default=0)
    ap.add_argument("--ss", default=None)
    ap.add_argument("--still", action="store_true", help="write a single PNG instead of video")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    p = load_params(a)
    graph, meta = build(p, probe(a.screen), probe(a.cam) if p["cam_on"] else (640, 480))
    if a.graph_out:
        open(a.graph_out, "w").write(graph + "\n")
    if a.print_graph_only:
        print(graph); return

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if not a.quiet:
        cmd += ["-stats"]
    if p["vaapi"]:
        cmd += ["-vaapi_device", "/dev/dri/renderD128"]
    if a.ss:
        cmd += ["-ss", a.ss]
    cmd += ["-i", a.screen]
    if a.ss:
        cmd += ["-ss", a.ss]
    cmd += ["-i", a.cam]
    # argv is capped at ~128 KiB per string (E2BIG at ~288 KB observed); a big
    # click list blows past that, so spill the graph to a script file.
    if len(graph) > 60000:
        gp = a.out + ".filtergraph"
        open(gp, "w").write(graph)
        cmd += ["-/filter_complex", gp]
    else:
        cmd += ["-filter_complex", graph]
    cmd += ["-map", "[vout]"]

    if a.still:
        cmd += ["-frames:v", "1", "-update", "1"]
    else:
        cmd += ["-map", "0:a?"]
        if a.frames:
            cmd += ["-frames:v", str(a.frames)]
        if p["vaapi"]:
            cmd += ["-c:v", "h264_vaapi", "-b:v", "10M"]
        else:
            cmd += ["-c:v", "libx264", "-crf", str(p["crf"]),
                    "-preset", p["preset"], "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-y", a.out]

    t0 = time.time()
    r = subprocess.run(cmd, capture_output=a.quiet)
    el = time.time() - t0
    print(json.dumps({"elapsed": round(el, 3), "rc": r.returncode,
                      "graph_chars": len(graph), "filter_chains": count_chains(graph),
                      "out": a.out, "meta": meta}))
    sys.exit(r.returncode)

if __name__ == "__main__":
    main()
