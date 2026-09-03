// The live self-view: your camera, in the shape it will be recorded in, where you put
// it. Drag it anywhere inside the capture rectangle; drag the grip to resize it.
//
// It renders INSIDE the sheet rather than as an mpv window beside it, which is what
// makes dragging and shaping honest: the circle is the same mask the editor and the
// export use (MultiEffect + an ellipse mask, not a rounded rectangle pretending), and
// the position is read straight off the item, so what you place is what gets recorded.
//
// WHERE YOU LEAVE IT IS WHERE IT LANDS. The rect is reported up to the bar in absolute
// logical desktop pixels and rides the contract line to capture.begin, which divides it
// by the capture rectangle and writes edit.json's webcam placement. Before that it was
// a preview and nothing more: you could position the bubble carefully and the editor
// would still open it at the 0.72/0.70 defaults, which read as the placement being
// ignored -- because it was.
//
// Bounds are the CAPTURE rect, not the sheet. Dragging over the whole monitor was wrong
// for any region or window target: the camera is composited inside the recorded
// rectangle, so a bubble parked outside it simply is not in the video.
//
// THE DEVICE IS EXCLUSIVE. gsr's camera fan-out opens /dev/videoN itself and a second
// opener gets EBUSY, so this preview must let go before capture starts -- and capture
// starts DURING the countdown, not after it, because the launcher prints its contract
// line the moment Record is pressed. `active` is therefore false as soon as counting
// begins, and record() stops it explicitly before the POST rather than relying on the
// binding to be evaluated in time.
import QtQuick
import QtQuick.Shapes
import QtMultimedia
import QtQuick.Effects
import ".."

