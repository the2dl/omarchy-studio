// The timeline tray: transport, ruler, and the rows (spec §1d region 5, §2a, §2c).
//
// The whole tray is indexed in SOURCE frames -- the same integers the project stores --
// so a boundary the user drags here is already on the frame grid when it reaches the
// model. Milliseconds appear only where a media player has to be told where to seek.
//
// What each row shows is real data or an honest placeholder, never fake content:
//   screen  uniform film cells; there is no thumbnail service yet, so the cells carry
//           no image rather than a stock one.
//   layers  one 18px sub-row per layer in list order (front-most first), each bar
//           spanning the layer's time range (spec §2a). Selection is shared with the
//           canvas and the layer list through preview.selectedId.
//   zoom    the zoom events derived from the resolved track (Preview.zoomSegments) --
//           the row you actually work in, so its gutter label is accented.
//   clicks  events/input.jsonl mapped to frames by the bridge; dots that fall inside a
//           zoom event read brighter because those are the clicks that produced one.
//   audio   uniform quiet bars; nothing analyses loudness yet, so the lane reads as
//           "audio track" without drawing a waveform the app never measured.
//
// Cuts are a fold, not a hole (spec §2c). Three states, all of the same edit.cuts data:
//   selecting  drag on the ruler (or C from the playhead); the range dims and carries
//              an accent cap with its duration.
//   collapsed  the span folds to a 16px seam spanning every row at once -- the x axis
//              itself folds (see buildFold), so one cut is one object, not a gap per
//              row. A chip on the ruler carries the removed duration.
//   expanded   click the seam and the frames come back in place, ghosted, with 3px
//              edge handles that retime the cut. Restore brings them back for good.
// Nothing here says "delete" and nothing is red: the source is never touched, a cut
// only changes what the export keeps.
import QtQuick
import QtQuick.Layouts
import QtQuick.Window
import "controls" as C

