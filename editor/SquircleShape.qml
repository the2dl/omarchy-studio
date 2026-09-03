// The squircle, drawn once and used everywhere: the editor's camera overlay, the setup
// bar's self-view, and nothing else has to know the maths.
//
// A superellipse -- |x/a|^n + |y/b|^n = 1 -- not a rounded rectangle with a generous
// radius. A rounded rect is straight, arc, straight, and the curvature JUMPS at both
// joins; a superellipse's curvature varies continuously, which is the entire reason the
// shape looks the way it does. The product already has a rounded rect ("corner"), so
// approximating one with the other would have shipped two names for one shape.
//
// n and the sampling here must match lib/omarchy_studio/layers.py's _squircle_mask, or
// the preview and the export disagree about the outline. That file's SQUIRCLE_N is the
// authority; this is the same number written in the other language.
import QtQuick
import QtQuick.Shapes

Shape {
    id: root

    // The Lamé exponent. 2 is an ellipse, 4 is the squircle people mean.
    property real n: 4.0
    property color fillColor: "black"
    // Enough segments that the flats stay flat and the corners stay smooth at the sizes
    // a camera bubble is ever drawn at. 128 costs nothing: the path is rebuilt only when
    // the geometry changes, not per frame.
    property int segments: 128

    preferredRendererType: Shape.CurveRenderer

    // Sampled on the parametric form rather than solved per-x: the parametric one is
    // stable at the corners, where dy/dx runs away.
    function outline() {
        var pts = []
        var a = width / 2, b = height / 2
        var e = 2.0 / n
        for (var i = 0; i < segments; ++i) {
            var t = (i / segments) * 2 * Math.PI
            var c = Math.cos(t), s = Math.sin(t)
            pts.push(Qt.point(
                a + a * Math.sign(c) * Math.pow(Math.abs(c), e),
                b + b * Math.sign(s) * Math.pow(Math.abs(s), e)))
        }
        pts.push(pts[0])        // closed, so the fill has no seam
        return pts
    }

    ShapePath {
        fillColor: root.fillColor
        strokeWidth: 0
        strokeColor: "transparent"
        PathPolyline {
            path: root.width > 0 && root.height > 0 ? root.outline() : []
        }
    }
}
