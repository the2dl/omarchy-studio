// A linear gradient painted along the CSS gradient line, for the backdrop preview and
// its swatches.
//
// A Canvas rather than a rotated Rectangle with a QML Gradient. QML's gradient runs
// top-to-bottom and can only be aimed by rotating the item, which means oversizing it,
// clipping it back, and converting a CSS angle into a QML rotation -- three chances to
// be a few degrees off from what the export draws. `createLinearGradient` takes the two
// endpoints directly, and `line` is the export's OWN endpoints, handed over by
// resolve_backdrop in canvas pixels. Nothing is re-derived for the case that matters.
//
// `angle` is the fallback for the swatches, which come from the catalogue route and
// carry no line of their own. It repeats backgrounds.gradient_line's construction, and
// tst_gradient.qml checks it against values taken from that function -- a second
// implementation is acceptable only while something asserts the two agree.
import QtQuick

Canvas {
    id: root

    property var colors: []
    // {x0, y0, x1, y1} in this item's pixels. Wins over `angle` when present.
    property var line: null
    // CSS degrees, clockwise from "up".
    property real angle: 180

    onColorsChanged: requestPaint()
    onLineChanged: requestPaint()
    onAngleChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()

    // The same construction as backgrounds.gradient_line: a line centred on the box and
    // long enough that the perpendicular through each corner meets it inside its own
    // length, which is what puts the first and last colour ON the extreme corners.
    function lineFor(a, w, h) {
        var rad = a * Math.PI / 180
        // y grows downward here as it does in ffmpeg, so "up" is -y and the CSS
        // direction vector (sin, cos) picks up a sign on its second component.
        var dx = Math.sin(rad)
        var dy = -Math.cos(rad)
        var length = Math.abs(w * dx) + Math.abs(h * dy)
        var cx = w / 2, cy = h / 2
        return { x0: cx - dx * length / 2, y0: cy - dy * length / 2,
                 x1: cx + dx * length / 2, y1: cy + dy * length / 2 }
    }

    onPaint: {
        var ctx = getContext("2d")
        ctx.reset()
        if (!colors || colors.length === 0)
            return
        var l = line ? line : lineFor(angle, width, height)
        var g = ctx.createLinearGradient(l.x0, l.y0, l.x1, l.y1)
        if (colors.length === 1) {
            g.addColorStop(0, colors[0])
            g.addColorStop(1, colors[0])
        } else {
            // Evenly spaced, because the generator behind the export has no positional
            // stops either -- see backgrounds.py. Faking positions here would preview a
            // gradient the render cannot produce.
            for (var i = 0; i < colors.length; ++i)
                g.addColorStop(i / (colors.length - 1), colors[i])
        }
        ctx.fillStyle = g
        ctx.fillRect(0, 0, width, height)
    }
}
