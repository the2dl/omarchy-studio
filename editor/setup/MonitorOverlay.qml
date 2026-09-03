// One transparent, monitor-sized window: the in-place source picker. This is where
// Record actually lives -- the reference design puts the Start button in the
// centre of the picked display/window/area with its name, resolution and fps, so
// the confirmation IS the thing itself, live on screen, not a thumbnail of it.
//
// The launcher positions this window over its monitor by title and strips border,
// shadow, rounding and animations with window rules; everything drawn here sits on
// a fully transparent surface over the live desktop.
import QtQuick
import ".."
import "../controls" as C

Window {
    id: ov

    property var app          // the bar window: state, selection, actions
    property var mon          // one entry of sources.monitors
    property int monIndex: 0

    width: mon.width
    height: mon.height
    minimumWidth: mon.width
    maximumWidth: mon.width
    minimumHeight: mon.height
    maximumHeight: mon.height
    title: "omarchy-setup-sheet-" + mon.name   // see OVERLAY_PREFIX in the launcher
    flags: Qt.FramelessWindowHint
    color: "transparent"
    visible: !app.picking && !app.surfacesGone
             && (!app.counting || isTarget())

    // Whether the current selection lives on this monitor -- rings, Start and the
    // countdown all render on exactly one overlay.
    function isTarget() {
        var s = app.sel
        if (s === null)
            return false
        if (s.kind === "monitor")
            return s.name === mon.name
        var cx = s.x + s.width / 2, cy = s.y + s.height / 2
        return cx >= mon.x && cx < mon.x + mon.width
               && cy >= mon.y && cy < mon.y + mon.height
    }

    // Selection rect in this overlay's coordinates (windows and regions arrive in
    // global logical coordinates; the overlay sits at the monitor's origin).
    function selRectLocal() {
        var s = app.sel
        if (s === null || s.kind === "monitor")
            return null
        return { x: s.x - mon.x, y: s.y - mon.y, w: s.width, h: s.height }
    }

    function windowsHere() {
        var out = []
        var ws = app.sources.windows
        for (var i = 0; i < ws.length; ++i) {
            var cx = ws[i].x + ws[i].width / 2, cy = ws[i].y + ws[i].height / 2
            if (cx >= mon.x && cx < mon.x + mon.width
                    && cy >= mon.y && cy < mon.y + mon.height)
                out.push(ws[i])
        }
        return out
    }

    readonly property string resLabel: mon.width + "×" + mon.height + " · " + mon.refresh + "FPS"
    property point menuAt: Qt.point(-1, -1)   // right-click window list; -1 = closed

    // --probe-input: name the control under the pointer, so tests/input_audit.py can
    // ask "is this control actually hittable?" without injecting a click. A hit test
    // rather than a bare "the window heard it": everything is one window now, so the
    // interesting failure is one item covering another, which only a hit test sees.
    function probeHit(item, x, y) {
        for (var i = item.children.length - 1; i >= 0; --i) {
            var c = item.children[i]
            if (!c.visible || c.width <= 0 || c.height <= 0)
                continue
            var p = c.mapFromItem(item, x, y)
            if (p.x < 0 || p.y < 0 || p.x >= c.width || p.y >= c.height)
                continue
            var deeper = probeHit(c, p.x, p.y)
            if (deeper !== "")
                return deeper
            if (String(c.objectName).indexOf("ctl:") === 0)
                return c.objectName
        }
        return ""
    }

    // For every named control: which control does a hit test actually find at its own
    // centre and corners? Anything but itself means something is drawn over it, and a
    // user aiming there would miss. This is geometry, not event delivery, and that is
    // the right question now: there is one window, so the compositor has nothing left
    // to route wrongly -- only QML's own z-order can hide a control.
    function probeReach(item) {
        if (String(item.objectName).indexOf("ctl:") === 0) {
            var pts = [[item.width / 2, item.height / 2], [2, 2],
                       [item.width - 3, 2], [2, item.height - 3],
                       [item.width - 3, item.height - 3]]
            var names = []
            for (var k = 0; k < pts.length; ++k) {
                var q = ov.contentItem.mapFromItem(item, pts[k][0], pts[k][1])
                var hit = ov.probeHit(ov.contentItem, q.x, q.y)
                names.push(hit === "" ? "-" : hit)
            }
            console.log("PROBE REACH " + item.objectName + " " + names.join(" "))
        }
        for (var i = 0; i < item.children.length; ++i)
            probeReach(item.children[i])
    }

    function probeRects(item) {
        if (String(item.objectName).indexOf("ctl:") === 0) {
            var p = item.mapToItem(null, 0, 0)
            console.log("PROBE RECT " + item.objectName
                        + " " + Math.round(ov.mon.x + p.x)
                        + " " + Math.round(ov.mon.y + p.y)
                        + " " + Math.round(item.width) + " " + Math.round(item.height))
        }
        for (var i = 0; i < item.children.length; ++i)
            probeRects(item.children[i])
    }

    Connections {
        target: app
        function onReleaseCameras() { selfView.release() }

        function onProbeReady() {
            if (app.openPickerArg !== "")
                setupBar.openPicker(app.openPickerArg)
            if (app.probeInput)
                probeSettle.restart()
        }
    }

    // A panel opened one statement earlier has not been laid out yet: measuring
    // straight away reported all three device rows stacked at the same y, which is
    // where they sit before the Column positions them.
    Timer {
        id: probeSettle
        interval: 250
        onTriggered: {
            ov.probeRects(ov.contentItem)
            ov.probeReach(ov.contentItem)
            console.log("PROBE RECTS-END")
        }
    }

    // --- Display mode: ring the monitor, card in the centre ----------------------

    Rectangle {   // the selected display's ring, inset so both edges stay on screen
        visible: !app.counting && app.mode === 0 && ov.isTarget()
        anchors.fill: parent
        anchors.margins: 2
        color: "transparent"
        border.width: 2
        border.color: Theme.accent
    }

    // --- Window mode: live rects, hover to preview, click to pick ----------------

    Repeater {
        model: !app.counting && app.mode === 1 ? ov.windowsHere() : []
        delegate: Rectangle {
            required property var modelData
            x: modelData.x - ov.mon.x
            y: modelData.y - ov.mon.y
            width: modelData.width
            height: modelData.height
            radius: Theme.radiusRow
            property bool picked: app.sel !== null && app.sel.target === modelData.target
            color: winHover.containsMouse && !picked ? Theme.fillSubtle : "transparent"
            border.width: picked ? 2 : (winHover.containsMouse ? 1.5 : 1)
            border.color: picked ? Theme.accent
                          : winHover.containsMouse ? Theme.text4 : Theme.hairline

            // Name chip above the rect (or inside its top edge at the screen top).
            Rectangle {
                visible: parent.picked || winHover.containsMouse
                x: 0
                y: parent.y > 30 ? -28 : 6
                width: chipText.implicitWidth + 20
                height: 24
                radius: Theme.radiusChip
                color: Theme.bg
                border.width: 1
                border.color: Theme.hairline
                Text {
                    id: chipText
                    anchors.centerIn: parent
                    text: modelData.title + "  ·  " + modelData.width + "×" + modelData.height
                    color: parent.parent.picked ? Theme.text : Theme.text3
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsCaption
                }
            }

            MouseArea {
                id: winHover
                anchors.fill: parent
                hoverEnabled: true
                acceptedButtons: Qt.LeftButton
                onClicked: { ov.menuAt = Qt.point(-1, -1); app.sel = modelData }
            }

            C.PrimaryButton {
                anchors.centerIn: parent
                visible: parent.picked
                text: "\uf192  Start recording"
                onClicked: app.record()
            }
        }
    }

    // Right-click list: every window on this monitor by title. Drawn by hand
    // rather than with Controls' Menu so it stays in this design's register.
    Rectangle {
        visible: ov.menuAt.x >= 0 && app.mode === 1 && !app.counting
        x: Math.min(ov.menuAt.x, ov.width - width - 8)
        y: Math.min(ov.menuAt.y, ov.height - height - 8)
        width: 340
        height: menuCol.implicitHeight + 12
        radius: Theme.radiusRow
        color: Theme.bg
        border.width: 1
        border.color: Theme.hairline

        Column {
            id: menuCol
            x: 6; y: 6
            width: parent.width - 12

            Repeater {
                model: ov.menuAt.x >= 0 ? ov.windowsHere() : []
                delegate: Rectangle {
                    required property var modelData
                    width: menuCol.width
                    height: 30
                    radius: Theme.radiusChip
                    color: rowMa.containsMouse ? Theme.fillHover : "transparent"
                    Row {
                        anchors.verticalCenter: parent.verticalCenter
                        x: 10
                        spacing: 10
                        Text {
                            text: modelData.title
                            width: 240
                            elide: Text.ElideRight
                            color: Theme.text2
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsRow
                        }
                        Text {
                            text: modelData.width + "×" + modelData.height
                            color: Theme.text5
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsHint
                        }
                    }
                    MouseArea {
                        id: rowMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { app.sel = modelData; ov.menuAt = Qt.point(-1, -1) }
                    }
                }
            }
        }
    }

    // --- Area mode: ring the picked region -------------------------------------

    Item {
        visible: !app.counting && app.mode === 2 && app.area !== null && ov.isTarget()
        x: ov.selRectLocal() !== null ? ov.selRectLocal().x : 0
        y: ov.selRectLocal() !== null ? ov.selRectLocal().y : 0
        width: ov.selRectLocal() !== null ? ov.selRectLocal().w : 0
        height: ov.selRectLocal() !== null ? ov.selRectLocal().h : 0

        Rectangle {
            anchors.fill: parent
            color: "transparent"
            border.width: 2
            border.color: Theme.accent
        }
        Rectangle {   // size chip, pinned to the region's top edge
            x: 0
            y: parent.y > 30 ? -28 : 6
            width: areaChip.implicitWidth + 20
            height: 24
            radius: Theme.radiusChip
            color: Theme.bg
            border.width: 1
            border.color: Theme.hairline
            Text {
                id: areaChip
                anchors.centerIn: parent
                text: app.area !== null
                      ? app.area.width + "×" + app.area.height + " · " + ov.mon.refresh + "FPS"
                      : ""
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
        }
        Column {
            anchors.centerIn: parent
            spacing: 10
            C.PrimaryButton {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "\uf192  Start recording"
                onClicked: app.record()
            }
            C.GhostButton {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Pick again"
                onClicked: app.pickArea()
            }
        }
    }

    // --- the centre card: Display confirm / Area invite / Camera pick ------------

    Rectangle {
        id: card
        visible: !app.counting && (
            (app.mode === 0)
            || (app.mode === 2 && app.area === null))
        anchors.centerIn: parent
        width: cardCol.implicitWidth + 56
        height: cardCol.implicitHeight + 44
        radius: Theme.radiusPanel
        color: Theme.bg
        border.width: 1
        border.color: Theme.hairline

        Column {
            id: cardCol
            anchors.centerIn: parent
            spacing: 12

            Text {   // headline: what this surface is
                anchors.horizontalCenter: parent.horizontalCenter
                text: app.mode === 0 ? ("\uf108  Display " + (ov.monIndex + 1))
                                     : "\uf125  Record an area"
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsTitle
                font.weight: Font.Medium
            }
            Text {   // resolution + fps: the fastest catch for "wrong monitor"
                anchors.horizontalCenter: parent.horizontalCenter
                text: app.mode === 0 ? ov.resLabel
                                     : "drag a region, or click a window to snap to it"
                color: Theme.text3
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsRow
            }

            C.PrimaryButton {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: app.mode === 0 && ov.isTarget()
                text: "\uf192  Start recording"
                onClicked: app.record()
            }
            Text {   // multi-monitor: unpicked displays invite the click instead
                anchors.horizontalCenter: parent.horizontalCenter
                visible: app.mode === 0 && !ov.isTarget()
                text: "click to record this display"
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
            Text {   // Area invite: the card itself is the button
                anchors.horizontalCenter: parent.horizontalCenter
                visible: app.mode === 2
                text: "click anywhere to open the picker"
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
        }

        MouseArea {
            visible: app.mode === 2
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: app.pickArea()
        }
    }

    // The self-view sits under the bar and the panels (declared before them) so it can
    // be dragged anywhere without ever covering the controls that configure it.
    // The teleprompter cannot be dragged directly while this sheet is up -- see
    // PrompterProxy.qml for why raising it does not work -- so the sheet drags it.
    // Only the monitor the prompter is actually on draws the proxy.
    PrompterProxy {
        app: ov.app
        rect: ov.app.prompterRect
        originX: ov.mon.x
        originY: ov.mon.y
        visible: ov.app.prompterRect && ov.app.prompterRect.running === true
                 && !ov.app.counting && !ov.app.picking
                 && ov.app.prompterRect.x + ov.app.prompterRect.width / 2 >= ov.mon.x
                 && ov.app.prompterRect.x + ov.app.prompterRect.width / 2
                    < ov.mon.x + ov.mon.width
        onMoveRequested: function (x, y) { ov.app.movePrompter(x, y) }
        z: 40
    }

    SelfView {
        id: selfView
        app: ov.app
        visible: ov.ownsSelfView && app.cameraMode > 0 && !app.counting && !app.picking
                 && app.sources.cameras.length > 0
        authoritative: ov.ownsSelfView
        monWidth: ov.mon.width
        monHeight: ov.mon.height
        originX: ov.mon.x
        originY: ov.mon.y
        // The capture rectangle, in this sheet's coordinates. A whole-display target
        // has no local rect (selRectLocal returns null for it) and is the whole sheet.
        // SelfView clamps itself into this, so the defaults can no longer put the
        // bubble half off the bottom of the screen the way a raw 0.70 did.
        bounds: {
            var r = ov.selRectLocal()
            return r === null ? ({ "x": 0, "y": 0, "w": ov.width, "h": ov.height })
                              : ({ "x": r.x, "y": r.y, "w": r.w, "h": r.h })
        }
    }

    // --- the bar ------------------------------------------------------------------
    // Declared after the click layer, so QML puts it above: that ordering is the
    // whole fix for the dead controls, and it is not the compositor's business.
    SetupBar {
        id: setupBar
        app: ov.app
        visible: ov.hasBar && !app.counting && !app.picking
        x: Math.round((ov.width - width) / 2)
        y: ov.height - height - 26        // the mock's bottom shelf
    }

    // --- device pickers: the mic and camera lists, opened from the bar -----------
    // Drawn here rather than in the bar because the bar's window is 70px tall and
    // anything taller is clipped by the compositor. Only the overlay carrying the
    // bar draws it, so a second monitor does not show a duplicate panel.

    readonly property bool hasBar: app.sources.focused === mon.name

    // Which sheet draws the self-view and owns its placement. The target's sheet, not
    // the focused one: the camera is composited inside the RECORDING, so it belongs on
    // whatever is being recorded -- the same monitor only by coincidence, and always so
    // on a one-display machine. Falls back to the focused sheet while nothing is picked
    // yet (Window mode before a pick, Area before the picker runs), because a camera
    // preview that vanishes the moment you change mode reads as the camera dying.
    readonly property bool ownsSelfView: isTarget() || (app.sel === null && hasBar)
                                   || (app.sources.focused === "" && monIndex === 0)
    readonly property var pickerModel:
        app.pickerOpen === "mic" ? (app.sources.mics || [])
        : app.pickerOpen === "camera" ? (app.sources.cameras || [])
        : []

    Rectangle {
        id: picker
        visible: ov.hasBar && app.pickerOpen !== "" && !app.counting
        width: 320
        // Anchored under the chip that opened it, clamped so a chip near either
        // edge still shows a whole panel.
        x: Math.max(8, Math.min(app.pickerAnchor - 10, ov.width - width - 8))
        y: setupBar.y - height - 10       // above the bar, 10px of air
        height: pickerCol.implicitHeight + 12
        radius: Theme.radiusRow
        color: Theme.bg
        border.width: 1
        border.color: Theme.hairline

        Column {
            id: pickerCol
            x: 6; y: 6
            width: parent.width - 12

            Repeater {
                model: ov.pickerModel
                delegate: Rectangle {
                    required property int index
                    required property var modelData
                    objectName: "ctl:pick-" + index
                    width: pickerCol.width
                    height: 30
                    radius: Theme.radiusChip
                    property bool picked:
                        app.pickerOpen === "mic"
                            ? (app.micEntry !== null && app.micEntry.name === modelData.name)
                            : (app.cameraEntry !== null
                               && app.cameraEntry.device === modelData.device)
                    color: picked ? Theme.accentWash
                           : pickMa.containsMouse ? Theme.fillHover : "transparent"

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        x: 10
                        width: parent.width - 20
                        elide: Text.ElideRight
                        // The mic's human description, the camera's model name --
                        // whichever field a person would recognise the device by.
                        text: app.pickerOpen === "mic" ? modelData.label : modelData.name
                        color: parent.picked ? Theme.text : Theme.text3
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsRow
                    }

                    MouseArea {
                        id: pickMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (app.pickerOpen === "mic")
                                app.pickMic(modelData.name)
                            else {
                                app.cameraDevice = modelData.device
                                app.pickerOpen = ""
                            }
                        }
                    }
                }
            }

            Text {   // an empty list must say why, not just be a blank rectangle
                visible: ov.pickerModel.length === 0
                x: 10
                height: 30
                verticalAlignment: Text.AlignVCenter
                text: app.pickerOpen === "mic" ? "no microphone detected"
                                               : "no camera detected"
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsRow
            }
        }
    }

    // --- countdown: the last setup pixel on screen -------------------------------
    // The contract line has already been printed; capture is initialising under
    // this number. It disappears (with the whole window) 400ms before the
    // countdown ends, so it can never reach a kept frame.
    Text {
        visible: app.counting
        anchors.centerIn: parent
        text: app.countLeft
        color: Theme.accent
        style: Text.Outline
        styleColor: Theme.bg
        font.family: Theme.fontFamily
        font.pixelSize: 160
        font.weight: Font.Medium
    }
}
