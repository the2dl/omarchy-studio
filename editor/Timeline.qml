// Transport, scrub bar, click ticks and cut regions.
//
// The whole bar is indexed in SOURCE frames -- the same integers the project stores --
// so a boundary the user drags here is already on the frame grid when it reaches the
// model. Milliseconds appear only where a media player has to be told where to seek.
//
// Click ticks come from events/input.jsonl, mapped to frames by the bridge. They are the
// reason the bar is worth scrubbing: they are where auto-zoom will fire, so a user can
// jump straight to the moment they want to check.
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Item {
    id: root

    property Item preview: null
    readonly property var st: Bridge.state
    readonly property int sourceFrames: st.source_frames || 0
    readonly property real msPerFrame: st.timebase ? st.timebase.ms_per_frame : 1000 / 60
    readonly property int frame: preview ? preview.frame : 0

    // The pending selection, in frames. A drag with the right button (or with a cut tool
    // active) marks a range; the Cut button turns it into a removed range.
    property int selStart: -1
    property int selEnd: -1
    readonly property bool hasSelection: selStart >= 0 && selEnd > selStart

    function frameToX(f) {
        return sourceFrames > 0 ? (f / sourceFrames) * bar.width : 0
    }

    function xToFrame(x) {
        return sourceFrames > 0 ? Math.round(Math.max(0, Math.min(1, x / bar.width)) * sourceFrames) : 0
    }

    // mm:ss.ff, counted in whole frames rather than from a millisecond position: the
    // position a player reports drifts inside a frame and would make the counter flicker
    // between two values while paused.
    function timeLabel(f) {
        var fps = Math.max(1, Math.round(st.timebase ? st.timebase.fps : 60))
        var mm = Math.floor(f / (60 * fps))
        var ss = Math.floor(f / fps) % 60
        var ff = f % fps
        return (mm < 10 ? "0" : "") + mm + ":" + (ss < 10 ? "0" : "") + ss + "." + (ff < 10 ? "0" : "") + ff
    }

    Rectangle {
        x: 0; y: 0
        width: root.width
        height: root.height
        color: Theme.surface
    }

    ColumnLayout {
        x: Theme.pad
        y: 4
        width: root.width - 2 * Theme.pad
        height: root.height - 8
        spacing: 4

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Button {
                text: root.preview && root.preview.playing ? "Pause" : "Play"
                enabled: root.preview && root.preview.hasScreen
                onClicked: root.preview.togglePlay()
            }
            Button {
                text: "◀"
                enabled: root.preview && root.preview.hasScreen
                onClicked: root.preview.step(-1)
            }
            Button {
                text: "▶"
                enabled: root.preview && root.preview.hasScreen
                onClicked: root.preview.step(1)
            }
            Text {
                text: root.timeLabel(root.frame) + "   frame " + root.frame
                      + " / " + root.sourceFrames
                color: Theme.foreground
                font.family: "monospace"
                font.pixelSize: 13
            }
            Item { Layout.fillWidth: true }
            Text {
                visible: root.hasSelection
                text: "selection " + root.selStart + "–" + root.selEnd
                      + " (" + ((root.selEnd - root.selStart) * root.msPerFrame / 1000).toFixed(2) + "s)"
                color: Theme.dim
                font.pixelSize: 13
            }
            Button {
                text: "Cut selection"
                enabled: root.hasSelection
                onClicked: {
                    // Sent in ms and snapped by Timebase.to_frame on the far side, so the
                    // boundary can only ever land on the frame grid.
                    Bridge.op("add_cut", {
                        start_ms: root.selStart * root.msPerFrame,
                        end_ms: root.selEnd * root.msPerFrame
                    })
                    root.selStart = -1
                    root.selEnd = -1
                }
            }
            Button {
                text: "Clear selection"
                enabled: root.hasSelection
                onClicked: { root.selStart = -1; root.selEnd = -1 }
            }
        }

        Item {
            id: bar
            Layout.fillWidth: true
            Layout.fillHeight: true

            Rectangle {
                x: 0; y: 0
                width: bar.width
                height: bar.height
                color: Theme.surfaceDeep
                radius: Theme.radius
            }

            // Cut regions: what the export will remove. Source time, so they sit exactly
            // where the removed material is on this bar.
            Repeater {
                model: root.st.cuts || []
                Rectangle {
                    x: root.frameToX(modelData.start)
                    y: 0
                    width: Math.max(2, root.frameToX(modelData.end) - root.frameToX(modelData.start))
                    height: bar.height
                    color: Qt.rgba(Theme.danger.r, Theme.danger.g, Theme.danger.b, 0.35)
                    border.color: Theme.danger
                    border.width: 1
                    MouseArea {
                        x: 0; y: 0
                        width: parent.width
                        height: parent.height
                        acceptedButtons: Qt.RightButton
                        cursorShape: Qt.PointingHandCursor
                        onClicked: Bridge.op("delete_cut", { index: index })
                    }
                }
            }

            Rectangle {
                visible: root.hasSelection
                x: root.frameToX(root.selStart)
                y: 0
                width: Math.max(2, root.frameToX(root.selEnd) - root.frameToX(root.selStart))
                height: bar.height
                color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.25)
                border.color: Theme.accent
                border.width: 1
            }

            // Click ticks. Clicking one seeks to it, because that is where auto-zoom
            // fires and it is the frame a user actually wants to inspect.
            Repeater {
                model: root.st.clicks || []
                Rectangle {
                    x: root.frameToX(modelData.frame) - 1
                    y: bar.height - 16
                    width: 2
                    height: 12
                    color: modelData.button === "left" ? Theme.ok : Theme.warning
                    MouseArea {
                        x: -4; y: -6
                        width: 10
                        height: 22
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.preview.seekFrame(modelData.frame)
                    }
                }
            }

            Rectangle {
                id: playhead
                x: root.frameToX(root.frame) - 1
                y: 0
                width: 2
                height: bar.height
                color: Theme.accent
            }

            MouseArea {
                id: scrub
                x: 0; y: 0
                width: bar.width
                height: bar.height
                acceptedButtons: Qt.LeftButton | Qt.RightButton
                hoverEnabled: true
                property int anchorFrame: 0
                onPressed: function (m) {
                    anchorFrame = root.xToFrame(m.x)
                    if (m.button === Qt.RightButton) {
                        root.selStart = anchorFrame
                        root.selEnd = anchorFrame
                    } else if (root.preview) {
                        root.preview.seekFrame(anchorFrame)
                    }
                }
                onPositionChanged: function (m) {
                    if (!pressed)
                        return
                    var f = root.xToFrame(m.x)
                    if (m.buttons & Qt.RightButton) {
                        root.selStart = Math.min(anchorFrame, f)
                        root.selEnd = Math.max(anchorFrame, f)
                    } else if (root.preview) {
                        root.preview.seekFrame(f)
                    }
                }
            }

            Text {
                x: 4
                y: 2
                text: "left-drag scrubs · right-drag marks a range · right-click a red band removes the cut"
                color: Theme.dim
                font.pixelSize: 11
            }
        }
    }
}
