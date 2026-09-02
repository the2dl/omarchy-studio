// The camera overlay: drag to move, corner grip to resize, circle / rounded / rect.
//
// The box is whatever the bridge resolved from WebcamSettings through Placement; a drag
// posts canvas pixels back and the reply -- clamped inside the canvas by Rect.clamped_to
// -- is what the item then follows. Nothing here converts between normalized and pixel
// coordinates.
//
// When the recording burned the camera into the screen pixels this whole item is inert
// and says so. Silently dead controls read as a bug in the editor rather than a
// property of the recording.
import QtQuick
import QtQuick.Shapes
import QtMultimedia
import QtQuick.Effects

Item {
    id: root

    property var cam: ({})
    property alias videoOutput: camOut
    property bool selected: false
    property bool editable: cam.editable === true && cam.enabled === true

    signal clicked()
    signal moved(var rect)

    readonly property var rect: cam.rect || ({ x: 0, y: 0, width: 0, height: 0 })

    // Same rule as LayerItem: never assign to x/y, or the binding to the model is gone
    // for the rest of the session.
    property bool dragging: false
    property real liveX: 0
    property real liveY: 0
    property real liveW: 0
    property real liveH: 0
    property real gripSize: 22

    x: dragging ? liveX : rect.x
    y: dragging ? liveY : rect.y
    width: dragging ? liveW : rect.width
    height: dragging ? liveH : rect.height
    visible: cam.enabled === true

    // The export's shapes, reproduced rather than approximated. layers._tile_webcam does
    // two things for circle and rounded: a SQUARE centre crop of the camera, then a
    // stretch into the box. Preserving the aspect against the box instead would crop
    // differently the moment the box is not square -- which the default 0.22x0.22 on a
    // 16:9 canvas already is.
    readonly property bool squareCrop: cam.shape === "circle" || cam.shape === "rounded"

    Item {
        id: clipBox
        x: 0; y: 0
        width: root.width
        height: root.height
        layer.enabled: root.cam.shape !== "rect"
        layer.effect: MultiEffect {
            maskEnabled: true
            // Two separate mask items, chosen here, rather than one item whose children
            // toggle `visible`: a layered mask whose child visibility changes does not
            // reliably re-render, and the failure is silent -- the webcam disappears.
            maskSource: root.cam.shape === "circle" ? ellipseMask : roundedMask
        }

        VideoOutput {
            id: camOut
            // Laid out as a SQUARE and then squashed into the box, which is the crop the
            // filtergraph takes: PreserveAspectCrop into a square item is the centre
            // square of the camera frame, and the scale is the stretch that follows it.
            x: 0; y: 0
            width: root.squareCrop ? root.side : root.width
            height: root.squareCrop ? root.side : root.height
            fillMode: root.squareCrop ? VideoOutput.PreserveAspectCrop : VideoOutput.Stretch
            transform: [
                // The export's hflip, in the same place in the chain: before the scale.
                Scale {
                    origin.x: camOut.width / 2
                    xScale: root.cam.mirror ? -1 : 1
                },
                Scale {
                    xScale: root.squareCrop ? root.width / root.side : 1
                    yScale: root.squareCrop ? root.height / root.side : 1
                }
            ]
        }
    }

    // The square the camera is cropped to before it is stretched into the box. Its exact
    // value cancels out; it only has to be big enough that the downscale is the one that
    // loses detail, not this.
    readonly property real side: Math.max(width, height, 2)

    // circle: the ellipse INSCRIBED in the tile, which is what layers._circle_mask draws.
    // A rounded rectangle of radius min(w,h)/2 would be a stadium, and the two diverge
    // visibly as soon as the box is not square -- which the 0.22x0.22 default on a 16:9
    // canvas already is.
    Item {
        id: ellipseMask
        x: 0; y: 0
        width: root.width
        height: root.height
        visible: false
        layer.enabled: true
        Shape {
            x: 0; y: 0
            width: ellipseMask.width
            height: ellipseMask.height
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                fillColor: "black"
                strokeWidth: 0
                strokeColor: "transparent"
                PathAngleArc {
                    centerX: ellipseMask.width / 2
                    centerY: ellipseMask.height / 2
                    radiusX: ellipseMask.width / 2
                    radiusY: ellipseMask.height / 2
                    startAngle: 0
                    sweepAngle: 360
                }
            }
        }
    }

    Item {
        id: roundedMask
        x: 0; y: 0
        width: root.width
        height: root.height
        visible: false
        layer.enabled: true
        Rectangle {
            x: 0; y: 0
            width: roundedMask.width
            height: roundedMask.height
            // Resolved from corner_radius against the short side, the same normalization
            // layers._radius_px uses.
            radius: root.cam.shape === "rounded" ? (root.cam.radius || 0) : 0
            color: "black"
        }
    }

    // Selection ring: 1.5px, never a heavy fill (spec §1 "Selected card"). The four
    // corner dots are the mock's handles; only the bottom-right one actually drags,
    // through the grip MouseArea sitting on top of it.
    Rectangle {
        x: 0; y: 0
        width: root.width; height: root.height
        visible: root.selected
        color: "transparent"
        border.color: root.editable ? Theme.accent : Theme.text4
        border.width: 1.5
    }
    Repeater {
        model: root.selected && root.editable ? 4 : 0
        Rectangle {
            width: 7; height: 7; radius: 3.5
            x: (index % 2 === 0 ? 0 : root.width) - 3.5
            y: (index < 2 ? 0 : root.height) - 3.5
            color: Theme.accent
        }
    }

    MouseArea {
        x: 0; y: 0
        width: root.width; height: root.height
        enabled: root.editable
        cursorShape: Qt.OpenHandCursor
        property real ox: 0
        property real oy: 0
        onPressed: function (m) {
            root.clicked()
            root.beginDrag()
            ox = m.x
            oy = m.y
        }
        onPositionChanged: function (m) {
            if (!pressed)
                return
            root.liveX += m.x - ox
            root.liveY += m.y - oy
        }
        onReleased: root.endDrag()
    }

    // Invisible: the bottom-right corner dot above is the visual; this is the hit area,
    // kept at gripSize because a 7px target is not draggable.
    Rectangle {
        id: grip
        visible: root.selected && root.editable
        width: root.gripSize
        height: root.gripSize
        x: root.width - width / 2
        y: root.height - height / 2
        radius: width / 2
        color: "transparent"
        MouseArea {
            x: 0; y: 0
            width: parent.width; height: parent.height
            cursorShape: Qt.SizeFDiagCursor
            onPressed: root.beginDrag()
            onPositionChanged: function (m) {
                if (!pressed)
                    return
                // Square by default: the export crops the camera to a square before
                // masking, so a free-form box would preview a crop the render will not
                // make. Hold Shift to size the two axes independently.
                var dx = m.x - grip.width / 2
                var dy = m.y - grip.height / 2
                var d = (m.modifiers & Qt.ShiftModifier) ? dy : Math.max(dx, dy)
                root.liveW = Math.max(root.gripSize * 2, root.liveW + ((m.modifiers & Qt.ShiftModifier) ? dx : d))
                root.liveH = Math.max(root.gripSize * 2, root.liveH + d)
            }
            onReleased: root.endDrag()
        }
    }

    function beginDrag() {
        liveX = rect.x
        liveY = rect.y
        liveW = rect.width
        liveH = rect.height
        dragging = true
    }

    function endDrag() {
        if (!dragging)
            return
        dragging = false
        moved({ x: liveX, y: liveY, width: liveW, height: liveH })
    }
}
