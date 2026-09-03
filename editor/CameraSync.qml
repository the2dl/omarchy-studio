// Where the camera file is, for a given moment of the screen file.
//
// A component rather than one expression inside Preview.qml because this is a SIGN, and
// a sign is the kind of thing that is either right or catastrophically wrong with
// nothing in between. It was wrong: the preview ADDED the offset, so the camera ran
// ahead of the screen by twice it -- 1.2 s on a 0.6 s offset -- and the lips in the
// bubble moved more than a second before the audio that goes with them. The export was
// always right, which is the worst version of this bug: what you check in the editor
// disagrees with what you ship, and only the editor is wrong.
//
// THE CONVENTION, stated once. `offsetMs` is camera anchor minus screen anchor, so a
// POSITIVE offset means the camera started LATER. Camera file time 0 is therefore the
// same instant as screen file time `offsetMs`:
//
//     screen_anchor + screenMs  ==  camera_anchor + cameraMs
//     cameraMs = screenMs - (camera_anchor - screen_anchor) = screenMs - offsetMs
//
// The camera file is always BEHIND the screen clock by the offset, never ahead. The
// export says the same thing the other way round -- render._align_camera pads the
// camera's head by `offset` frames to push its content later on a shared timeline --
// and the two must never drift apart again.
import QtQuick

QtObject {
    id: root

    // Signed, read per recording: the gap between two files is launch order plus
    // per-pipeline warm-up (KMS 128-137 ms, V4L2 210-228 ms), not a constant.
    property real offsetMs: 0

    // The camera's auto-exposure ramp at the head of its own file, in camera ms. Those
    // frames are black-to-bright rubbish and the export drops them.
    property real warmupMs: 0

    function cameraMsFor(screenMs) {
        // Clamped at the warm-up, not at zero. Below it there are two reasons not to
        // seek: for the first `offsetMs` the camera does not exist yet and a negative
        // seek is refused or wraps, and for `warmupMs` after that the camera is a sensor
        // waking up from black. The export holds the first SETTLED frame across both
        // spans (trim then tpad start_mode=clone), so this must land on the same frame
        // -- otherwise the editor shows a black bubble the exported file does not have.
        return Math.max(root.warmupMs, screenMs - root.offsetMs)
    }

    // Whether the camera has started at all at this point on the screen's clock. The
    // preview can use it to leave the bubble out rather than show a frozen first frame.
    function cameraExistsAt(screenMs) {
        return screenMs >= root.offsetMs + root.warmupMs
    }
}
