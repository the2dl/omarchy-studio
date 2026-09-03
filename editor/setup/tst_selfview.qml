// Picking the right camera for the self-view.
//
//   /usr/lib/qt6/bin/qmltestrunner -input editor/setup/tst_selfview.qml
//
// THE BUG THIS EXISTS FOR. `QCameraDevice.id` is a QByteArray, and QML hands that over
// as an OBJECT: `typeof id` is "object", so `id === "/dev/video2"` is false for every
// device, including the right one. The match therefore never succeeded and the preview
// fell through to defaultVideoInput every time -- the setup bar showed the camera you
// had picked, the self-view showed the built-in one, and toggling between them changed
// nothing at all, because the choice was never being consulted.
//
// The stub below reproduces that: `id` is an object with a toString, exactly like the
// QByteArray, so a test written against plain JS strings would pass while the real
// thing failed. That is the whole point of it.
import QtQuick
import QtTest
import "." as S

Item {
    // Mimics QCameraDevice: an id that stringifies but is not a string.
    function qbytearray(text) {
        return { toString: function () { return text } }
    }

    property var stubDevices: ({
        videoInputs: [
            { id: qbytearray("/dev/video0"), description: "Studio Display" },
            { id: qbytearray("/dev/video2"), description: "ZV-E10M2" }
        ],
        defaultVideoInput: { id: qbytearray("/dev/video0"), description: "Studio Display" }
    })

    S.SelfView {
        id: view
        app: fakeApp
        width: 200
        height: 200
    }

    QtObject {
        id: fakeApp
        property var cameraModes: ["off", "circle", "rounded", "rect"]
        property int cameraMode: 1
        property bool counting: false
        property bool picking: false
        property var sources: ({ cameras: [{ device: "/dev/video2", name: "ZV-E10M2" }] })
        property var cameraEntry: ({ device: "/dev/video2", name: "ZV-E10M2" })
        property var cameraRect: ({})
    }

    TestCase {
        name: "SelfView"

        function test_the_picked_camera_is_the_one_used() {
            var d = view.deviceFor(stubDevices)
            compare(String(d.description), "ZV-E10M2",
                    "the self-view must use the camera the bar is showing")
        }

        function test_a_bare_strict_compare_would_have_failed() {
            // Guards the guard: if this ever starts passing, the stub has stopped
            // reproducing QByteArray semantics and the test above proves nothing.
            var id = stubDevices.videoInputs[1].id
            verify(!(id === "/dev/video2"), "the stub must not be a plain string")
            verify(String(id) === "/dev/video2")
        }

        function test_switching_the_pick_switches_the_camera() {
            fakeApp.cameraEntry = { device: "/dev/video0", name: "Studio Display" }
            compare(String(view.deviceFor(stubDevices).description), "Studio Display")
            fakeApp.cameraEntry = { device: "/dev/video2", name: "ZV-E10M2" }
            compare(String(view.deviceFor(stubDevices).description), "ZV-E10M2")
        }

        function test_an_unknown_device_falls_back_to_the_default() {
            // A camera that was unplugged between the pick and the preview: showing the
            // default beats showing nothing, as long as the match above actually works.
            fakeApp.cameraEntry = { device: "/dev/video9", name: "gone" }
            compare(String(view.deviceFor(stubDevices).description), "Studio Display")
            fakeApp.cameraEntry = { device: "/dev/video2", name: "ZV-E10M2" }
        }
    }
}
