#!/usr/bin/env python3
"""
SPIKE 7 -- time-ranged overlay layers + cuts, as one generated ffmpeg filtergraph.

Extends the round-1 render.py idea from "a settings blob" to "a versioned list of
time-ranged layers".

CORE MODEL
----------
A project is:

  { "version": 2,
    "canvas":  {"w":1920,"h":1080,"fps":30},
    "sources": {"screen":"base.mp4", "cam":"cam.mp4"},
    "cuts":    [[5.0,8.0],[12.0,14.0]],       # SOURCE time, half-open
    "layers":  [ {...}, ... ] }               # z order = list order

Layer times ("t": [a,b]) are ALWAYS stored in SOURCE time.  The renderer maps
them to output time.  See map_interval()/s2o().

THE ONE OVERLAY PRIMITIVE
-------------------------
Every non-redact layer compiles to the same three things:

    an RGBA tile of size w x h   ->   an alpha ramp (fade in/out)   ->
    overlay at (x,y) gated by enable='gte(t,A)*lt(t,B)[+...]'

image / text / shape / webcam differ ONLY in how the tile is produced.  That is
what keeps the QML preview and the ffmpeg export in agreement: in QML the same
layer is an Item(x,y,w,h) with an opacity ramp and a visible gate.

'redact' is the one exception: it reads the pixels beneath it, so it compiles to
split -> crop -> blur/pixelize -> overlay-back.
"""
import argparse, json, math, os, subprocess, sys, time

# ------------------------------------------------------------------ cut maths
def merge_cuts(cuts):
    cs = sorted([float(a), float(b)] for a, b in cuts if float(b) > float(a))
    out = []
    for c in cs:
        if out and c[0] <= out[-1][1]:
            out[-1][1] = max(out[-1][1], c[1])
        else:
            out.append(list(c))
    return out

def s2o(t, cuts):
    """SOURCE time -> OUTPUT time. t inside a cut maps to the cut's start."""
    removed = 0.0
    for a, b in cuts:
        if t <= a:
            break
        removed += min(t, b) - a
    return t - removed

def map_interval(a, b, cuts):
    """A source interval -> the list of OUTPUT intervals it survives as."""
    segs, cur = [], a
    for ca, cb in cuts:
        if cb <= cur or ca >= b:
            continue
        if ca > cur:
            segs.append((cur, min(ca, b)))
        cur = max(cur, cb)
        if cur >= b:
            break
    if cur < b:
        segs.append((cur, b))
    return [(s2o(x, cuts), s2o(y, cuts)) for x, y in segs if y > x]

def enable_expr(ivals):
    """Half-open gates. between() is inclusive at BOTH ends and would light one
    extra frame per range; gte*lt gives exactly ceil(A*fps) .. ceil(B*fps)-1."""
    return "+".join(f"gte(t,{a:.6f})*lt(t,{b:.6f})" for a, b in ivals)

# ------------------------------------------------------------- mask helpers
# (lifted from round-1 render.py: a 1-frame lavfi source + geq, so the per-pixel
#  interpreter runs ONCE, not once per frame)
def rounded_rect_geq(w, h, r):
    if r <= 0:
        return "255"
    dx = f"max(max({r}-X,X-({w}-1-{r})),0)"
    dy = f"max(max({r}-Y,Y-({h}-1-{r})),0)"
    return f"clip(255*({r}-hypot({dx},{dy})+0.5),0,255)"

def circle_geq(size):
    r = size / 2.0
    return f"clip(255*({r}-hypot(X-{r}+0.5,Y-{r}+0.5)+0.5),0,255)"

