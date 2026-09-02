// The layer list (spec §2a): a 236px column on bgDeep beside the tool rail, hairline
// on both sides. The list IS the z-order -- top of the list is front-most -- and the
// `front` / `back` captions at the ends keep the direction unambiguous, because a
// vertical list has no inherent stacking convention.
//
// Selection is the editor-wide selection: main.qml wires `selectedId` to
// preview.selectedId and `selectLayer` back into it, the same contract SettingsPanel
// uses, so the canvas, the inspector and the timeline's layer rows all follow one
// value. Every mutation here posts an intent and renders whatever comes back; the rows
// never hold their own order.
//
// Numbers are the mockup's inline styles (the 1x set): header 13/14/11, rows 9px/10px
// padding at 9px radius with a 2px gap, footer 11/14/13, drop panel 16/12 on an accent
// 5% wash ringed at 28%.
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Dialogs
import "controls" as C

Rectangle {
    id: root

    property string selectedId: ""
    // Optional. Supplies the playhead for "lands at the playhead" and the blur tool
    // for placing a redaction by dragging; the list degrades without it.
    property Item preview: null
    signal selectLayer(string id)

    readonly property var st: Bridge.state
    readonly property var canvas: st.canvas || ({ width: 1920, height: 1080 })
    readonly property real msPerFrame: st.timebase ? st.timebase.ms_per_frame : 1000 / 60

    // Front-most first: the bridge sends layers sorted by ascending z (back to front),
    // and this list draws the opposite way up on purpose.
    readonly property var ordered: (st.layers || []).slice().reverse()

    implicitWidth: 236
    color: Theme.bgDeep

    Rectangle { x: 0; y: 0; width: 1; height: parent.height; color: Theme.hairline }
    Rectangle { x: parent.width - 1; y: 0; width: 1; height: parent.height; color: Theme.hairline }

    function displayName(l) {
        if (!l)
            return ""
        if (l.type === "image")
            return (l.props && l.props.asset) ? l.props.asset : l.id
        if (l.type === "text")
            return "\"" + ((l.props && l.props.text) ? l.props.text : "") + "\""
        if (l.type === "blur" || l.type === "pixelate" || (l.props && l.props.redact))
            return "Redact · " + l.id
        return l.id
    }

    function glyphFor(l) {
        // nf-fa: image / font / square_o / eye_slash / th, matching the rail's register.
        switch (l.type) {
        case "image": return ""
        case "text": return ""
        case "shape": return ""
        case "pixelate": return ""
        default: return ""
        }
    }

    function rangeLabel(l) {
        function mmss(f) {
            var fps = Math.max(1, st.timebase ? st.timebase.fps : 60)
            var s = Math.floor(f / fps)
            return Math.floor(s / 60) + ":" + (s % 60 < 10 ? "0" : "") + (s % 60)
        }
        return l.t ? (mmss(l.t.start) + " – " + mmss(l.t.end))
                   : ("0:00 – " + mmss(st.source_frames || 0))
    }

    // The added layer is the one whose id was not there before the op replied.
    function selectNew(before, s) {
        if (!s || !s.layers)
            return
        for (var i = 0; i < s.layers.length; ++i)
            if (!before[s.layers[i].id])
                root.selectLayer(s.layers[i].id)
    }

    function idsBefore() {
        var before = {}
        var layers = st.layers || []
        for (var i = 0; i < layers.length; ++i)
            before[layers[i].id] = true
        return before
    }

    function deleteSelected() {
        if (root.selectedId === "")
            return
        Bridge.op("delete_layer", { id: root.selectedId })
        root.selectLayer("")
    }

    // -- header: caption, the `front` end marker, add ------------------------
    Item {
        id: header
        x: 0
        y: 0
        width: parent.width
        height: 40

        C.Caption { x: 14; anchors.verticalCenter: parent.verticalCenter; text: "layers" }
        Text {
            anchors.right: addBtn.left
            anchors.rightMargin: 10
            anchors.verticalCenter: parent.verticalCenter
            text: "front"
            color: Theme.text6
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsHint
        }
        Item {
            id: addBtn
            anchors.right: parent.right
            anchors.rightMargin: 10
            anchors.verticalCenter: parent.verticalCenter
            width: 26
            height: 26
            Rectangle {
                anchors.fill: parent
                radius: Theme.radiusChip
                color: addMa.containsMouse ? Theme.fillHover : "transparent"
            }
            Text {   // nf-fa-plus
                anchors.centerIn: parent
                text: ""
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: 13
            }
            MouseArea {
                id: addMa
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: addMenu.open()
            }
        }
    }

    // The add menu (spec §2a: Image, Text, Shape, Redact). A hand-rolled popup rather
    // than Controls' Menu, whose default style fights every token this app has.
    Popup {
        id: addMenu
        x: parent.width - width - 8
        y: header.height - 2
        width: 150
        padding: 5
        background: Rectangle {
            radius: Theme.radiusRow
            color: Theme.bgFloat
            border.width: 1
            border.color: Theme.hairline
        }
        contentItem: Column {
            spacing: 1
            Repeater {
                model: [
                    { label: "Image…", act: "image" },
                    { label: "Text", act: "text" },
                    { label: "Shape", act: "shape" },
                    { label: "Redact", act: "redact" }
                ]
                delegate: Rectangle {
                    required property var modelData
                    width: 140
                    height: 28
                    radius: Theme.radiusChip
                    color: itemMa.containsMouse ? Theme.fillHover : "transparent"
                    Text {
                        x: 10
                        anchors.verticalCenter: parent.verticalCenter
                        text: parent.modelData.label
                        color: Theme.text2
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsRow
                    }
                    MouseArea {
                        id: itemMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            addMenu.close()
                            root.addLayer(parent.modelData.act)
                        }
                    }
                }
            }
        }
    }

    function addLayer(kind) {
        var before = idsBefore()
        var cw = canvas.width
        var ch = canvas.height
        if (kind === "image") {
            imageDialog.open()
        } else if (kind === "text") {
            Bridge.op("add_text", { rect: { x: cw * 0.34, y: ch * 0.42,
                                            width: cw * 0.32, height: ch * 0.07 } },
                      function (s, ok) { if (ok) root.selectNew(before, s) })
        } else if (kind === "shape") {
            Bridge.op("add_shape", { rect: { x: cw * 0.375, y: ch * 0.4,
                                             width: cw * 0.25, height: ch * 0.2 } },
                      function (s, ok) { if (ok) root.selectNew(before, s) })
        } else if (kind === "redact") {
            // A redaction placed blind covers nothing in particular; hand the user the
            // blur tool so the box is drawn over the thing it is meant to hide.
            if (root.preview)
                root.preview.tool = "blur"
            else
                Bridge.op("add_blur", { rect: { x: cw * 0.3, y: ch * 0.44,
                                                width: cw * 0.4, height: ch * 0.12 } },
                          function (s, ok) { if (ok) root.selectNew(before, s) })
        }
    }

    FileDialog {
        id: imageDialog
        title: "Add an image"
        nameFilters: ["Images (*.png *.jpg *.jpeg *.webp *.bmp *.svg)"]
        onAccepted: {
            var before = root.idsBefore()
            var path = decodeURIComponent(selectedFile.toString().replace("file://", ""))
            Bridge.op("add_image", { path: path },
                      function (s, ok) { if (ok) root.selectNew(before, s) })
        }
    }

    // -- the rows ------------------------------------------------------------
    Flickable {
        id: listFlick
        x: 0
        y: header.height
        width: parent.width
        height: footer.y - y
        contentHeight: rowsCol.height
        clip: true
        interactive: contentHeight > height

        Column {
            id: rowsCol
            x: 7
            width: listFlick.width - 14
            spacing: 2

            Repeater {
                id: rowRepeater
                model: root.ordered

                delegate: Item {
                    id: row
                    required property int index
                    required property var modelData

                    readonly property bool sel: root.selectedId === modelData.id
                    readonly property bool hidden: modelData.enabled === false

                    width: rowsCol.width
                    height: 46
                    z: handleMa.pressed ? 10 : 0

                    // Live drag offset for reordering. The row follows the pointer;
                    // the real order only changes when the bridge's reply re-sorts
                    // the model on release.
                    transform: Translate { y: handleMa.pressed ? handleMa.dy : 0 }

                    Rectangle {
                        anchors.fill: parent
                        radius: 9
                        color: row.sel ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.11)
                             : rowMa.containsMouse ? Theme.fillHover : "transparent"
                        border.width: row.sel ? 1.5 : 0
                        border.color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.4)
                        Behavior on color { ColorAnimation { duration: Theme.durFast } }
                    }

                    MouseArea {
                        id: rowMa
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: root.selectLayer(row.modelData.id)
                    }

                    // Drag handle: the mock's drag_indicator, drawn as a 2x3 dot grip
                    // rather than guessed at from a glyph table.
                    Item {
                        id: handle
                        x: 10
                        width: 10
                        height: parent.height
                        Grid {
                            anchors.centerIn: parent
                            columns: 2
                            spacing: 3
                            Repeater {
                                model: 6
                                Rectangle {
                                    width: 2.5
                                    height: 2.5
                                    radius: 1.25
                                    color: row.sel ? Theme.accentDim : Theme.text6
                                }
                            }
                        }
                        MouseArea {
                            id: handleMa
                            anchors.fill: parent
                            anchors.margins: -6
                            cursorShape: Qt.SizeVerCursor
                            property real startY: 0
                            property real dy: 0
                            onPressed: function (m) {
                                startY = mapToItem(rowsCol, m.x, m.y).y
                                dy = 0
                                root.selectLayer(row.modelData.id)
                            }
                            onPositionChanged: function (m) {
                                if (pressed)
                                    dy = mapToItem(rowsCol, m.x, m.y).y - startY
                            }
                            onReleased: {
                                var step = row.height + rowsCol.spacing
                                var moved = Math.round(dy / step)
                                dy = 0
                                if (moved !== 0)
                                    root.reorder(row.index, row.index + moved)
                            }
                        }
                    }

                    Text {
                        id: typeGlyph
                        x: 30
                        anchors.verticalCenter: parent.verticalCenter
                        text: root.glyphFor(row.modelData)
                        // Hidden drops the glyph a step (spec: #6d6863); text5 is the
                        // theme-resolved equivalent so light themes keep the relation.
                        color: row.hidden ? Theme.text5 : row.sel ? Theme.accent : Theme.text3
                        font.family: Theme.fontFamily
                        font.pixelSize: 14
                    }

                    Column {
                        x: 52
                        width: eye.x - x - 6
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 2
                        Text {
                            width: parent.width
                            text: root.displayName(row.modelData)
                            color: row.hidden ? Theme.text5 : row.sel ? Theme.text : Theme.text3
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsRow
                            elide: Text.ElideRight
                        }
                        Text {
                            width: parent.width
                            text: root.rangeLabel(row.modelData)
                            color: row.sel ? Theme.accentDim : Theme.text6
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsHint
                            elide: Text.ElideRight
                        }
                    }

                    Item {
                        id: eye
                        anchors.right: parent.right
                        anchors.rightMargin: 6
                        anchors.verticalCenter: parent.verticalCenter
                        width: 26
                        height: 26
                        Rectangle {
                            anchors.fill: parent
                            radius: Theme.radiusChip
                            color: eyeMa.containsMouse ? Theme.fillHover : "transparent"
                        }
                        Text {   // nf-fa-eye / eye_slash; off sits far darker (spec: #3d3936)
                            anchors.centerIn: parent
                            text: row.hidden ? "" : ""
                            color: row.hidden
                                   ? Qt.rgba(Theme.text6.r, Theme.text6.g, Theme.text6.b, 0.55)
                                   : row.sel ? Theme.accent : Theme.text5
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                        }
                        MouseArea {
                            id: eyeMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: Bridge.op("update_layer", {
                                id: row.modelData.id, enabled: row.hidden })
                        }
                    }
                }
            }
        }

        Text {
            visible: root.ordered.length === 0
            x: 14
            y: 8
            width: parent.width - 28
            text: "No layers yet. Add one above, drop an image below, or drag a blur box on the canvas."
            color: Theme.text6
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsHint
            wrapMode: Text.WordWrap
        }
    }

    // Reorder by display index (0 = front-most). z is rewritten for the whole stack --
    // n ops at 0.27ms each -- because relative z juggling accumulates duplicates.
    function reorder(from, to) {
        var ids = []
        for (var i = 0; i < ordered.length; ++i)
            ids.push(ordered[i].id)
        to = Math.max(0, Math.min(ids.length - 1, to))
        if (from === to)
            return
        var id = ids.splice(from, 1)[0]
        ids.splice(to, 0, id)
        for (var j = 0; j < ids.length; ++j)
            Bridge.op("update_layer", { id: ids[j], z: ids.length - j })
    }

    // -- footer: the `back` end marker and delete ----------------------------
    Item {
        id: footer
        x: 0
        y: drop.y - height
        width: parent.width
        height: 36

        Text {
            x: 14
            anchors.verticalCenter: parent.verticalCenter
            text: "back"
            color: Theme.text6
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsHint
        }
        Item {
            anchors.right: parent.right
            anchors.rightMargin: 10
            anchors.verticalCenter: parent.verticalCenter
            width: 26
            height: 26
            opacity: root.selectedId === "" ? 0.35 : 1
            Rectangle {
                anchors.fill: parent
                radius: Theme.radiusChip
                color: delMa.containsMouse && root.selectedId !== "" ? Theme.fillHover : "transparent"
            }
            Text {   // nf-fa-trash
                anchors.centerIn: parent
                text: ""
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: 13
            }
            MouseArea {
                id: delMa
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.deleteSelected()
            }
        }
    }

    // -- drop target, pinned to the bottom and always visible ----------------
    Rectangle {
        id: drop
        x: 10
        y: parent.height - height - 12
        width: parent.width - 20
        height: 86
        radius: 11
        color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b,
                       dropArea.containsDrag ? 0.12 : 0.05)
        border.width: 1.5
        border.color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b,
                              dropArea.containsDrag ? 0.6 : 0.28)
        Behavior on color { ColorAnimation { duration: Theme.durFast } }

        Column {
            anchors.centerIn: parent
            spacing: 7
            Text {   // nf-fa-image
                anchors.horizontalCenter: parent.horizontalCenter
                text: ""
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: 17
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Drop an image"
                color: Theme.text3
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "lands at the playhead"
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
            }
        }

        DropArea {
            id: dropArea
            anchors.fill: parent
            onDropped: function (d) {
                if (!d.hasUrls)
                    return
                var before = root.idsBefore()
                var path = decodeURIComponent(d.urls[0].toString().replace("file://", ""))
                // "lands at the playhead": visible from the drop onward. The canvas
                // DropArea (Preview) is the positional twin -- it lands at the drop
                // POINT instead; this one takes the default centre placement.
                var startMs = root.preview ? root.preview.frame * root.msPerFrame : 0
                var endMs = root.st.duration_ms || 0
                Bridge.op("add_image", { path: path }, function (s, ok) {
                    if (!ok)
                        return
                    var newId = ""
                    for (var i = 0; i < s.layers.length; ++i)
                        if (!before[s.layers[i].id])
                            newId = s.layers[i].id
                    if (newId === "")
                        return
                    if (endMs > startMs && startMs > 0)
                        Bridge.op("update_layer", { id: newId, start_ms: startMs, end_ms: endMs })
                    root.selectLayer(newId)
                })
                d.accept()
            }
        }
    }

    // Backspace deletes the selection (spec §2a), but never while something focusable
    // is being typed in -- Shortcut outranks the focused item's key handling, and
    // eating a Backspace out of a text field to delete a layer would be a disaster.
    Shortcut {
        sequence: "Backspace"
        enabled: root.visible && root.selectedId !== ""
                 && !(root.Window.activeFocusItem
                      && root.Window.activeFocusItem.hasOwnProperty("cursorPosition"))
        onActivated: root.deleteSelected()
    }
}
