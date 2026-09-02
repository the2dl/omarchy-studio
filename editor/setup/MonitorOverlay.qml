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
    title: "omarchy-setup-overlay-" + mon.name
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
        if (s.kind === "camera")   // no on-screen rect; use the focused monitor
            return app.sources.focused === mon.name
                   || (app.sources.focused === "" && monIndex === 0)
        var cx = s.x + s.width / 2, cy = s.y + s.height / 2
        return cx >= mon.x && cx < mon.x + mon.width
               && cy >= mon.y && cy < mon.y + mon.height
    }

    // Selection rect in this overlay's coordinates (windows and regions arrive in
    // global logical coordinates; the overlay sits at the monitor's origin).
    function selRectLocal() {
        var s = app.sel
        if (s === null || s.kind === "monitor" || s.kind === "camera")
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

    // Base click layer: pick in place. Right-click in Window mode opens the plain
    // list -- the shipping product offers it as the alternative to click-to-pick.
    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        enabled: !app.counting
        onClicked: function (mouse) {
            if (mouse.button === Qt.RightButton) {
                if (app.mode === 1)
                    ov.menuAt = Qt.point(mouse.x, mouse.y)
                return
            }
            ov.menuAt = Qt.point(-1, -1)
            if (app.mode === 0)
                app.sel = app.monitorSel(mon)
            else if (app.mode === 2 && app.area === null)
                app.pickArea()
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
            || (app.mode === 2 && app.area === null)
            || (app.mode === 3 && ov.isTarget()))
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
                    : app.mode === 2 ? "\uf125  Record an area"
                    : "\uf03d  Record the camera"
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsTitle
                font.weight: Font.Medium
            }
            Text {   // resolution + fps: the fastest catch for "wrong monitor"
                anchors.horizontalCenter: parent.horizontalCenter
                text: app.mode === 0 ? ov.resLabel
                    : app.mode === 2 ? "drag a region, or click a window to snap to it"
                    : (app.sources.cameras.length > 0 ? app.sources.cameras[0].name
                                                       : "no camera detected")
                color: Theme.text3
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsRow
            }

            // Camera device rows, only when there is a choice to make.
            Column {
                visible: app.mode === 3 && app.sources.cameras.length > 1
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 4
                Repeater {
                    model: app.mode === 3 ? app.sources.cameras : []
                    delegate: Rectangle {
                        required property var modelData
                        width: 280
                        height: 28
                        radius: Theme.radiusChip
                        property bool picked: app.sel !== null
                                              && app.sel.target === "camera:" + modelData.device
                        color: picked ? Theme.accentWash
                               : camMa.containsMouse ? Theme.fillHover : "transparent"
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            x: 10
                            width: parent.width - 20
                            elide: Text.ElideRight
                            text: modelData.name + "   " + modelData.device
                            color: parent.picked ? Theme.text : Theme.text3
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsRow
                        }
                        MouseArea {
                            id: camMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: app.sel = { kind: "camera", name: modelData.name,
                                                   device: modelData.device,
                                                   target: "camera:" + modelData.device }
                        }
                    }
                }
            }

            C.PrimaryButton {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: (app.mode === 0 && ov.isTarget())
                         || (app.mode === 3 && app.sel !== null)
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