def arrow_geq(w, h, x0, y0, x1, y1, thick, head):
    """Coverage mask for an arrow from (x0,y0) to (x1,y1) in tile coords.
    shaft = capsule(P, A, B'); head = cone that narrows to the tip."""
    dx, dy = x1 - x0, y1 - y0
    L = max(math.hypot(dx, dy), 1e-6)
    ux, uy = dx / L, dy / L
    # s = distance along the axis, d = perpendicular distance
    s = f"((X-{x0:.3f})*{ux:.6f}+(Y-{y0:.3f})*{uy:.6f})"
    d = f"abs((X-{x0:.3f})*{-uy:.6f}+(Y-{y0:.3f})*{ux:.6f})"
    shaft_len = max(L - head, 0.0)
    # shaft: 0<=s<=shaft_len and d<=thick/2   (signed coverage, +0.5 = AA)
    shaft = (f"min(min({s},{shaft_len:.3f}-{s}),{thick/2.0:.3f}-{d})")
    # head: shaft_len<=s<=L and d <= (L-s)*head_halfwidth/head
    hw = max(thick * 1.6, thick / 2.0 + 1)
    head_c = (f"min(min({s}-{shaft_len:.3f},{L:.3f}-{s}),"
              f"({L:.3f}-{s})*{hw/max(head,1e-6):.6f}-{d})")
    return f"clip(255*(max({shaft},{head_c})+0.5),0,255)"

def hexcol(h):
    return "0x" + h.lstrip("#") if h.startswith("#") else h

def esc_text(s):
    return (s.replace("\\", "\\\\").replace(":", "\\:")
             .replace("'", "’").replace(",", "\\,").replace("%", "\\%"))

# ------------------------------------------------------------------ defaults
LAYER_DEFAULTS = {
    "fade": 0.0, "opacity": 1.0, "radius": 0,
    "x": 0, "y": 0, "w": 400, "h": 300,
}

