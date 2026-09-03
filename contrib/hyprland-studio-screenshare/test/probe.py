"""Report what the screenshare saw where the excluded window is.

green  = the background showed through -> the window was EXCLUDED (what we want)
red    = the window itself was captured -> no exclusion at all
black  = stock behaviour: a black box was painted over it
"""
import sys
from PIL import Image

def classify(px):
    r, g, b = px[:3]
    if r < 40 and g < 40 and b < 40:                 return "BLACK (black-box)"
    if g > 120 and r < 80 and b < 80:                return "GREEN (excluded - background shows)"
    if r > 120 and g < 80 and b < 80:                return "RED (window captured - no exclusion)"
    return f"other {px[:3]}"

im = Image.open(sys.argv[1]).convert("RGB")
W, H = im.size
print(f"{sys.argv[1]}  {W}x{H}")
for name, (x, y) in [("window centre", (W//2, H//2)), ("control (top-left)", (12, 12)),
                     ("control (bottom-right)", (W-12, H-12))]:
    px = im.getpixel((x, y))
    print(f"  {name:24} ({x:4},{y:4})  {px}  -> {classify(px)}")
