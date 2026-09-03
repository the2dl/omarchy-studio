// The camera/screen time conversion, and above all its SIGN.
//
//   /usr/lib/qt6/bin/qmltestrunner -input editor/tst_camerasync.qml
//
// This exists because the sign was wrong in the preview while being right in the export:
// the bubble ran a full second and a bit ahead of the audio in the editor, and the
// exported file was fine. The numbers below are from a real recording -- screen anchor
// 83899411946, camera anchor 83900002742, so the camera started 590.8 ms late, which the
// bridge rounds to 18 frames at 30 fps = 600 ms.
import QtQuick
import QtTest
import "." as E

Item {
    E.CameraSync {
        id: sync
        offsetMs: 600
    }

    TestCase {
        name: "CameraSync"

        function test_the_camera_is_behind_the_screen_not_ahead() {
            // The whole bug in one assertion. A camera that started LATER has less of
            // itself recorded so far, so at any moment its file is EARLIER than the
            // screen's -- never later.
            verify(sync.cameraMsFor(5000) < 5000)
            compare(sync.cameraMsFor(5000), 4400)
        }

        function test_camera_frame_zero_is_the_screen_at_the_offset() {
            // The definition of the anchor difference, restated as a round trip.
            compare(sync.cameraMsFor(600), 0)
        }

        function test_before_the_camera_woke_up_it_holds_at_zero() {
            // The export pads the head with a clone of frame 0 over this span, so the
            // preview showing frame 0 matches what gets rendered.
            compare(sync.cameraMsFor(0), 0)
            compare(sync.cameraMsFor(300), 0)
            verify(!sync.cameraExistsAt(300))
            verify(sync.cameraExistsAt(600))
        }

        function test_a_camera_that_started_first_runs_ahead() {
            // The offset is signed and both directions happen: launch order plus
            // per-pipeline warm-up is not a fixed property of the machine.
            sync.offsetMs = -200
            compare(sync.cameraMsFor(5000), 5200)
            verify(sync.cameraExistsAt(0))
            sync.offsetMs = 600
        }

        function test_the_warm_up_is_skipped_like_the_export_skips_it() {
            // The camera's first frames are its sensor waking up from black -- measured
            // 0 -> 29 -> 75 -> 101, settling at ~107 by frame 12. The export trims them
            // and holds the first settled frame across the head; the preview must land
            // on that same frame or the editor shows a black bubble the file does not.
            sync.warmupMs = 300
            compare(sync.cameraMsFor(0), 300)          // head: the first settled frame
            compare(sync.cameraMsFor(600), 300)        // camera "frame 0" is still warm-up
            compare(sync.cameraMsFor(900), 300)        // exactly at the settled frame
            compare(sync.cameraMsFor(1600), 1000)      // past it, ordinary arithmetic
            sync.warmupMs = 0
        }

        function test_the_camera_is_not_considered_live_until_it_has_settled() {
            sync.warmupMs = 300
            verify(!sync.cameraExistsAt(600))          // exists, but still black
            verify(sync.cameraExistsAt(900))
            sync.warmupMs = 0
        }

        function test_no_warm_up_leaves_the_arithmetic_untouched() {
            // The normal case, and the one that must not regress: a camera that starts
            // clean behaves exactly as before this was added.
            compare(sync.warmupMs, 0)
            compare(sync.cameraMsFor(5000), 4400)
            compare(sync.cameraMsFor(0), 0)
        }

        function test_no_offset_is_the_identity() {
            sync.offsetMs = 0
            compare(sync.cameraMsFor(1234), 1234)
            sync.offsetMs = 600
        }
    }
}