class Build:
    def __init__(self, proj, root="."):
        self.p = proj
        self.root = root
        self.c = proj["canvas"]
        self.W, self.H, self.FPS = self.c["w"], self.c["h"], self.c["fps"]
        self.cuts = merge_cuts(proj.get("cuts", []))
        self.cut_stage = proj.get("cut_stage", "pre")     # "pre" | "post"
        self.cut_method = proj.get("cut_method", "select")  # "select" | "concat"
        self.src_dur = float(proj["src_dur"])
        self.removed = sum(b - a for a, b in self.cuts)
        self.out_dur = self.src_dur - self.removed
        # the timeline the LAYER streams live on
        self.tl_dur = self.out_dur if self.cut_stage == "pre" else self.src_dur
        self.inputs = []          # list of (argv-fragment list)
        self.g = []               # filter chains
        self.n = 0

    def add_input(self, args):
        self.inputs.append(args)
        self.n += 1
        return self.n - 1

    # -- layer time range in the timeline the overlay is gated on -------------
    def gates(self, lay):
        a, b = lay.get("t", [0.0, self.src_dur])
        a = 0.0 if a is None else float(a)
        b = self.src_dur if b is None else float(b)
        if self.cut_stage == "pre":
            return map_interval(a, b, self.cuts)      # remapped to output time
        return [(a, b)]                                # source time, verbatim

    # -- common tail: alpha ramp, global opacity ------------------------------
    def ramp(self, lay, ivals):
        f = float(lay.get("fade", 0.0))
        bits = []
        if f > 0 and ivals:
            A, B = ivals[0][0], ivals[-1][1]
            bits.append(f"fade=t=in:st={A:.6f}:d={f}:alpha=1")
            bits.append(f"fade=t=out:st={max(B-f,A):.6f}:d={f}:alpha=1")
        op = float(lay.get("opacity", 1.0))
        if op < 1.0:
            bits.append(f"colorchannelmixer=aa={op}")
        return ("," + ",".join(bits)) if bits else ""

    # ---------------------------------------------------------------- build
    def build(self):
        FPS, W, H = self.FPS, self.W, self.H
        base_in = self.add_input(["-i", os.path.join(self.root,
                                                     self.p["sources"]["screen"])])
        # --- base video/audio, optionally cut FIRST -------------------------
        vchain = [f"[{base_in}:v]fps={FPS}", "setsar=1", "format=rgba"]
        if self.cut_stage == "pre" and self.cuts:
            self.g.append(",".join(vchain) + "[bpre]")
            self.emit_cut("[bpre]", f"[{base_in}:a]", "[base]", "[aout]")
        else:
            self.g.append(",".join(vchain) + "[base]")
            self.g.append(f"[{base_in}:a]anull[apre]")

        cur = "[base]"
        for i, lay in enumerate(self.p["layers"]):
            cur = self.layer(i, lay, cur)

        if self.cut_stage == "post" and self.cuts:
            self.emit_cut(cur, "[apre]", "[vfin]", "[aout]")
        elif self.cut_stage == "post":
            self.g.append(f"{cur}null[vfin]")
            self.g.append("[apre]anull[aout]")
        else:
            self.g.append(f"{cur}null[vfin]")
            if not self.cuts:
                self.g.append("[apre]anull[aout]")
        self.g.append("[vfin]format=yuv420p[vout]")
        return ";".join(self.g)

    # ------------------------------------------------------------- the cut
    def emit_cut(self, vin, ain, vout, aout):
        """Two mechanisms, same semantics.

        select : one pass, O(1) graph size in cut count, regenerates a gapless
                 timeline with setpts=N/FRAME_RATE/TB and asetpts=N/SR/TB.
        concat : split -> trim/atrim per KEPT segment -> concat=v=1:a=1, which
                 is the filter that exists specifically to keep A and V together.
        """
        keep = []
        cur = 0.0
        for a, b in self.cuts:
            if a > cur:
                keep.append((cur, a))
            cur = max(cur, b)
        if cur < self.src_dur:
            keep.append((cur, self.src_dur))

        if self.cut_method == "select":
            # between() is inclusive at BOTH ends and drops one EXTRA frame
            # per cut; half-open gte*lt is the correct gate.
            drop = "+".join(f"gte(t,{a:.6f})*lt(t,{b:.6f})" for a, b in self.cuts)
            self.g.append(f"{vin}select='not({drop})',setpts=N/FRAME_RATE/TB{vout}")
            self.g.append(f"{ain}aselect='not({drop})',asetpts=N/SR/TB{aout}")
        else:
            k = len(keep)
            self.g.append(f"{vin}split={k}" + "".join(f"[cv{i}]" for i in range(k)))
            self.g.append(f"{ain}asplit={k}" + "".join(f"[ca{i}]" for i in range(k)))
            for i, (a, b) in enumerate(keep):
                self.g.append(f"[cv{i}]trim={a:.6f}:{b:.6f},setpts=PTS-STARTPTS[tv{i}]")
                self.g.append(f"[ca{i}]atrim={a:.6f}:{b:.6f},asetpts=PTS-STARTPTS[ta{i}]")
            pads = "".join(f"[tv{i}][ta{i}]" for i in range(k))
            self.g.append(f"{pads}concat=n={k}:v=1:a=1{vout}{aout}")

    def cut_video_only(self, vin, vout):
        """cut_stage='pre' cuts the BASE, so every other time-VARYING input has
        to be cut identically or it plays on the uncut clock and drifts.
        (Static image/text/shape tiles need no cut: their content is constant.)"""
        keep, cur = [], 0.0
        for a, b in self.cuts:
            if a > cur:
                keep.append((cur, a))
            cur = max(cur, b)
        if cur < self.src_dur:
            keep.append((cur, self.src_dur))
        k = len(keep)
        u = vin.strip("[]")
        self.g.append(f"{vin}split={k}" + "".join(f"[{u}cv{i}]" for i in range(k)))
        for i, (a, b) in enumerate(keep):
            self.g.append(f"[{u}cv{i}]trim={a:.6f}:{b:.6f},setpts=PTS-STARTPTS[{u}tv{i}]")
        self.g.append("".join(f"[{u}tv{i}]" for i in range(k)) +
                      f"concat=n={k}:v=1:a=0{vout}")

    # ------------------------------------------------------------ one layer
    def layer(self, i, lay, cur):
        L = dict(LAYER_DEFAULTS); L.update(lay)
        ivals = self.gates(L)
        if not ivals:                       # entirely inside a cut -> dropped
            return cur
        en = enable_expr(ivals)
        typ = L["type"]
        tag = f"L{i}"
        FPS, D = self.FPS, self.tl_dur

        if typ == "redact":
            x, y, w, h = int(L["x"]), int(L["y"]), int(L["w"]), int(L["h"])
            if L.get("mode", "blur") == "pixelate":
                blur = f"pixelize=w={L.get('block',24)}:h={L.get('block',24)}"
            else:
                blur = f"boxblur={L.get('strength',18)}:2"
            self.g.append(f"{cur}split=2[{tag}a][{tag}b]")
            self.g.append(f"[{tag}a]crop={w}:{h}:{x}:{y},{blur},format=rgba"
                          f"{self.ramp(L, ivals)}[{tag}t]")
            self.g.append(f"[{tag}b][{tag}t]overlay=x={x}:y={y}:enable='{en}'"
                          f":eof_action=repeat:shortest=0:format=rgb[{tag}o]")
            return f"[{tag}o]"

        # ---- produce the RGBA tile -----------------------------------------
        x, y, w, h = int(L["x"]), int(L["y"]), int(L["w"]), int(L["h"])
        if typ == "image":
            idx = self.add_input(["-loop", "1", "-framerate", str(FPS),
                                  "-t", f"{D:.4f}",
                                  "-i", os.path.join(self.root, L["asset"])])
            self.g.append(f"[{idx}:v]format=rgba,scale={w}:{h}:flags=bicubic,"
                          f"setsar=1[{tag}s]")
            src = f"[{tag}s]"
        elif typ == "webcam":
            idx = self.add_input(["-i", os.path.join(self.root,
                                                     self.p["sources"][L["source"]])])
            cw, ch = probe_wh(os.path.join(self.root, self.p["sources"][L["source"]]))
            if L.get("crop", "square") == "none":
                pre = ""
            else:
                side = min(cw, ch)
                pre = f"crop={side}:{side}:{(cw-side)//2}:{(ch-side)//2},"
            self.g.append(f"[{idx}:v]fps={FPS},{pre}scale={w}:{h}:flags=bicubic,"
                          f"setsar=1,format=rgba[{tag}c]")
            mexpr = (circle_geq(w) if L.get("shape", "circle") == "circle"
                     else rounded_rect_geq(w, h, L["radius"]))
            self.g.append(f"color=c=black:s={w}x{h}:r=1:d=1,format=gray,"
                          f"geq=lum='{mexpr}'[{tag}m]")
            self.g.append(f"[{tag}c][{tag}m]alphamerge=repeatlast=1:shortest=0[{tag}s]")
            src = f"[{tag}s]"
            if self.cut_stage == "pre" and self.cuts:
                self.cut_video_only(src, f"[{tag}k]")
                src = f"[{tag}k]"
        elif typ == "text":
            # BUG FOUND BY THE QML CROSS-CHECK: alphamerge REPLACES the alpha
            # plane, so a translucent box colour ("0x101820@0.85") came out
            # fully opaque while the QML preview blended it correctly.
            # Fix: keep the box colour opaque, fold the box alpha INTO the
            # rounded-rect mask, and draw the text AFTER the alphamerge so the
            # glyphs stay at alpha 1 (which is what the QML sibling Text does).
            bg = L.get("box_color", "black@0.0")
            rgb, _, al = bg.partition("@")
            A = float(al) if al else 1.0
            self.g.append(f"color=c={rgb}:s={w}x{h}:r={FPS}:d={D:.4f},"
                          f"format=rgba[{tag}r]")
            mexpr = rounded_rect_geq(w, h, L["radius"])
            self.g.append(f"color=c=black:s={w}x{h}:r=1:d=1,format=gray,"
                          f"geq=lum='({mexpr})*{A}'[{tag}m]")
            self.g.append(f"[{tag}r][{tag}m]alphamerge=repeatlast=1:shortest=0[{tag}b]")
            self.g.append(
                f"[{tag}b]drawtext=text='{esc_text(L['text'])}'"
                f":fontsize={L.get('fontsize',48)}:fontcolor={L.get('color','white')}"
                f":x={L.get('tx','(w-tw)/2')}:y={L.get('ty','(h-th)/2')}[{tag}s]")
            src = f"[{tag}s]"
        elif typ == "shape":
            col = L.get("color", "#ff3b30")
            if L.get("shape", "rect") == "arrow":
                mexpr = arrow_geq(w, h, L.get("x0", 8), L.get("y0", h - 8),
                                  L.get("x1", w - 8), L.get("y1", 8),
                                  L.get("thick", 18), L.get("head", 54))
                self.g.append(f"color=c={hexcol(col)}:s={w}x{h}:r={FPS}:d={D:.4f},"
                              f"format=rgba[{tag}r]")
                self.g.append(f"color=c=black:s={w}x{h}:r=1:d=1,format=gray,"
                              f"geq=lum='{mexpr}'[{tag}m]")
                self.g.append(f"[{tag}r][{tag}m]alphamerge=repeatlast=1:shortest=0[{tag}s]")
            else:
                self.g.append(f"color=c={hexcol(col)}:s={w}x{h}:r={FPS}:d={D:.4f},"
                              f"format=rgba[{tag}r]")
                self.g.append(f"color=c=black:s={w}x{h}:r=1:d=1,format=gray,"
                              f"geq=lum='{rounded_rect_geq(w,h,L['radius'])}'[{tag}m]")
                self.g.append(f"[{tag}r][{tag}m]alphamerge=repeatlast=1:shortest=0[{tag}s]")
            src = f"[{tag}s]"
        else:
            raise SystemExit(f"unknown layer type {typ}")

        ramp = self.ramp(L, ivals)
        if ramp:
            self.g.append(f"{src}{ramp.lstrip(',')}[{tag}f]")
            src = f"[{tag}f]"
        self.g.append(f"{cur}{src}overlay=x={x}:y={y}:enable='{en}'"
                      f":eof_action=repeat:shortest=0:format=rgb[{tag}o]")
        return f"[{tag}o]"

