// The timeline tray: transport, ruler, and the four rows (spec §1d region 5).
//
// The whole tray is indexed in SOURCE frames -- the same integers the project stores --
// so a boundary the user drags here is already on the frame grid when it reaches the
// model. Milliseconds appear only where a media player has to be told where to seek.
//
// What each row shows is real data or an honest placeholder, never fake content:
//   screen  uniform film cells; there is no thumbnail service yet, so the cells carry
//           no image rather than a stock one. Cuts overlay here as darkened spans.
//   zoom    the zoom events derived from the resolved track (Preview.zoomSegments) --
//           the row you actually work in, so its gutter label alone is accented.
//   clicks  events/input.jsonl mapped to frames by the bridge; dots that fall inside a
//           zoom event read brighter because those are the clicks that produced one.
//   audio   uniform quiet bars; nothing analyses loudness yet, so the lane reads as
//           "audio track" without drawing a waveform the app never measured.
import QtQuick
import QtQuick.Layouts
import "controls" as C

Item {
    id: root

    property Item preview: null
    readonly property var st: Bridge.state
    readonly property int sourceFrames: st.source_frames || 0
    readonly property real msPerFrame: st.timebase ? st.timebase.ms_per_frame : 1000 / 60
    readonly property int frame: preview ? preview.frame : 0
    readonly property var segments: preview ? preview.zoomSegments : []

    // The pending selection, in frames. A right-button drag marks a range; the Cut
    // button turns it into a removed range.
    property int selStart: -1
    property int selEnd: -1
    readonly property bool hasSelection: selStart >= 0 && selEnd > selStart

    // Every row maps frames through the same track origin/width, so the playhead, the
    // ticks and the blocks can never disagree about where a frame is.
    readonly property real trackX: Style.pad + Style.gutterWidth + 12
    readonly property real trackW: Math.max(1, width - trackX - Style.pad)

    function frameToX(f) {
        return sourceFrames > 0 ? (f / sourceFrames) * trackW : 0
    }

    function xToFrame(x) {
        return sourceFrames > 0 ? Math.round(Math.max(0, Math.min(1, x / trackW)) * sourceFrames) : 0
    }

    // mm:ss.ff, counted in whole frames rather than from a millisecond position: the
    // position a player reports drifts inside a frame and would make the counter
    // flicker between two values while paused.
    function timeLabel(f) {
        var fps = Math.max(1, Math.round(st.timebase ? st.timebase.fps : 60))
        var mm = Math.floor(f / (60 * fps))
        var ss = Math.floor(f / fps) % 60
        var ff = f % fps
        return mm + ":" + (ss < 10 ? "0" : "") + ss + "." + (ff < 10 ? "0" : "") + ff
    }

    // m:ss for the ruler and the total -- sub-second precision there would just churn.
    function shortTime(f) {
        var fps = Math.max(1, st.timebase ? st.timebase.fps : 60)
        var secs = Math.floor(f / fps)
        return Math.floor(secs / 60) + ":" + (secs % 60 < 10 ? "0" : "") + (secs % 60)
    }

    // A couple of frames of slack before the segment: the ease ramp's first
    // non-identity sample lands after the click that caused it, so the causing click
    // itself sits just outside [start, end) and would read as inert without this.
    function clickMadeZoom(f) {
        for (var i = 0; i < segments.length; ++i)
            if (f >= segments[i].start - 2 && f < segments[i].end)
                return true
        return false
    }

    // Recessed tray: bgDeep under everything, hairline along the top edge.
    Rectangle {
        x: 0; y: 0
        width: root.width
        height: root.height
        color: Theme.bgDeep
    }
    Rectangle {
        x: 0; y: 0
        width: root.width
        height: 1
        color: Theme.hairline
    }

    // -- transport ---------------------------------------------------------
    RowLayout {
        id: transport
        x: Style.pad
        y: 1
        width: root.width - 2 * Style.pad
        height: 38
        spacing: 14

        C.GhostButton {   // nf-fa-play / nf-fa-pause
            text: root.preview && root.preview.playing ? "" : ""
            enabled: root.preview && root.preview.hasScreen
            onClicked: root.preview.togglePlay()
        }
        C.GhostButton {   // nf-fa-step_backward
            text: ""
            enabled: root.preview && root.preview.hasScreen
            onClicked: root.preview.step(-1)
        }
        C.GhostButton {   // nf-fa-step_forward
            text: ""
            enabled: root.preview && root.preview.hasScreen
            onClicked: root.preview.step(1)
        }
        Text {
            text: root.timeLabel(root.frame)
            color: Theme.text3
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsRow
            Text {
                anchors.left: parent.right
                anchors.baseline: parent.baseline
                text: " / " + root.shortTime(root.sourceFrames)
                color: Theme.text6
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsRow
            }
        }
        Item { Layout.fillWidth: true }
        Text {
            visible: root.hasSelection
            text: root.shortTime(root.selStart) + " – " + root.shortTime(root.selEnd)
                  + " · " + ((root.selEnd - root.selStart) * root.msPerFrame / 1000).toFixed(2) + "s"
            color: Theme.text5
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsCaption
        }
        C.GhostButton {   // nf-fa-scissors
            text: " Cut"
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
        C.GhostButton {
            text: "Clear"
            enabled: root.hasSelection
            onClicked: { root.selStart = -1; root.selEnd = -1 }
        }
    }

    // -- ruler -------------------------------------------------------------
    Item {
        id: ruler
        x: root.trackX
        y: transport.y + transport.height
        width: root.trackW
        height: 18

        Repeater {
            model: 6
            Text {
                x: index / 5 * (ruler.width - width)
                y: 0
                text: root.shortTime(Math.round(index / 5 * root.sourceFrames))
                color: Theme.text6
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
            }
        }
    }

    // -- rows --------------------------------------------------------------
    Column {
        id: rows
        x: Style.pad
        y: ruler.y + ruler.height
        width: root.width - 2 * Style.pad
        spacing: Style.rowGap

        // screen: the recording's own lane. Uniform cells with 2px gaps stand in for
        // filmstrip thumbnails until something can actually render them.
        Row {
            width: parent.width
            spacing: 12
            C.Caption { width: Style.gutterWidth; text: "screen"; anchors.verticalCenter: parent.verticalCenter; font.pixelSize: Theme.fsHint }
            Rectangle {
                width: root.trackW
                height: Style.screenRowH
                radius: Theme.radiusChip
                color: Theme.fillSubtle
                clip: true
                Row {
                    x: 3; y: 3
                    spacing: 2
                    Repeater {
                        model: Math.max(1, Math.floor((root.trackW - 6) / 46))
                        Rectangle {
                            width: (root.trackW - 6 - 2 * (Math.max(1, Math.floor((root.trackW - 6) / 46)) - 1))
                                   / Math.max(1, Math.floor((root.trackW - 6) / 46))
                            height: Style.screenRowH - 6
                            radius: 4
                            color: Theme.canvasA
                            opacity: 0.5
                        }
                    }
                }
            }
        }

        // zoom: the working row, so its label alone is accented (spec §1d region 5).
        Row {
            width: parent.width
            spacing: 12
            C.Caption { width: Style.gutterWidth; text: "zoom"; color: Theme.accent; anchors.verticalCenter: parent.verticalCenter; font.pixelSize: Theme.fsHint }
            Rectangle {
                width: root.trackW
                height: Style.zoomRowH
                radius: Theme.radiusChip
                color: Theme.fillSubtle
                opacity: 1

                Repeater {
                    model: root.segments
                    Rectangle {
                        readonly property bool sel: root.preview && root.preview.selectedZoomIndex === index
                        x: root.frameToX(modelData.start)
                        y: 4
                        width: Math.max(6, root.frameToX(modelData.end) - root.frameToX(modelData.start))
                        height: 18
                        radius: Theme.radiusChip - 2
                        color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, sel ? 0.28 : 0.2)
                        border.width: sel ? 1.5 : 1
                        border.color: sel ? Theme.accent
                                          : Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.45)
                        Behavior on color { ColorAnimation { duration: Theme.durSlow } }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.preview.selectedZoomIndex = sel ? -1 : index
                        }
                    }
                }
            }
        }

        // clicks: every recorded click; the ones inside a zoom event read brighter.
        Row {
            width: parent.width
            spacing: 12
            C.Caption { width: Style.gutterWidth; text: "clicks"; anchors.verticalCenter: parent.verticalCenter; font.pixelSize: Theme.fsHint }
            Rectangle {
                width: root.trackW
                height: Style.clicksRowH
                radius: Theme.radiusChip
                color: Theme.fillSubtle

                Repeater {
                    model: root.st.clicks || []
                    Rectangle {
                        x: root.frameToX(modelData.frame) - 3
                        y: 5
                        width: 6
                        height: 6
                        radius: 3
                        // The spec's one literal grey outside the tokens: inactive
                        // click dots, like off-state knobs, are #6d6863 everywhere.
                        color: root.clickMadeZoom(modelData.frame) ? Theme.text3 : "#6d6863"
                        MouseArea {
                            x: -4; y: -4
                            width: 14; height: 14
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.preview.seekFrame(modelData.frame)
                        }
                    }
                }
            }
        }

        // audio: quiet uniform bars until something measures loudness.
        Row {
            width: parent.width
            spacing: 12
            C.Caption { width: Style.gutterWidth; text: "audio"; anchors.verticalCenter: parent.verticalCenter; font.pixelSize: Theme.fsHint }
            Rectangle {
                width: root.trackW
                height: Style.audioRowH
                radius: Theme.radiusChip
                color: Theme.fillSubtle
                Row {
                    x: 5
                    height: parent.height
                    spacing: 2
                    Repeater {
                        model: Math.max(1, Math.floor((root.trackW - 10) / 6))
                        Rectangle {
                            width: 4
                            height: 9
                            radius: 1
                            anchors.verticalCenter: parent.verticalCenter
                            color: Theme.text6
                            opacity: 0.55
                        }
                    }
                }
            }
        }
    }

    // -- overlays across the track area -----------------------------------
    // Cut spans: removed time, so they darken every row rather than colouring one.
    // Right-click removes the cut. Positioned above the scrub area so the handler wins.
    Item {
        id: overlays
        x: root.trackX
        y: rows.y
        width: root.trackW
        height: rows.height

        // Scrub/mark surface first, underneath the cut spans' own mouse areas.
        MouseArea {
            id: scrub
            x: 0; y: -ruler.height
            width: parent.width
            height: parent.height + ruler.height
            acceptedButtons: Qt.LeftButton | Qt.RightButton
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

        Repeater {
            model: root.st.cuts || []
            Rectangle {
                x: root.frameToX(modelData.start)
                y: 0
                width: Math.max(2, root.frameToX(modelData.end) - root.frameToX(modelData.start))
                height: overlays.height
                radius: Theme.radiusChip - 3
                color: Qt.rgba(0, 0, 0, 0.5)
                border.width: 1
                border.color: Theme.hairline
                MouseArea {
                    anchors.fill: parent
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
            height: overlays.height
            radius: Theme.radiusChip - 3
            color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.12)
            border.width: 1
            border.color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.5)
        }

        // Playhead: through the ruler and every row, so the eye can drop from the time
        // labels straight down to the audio lane.
        Rectangle {
            x: root.frameToX(root.frame) - 1
            y: -ruler.height
            width: 2
            height: overlays.height + ruler.height
            color: Theme.accent
        }
    }
}
