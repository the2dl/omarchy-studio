// Interaction tests for the cut UI (spec §2c), run against a REAL bridge server so the
// Backspace/Restore/retime round trips exercise the same delete_cut/add_cut path the
// app uses -- a mocked bridge would prove nothing about whether the frames come back.
//
// Run (see scratchpad tl/server.py for the server half):
//   .venv/bin/python server.py &            # writes {port, token} to tl/bridge.json
//   qmltestrunner -input editor/timeline/tst_cuts.qml
//
// The harness reads bridge.json with an XHR because qmltestrunner cannot pass argv
// through to Bridge.qml's --port/--token scan.
import QtQuick
import QtTest
import ".." as E

Item {
    id: harness
    width: 1560
    height: tl.implicitHeight

    // Just enough of Preview.qml's surface for Timeline to bind against.
    Item {
        id: previewStub
        property bool hasScreen: false
        property bool playing: false
        property int frame: 0
        property var zoomSegments: []
        property int selectedZoomIndex: -1
        property string selectedId: ""
        function seekFrame(f) { frame = f }
        function togglePlay() { }
        function step(d) { frame += d }
    }

    E.Timeline {
        id: tl
        width: harness.width
        height: implicitHeight
        preview: previewStub
    }

    TestCase {
        id: tc
        name: "cuts"
        when: windowShown

        readonly property string shotDir:
            "/tmp/claude-1000/-home-dan/60c17442-ebb5-4cc3-aa4c-5e583aa2ad8c/scratchpad/tl/"

        function initTestCase() {
            var info = null
            var x = new XMLHttpRequest()
            x.onreadystatechange = function () {
                if (x.readyState === XMLHttpRequest.DONE)
                    info = JSON.parse(x.responseText)
            }
            x.open("GET", "file://" + shotDir + "bridge.json")
            x.send()
            tryVerify(function () { return info !== null }, 5000)
            E.Bridge.port = info.port
            E.Bridge.token = info.token
            E.Bridge.refresh()
            tryVerify(function () { return (E.Bridge.state.source_frames || 0) > 0 }, 5000)
            // Warm-up: on the offscreen platform the very first synthetic click can be
            // swallowed before the window settles, so spend one on dead space.
            mouseClick(tl, 5, tl.implicitHeight - 5)
            wait(50)
        }

        function shoot(name) {
            wait(50)
            grabImage(tl).save(shotDir + name)
        }

        // One frame inside the screen row, at the given track x.
        function rowY() { return 57 + 20 }
        function seamCenterX(cut) { return tl.trackX + tl.frameToX(cut.start) + tl.seamW / 2 }

        function test_00_layer_bar_selection_roundtrips() {
            // The layers row shares selection with the canvas/list through
            // preview.selectedId (spec §2a timeline representation).
            compare(tl.layerRows.length, 3)
            compare(tl.layerRows[0].id, "shape1")   // list order: front-most first
            // blur1 (frames 720-1800) sits in the middle sub-row: y = 57 rows top
            // + 40 screen + 7 gap + 18 lane + 3 gap + 9 = 134.
            var bx = tl.trackX + tl.frameToX(1260)
            mouseClick(tl, bx, 134)
            compare(previewStub.selectedId, "blur1")
            shoot("t_layersel.png")
            mouseClick(tl, bx, 134)   // click again deselects
            compare(previewStub.selectedId, "")
        }

        function test_01_collapsed_seam_expands_on_click() {
            compare(tl.cuts.length, 1)
            verify(tl.expandedCutIndex === -1)
            shoot("t_collapsed.png")
            mouseClick(tl, seamCenterX(tl.cuts[0]), rowY())
            compare(tl.expandedCutIndex, 0)
            shoot("t_expanded.png")
        }

        function test_02_backspace_removes_and_frames_come_back() {
            var c = tl.cuts[0]
            compare(tl.expandedCutIndex, 0)
            keyClick(Qt.Key_Backspace)
            // The round trip: delete_cut runs server-side and the pushed-back state
            // has no cuts -- the fold disappears and editFrames grows back.
            tryVerify(function () { return tl.cuts.length === 0 }, 5000)
            compare(tl.editFrames, tl.sourceFrames)
            // Re-add the same cut through the same op the Cut button uses.
            E.Bridge.op("add_cut", { start_ms: c.start * tl.msPerFrame,
                                     end_ms: c.end * tl.msPerFrame })
            tryVerify(function () { return tl.cuts.length === 1 }, 5000)
            compare(tl.expandedCutIndex, -1)   // back to a seam
        }

        function test_03_ruler_drag_selects_and_C_commits() {
            // Drag on the ruler from 20% to 30% of the track.
            var y = 57 - 9   // inside the 18px ruler band
            mousePress(tl, tl.trackX + tl.trackW * 0.2, y)
            mouseMove(tl, tl.trackX + tl.trackW * 0.25, y)
            mouseMove(tl, tl.trackX + tl.trackW * 0.3, y)
            shoot("t_selecting.png")
            mouseRelease(tl, tl.trackX + tl.trackW * 0.3, y)
            verify(tl.hasSelection)
            var s0 = tl.selStart, s1 = tl.selEnd
            keyClick(Qt.Key_C)   // with a selection pending, C commits it
            tryVerify(function () { return tl.cuts.length === 2 }, 5000)
            verify(!tl.hasSelection)
            // Clean up through the UI: expand the new seam, Backspace it away.
            var idx = -1
            for (var i = 0; i < tl.cuts.length; ++i)
                if (Math.abs(tl.cuts[i].start - s0) <= 1)
                    idx = i
            verify(idx >= 0)
            mouseClick(tl, seamCenterX(tl.cuts[idx]), rowY())
            compare(tl.expandedCutIndex, idx)
            keyClick(Qt.Key_Backspace)
            tryVerify(function () { return tl.cuts.length === 1 }, 5000)
        }

        function test_04_retime_by_edge_drag() {
            mouseClick(tl, seamCenterX(tl.cuts[0]), rowY())
            compare(tl.expandedCutIndex, 0)
            var before = tl.cuts[0].end - tl.cuts[0].start
            var hx = tl.trackX + tl.frameToX(tl.cuts[0].end)
            var y = rowY() + 40
            mousePress(tl, hx, y)
            mouseMove(tl, hx + 30, y)
            mouseMove(tl, hx + 60, y)
            mouseRelease(tl, hx + 60, y)
            // delete_cut + add_cut land server-side; the cut must end later now.
            tryVerify(function () {
                return tl.cuts.length === 1 && tl.cuts[0].end - tl.cuts[0].start > before
            }, 5000)
            compare(tl.expandedCutIndex, 0)   // still open on the retimed range
            shoot("t_retimed.png")
        }

        function test_05_restore_footer() {
            compare(tl.expandedCutIndex, 0)
            var chip = findChild(tl, "cutRestore")
            verify(chip !== null)
            mouseClick(chip, chip.width / 2, chip.height / 2)
            tryVerify(function () { return tl.cuts.length === 0 }, 5000)
            compare(tl.editFrames, tl.sourceFrames)
            shoot("t_restored.png")
        }

        function test_06_escape_collapses() {
            E.Bridge.op("add_cut", { start_ms: 30000, end_ms: 36000 })
            tryVerify(function () { return tl.cuts.length === 1 }, 5000)
            mouseClick(tl, seamCenterX(tl.cuts[0]), rowY())
            compare(tl.expandedCutIndex, 0)
            keyClick(Qt.Key_Escape)
            compare(tl.expandedCutIndex, -1)
            // Leave the bundle's in-memory edit as we found it.
            E.Bridge.op("delete_cut", { index: 0 })
            tryVerify(function () { return tl.cuts.length === 0 }, 5000)
        }
    }
}