# ---------------------------------------------------------------- utilities
_wh_cache = {}
def probe_wh(path):
    if path not in _wh_cache:
        o = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "json",
                            path], capture_output=True, text=True).stdout
        s = json.loads(o)["streams"][0]
        _wh_cache[path] = (int(s["width"]), int(s["height"]))
    return _wh_cache[path]

def probe_dur(path):
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True).stdout.strip()
    return float(o)

def count_chains(graph):
    n, depth, q = 1, 0, False
    for ch in graph:
        if ch == "'": q = not q
        elif q: continue
        elif ch == "(": depth += 1
        elif ch == ")": depth -= 1
        elif ch == ";" and depth == 0: n += 1
    return n

def render(proj, out, root=".", graph_out=None, print_only=False, extra=None,
           quiet=True):
    if "src_dur" not in proj:
        proj["src_dur"] = probe_dur(os.path.join(root, proj["sources"]["screen"]))
    b = Build(proj, root)
    graph = b.build()
    if graph_out:
        open(graph_out, "w").write(graph + "\n")
    if print_only:
        print(graph)
        return None
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for a in b.inputs:
        cmd += a
    if len(graph) > 60000:
        gp = out + ".filtergraph"
        open(gp, "w").write(graph)
        cmd += ["-/filter_complex", gp]
    else:
        cmd += ["-filter_complex", graph]
    cmd += ["-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-crf", str(proj.get("crf", 20)),
            "-preset", proj.get("preset", "medium"), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k"]
    cmd += (extra or [])
    cmd += [out]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    el = time.time() - t0
    if r.returncode:
        print(r.stderr[-4000:], file=sys.stderr)
    return {"elapsed": round(el, 3), "rc": r.returncode, "out": out,
            "graph_chars": len(graph), "chains": count_chains(graph),
            "inputs": len(b.inputs), "out_dur": round(b.out_dur, 4),
            "speed": round(b.out_dur / el, 2) if el else None}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--out", default="out.mp4")
    ap.add_argument("--root", default=".")
    ap.add_argument("--graph-out")
    ap.add_argument("--print-graph-only", action="store_true")
    ap.add_argument("--set", action="append", default=[])
    a = ap.parse_args()
    proj = json.load(open(a.project))
    for kv in a.set:
        k, v = kv.split("=", 1)
        proj[k] = json.loads(v) if v[:1] in "[{\"0123456789-" or v in ("true","false","null") else v
    r = render(proj, a.out, a.root, a.graph_out, a.print_graph_only)
    if r:
        print(json.dumps(r))
        sys.exit(r["rc"])

if __name__ == "__main__":
    main()
