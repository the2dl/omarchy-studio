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
//
// Horizontal zoom (spec §5, and the transport's `− Fit +` per the mock): the rows
// scale about the playhead (Ctrl+scroll: about the cursor) while the 74px gutter and
// its labels stay fixed. Zoom composes with the fold -- see the fold section.
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
    // The shared axis: negative inside the head pad, past sourceFrames in the tail.
    // Reading preview.frame here pinned the playhead to the first recorded frame for
    // the whole of an intro, so the card played and the marker did not move.
    readonly property int frame: preview ? preview.timelineFrame : 0
    readonly property var segments: preview ? preview.zoomSegments : []
    readonly property var cuts: st.cuts || []

    // Layers in LIST order: state.layers arrives z-ascending and the list shows
    // front-most first (spec §2a "list = z-order"), so the timeline reverses it to
    // agree with the list rather than with the paint order.
    readonly property var layerRows: {
        // Camera segments are layers, but they have their own row below -- listing them
        // here as well drew every one of them twice.
        var a = (st.layers || []).filter(function (l) { return l.type !== "webcam" })
        a.reverse()
        return a
    }

    readonly property var camSegments: st.webcam_track ? st.webcam_track.segments : []

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
    // What the exported file will actually be: the kept recording plus both pads.
    // The transport showed editFrames as the total, so a five-second output with an
    // intro read "0:03".
    readonly property int outputTotalFrames: editFrames + headPad + tailPad

    // Every row maps frames through the same track origin/width, so the playhead, the
    // ticks and the blocks can never disagree about where a frame is.
    readonly property real trackX: Style.pad + Style.gutterWidth + 12
    readonly property real trackW: Math.max(1, width - trackX - Style.pad)

    // -- horizontal zoom (spec §5: "Timeline rows scale horizontally with zoom
    // level; gutter labels stay fixed") -------------------------------------
    // zoomX 1 == Fit, the whole recording in trackW; that is also the floor, because
    // a view smaller than the recording shows nothing Fit does not. The cap keeps one
    // frame ~4px wide -- wider than the 2px playhead, so a single frame is a real
    // click target -- instead of allowing infinite magnification.
    property real zoomX: 1
    // How far the view is slid into the zoomed content, in px. 0..contentW-trackW.
    property real panX: 0
    readonly property real contentW: trackW * zoomX
    readonly property real maxZoomX: Math.max(1, sourceFrames * 4 / trackW)
    onTrackWChanged: panX = clampPan(panX)

    function clampPan(p) {
        return Math.max(0, Math.min(p, contentW - trackW))
    }

    // Zoom keeping the frame `anchor` at viewport x `anchorVx`. Reading foldX after
    // the zoomX write sees the rebuilt fold, so the pan lands on the new geometry.
    function setZoomAt(z, anchor, anchorVx) {
        z = Math.max(1, Math.min(z, maxZoomX))
        if (z === zoomX) {
            panX = clampPan(panX)
            return
        }
        zoomX = z
        panX = clampPan(foldX(anchor) - anchorVx)
    }

    // The buttons zoom about the PLAYHEAD: the user zooms in to inspect the thing
    // they are parked on, so that thing must stay put. If the playhead was panned
    // out of view its virtual x is kept anyway and the pan clamp pulls the view
    // back into range.
    function zoomAboutPlayhead(factor) {
        setZoomAt(zoomX * factor, frame, foldX(frame) - panX)
    }

    function fitZoom() {
        zoomX = 1
        panX = 0
    }

    // -- the fold ----------------------------------------------------------
    // Piecewise x mapping: each collapsed cut occupies exactly 16px (the seam, spec
    // §2c state 2) and the kept time -- plus the expanded cut, whose frames return in
    // place -- shares the rest proportionally. Every row draws through frameToX, so
    // the whole tray folds together instead of each row drawing its own gap.
    //
    // Zoom composes with the fold rather than replacing it: the fold is built at the
    // zoomed CONTENT width, and buildFold hands each seam its fixed 16px off the top
    // before scaling what remains -- a seam is a splice, not a duration, so it must
    // not grow with the kept time around it. frameToX then subtracts the pan, so all
    // rows slide together.
    readonly property int seamW: 16
    // The output timeline: pads at the ends, recorded material between them. From the
    // bridge, not re-derived here -- see Preview's note on why this mapping has exactly
    // one implementation.
    readonly property var tl: st.timeline || ({ output_frames: 0, head: 0, tail: 0,
                                                recorded_frames: 0, kept: [] })
    readonly property int headPad: tl.head || 0
    readonly property int tailPad: tl.tail || 0
    readonly property int outputFrames: tl.output_frames || 0

    // TIMELINE FRAMES, an axis that extends past the recording at both ends:
    //
    //   [-headPad, 0)                    the head pad
    //   [0, sourceFrames)                the recording, cuts folded as before
    //   [sourceFrames, +tailPad)         the tail pad
    //
    // Deliberately NOT a switch to output frames. Every row here -- cuts, clicks,
    // layers, camera, selection -- passes source frames to frameToX, and rebasing them
    // all on output time means converting twenty-odd call sites, each a place to be
    // silently wrong. On this axis a source frame is still itself, so those rows are
    // untouched and only the ends are new.
    readonly property var fold: buildFold(cuts, expandedCutIndex, contentW, sourceFrames,
                                          headPad, tailPad)

    function buildFold(cs, xi, w, total, head, tail) {
        var pieces = []
        if (total <= 0)
            return pieces
        head = head || 0
        tail = tail || 0
        var folded = 0, seams = 0
        for (var i = 0; i < cs.length; ++i)
            if (i !== xi) {
                seams++
                folded += cs[i].end - cs[i].start
            }
        // Pads share the axis with the recording, so they share its scale -- a second
        // of intro has to be as wide as a second of video or the ruler lies about
        // where things are.
        var keep = Math.max(1, total - folded + head + tail)
        var scale = Math.max(0, w - seams * seamW) / keep
        var x = 0, f = 0
        if (head > 0) {
            pieces.push({ f0: -head, f1: 0, x0: 0, x1: head * scale, pad: "head" })
            x = head * scale
        }
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
        if (f < total) {
            var lastX = tail > 0 ? x + (total - f) * scale : w
            pieces.push({ f0: f, f1: total, x0: x, x1: lastX })
            x = lastX
        }
        if (tail > 0)
            pieces.push({ f0: total, f1: total + tail, x0: x, x1: w, pad: "tail" })
        if (!pieces.length)
            pieces.push({ f0: 0, f1: total, x0: 0, x1: w })
        return pieces
    }

    // The first and last frame the playhead can reach on this axis.
    readonly property int firstFrame: -headPad
    readonly property int lastFrame: sourceFrames + tailPad

    // Frame -> x in CONTENT space (the zoomed, folded axis, before the pan).
    function foldX(f) {
        var p = fold
        if (!p.length)
            return 0
        // p[0].f0 is -headPad when there is a head pad, so a negative frame lands
        // inside the first piece rather than being clamped to its start.
        if (f <= p[0].f0)
            return p[0].x0
        for (var i = 0; i < p.length; ++i)
            if (f < p[i].f1)
                return p[i].x0 + (f - p[i].f0) / (p[i].f1 - p[i].f0) * (p[i].x1 - p[i].x0)
        return p[p.length - 1].x1
    }

    // Frame -> x in VIEWPORT space (what the rows draw at). At Fit the two agree.
    function frameToX(f) {
        return foldX(f) - panX
    }

    function xToFrame(x) {
        x += panX   // viewport -> content, then invert the fold
        var p = fold
        if (!p.length)
            return 0
        if (x <= p[0].x0)
            return p[0].f0            // -headPad, i.e. the very start of the intro
        for (var i = 0; i < p.length; ++i)
            if (x < p[i].x1 || i === p.length - 1)
                return Math.round(Math.min(p[i].f1, p[i].f0
                       + Math.max(0, x - p[i].x0) / Math.max(0.001, p[i].x1 - p[i].x0)
                         * (p[i].f1 - p[i].f0)))
        return lastFrame
    }

    // Timeline frame -> output frame, for the readouts. Pads and the head trim are
    // part of the output too: without them the counter read a negative time through an
    // intro and was short by the trim everywhere else.
    function outFrame(f) {
        if (f < 0)
            return root.headPad + f                 // inside the head pad
        if (f >= sourceFrames)
            return root.headPad + editFrames + (f - sourceFrames)
        var o = root.headPad + f
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
        // Cuts remove RECORDED material, so a selection is clamped to the recording
        // even though the axis now runs either side of it -- there is nothing in a pad
        // to cut out.
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
                text: " / " + root.shortTime(root.outputTotalFrames)
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

        // Zoom cluster, right of the divider like the mock's `− Fit +` group. Steps
        // of 1.5x: three presses roughly triple the view, small enough that nothing
        // the user was watching leaves the screen between presses.
        Rectangle { width: 1; height: 15; color: Theme.hairline }
        C.GhostButton {   // U+2212 minus sign, same glyph weight as the + below
            objectName: "zoomOut"   // reached by editor/timeline/tst_cuts.qml
            text: "−"
            enabled: root.zoomX > 1
            onClicked: root.zoomAboutPlayhead(1 / 1.5)
        }
        C.GhostButton {
            objectName: "zoomFit"   // reached by editor/timeline/tst_cuts.qml
            text: "Fit"
            enabled: root.zoomX > 1
            onClicked: root.fitZoom()
        }
        C.GhostButton {
            objectName: "zoomIn"    // reached by editor/timeline/tst_cuts.qml
            text: "+"
            enabled: root.zoomX < root.maxZoomX && root.sourceFrames > 0
            onClicked: root.zoomAboutPlayhead(1.5)
        }
    }

    // -- ruler -------------------------------------------------------------
    Item {
        id: ruler
        x: root.trackX
        y: transport.y + transport.height
        width: root.trackW
        height: 18
        clip: true   // zoomed, most ticks live off-screen; they must not spill

        // 6 ticks at Fit (the mock's 0:00..2:30); more as the axis stretches, so the
        // visible stretch keeps roughly the same label density at any zoom.
        readonly property int tickCount: 1 + 5 * Math.max(1, Math.ceil(root.zoomX))

        Repeater {
            model: ruler.tickCount
            Text {
                // Ticks stay at fixed source fractions and are PLACED through the
                // fold, so a collapsed cut shifts them the way it shifts everything.
                readonly property int tickFrame: Math.round(index / (ruler.tickCount - 1)
                                                            * root.sourceFrames)
                // Clamp in CONTENT space so the last label tucks inside the axis end
                // rather than being pinned to the viewport edge while panning.
                x: Math.min(root.contentW - width, root.foldX(tickFrame)) - root.panX
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
        acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
        property int anchorFrame: 0
        property bool selecting: false
        property bool fromRuler: false
        // Middle-drag pans the zoomed view; scrub (left) and mark (right) keep their
        // buttons, so panning never fights the edit gestures.
        property bool panning: false
        property real panGrabX: 0
        property real panGrabPan: 0
        cursorShape: panning ? Qt.ClosedHandCursor : Qt.ArrowCursor
        onPressed: function (m) {
            if (m.button === Qt.MiddleButton) {
                panning = true
                panGrabX = m.x
                panGrabPan = root.panX
                return
            }
            panning = false
            anchorFrame = root.xToFrame(m.x)
            fromRuler = m.y < ruler.height
            selecting = m.button === Qt.RightButton
            if (selecting) {
                root.markAnchor = -1
                root.selStart = anchorFrame
                root.selEnd = anchorFrame
            } else if (!fromRuler && root.preview) {
                root.preview.seekTimelineFrame(anchorFrame)
            }
        }
        onPositionChanged: function (m) {
            if (!pressed)
                return
            if (panning) {
                root.panX = root.clampPan(panGrabPan - (m.x - panGrabX))
                return
            }
            var f = root.xToFrame(m.x)
            if (!selecting && fromRuler && Math.abs(f - anchorFrame) >= 1) {
                selecting = true
                root.markAnchor = -1
            }
            if (selecting) {
                root.selStart = Math.min(anchorFrame, f)
                root.selEnd = Math.max(anchorFrame, f)
            }
            if (!selecting && root.preview) {
                root.preview.seekTimelineFrame(f)
            }
        }
        onReleased: function (m) {
            if (panning) {
                panning = false
                return
            }
            // An unmoved press on the ruler is still a seek.
            if (fromRuler && !selecting && root.preview)
                root.preview.seekTimelineFrame(anchorFrame)
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
                id: screenLane
                width: root.trackW
                height: Style.screenRowH
                radius: Theme.radiusChip
                color: Theme.fillSubtle
                clip: true

                // Cells are laid out across the zoomed CONTENT width (rows scale
                // with zoom, spec §5) but only the viewport's worth is instantiated:
                // at the 4px/frame cap the content runs to tens of thousands of px,
                // and a Rectangle per off-screen cell would be thousands of items.
                readonly property int cellCount: Math.max(1, Math.floor((root.contentW - 6) / 46))
                readonly property real cellW: (root.contentW - 6 - 2 * (cellCount - 1)) / cellCount
                readonly property real pitch: cellW + 2
                readonly property int firstCell: Math.max(0, Math.floor((root.panX - 3) / pitch))
                Repeater {
                    model: Math.max(0, Math.min(screenLane.cellCount - screenLane.firstCell,
                                                Math.ceil(root.trackW / screenLane.pitch) + 1))
                    Rectangle {
                        x: 3 + (screenLane.firstCell + index) * screenLane.pitch - root.panX
                        y: 3
                        width: screenLane.cellW
                        height: Style.screenRowH - 6
                        radius: 4
                        color: Theme.canvasA
                        opacity: 0.5
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
                        clip: true   // zoomed, a bar can run past the viewport
                        readonly property bool sel: root.preview && root.preview.selectedId === modelData.id
                        readonly property bool whole: !modelData.t
                        // A layer in a pad has its range in PAD frames -- read as
                        // source frames it drew over the recording instead, so a title
                        // card appeared to sit on top of the video it precedes.
                        readonly property int padBase:
                            (modelData.pad || "") === "head" ? -root.headPad
                            : (modelData.pad || "") === "tail" ? root.sourceFrames : 0
                        readonly property int f0:
                            modelData.t ? padBase + modelData.t.start : padBase
                        readonly property int f1:
                            modelData.t ? padBase + modelData.t.end
                            : ((modelData.pad || "") === "" ? root.sourceFrames
                                                            : padBase + 1)

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
                clip: true   // zoomed, blocks can run past the viewport

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

        // camera: the segments the head is on for. A track of clips, not a curve --
        // see layers.webcam_segments for why. An empty stretch is the head being off,
        // which is the whole point of the row, so the lane's own background IS the
        // "camera off" state and gets no block drawn over it.
        Row {
            visible: root.camSegments.length > 0 || (root.st.webcam_track ? root.st.webcam_track.explicit : false)
            width: parent.width
            spacing: 12
            C.Caption {
                width: Style.gutterWidth
                text: "camera"
                color: Theme.accent
                anchors.verticalCenter: parent.verticalCenter
                font.pixelSize: Theme.fsHint
            }
            Rectangle {
                id: camLane
                width: root.trackW
                height: Style.zoomRowH
                radius: Theme.radiusChip
                color: Theme.fillSubtle
                clip: true

                // A click on bare lane brings the head BACK for that stretch, which is
                // the other half of the ask ("...and back at the end"). The range runs
                // to the next segment or the end of the recording, so one click is
                // enough and the result can then be trimmed like any other segment.
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: function (mouse) {
                        var f = root.xToFrame(mouse.x)
                        for (var i = 0; i < root.camSegments.length; ++i) {
                            var s = root.camSegments[i]
                            if (f >= s.start && f < s.end)
                                return          // the block's own handler owns this
                        }
                        var end = root.sourceFrames
                        for (var j = 0; j < root.camSegments.length; ++j)
                            if (root.camSegments[j].start > f)
                                end = Math.min(end, root.camSegments[j].start)
                        if (end - f < 2)
                            return
                        Bridge.op("add_webcam_segment", {
                            start_ms: f * root.msPerFrame,
                            end_ms: end * root.msPerFrame
                        })
                    }
                }

                Repeater {
                    model: root.camSegments
                    Rectangle {
                        id: block
                        readonly property bool sel: root.preview && root.preview.selectedId === modelData.id

                        // While a drag is live the block follows the POINTER, not the
                        // model: the bridge round-trip is not instant, and snapping back
                        // to the old range for a frame on every move is the "double
                        // bounce" the layer items already avoid this way. -1 means "no
                        // drag in progress, use the model".
                        property int dragF0: -1
                        property int dragF1: -1
                        readonly property int f0: dragF0 >= 0 ? dragF0 : modelData.start
                        readonly property int f1: dragF1 >= 0 ? dragF1 : modelData.end

                        // A segment cannot cross its neighbours. The model refuses an
                        // overlapping ADD, but a drag has to refuse it continuously --
                        // the edge simply stops -- because a drag that silently reverts
                        // on release reads as the app ignoring you.
                        readonly property int minStart:
                            index > 0 ? root.camSegments[index - 1].end : 0
                        readonly property int maxEnd:
                            index < root.camSegments.length - 1
                            ? root.camSegments[index + 1].start : root.sourceFrames

                        x: root.frameToX(f0)
                        y: 4
                        width: Math.max(6, root.frameToX(f1) - root.frameToX(f0))
                        height: 18
                        radius: Theme.radiusChip - 2
                        color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, sel ? 0.28 : 0.2)
                        border.width: sel ? 1.5 : 1
                        border.color: sel ? Theme.accent
                                          : Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.45)
                        Behavior on color { ColorAnimation { duration: Theme.durSlow } }

                        function commit() {
                            // Cleared in the callback, not here: until the reply lands
                            // the model still holds the old range.
                            Bridge.op("update_layer", {
                                id: modelData.id,
                                start_ms: block.f0 * root.msPerFrame,
                                end_ms: block.f1 * root.msPerFrame
                            }, function () { block.dragF0 = -1; block.dragF1 = -1 })
                        }

                        // Frames are read back through xToFrame rather than from a pixel
                        // delta: the ruler FOLDS cut regions away, so the mapping is
                        // piecewise and a constant px-per-frame is wrong the moment a
                        // cut exists anywhere left of the segment.
                        function frameAt(area, mx) {
                            return root.xToFrame(area.mapToItem(camLane, mx, 0).x)
                        }

                        readonly property int grip: 6

                        // -- body: slide the segment, keeping its length ---------
                        MouseArea {
                            id: bodyMa
                            anchors.fill: parent
                            anchors.leftMargin: block.grip
                            anchors.rightMargin: block.grip
                            cursorShape: pressed ? Qt.ClosedHandCursor : Qt.PointingHandCursor
                            property int grabF: 0
                            property real grabX: 0
                            property int origF0: 0
                            property int origF1: 0
                            property bool dragged: false
                            // In PIXELS, not frames. Keyed off the frame delta, a click
                            // carrying one pixel of hand movement counted as a drag --
                            // zoomed out, one pixel is several frames -- so selecting a
                            // segment nudged it instead, and committed the nudge.
                            readonly property real dragThreshold: 4
                            onPressed: function (mouse) {
                                grabF = block.frameAt(bodyMa, mouse.x)
                                grabX = bodyMa.mapToItem(camLane, mouse.x, 0).x
                                origF0 = block.f0
                                origF1 = block.f1
                                dragged = false
                            }
                            onPositionChanged: function (mouse) {
                                if (!pressed)
                                    return
                                var nowX = bodyMa.mapToItem(camLane, mouse.x, 0).x
                                if (!dragged && Math.abs(nowX - grabX) < dragThreshold)
                                    return
                                var d = block.frameAt(bodyMa, mouse.x) - grabF
                                dragged = true
                                var len = origF1 - origF0
                                var s = Math.max(block.minStart,
                                                 Math.min(origF0 + d, block.maxEnd - len))
                                block.dragF0 = s
                                block.dragF1 = s + len
                            }
                            onReleased: {
                                if (dragged)
                                    block.commit()
                                else
                                    root.preview.selectedId = block.sel ? "" : modelData.id
                            }
                        }

                        // -- edges: trim ------------------------------------------
                        // Written out twice rather than shared through a Component: a
                        // Component reparents to its Loader, so `parent.height` and the
                        // `this` a handler sees both stop meaning what they read as.
                        // Two short handlers beat one clever one nobody can follow.
                        MouseArea {
                            id: headMa
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            width: block.grip
                            cursorShape: Qt.SizeHorCursor
                            onPositionChanged: function (mouse) {
                                if (!pressed)
                                    return
                                // Never past the previous segment, never within two
                                // frames of its own tail.
                                block.dragF0 = Math.max(
                                    block.minStart,
                                    Math.min(block.frameAt(headMa, mouse.x), block.f1 - 2))
                            }
                            onReleased: if (block.dragF0 >= 0)
                                            block.commit()
                        }
                        MouseArea {
                            id: tailMa
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            width: block.grip
                            cursorShape: Qt.SizeHorCursor
                            onPositionChanged: function (mouse) {
                                if (!pressed)
                                    return
                                block.dragF1 = Math.min(
                                    block.maxEnd,
                                    Math.max(block.frameAt(tailMa, mouse.x), block.f0 + 2))
                            }
                            onReleased: if (block.dragF1 >= 0)
                                            block.commit()
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
                clip: true   // zoomed, dots can pan out of view (and must not catch clicks there)

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
                            onClicked: root.preview.seekTimelineFrame(modelData.frame)
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
                id: audioLane
                width: root.trackW
                height: Style.audioRowH
                radius: Theme.radiusChip
                color: Theme.fillSubtle
                clip: true

                // Same windowed layout as the screen cells: bars pitch across the
                // zoomed content on the 6px grid, only the visible ones exist.
                readonly property int barCount: Math.max(1, Math.floor((root.contentW - 10) / 6))
                readonly property int firstBar: Math.max(0, Math.floor((root.panX - 5) / 6))
                Repeater {
                    model: Math.max(0, Math.min(audioLane.barCount - audioLane.firstBar,
                                                Math.ceil(root.trackW / 6) + 2))
                    Rectangle {
                        x: 5 + (audioLane.firstBar + index) * 6 - root.panX
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
    // The outer Item is the overlay VIEWPORT: it clips the cut objects, the selection
    // and the playhead to the track columns, because panned content sliding over the
    // 74px gutter would break "gutter labels stay fixed" (spec §5). 2px of headroom
    // above the ruler keeps the duration chips' top edge, which draws 1px proud of
    // the ruler band.
    Item {
        x: root.trackX
        y: ruler.y - 2
        width: root.trackW
        height: 2 + ruler.height + rows.height
        clip: true

        Item {
            id: overlays
            x: 0
            y: 2 + ruler.height
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

    // -- wheel over the tray -----------------------------------------------
    // A WheelHandler on a top-most Item takes only wheel events: the rows' own
    // MouseAreas (seam clicks, dots, layer bars) keep their clicks, hovers and
    // cursor shapes. Ctrl+scroll zooms about the CURSOR -- the pointer is what the
    // user is aiming at -- where the buttons zoom about the playhead; Shift+scroll
    // and a sideways wheel pan the zoomed view.
    Item {
        x: root.trackX
        y: ruler.y
        width: root.trackW
        height: ruler.height + rows.height

        WheelHandler {
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            onWheel: function (ev) {
                if (ev.modifiers & Qt.ControlModifier) {
                    // 120 units = one notch = x1.25, matching the buttons' feel.
                    var factor = Math.pow(1.25, ev.angleDelta.y / 120)
                    root.setZoomAt(root.zoomX * factor, root.xToFrame(ev.x), ev.x)
                } else if (ev.modifiers & Qt.ShiftModifier) {
                    // Shift+scroll pans; most mice keep the delta on y under shift.
                    root.panX = root.clampPan(root.panX
                                              - (ev.angleDelta.x || ev.angleDelta.y))
                } else if (ev.angleDelta.x !== 0) {
                    // A real horizontal wheel / two-finger sideways scroll.
                    root.panX = root.clampPan(root.panX - ev.angleDelta.x)
                }
            }
        }
    }
}
