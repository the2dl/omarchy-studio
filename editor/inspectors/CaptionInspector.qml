// The caption layer inspector (spec §2b's pattern): identity -> cues -> appearance ->
// placement -> timing.
//
// A caption layer is one layer holding a whole transcript, which makes it the only
// inspector here whose CONTENT is a list rather than a value. The cue list is editable
// because local speech-to-text gets proper nouns and product names wrong on the first
// pass every time, and re-running the model does not fix a name it has never heard --
// so fixing it by hand has to be a first-class thing rather than a reason to give up on
// the feature.
//
// Times are read-only. Retiming a cue means retiming the ones around it, and a field
// that lets you type an overlap into the middle of a transcript is a worse tool than no
// field at all. captions.py resolves overlaps by truncating the earlier cue, so a
// mistyped time would silently eat a line.
import QtQuick
import QtQuick.Layouts
import ".."
import "../controls" as C

ColumnLayout {
    id: root

    property var spec: ({})
    property var canvas: ({ width: 1920, height: 1080 })
    signal selectLayer(string id)

    readonly property var props: spec.props || ({})
    readonly property var segments: props.segments || []
    readonly property var rect: spec.rect || ({ x: 0, y: 0, width: 0, height: 0 })

    // The plate is `box_color` as "#rrggbb@a" -- drawtext's own syntax, which is what
    // captions.py hands to the filtergraph. Split here rather than carrying two fields,
    // so there is one source of truth for what the render will draw.
    readonly property string boxSpec: props.box_color === undefined ? "#000000@0.55"
                                                                    : props.box_color
    readonly property real plateAlpha: {
        var at = boxSpec.indexOf("@")
        return at < 0 ? 1.0 : parseFloat(boxSpec.substring(at + 1))
    }
    readonly property string plateRgb: {
        var at = boxSpec.indexOf("@")
        return at < 0 ? boxSpec : boxSpec.substring(0, at)
    }

    function setProps(p) {
        Bridge.op("update_layer", { id: root.spec.id, props: p })
    }

    function clock(s) {
        var t = Math.max(0, s)
        var m = Math.floor(t / 60)
        var sec = t - m * 60
        return m + ":" + (sec < 10 ? "0" : "") + sec.toFixed(1)
    }

    spacing: 14

    // -- identity ------------------------------------------------------------------
    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        Text {
            text: ""                      // nf-fa-comment
            color: Theme.accent
            font.family: Theme.fontFamily
            font.pixelSize: 13
        }
        Text {
            Layout.fillWidth: true
            text: root.segments.length + (root.segments.length === 1 ? " cue" : " cues")
            color: Theme.text2
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsBody
        }
        Text {
            text: root.segments.length > 0
                  ? root.clock(root.segments[root.segments.length - 1].end)
                  : ""
            color: Theme.text5
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsCaption
        }
    }

    // -- the cues ------------------------------------------------------------------
    C.Caption { text: "cues" }

    Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: Math.min(240, Math.max(60, root.segments.length * 46))
        radius: Theme.radiusRow
        color: Qt.rgba(1, 1, 1, 0.025)
        clip: true

        ListView {
            id: cueList
            anchors.fill: parent
            anchors.margins: 6
            spacing: 2
            model: root.segments
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            delegate: Rectangle {
                required property var modelData
                required property int index
                width: cueList.width
                height: 42
                radius: 7
                color: cueMouse.containsMouse || cueField.activeFocus
                       ? Theme.fillSubtle : "transparent"

                MouseArea {
                    id: cueMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    acceptedButtons: Qt.NoButton
                }

                Text {
                    id: stamp
                    x: 8
                    y: 5
                    text: root.clock(modelData.start)
                    color: Theme.text5
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsHint
                }

                TextInput {
                    id: cueField
                    x: 8
                    y: 18
                    width: parent.width - 16
                    text: modelData.text
                    color: Theme.text2
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                    selectionColor: Theme.accentWash
                    selectedTextColor: Theme.text
                    clip: true
                    // A TextInput shows the end of an over-long string, so every cue in
                    // the list read as its own last few words -- which for a transcript
                    // is exactly the half you cannot recognise a line by. Park the view
                    // at the start; clicking still puts the caret where you clicked.
                    Component.onCompleted: cursorPosition = 0
                    onTextChanged: if (!activeFocus) cursorPosition = 0
                    // Committed on focus loss and on Enter, never per keystroke: every
                    // commit rebuilds the layer's whole segment list and the preview
                    // behind it.
                    onEditingFinished: {
                        if (text === modelData.text)
                            return
                        var next = []
                        for (var i = 0; i < root.segments.length; ++i) {
                            var s = root.segments[i]
                            next.push({ start: s.start, end: s.end,
                                        text: i === index ? text : s.text })
                        }
                        root.setProps({ segments: next })
                    }
                }
            }
        }

        Text {
            anchors.centerIn: parent
            visible: root.segments.length === 0
            text: "No cues yet"
            color: Theme.text5
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsRow
        }
    }

    // -- appearance ----------------------------------------------------------------
    C.Caption { text: "appearance" }

    ModelSlider {
        Layout.fillWidth: true
        label: "text size"
        subject: false
        from: 0
        to: 96
        modelValue: root.props.font_px || 0
        // 0 is not "invisible": captions.py solves a size from the tile and the line
        // count when none is given, which is the right answer far more often than a
        // number typed once against one recording's dimensions.
        display: liveValue < 1 ? "auto" : Math.round(liveValue) + " px"
        onCommitted: function (v) { root.setProps({ font_px: Math.round(v) }) }
    }

    ModelSlider {
        Layout.fillWidth: true
        label: "plate"
        subject: false
        from: 0
        to: 1
        modelValue: root.plateAlpha
        display: liveValue < 0.02 ? "none" : Math.round(liveValue * 100) + " %"
        onCommitted: function (v) {
            root.setProps({ box_color: root.plateRgb + "@" + v.toFixed(2) })
        }
    }

    C.Caption { text: "lines" }

    C.Segmented {
        Layout.fillWidth: true
        model: ["1", "2", "3"]
        currentIndex: Math.max(0, Math.min(2, (root.props.max_lines || 2) - 1))
        onActivated: function (i) { root.setProps({ max_lines: i + 1 }) }
    }

    // -- placement -----------------------------------------------------------------
    C.Caption { text: "placement" }

    PlacementGrid {
        Layout.fillWidth: true
        currentCell: {
            var cx = root.rect.x + root.rect.width / 2
            var cy = root.rect.y + root.rect.height / 2
            var col = Math.min(2, Math.max(0, Math.floor(cx / (root.canvas.width / 3))))
            var row = Math.min(2, Math.max(0, Math.floor(cy / (root.canvas.height / 3))))
            return row * 3 + col
        }
        onPlaced: function (col, row) {
            var m = 0.04 * Math.min(root.canvas.width, root.canvas.height)
            var w = root.rect.width
            var h = root.rect.height
            var x = col === 0 ? m : col === 1 ? (root.canvas.width - w) / 2
                                              : root.canvas.width - w - m
            var y = row === 0 ? m : row === 1 ? (root.canvas.height - h) / 2
                                              : root.canvas.height - h - m
            Bridge.op("update_layer", { id: root.spec.id,
                                        rect: { x: x, y: y, width: w, height: h } })
        }
    }

    TimingSection {
        Layout.fillWidth: true
        spec: root.spec
    }
}
