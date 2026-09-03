// Interaction tests for the camera overlay's move and resize (spec 1g).
//
// Run:
//   qmltestrunner -input editor/tst_webcam_overlay.qml
//
// No bridge here: the overlay is a pure view -- it reports a rect through `moved` and
// the model side is covered by tests/test_qmlbridge.py. What cannot be checked any
// other way is whether the grip is reachable and whether dragging it changes a size,
// which is exactly what was reported broken.
import QtQuick
import QtTest
import "." as E

Item {
    id: harness
    width: 1280
    height: 720

    property var lastMoved: null

    E.WebcamOverlay {
        id: cam
        cam: ({
            rect: { x: 400, y: 300, width: 200, height: 200 },
            editable: true,
            enabled: true,
            shape: "circle",
            mirror: true,
            radius: 0
        })
        onMoved: function (r) { harness.lastMoved = r }
    }

    TestCase {
        name: "WebcamOverlay"
        when: windowShown

        function init() {
            harness.lastMoved = null
            cam.selected = false
        }

        function test_pressing_the_camera_selects_it() {
            var got = 0
            function count() { got++ }
            cam.clicked.connect(count)
            mousePress(cam, 100, 100)
            mouseRelease(cam, 100, 100)
            cam.clicked.disconnect(count)
            compare(got, 1, "pressing the overlay must emit clicked(), which is what "
                          + "reveals the resize grip")
        }

        function test_dragging_the_body_moves_without_resizing() {
            cam.selected = true
            mousePress(cam, 100, 100)
            mouseMove(cam, 140, 130)
            mouseRelease(cam, 140, 130)
            verify(harness.lastMoved !== null, "a body drag must report a rect")
            compare(harness.lastMoved.width, 200, "moving must not resize")
            compare(harness.lastMoved.height, 200)
        }

        function test_dragging_the_grip_resizes() {
            cam.selected = true
            // The grip is centred on the bottom-right corner, so its centre in overlay
            // coordinates IS the corner.
            var gx = cam.width
            var gy = cam.height
            mousePress(cam, gx, gy)
            mouseMove(cam, gx + 60, gy + 60)
            mouseRelease(cam, gx + 60, gy + 60)
            verify(harness.lastMoved !== null, "a grip drag must report a rect")
            verify(harness.lastMoved.width > 200,
                   "dragging the grip out must make the camera bigger, got "
                   + harness.lastMoved.width)
        }

        function test_the_grip_is_reachable_without_selecting_first() {
            // What made this feel like a missing feature: the grip only appeared after
            // a click that had no visible result, so a user who never happened to click
            // the camera never saw one. Hovering it is enough now, and grabbing the
            // grip selects the camera on the way.
            cam.selected = false
            mouseMove(cam, cam.width - 4, cam.height - 4)
            verify(cam.showHandles, "hovering the camera must reveal its handles")
            var picked = 0
            function count() { picked++ }
            cam.clicked.connect(count)
            var gx = cam.width
            var gy = cam.height
            mousePress(cam, gx, gy)
            mouseMove(cam, gx + 40, gy + 40)
            mouseRelease(cam, gx + 40, gy + 40)
            cam.clicked.disconnect(count)
            verify(harness.lastMoved !== null && harness.lastMoved.width > 200,
                   "the grip must resize without a prior selecting click")
            // `selected` is owned by Preview.qml, which sets it from this signal -- so
            // emitting it is the whole of this component's part in getting selected.
            compare(picked, 1, "grabbing the grip selects the camera")
        }

        function test_handles_are_a_constant_size_on_screen() {
            // Everything here lives inside the stage's fit transform, so a fixed pixel
            // size shrinks with the canvas. A 2526x2768 window recording fits at about a
            // quarter the scale of a 2560x1440 display capture, and the grip came out
            // around 5px -- indistinguishable from the undraggable dot it replaced, and
            // reported as "it fell back to the old tiny little corner thing".
            cam.uiScale = 1
            var atFullScale = cam.gripSize
            cam.uiScale = 4                     // a canvas fitted at 1/4
            compare(cam.gripSize, atFullScale * 4,
                    "the grip must grow in canvas pixels as the canvas shrinks on screen")
            compare(cam.handleSize, 7 * 4)
            compare(cam.ringWidth, 1.5 * 4)
            cam.uiScale = 1
        }

        function test_the_grip_is_still_grabbable_on_a_shrunken_canvas() {
            // The size is only half of it: the hit area has to move with it, or the
            // target is in the right place at the wrong scale.
            cam.uiScale = 3
            cam.selected = true
            harness.lastMoved = null
            mousePress(cam, cam.width, cam.height)
            mouseMove(cam, cam.width + 50, cam.height + 50)
            mouseRelease(cam, cam.width + 50, cam.height + 50)
            verify(harness.lastMoved !== null && harness.lastMoved.width > 200,
                   "the grip must still resize when the canvas is fitted small")
            cam.uiScale = 1
        }

        function test_the_position_is_held_until_the_model_replies() {
            // The "double bounce": releasing the local position when the mouse came up
            // showed one frame at the OLD rect, because the new one is still in flight.
            // One gesture must produce ONE position change.
            cam.selected = true
            mousePress(cam, 100, 100)
            mouseMove(cam, 160, 140)
            var duringX = cam.x
            mouseRelease(cam, 160, 140)
            verify(!cam.dragging, "the drag is over")
            verify(cam.committing, "but the move is still in flight")
            compare(cam.x, duringX,
                    "it must not snap back to the model's stale rect on release")
            cam.commitDone()
            verify(!cam.committing)
            // The model has not actually moved in this harness, so releasing the
            // override now legitimately returns it to rect.x -- one transition, not two.
            compare(cam.x, cam.rect.x)
        }

        function test_a_reply_that_never_comes_does_not_freeze_the_item() {
            // An op that errors never calls back. The override has to lapse on its own
            // or the layer is stuck at a position the project does not have.
            cam.selected = true
            mousePress(cam, 100, 100)
            mouseMove(cam, 150, 150)
            mouseRelease(cam, 150, 150)
            verify(cam.committing)
            tryVerify(function () { return !cam.committing }, 3000)
        }

        function test_handles_stay_hidden_in_preview_mode() {
            cam.selected = true
            cam.previewMode = true
            verify(!cam.showHandles, "preview mode shows content, not furniture")
            cam.previewMode = false
        }
    }
}