Item {
    id: root

    property Item preview: null
    readonly property var st: Bridge.state
    readonly property int sourceFrames: st.source_frames || 0
    readonly property real msPerFrame: st.timebase ? st.timebase.ms_per_frame : 1000 / 60
    readonly property int frame: preview ? preview.frame : 0
    readonly property var segments: preview ? preview.zoomSegments : []
    readonly property var cuts: st.cuts || []

    // Layers in LIST order: state.layers arrives z-ascending and the list shows
    // front-most first (spec §2a "list = z-order"), so the timeline reverses it to
    // agree with the list rather than with the paint order.
    readonly property var layerRows: {
        var a = (st.layers || []).slice()
        a.reverse()
        return a
    }

    // The pending selection, in frames. A ruler drag or a right-button drag on the
    // rows marks a range; Cut (or a second C) turns it into a removed range.
    property int selStart: -1
    property int selEnd: -1
    readonly property bool hasSelection: selStart >= 0 && selEnd > selStart

    // C anchors a mark at the playhead and the selection follows the playhead until
    // the second C commits it -- "C to cut from the playhead" (spec §2c state 1).
    property int markAnchor: -1
    onFrameChanged: {
        if (markAnchor >= 0) {
            selStart = Math.min(markAnchor, frame)
            selEnd = Math.max(markAnchor, frame)
        }
    }

    // The expanded cut is remembered by its start frame, not its index: retiming
    // deletes and re-adds the cut and normalize() may merge it with a neighbour, so an
    // index would dangle where the start frame still points into the right range.
    property real expandedCutStart: -1
    readonly property int expandedCutIndex: {
        for (var i = 0; i < cuts.length; ++i)
            if (expandedCutStart >= cuts[i].start - 1 && expandedCutStart < cuts[i].end)
                return i
        return -1
    }

    // Live override while an edge handle is being dragged; committed on release.
    property int dragStart: -1
    property int dragEnd: -1

    readonly property int cutFrames: {
        var n = 0
        for (var i = 0; i < cuts.length; ++i)
            n += cuts[i].end - cuts[i].start
        return n
    }
    // What the export will keep -- the length the transport counts in (spec §2c:
    // "0:14 / 2:41" with "2:47 recorded · 6s cut" beside it).
    readonly property int editFrames: Math.max(0, sourceFrames - cutFrames)

    // Every row maps frames through the same track origin/width, so the playhead, the
    // ticks and the blocks can never disagree about where a frame is.
    readonly property real trackX: Style.pad + Style.gutterWidth + 12
    readonly property real trackW: Math.max(1, width - trackX - Style.pad)

    // -- the fold ----------------------------------------------------------
    // Piecewise x mapping: each collapsed cut occupies exactly 16px (the seam, spec
    // §2c state 2) and the kept time -- plus the expanded cut, whose frames return in
    // place -- shares the rest proportionally. Every row draws through frameToX, so
    // the whole tray folds together instead of each row drawing its own gap.
    readonly property int seamW: 16
    readonly property var fold: buildFold(cuts, expandedCutIndex, trackW, sourceFrames)

    function buildFold(cs, xi, w, total) {
        var pieces = []
        if (total <= 0)
            return pieces
        var folded = 0, seams = 0
        for (var i = 0; i < cs.length; ++i)
            if (i !== xi) {
                seams++
                folded += cs[i].end - cs[i].start
            }
        var keep = Math.max(1, total - folded)
        var scale = Math.max(0, w - seams * seamW) / keep
        var x = 0, f = 0
        for (i = 0; i < cs.length; ++i) {
            if (i === xi)
                continue        // expanded: its frames are back in place, so no seam
            var c = cs[i]
            if (c.start > f) {
                pieces.push({ f0: f, f1: c.start, x0: x, x1: x + (c.start - f) * scale })
                x += (c.start - f) * scale
            }
            pieces.push({ f0: c.start, f1: c.end, x0: x, x1: x + seamW, seam: true })
            x += seamW
            f = c.end
        }
        if (f < total)
            pieces.push({ f0: f, f1: total, x0: x, x1: w })
        if (!pieces.length)
            pieces.push({ f0: 0, f1: total, x0: 0, x1: w })
        return pieces
    }

    function frameToX(f) {
        var p = fold
        if (!p.length)
            return 0
        if (f <= p[0].f0)
            return p[0].x0
        for (var i = 0; i < p.length; ++i)
            if (f < p[i].f1)
                return p[i].x0 + (f - p[i].f0) / (p[i].f1 - p[i].f0) * (p[i].x1 - p[i].x0)
        return p[p.length - 1].x1
    }

    function xToFrame(x) {
        var p = fold
        if (!p.length)
            return 0
        if (x <= p[0].x0)
            return p[0].f0
        for (var i = 0; i < p.length; ++i)
            if (x < p[i].x1 || i === p.length - 1)
                return Math.round(Math.min(p[i].f1, p[i].f0
                       + Math.max(0, x - p[i].x0) / Math.max(0.001, p[i].x1 - p[i].x0)
                         * (p[i].f1 - p[i].f0)))
        return sourceFrames
    }

    // Source frame -> output frame: subtract the cut time before it, so the transport
    // counts in the length the export will actually have.
    function outFrame(f) {
        var o = f
        for (var i = 0; i < cuts.length; ++i) {
            if (f >= cuts[i].end)
                o -= cuts[i].end - cuts[i].start
            else if (f > cuts[i].start)
                o -= f - cuts[i].start
        }
        return o
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

    // "6.0s" on the chips and the cap; the transport rounds to "6s" like the mock.
    function cutSecs(frames) {
        return (frames * msPerFrame / 1000).toFixed(1) + "s"
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

    function commitCut() {
        if (!hasSelection)
            return
        // Sent in ms and snapped by Timebase.to_frame on the far side, so the
        // boundary can only ever land on the frame grid.
        Bridge.op("add_cut", {
            start_ms: selStart * msPerFrame,
            end_ms: selEnd * msPerFrame
        })
        selStart = -1
        selEnd = -1
        markAnchor = -1
    }

    function clearSelection() {
        selStart = -1
        selEnd = -1
        markAnchor = -1
    }

    // Retime = remove + re-add through the two ops the bridge has. Chained on the
    // callbacks so the second intent operates on the state the first one produced.
    function commitRetime(index, f0, f1) {
        dragStart = -1
        dragEnd = -1
        f0 = Math.max(0, Math.min(f0, sourceFrames - 1))
        f1 = Math.max(f0 + 1, Math.min(f1, sourceFrames))
        Bridge.op("delete_cut", { index: index }, function () {
            Bridge.op("add_cut", {
                start_ms: f0 * root.msPerFrame,
                end_ms: f1 * root.msPerFrame
            }, function () {
                root.expandedCutStart = f0
            })
        })
    }

    // Restore: the frames come back because the cut range is removed from the edit --
    // the recording itself was never touched (spec §2c, "a view, not a deletion").
    function restoreCut(index) {
        expandedCutStart = -1
        Bridge.op("delete_cut", { index: index })
    }

    // main.qml pins the tray to the constant Style.timelineHeight, which was sized for
    // the four fixed rows. The layers row and the expanded-cut footer are dynamic, so
    // the tray takes over its own layout request here instead of teaching main.qml
    // about its internals. (16px bottom pad and 1px hairline per Style's breakdown.)
    implicitHeight: rows.y + rows.height + (cutFooter.visible ? 9 + cutFooter.height : 0)
                    + Style.pad + 1
    Component.onCompleted: {
        root.Layout.preferredHeight = Qt.binding(function () { return root.implicitHeight })
        root.Layout.minimumHeight = Qt.binding(function () { return root.implicitHeight })
    }

    // Keys go through Shortcuts (the tray never holds focus), gated off while a text
    // field has focus so typing a name cannot fire an edit.
    readonly property Item focusItem: Window.window ? Window.window.activeFocusItem : null
    readonly property bool typingFocus: focusItem !== null && focusItem !== undefined
                                        && focusItem.hasOwnProperty("cursorPosition")

    Shortcut {
        sequence: "C"
        enabled: !root.typingFocus && root.sourceFrames > 0
        onActivated: {
            if (root.hasSelection)
                root.commitCut()          // second C: the marked range becomes the cut
            else {
                root.markAnchor = root.frame
                root.selStart = root.frame
                root.selEnd = root.frame
            }
        }
    }
    Shortcut {
        // Spec §2c: "⌫ removes the cut" -- on the open (expanded) cut only, so a stray
        // Backspace with nothing open cannot take time out of the movie.
        sequence: "Backspace"
        enabled: !root.typingFocus && root.expandedCutIndex >= 0
        onActivated: root.restoreCut(root.expandedCutIndex)
    }
    Shortcut {
        sequence: "Escape"
        enabled: !root.typingFocus
                 && (root.hasSelection || root.markAnchor >= 0 || root.expandedCutIndex >= 0)
        onActivated: {
            if (root.hasSelection || root.markAnchor >= 0)
                root.clearSelection()
            else
                root.expandedCutStart = -1   // collapse back to the seam
        }
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
            text: root.preview && root.preview.playing ? "\uf04c" : "\uf04b"
            enabled: root.preview && root.preview.hasScreen
            onClicked: root.preview.togglePlay()
        }
        C.GhostButton {   // nf-fa-step_backward
            text: "\uf048"
            enabled: root.preview && root.preview.hasScreen
            onClicked: root.preview.step(-1)
        }
        C.GhostButton {   // nf-fa-step_forward
            text: "\uf051"
            enabled: root.preview && root.preview.hasScreen
            onClicked: root.preview.step(1)
        }
        Text {
            // Position and total in OUTPUT time; the recorded length stays visible
            // beside it whenever a cut exists (spec §2c transport).
            text: root.timeLabel(root.outFrame(root.frame))
            color: Theme.text3
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsRow
            Text {
                id: totalLabel
                anchors.left: parent.right
                anchors.baseline: parent.baseline
                text: " / " + root.shortTime(root.editFrames)
                color: Theme.text6
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsRow
            }
            Text {
                visible: root.cutFrames > 0
                anchors.left: totalLabel.right
                anchors.leftMargin: 12
                anchors.baseline: parent.baseline
                text: root.shortTime(root.sourceFrames) + " recorded · "
                      + Math.round(root.cutFrames * root.msPerFrame / 1000) + "s cut"
                color: Theme.text6
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
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
            text: "\uf0c4 Cut"
            enabled: root.hasSelection
            onClicked: root.commitCut()
        }
        C.GhostButton {
            text: "Clear"
            enabled: root.hasSelection
            onClicked: root.clearSelection()
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
                // Ticks stay at fixed source fractions and are PLACED through the
                // fold, so a collapsed cut shifts them the way it shifts everything.
                readonly property int tickFrame: Math.round(index / 5 * root.sourceFrames)
                x: Math.min(ruler.width - width, root.frameToX(tickFrame))
                y: 0
                text: root.shortTime(tickFrame)
                color: Theme.text6
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
            }
        }
    }

    // Scrub/mark surface UNDER the rows, so the blocks, dots and layer bars keep
    // their own mouse areas. Left-drag on the ruler band selects a cut range (spec
    // §2c state 1); left elsewhere scrubs; right-drag marks a range anywhere.
    MouseArea {
        id: scrub
        x: root.trackX
        y: ruler.y
        width: root.trackW
        height: ruler.height + rows.height
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        property int anchorFrame: 0
        property bool selecting: false
        property bool fromRuler: false
        onPressed: function (m) {
            anchorFrame = root.xToFrame(m.x)
            fromRuler = m.y < ruler.height
            selecting = m.button === Qt.RightButton
            if (selecting) {
                root.markAnchor = -1
                root.selStart = anchorFrame
                root.selEnd = anchorFrame
            } else if (!fromRuler && root.preview) {
                root.preview.seekFrame(anchorFrame)
            }
        }
        onPositionChanged: function (m) {
            if (!pressed)
                return
            var f = root.xToFrame(m.x)
            if (!selecting && fromRuler && Math.abs(f - anchorFrame) >= 1) {
                selecting = true
                root.markAnchor = -1
            }
            if (selecting) {
                root.selStart = Math.min(anchorFrame, f)
                root.selEnd = Math.max(anchorFrame, f)
            } else if (root.preview) {
                root.preview.seekFrame(f)
            }
        }
        onReleased: function (m) {
            // An unmoved press on the ruler is still a seek.
            if (fromRuler && !selecting && root.preview)
                root.preview.seekFrame(anchorFrame)
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

        // layers: one 18px sub-row per layer in list order (spec §2a). The bar is the
        // layer's time range; a layer with no range spans the whole recording -- that
        // full-width read IS the "always on" statement, so it draws slightly fainter
        // rather than differently.
        Row {
            visible: root.layerRows.length > 0
            width: parent.width
            spacing: 12
            C.Caption { width: Style.gutterWidth; text: "layers"; color: Theme.accent; anchors.verticalCenter: parent.verticalCenter; font.pixelSize: Theme.fsHint }
            Column {
                spacing: 3
                Repeater {
                    model: root.layerRows
                    Rectangle {
                        width: root.trackW
                        height: 18
                        radius: Theme.radiusChip - 1
                        color: Theme.fillSubtle
                        readonly property bool sel: root.preview && root.preview.selectedId === modelData.id
                        readonly property bool whole: !modelData.t
                        readonly property int f0: modelData.t ? modelData.t.start : 0
                        readonly property int f1: modelData.t ? modelData.t.end : root.sourceFrames

                        Rectangle {
                            x: root.frameToX(parent.f0)
                            y: 3
                            width: Math.max(6, root.frameToX(parent.f1) - x)
                            height: 12
                            radius: 4
                            // Mock: text-3 at 22% with a 40% ring; the whole-recording
                            // bar at 16%/30%; selected switches to the accent treatment.
                            color: parent.sel
                                   ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.3)
                                   : Qt.rgba(Theme.text3.r, Theme.text3.g, Theme.text3.b, parent.whole ? 0.16 : 0.22)
                            border.width: parent.sel ? 1.5 : 1
                            border.color: parent.sel
                                          ? Theme.accent
                                          : Qt.rgba(Theme.text3.r, Theme.text3.g, Theme.text3.b, parent.whole ? 0.3 : 0.4)
                            Behavior on color { ColorAnimation { duration: Theme.durSlow } }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.preview.selectedId = parent.parent.sel ? "" : modelData.id
                            }
                        }
                    }
                }
            }
        }

        // zoom: the working row, so its label is accented (spec §1d region 5).
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

    // -- expanded-cut footer (spec §2c state 3) ----------------------------
    Row {
        id: cutFooter
        visible: root.expandedCutIndex >= 0
        x: Style.pad
        y: rows.y + rows.height + 9
        spacing: 12

        Item { width: Style.gutterWidth; height: 1 }
        Row {
            spacing: 14
            Rectangle {
                objectName: "cutRestore"   // reached by editor/timeline/tst_cuts.qml
                width: restoreRow.width + 22
                height: 24
                radius: Theme.radiusChip + 1
                color: restoreMa.containsMouse ? Theme.track : Theme.fillHover
                Behavior on color { ColorAnimation { duration: Theme.durFast } }
                Row {
                    id: restoreRow
                    anchors.centerIn: parent
                    spacing: 7
                    Text {   // nf-fa-undo, standing in for the mock's `restore`
                        text: "\uf0e2"
                        color: Theme.text3
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsRow
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    Text {
                        text: "Restore " + root.cutSecs(root.expandedCutIndex >= 0
                              ? root.cuts[root.expandedCutIndex].end - root.cuts[root.expandedCutIndex].start : 0)
                        color: Theme.text2
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsCaption
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }
                MouseArea {
                    id: restoreMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.restoreCut(root.expandedCutIndex)
                }
            }
            Item {
                objectName: "cutCollapse"   // reached by editor/timeline/tst_cuts.qml
                width: collapseRow.width + 22
                height: 24
                Row {
                    id: collapseRow
                    anchors.centerIn: parent
                    spacing: 7
                    Text {   // nf-fa-compress, standing in for the mock's `unfold_less`
                        text: "\uf066"
                        color: "#6d6863"   // the spec's literal grey for inactive glyphs
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsCaption
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    Text {
                        text: "Collapse"
                        color: collapseMa.containsMouse ? Theme.text3 : Theme.text4
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsCaption
                        anchors.verticalCenter: parent.verticalCenter
                        Behavior on color { ColorAnimation { duration: Theme.durFast } }
                    }
                }
                MouseArea {
                    id: collapseMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.expandedCutStart = -1
                }
            }
            Text {
                text: "drag either edge to retime · ⌫ removes the cut"
                color: Theme.text6
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    // -- overlays across the track area ------------------------------------
    // Cut objects and the pending selection. Drawn over the rows because a cut is one
    // object across the whole tray, never a per-row gap (spec §2c).
    Item {
        id: overlays
        x: root.trackX
        y: rows.y
        width: root.trackW
        height: rows.height

        Repeater {
            model: root.cuts
            Item {
                readonly property bool expanded: index === root.expandedCutIndex
                // While a handle drags, the ghost tracks the pending range.
                readonly property int c0: expanded && root.dragStart >= 0 ? root.dragStart : modelData.start
                readonly property int c1: expanded && root.dragEnd >= 0 ? root.dragEnd : modelData.end
                readonly property real gx0: root.frameToX(c0)
                readonly property real gx1: root.frameToX(c1)

                // --- collapsed: the 16px seam, one object through every row -------
                Rectangle {
                    id: seam
                    visible: !expanded
                    x: gx0
                    y: 0
                    width: root.seamW
                    height: overlays.height
                    radius: 4
                    color: Theme.bgDeep
                    border.width: 1
                    border.color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.5)

                    // The dashed accent stripe down the middle (mock: 3px dash, 4px gap).
                    Column {
                        anchors.horizontalCenter: parent.horizontalCenter
                        y: 5
                        spacing: 4
                        Repeater {
                            model: Math.max(1, Math.floor((seam.height - 10 + 4) / 7))
                            Rectangle { width: 2; height: 3; color: Theme.accent }
                        }
                    }
                    MouseArea {
                        anchors.fill: parent
                        acceptedButtons: Qt.LeftButton | Qt.RightButton
                        cursorShape: Qt.PointingHandCursor
                        onClicked: function (m) {
                            if (m.button === Qt.RightButton)
                                root.restoreCut(index)   // pre-seam behaviour, kept
                            else
                                root.expandedCutStart = modelData.start
                        }
                    }
                }
                // The duration chip over the ruler: unfold hint + removed time.
                Rectangle {
                    visible: !expanded
                    x: gx0 + root.seamW / 2 - width / 2
                    y: -ruler.height - 1
                    width: chipRow.width + 12
                    height: 17
                    radius: 5
                    color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.14)
                    Row {
                        id: chipRow
                        anchors.centerIn: parent
                        spacing: 5
                        Text {   // nf-fa-arrows_v, standing in for the mock's `unfold_more`
                            text: "\uf07d"
                            color: Theme.accent
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsHint
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        Text {
                            text: root.cutSecs(modelData.end - modelData.start)
                            color: Theme.accent
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsHint
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.expandedCutStart = modelData.start
                    }
                }

                // --- expanded: the frames back in place, ghosted ------------------
                Rectangle {
                    visible: expanded
                    x: gx0
                    y: 0
                    width: Math.max(2, gx1 - gx0)
                    height: overlays.height
                    color: Qt.rgba(Theme.bgDeep.r, Theme.bgDeep.g, Theme.bgDeep.b, 0.66)

                    // 45° accent hatch at 10% (mock: 5px stripe, 5px gap) -- the same
                    // "editing object, not content" mark the redact layer uses.
                    Canvas {
                        id: hatch
                        anchors.fill: parent
                        onPaint: {
                            var ctx = getContext("2d")
                            ctx.clearRect(0, 0, width, height)
                            ctx.strokeStyle = Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.10)
                            ctx.lineWidth = 5
                            // 45°: step 10px * sqrt(2) along x keeps 5px on / 5px off.
                            for (var sx = -height; sx < width + height; sx += 14.14) {
                                ctx.beginPath()
                                ctx.moveTo(sx, height)
                                ctx.lineTo(sx + height, 0)
                                ctx.stroke()
                            }
                        }
                        onWidthChanged: requestPaint()
                        onHeightChanged: requestPaint()
                    }
                    MouseArea {
                        anchors.fill: parent
                        acceptedButtons: Qt.RightButton
                        onClicked: root.restoreCut(index)   // pre-seam behaviour, kept
                    }
                }
                // 3px accent edge handles; dragging either edge retimes the cut.
                Repeater {
                    model: expanded ? 2 : 0
                    Item {
                        readonly property bool leftEdge: index === 0
                        x: (leftEdge ? gx0 : gx1) - 6
                        y: 0
                        width: 12
                        height: overlays.height
                        Rectangle {
                            x: 4.5
                            width: 3
                            height: parent.height
                            radius: 2
                            color: Theme.accent
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.SizeHorCursor
                            onPressed: {
                                root.dragStart = c0
                                root.dragEnd = c1
                            }
                            onPositionChanged: function (m) {
                                if (!pressed)
                                    return
                                var f = root.xToFrame(mapToItem(overlays, m.x, 0).x)
                                if (leftEdge)
                                    root.dragStart = Math.max(0, Math.min(f, root.dragEnd - 1))
                                else
                                    root.dragEnd = Math.min(root.sourceFrames, Math.max(f, root.dragStart + 1))
                            }
                            onReleased: root.commitRetime(root.expandedCutIndex, root.dragStart, root.dragEnd)
                        }
                    }
                }
            }
        }

        // Pending selection (spec §2c state 1): the frames it will remove dim under a
        // bgDeep scrim with 1.5px accent edges -- a preview of the fold, not a wash.
        Rectangle {
            visible: root.hasSelection
            x: root.frameToX(root.selStart)
            y: 0
            width: Math.max(3, root.frameToX(root.selEnd) - root.frameToX(root.selStart))
            height: overlays.height
            color: Qt.rgba(Theme.bgDeep.r, Theme.bgDeep.g, Theme.bgDeep.b, 0.72)
            Rectangle { x: 0; width: 1.5; height: parent.height; color: Theme.accent }
            Rectangle { x: parent.width - 1.5; width: 1.5; height: parent.height; color: Theme.accent }
        }
        // The accent cap on the ruler carrying the selection's duration.
        Rectangle {
            visible: root.hasSelection
            x: root.frameToX(root.selStart)
            y: -ruler.height - 2
            width: Math.max(3, root.frameToX(root.selEnd) - root.frameToX(root.selStart))
            height: 16
            topLeftRadius: 4
            topRightRadius: 4
            bottomLeftRadius: 0
            bottomRightRadius: 0
            color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.2)
            // Not clipped: a narrow selection must still show its duration ("shows the
            // resulting duration", spec §2c), so the label may spill past the cap.
            Text {
                anchors.centerIn: parent
                text: root.cutSecs(root.selEnd - root.selStart)
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
            }
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
