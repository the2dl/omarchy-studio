// The camera overlay: drag to move, corner grip to resize, circle / rounded / rect --
// the same three names the setup bar, the model and the live self-view use.
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
    // Preview mode: ring, handles and grip hide and dragging goes inert; the camera
    // pixels themselves are content and render exactly as before.
    property bool previewMode: false

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
    // Chrome is measured in SCREEN pixels, not canvas pixels. Everything here lives
    // inside the stage's fit transform, so a plain 22 shrinks with the canvas: a
    // 2526x2768 window recording fits at about a quarter the scale of a 2560x1440
    // display capture, and the grip came out at ~5px -- indistinguishable from the
    // undraggable dot it replaced. `uiScale` is 1/fit, so handles stay the same size
    // on screen whatever shape the recording is.
    // Held from the drop until the model actually comes back. Releasing the local
    // override the moment the mouse came up rendered a frame at the OLD rect -- the new
    // one is an intent in flight, not applied yet -- so the object visibly snapped back
    // to where it started and then jumped forward. Reported as a "double bounce", and it
    // is one: two position changes for one gesture.
    property bool committing: false
    readonly property bool showingLive: dragging || committing

    // Called by whoever posted the intent, when the reply lands. The timer is only a
    // backstop: an op that errors never calls back, and the override must not stick.
    function commitDone() {
        committing = false
        commitTimeout.stop()
    }
    Timer {
        id: commitTimeout
        interval: 1200
        onTriggered: root.committing = false
    }

    property real uiScale: 1
    readonly property real gripSize: 22 * uiScale
    readonly property real handleSize: 7 * uiScale
    readonly property real ringWidth: 1.5 * uiScale

    x: showingLive ? liveX : rect.x
    y: showingLive ? liveY : rect.y
    width: showingLive ? liveW : rect.width
    height: showingLive ? liveH : rect.height
    visible: cam.enabled === true

    // The export's shapes, reproduced rather than approximated -- this is the twin of
    // layers._tile_webcam and has to move with it.
    //
    // It does ONE thing for every shape now: a centre crop of the camera to the BOX's
    // aspect, then a scale into the box. It used to special-case the round ones and let
    // `rect` stretch, which is what put a 4:3 sensor into a 716x716 square and made
    // every face in a rect camera tall. Fixing the renderer alone left this half
    // stretching, so the editor showed something the export would not produce -- the
    // failure this file's own comment exists to prevent.
    readonly property bool squareCrop: true

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
            maskSource: root.cam.shape === "circle" ? ellipseMask : squircleMask
        }

        VideoOutput {
            id: camOut
            // Laid out at the BOX's size and filled by cropping, which is exactly what
            // the filtergraph does: crop the camera to this rectangle's aspect, then
            // scale. No stretch anywhere, for any shape.
            x: 0; y: 0
            width: root.width
            height: root.height
            fillMode: VideoOutput.PreserveAspectCrop
            transform: [
                // The export's hflip, in the same place in the chain: before the scale.
                Scale {
                    origin.x: camOut.width / 2
                    xScale: root.cam.mirror ? -1 : 1
                }
            ]
        }
    }

    // Kept for the masks, which are drawn against the tile. The camera itself is no
    // longer laid out as a square and squashed: it is sized to the box and filled by
    // cropping, which is what the filtergraph does.
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

    // The superellipse, from the same SquircleShape the setup self-view uses -- so the
    // bubble you placed before recording is the bubble the editor shows. This is what
    // `rounded` means now; the shallow rounded rectangle that used to own the name is
    // gone, and layers._tile_webcam draws the matching mask for the export.
    Item {
        id: squircleMask
        x: 0; y: 0
        width: root.width
        height: root.height
        visible: false
        layer.enabled: true
        SquircleShape { anchors.fill: parent; fillColor: "black" }
    }

    // Hover reveals the same furniture selection does, at half strength. Without it the
    // camera advertised nothing: the resize grip only existed once you had clicked the
    // camera, and what it drew then was a 7px dot over an invisible hit area -- so the
    // camera read as a thing you can move and reshape but not resize. It always could.
    HoverHandler {
        id: hover
        enabled: root.editable && !root.previewMode
    }
    readonly property bool showHandles: root.editable && !root.previewMode
                                        && (root.selected || hover.hovered || gripArea.pressed)

    // Selection ring: 1.5px, never a heavy fill (spec §1 "Selected card").
    Rectangle {
        x: 0; y: 0
        width: root.width; height: root.height
        visible: (root.selected || hover.hovered) && !root.previewMode
        color: "transparent"
        border.color: root.editable ? Theme.accent : Theme.text4
        border.width: root.ringWidth
        opacity: root.selected ? 1.0 : 0.55
        Behavior on opacity { NumberAnimation { duration: Theme.durFast } }
    }

    // Three plain corner dots; the fourth corner is the grip below and draws itself.
    Repeater {
        model: root.showHandles ? 3 : 0
        Rectangle {
            width: root.handleSize; height: root.handleSize
            radius: root.handleSize / 2
            x: (index % 2 === 0 ? 0 : root.width) - root.handleSize / 2
            y: (index < 2 ? 0 : root.height) - root.handleSize / 2
            color: Theme.accent
            opacity: root.selected ? 1.0 : 0.55
        }
    }

    MouseArea {
        x: 0; y: 0
        width: root.width; height: root.height
        enabled: root.editable && !root.previewMode
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

    // The resize grip: the same object the setup window's self-view uses, so the gesture
    // that sizes the camera before recording is the gesture that sizes it after. Drawn,
    // not implied -- a filled dot with the two diagonal strokes that mean "drag me",
    // sitting on the corner rather than inside it.
    Rectangle {
        id: gripDot
        visible: root.showHandles
        width: root.gripSize
        height: root.gripSize
        x: root.width - width / 2
        y: root.height - height / 2
        radius: width / 2
        color: gripArea.pressed || gripArea.containsMouse ? Theme.accent : Theme.panel
        border.width: root.ringWidth
        border.color: Theme.accent
        opacity: root.selected || gripArea.containsMouse || gripArea.pressed ? 1.0 : 0.75
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
        Behavior on opacity { NumberAnimation { duration: Theme.durFast } }

        Repeater {
            model: 2
            Rectangle {
                width: (7 - index * 3) * root.uiScale
                height: 1.5 * root.uiScale
                radius: height / 2
                color: gripArea.pressed || gripArea.containsMouse ? Theme.accentOn : Theme.text2
                rotation: -45
                x: (gripDot.width - width) / 2 + index * 2.5 * root.uiScale
                y: (gripDot.height - height) / 2 + index * 2.5 * root.uiScale
            }
        }
    }

    // Bigger than the dot it draws, because an 18px target is a miss waiting to happen
    // and the hit area is free. Kept a sibling of the dot so it stays hit-testable while
    // the dot animates.
    MouseArea {
        id: gripArea
        enabled: root.showHandles
        width: root.gripSize + 12 * root.uiScale
        height: root.gripSize + 12 * root.uiScale
        x: gripDot.x + gripDot.width / 2 - width / 2
        y: gripDot.y + gripDot.height / 2 - height / 2
        hoverEnabled: true
        cursorShape: Qt.SizeFDiagCursor
        onPressed: {
            root.clicked()      // grabbing the grip selects the camera too
            root.beginDrag()
        }
        onPositionChanged: function (m) {
            if (!pressed)
                return
            // Square by default: the export crops the camera to a square before
            // masking, so a free-form box would preview a crop the render will not
            // make. Hold Shift to size the two axes independently.
            var dx = m.x - width / 2
            var dy = m.y - height / 2
            var d = (m.modifiers & Qt.ShiftModifier) ? dy : Math.max(dx, dy)
            root.liveW = Math.max(root.gripSize * 2, root.liveW + ((m.modifiers & Qt.ShiftModifier) ? dx : d))
            root.liveH = Math.max(root.gripSize * 2, root.liveH + d)
        }
        onReleased: root.endDrag()
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
        committing = true
        commitTimeout.restart()
        moved({ x: liveX, y: liveY, width: liveW, height: liveH })
    }
}