Item {
    id: self

    property var app
    property real monWidth: 1920
    property real monHeight: 1080

    // The capture rectangle in SHEET coordinates -- what the recording will actually
    // contain. MonitorOverlay resolves it from the selection; a whole-display target
    // is the whole sheet.
    property var bounds: ({ x: 0, y: 0, w: 1920, h: 1080 })
    // This sheet's origin in absolute logical desktop pixels, so the reported rect is
    // in the one coordinate space the Python side can divide against.
    property real originX: 0
    property real originY: 0
    // Exactly one sheet owns the placement -- the one the capture is on. The others
    // hold invisible self-views that must not overwrite it.
    property bool authoritative: false

    readonly property real minSize: 96
    // Matches WebcamSettings.w. 0.22 -- nearly a quarter of the frame across -- was the
    // first thing people wanted to change about the camera, before anything else.
    readonly property real defaultFraction: 0.14

    // The bar's vocabulary IS the model's, so this is a straight read. It used to be a
    // translation, and capture.SETUP_SHAPES was a second one that had to agree with it.
    readonly property string shape: app.cameraModes[app.cameraMode] || "circle"
    visible: app.cameraMode > 0 && !app.counting && !app.picking
             && app.sources.cameras.length > 0

    // Square in PIXELS for a circle -- the same correction WebcamSettings.placement
    // makes, and the reason a naive 0.22/0.22 shipped as an ellipse once.
    height: width

    // Plain values, not bindings: dragging and the grip both assign straight to x, y
    // and width, which would silently break a binding anyway. place() owns them, and
    // is re-run whenever the capture rectangle changes underneath.
    function place() {
        var b = bounds
        if (width <= 0)
            width = Math.round(Math.max(minSize, b.w * defaultFraction))
        clampIntoBounds()
    }

    function clampIntoBounds() {
        var b = bounds
        width = Math.round(Math.max(minSize, Math.min(width, Math.min(b.w, b.h) * 0.9)))
        x = Math.round(Math.max(b.x, Math.min(x, b.x + b.w - width)))
        y = Math.round(Math.max(b.y, Math.min(y, b.y + b.h - height)))
        report()
    }

    // Absolute logical desktop pixels. Sent even when the user never touched it, so
    // "the default placement" and "the placement they chose" travel the same path and
    // there is only one behaviour to get right.
    function report() {
        if (!authoritative || !app)
            return
        app.cameraRect = { "x": Math.round(originX + x), "y": Math.round(originY + y),
                           "width": Math.round(width), "height": Math.round(height) }
    }

    onBoundsChanged: place()
    Component.onCompleted: {
        // Bottom-right with an even inset, rather than WebcamSettings' literal
        // 0.72/0.70. Those are a top-left corner for a box whose height is derived
        // (0.22 of the width is 0.39 of the height on 16:9), so 0.70 puts the bottom
        // of the bubble at 1.09 -- off the screen. Clamping alone rescued it but left
        // it flush in the corner, which reads as a layout accident; an explicit margin
        // reads as a choice. Whatever it opens at is sent, so this IS the default now.
        var m = Math.round(Math.min(bounds.w, bounds.h) * 0.03)
        width = Math.round(Math.max(minSize, bounds.w * defaultFraction))
        x = Math.round(bounds.x + bounds.w - width - m)
        y = Math.round(bounds.y + bounds.h - height - m)
        clampIntoBounds()
    }
    onAuthoritativeChanged: report()

    // The QCameraDevice whose id matches the picked one, or the system default.
    //
    // String(), NOT a bare ===. `QCameraDevice.id` is a QByteArray, which QML hands over
    // as an OBJECT, not a string: `typeof ins[i].id` is "object", and `ins[i].id ===
    // "/dev/video2"` is false for EVERY device, including the right one. So the loop
    // never matched and every preview silently fell through to defaultVideoInput -- the
    // bar showed the camera you picked, the self-view showed the built-in one, and
    // switching between them changed nothing. (`==` would coerce and work; `String()`
    // says why it is there.)
    function deviceFor(mediaDevices) {
        var want = app && app.cameraEntry !== null ? app.cameraEntry.device : ""
        var ins = mediaDevices.videoInputs
        for (var i = 0; i < ins.length; ++i)
            if (String(ins[i].id) === want)
                return ins[i]
        return mediaDevices.defaultVideoInput
    }

    function release() {
        console.log("SELFVIEW release() at " + Date.now())
        session.active = false
    }

    // Behind a Loader, not merely inactive: a Camera object opens /dev/videoN just by
    // existing, verified with fuser -- the setup process held the device with the
    // self-view switched Off, which is precisely the state a recording starts from,
    // and the camera fan-out would have got EBUSY. Nothing exists here until the
    // self-view is wanted, and it is torn down the moment it is not.
    Loader {
        id: session
        active: self.visible
        onActiveChanged: console.log("SELFVIEW loader active=" + active
                                     + " at " + Date.now())
        sourceComponent: Component {
            Item {
                MediaDevices { id: devices }
                CaptureSession {
                    camera: Camera {
                        active: true
                        cameraDevice: self.deviceFor(devices)
                    }
                    videoOutput: video
                }
            }
        }
    }

    Item {
        id: frame
        anchors.fill: parent
        // `rect` wants no mask at all -- masking it with a square would only cost a
        // layer and soften its edges.
        layer.enabled: self.shape !== "rect"
        layer.effect: MultiEffect {
            maskEnabled: true
            maskSource: self.shape === "circle" ? ellipseMask : squircleMask
            maskThresholdMin: 0.5
            maskSpreadAtMin: 0.0
        }

        VideoOutput {
            id: video
            anchors.fill: parent
            fillMode: VideoOutput.PreserveAspectCrop
            // Mirrored, like every self-view: an unmirrored preview reads as someone
            // else's face and people fix their framing the wrong way round.
            transform: Scale { xScale: -1; origin.x: frame.width / 2 }
        }
    }

    Item {
        id: ellipseMask
        width: self.width; height: self.height
        layer.enabled: true
        visible: false
        Shape {
            anchors.fill: parent
            ShapePath {
                fillColor: "black"; strokeWidth: 0
                PathAngleArc {
                    centerX: ellipseMask.width / 2; centerY: ellipseMask.height / 2
                    radiusX: ellipseMask.width / 2; radiusY: ellipseMask.height / 2
                    startAngle: 0; sweepAngle: 360
                }
            }
        }
    }

    Item {
        id: squircleMask
        width: self.width; height: self.height
        layer.enabled: true
        visible: false
        SquircleShape { anchors.fill: parent; fillColor: "black" }
    }

    readonly property bool engaged: dragArea.containsMouse || dragArea.drag.active
                                    || grip.containsMouse || grip.pressed

    // The ring says "this is a live object you can move", and disappears from the
    // recording because nothing here is captured -- the sheet is gone before gsr runs.
    Rectangle {
        anchors.fill: parent
        color: "transparent"
        // A superellipse ring cannot be a Rectangle radius, so `rounded` is stroked as a
        // SquircleShape below and this one steps aside for it.
        visible: self.shape !== "rounded"
        radius: self.shape === "circle" ? width / 2 : 0
        border.width: self.engaged ? 2 : 1
        border.color: self.engaged ? Theme.accent : Theme.hairline
        Behavior on border.color { ColorAnimation { duration: Theme.durFast } }
    }

    // The superellipse's ring, stroked on the same outline the mask fills.
    Shape {
        anchors.fill: parent
        visible: self.shape === "rounded"
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            fillColor: "transparent"
            strokeColor: self.engaged ? Theme.accent : Theme.hairline
            strokeWidth: self.engaged ? 2 : 1
            PathPolyline { path: ring.outline() }
        }
    }
    SquircleShape { id: ring; anchors.fill: parent; visible: false }

    MouseArea {
        id: dragArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: drag.active ? Qt.ClosedHandCursor : Qt.OpenHandCursor
        drag.target: self
        // Clamped to the capture rect: a self-view dragged outside it is a camera that
        // is not in the video at all, and the compositor will not stop you.
        drag.minimumX: self.bounds.x
        drag.minimumY: self.bounds.y
        drag.maximumX: self.bounds.x + self.bounds.w - self.width
        drag.maximumY: self.bounds.y + self.bounds.h - self.height
        drag.onActiveChanged: if (!drag.active) self.clampIntoBounds()
        onPositionChanged: self.report()
    }

    // A grip, because scroll-to-resize is invisible: the wheel worked before this and
    // nobody could tell it was there, which reads as "you cannot resize the camera".
    // Placed at 45 degrees on the ring so it sits on the shape's edge for a circle and
    // in the corner for a rounded box.
    Rectangle {
        id: gripDot
        width: 18
        height: 18
        radius: width / 2
        color: grip.pressed || grip.containsMouse ? Theme.accent : Theme.panel
        border.width: 1
        border.color: self.engaged ? Theme.accent : Theme.hairline
        opacity: self.engaged ? 1 : 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: Theme.durFast } }
        x: self.shape === "circle"
           ? self.width / 2 + self.width / 2 * 0.7071 - width / 2
           : self.width - width / 2
        y: self.shape === "circle"
           ? self.height / 2 + self.height / 2 * 0.7071 - height / 2
           : self.height - height / 2

        // Two strokes, the universal resize mark.
        Repeater {
            model: 2
            Rectangle {
                width: 7 - index * 3
                height: 1.5
                radius: 0.75
                color: grip.pressed || grip.containsMouse ? Theme.accentOn : Theme.text3
                rotation: -45
                x: (gripDot.width - width) / 2 + index * 2.5
                y: (gripDot.height - height) / 2 + index * 2.5
            }
        }
    }

    MouseArea {
        id: grip
        // Bigger than the dot it draws: an 18px target is a miss waiting to happen,
        // and the hit area is free.
        width: 34
        height: 34
        x: gripDot.x + gripDot.width / 2 - width / 2
        y: gripDot.y + gripDot.height / 2 - height / 2
        hoverEnabled: true
        cursorShape: Qt.SizeFDiagCursor
        property real startW: 0
        property point startPos

        onPressed: function (ev) {
            startW = self.width
            startPos = mapToItem(self.parent, ev.x, ev.y)
        }
        onPositionChanged: function (ev) {
            if (!pressed)
                return
            var p = mapToItem(self.parent, ev.x, ev.y)
            // The mean of the two axes, so a diagonal drag resizes at the speed the
            // pointer moves rather than at whichever axis happens to move more.
            var delta = ((p.x - startPos.x) + (p.y - startPos.y)) / 2
            // Anchored at the top-left, so the bubble grows away from the corner the
            // user is holding and never walks across the screen while being resized.
            var maxW = Math.min(self.bounds.x + self.bounds.w - self.x,
                                self.bounds.y + self.bounds.h - self.y)
            self.width = Math.round(Math.max(self.minSize,
                                             Math.min(startW + delta, maxW)))
            self.report()
        }
        onReleased: self.clampIntoBounds()
    }

    // Scroll still resizes -- it is faster than the grip once you know it is there --
    // but it is no longer the only way in. Bounded so it cannot vanish or overflow the
    // capture rectangle.
    WheelHandler {
        onWheel: function (ev) {
            var next = self.width + (ev.angleDelta.y > 0 ? 20 : -20)
            var maxW = Math.min(self.bounds.w, self.bounds.h) * 0.9
            var cx = self.x + self.width / 2, cy = self.y + self.height / 2
            self.width = Math.max(self.minSize, Math.min(maxW, next))
            self.x = cx - self.width / 2
            self.y = cy - self.height / 2
            self.clampIntoBounds()
        }
    }
}
