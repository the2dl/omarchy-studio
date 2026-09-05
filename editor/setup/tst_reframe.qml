// Whether the re-frame option is on screen when someone would look for it.
//
//   /usr/lib/qt6/bin/qmltestrunner -input editor/setup/tst_reframe.qml
//
// THE BUG THIS EXISTS FOR. The control was gated on `app.mode !== 0 && app.sel !== null`,
// and setMode(1) sets `sel = null` -- so it was invisible for the entire act of
// choosing a window, which is exactly when the choice matters. The user hit their
// hotkey, clicked Window, and reported seeing nothing, three times.
//
// It survived review because the SELFTEST auto-picks a window (main.qml: "Selftests
// cannot hover or click; give them the first window so the picked state is
// photographable"). Every screenshot therefore had a selection the real flow does not,
// and the screenshots were how it was checked. A test that fakes the state the bug
// depends on cannot see the bug -- hence this one drives `sel` itself.
import QtQuick
import QtTest
import "." as S

Item {
    width: 1200; height: 80

    S.SetupBar {
        id: bar
        app: fakeApp
    }

    QtObject {
        id: fakeApp
        property int mode: 1
        property var sel: null
        property bool fullMonitor: false
        property bool windowOnly: false
        readonly property bool audioAvailable: !(mode === 1 && windowOnly)
        property bool micOn: false
        property bool desktopAudio: false
        property bool prompterOn: false
        property int cameraMode: 0
        property var cameraModes: ["off", "circle", "rounded", "rect"]
        property var micEntry: null
        property var cameraEntry: null
        property var sources: ({ cameras: [], monitors: [], windows: [], mic: null, mics: [] })
        property string pickerOpen: ""
        property real micDb: -60
        property bool counting: false
        property bool prompterAvailable: true
        function togglePicker() {}
        function setPrompter() {}
    }

    TestCase {
        name: "ReframeOption"
        when: windowShown

        // Inside the TestCase: findChild is one of its methods, not a global. Defined
        // on the root Item it resolves to nothing and every test dies the same way.
        function control() {
            return findChild(bar, "ctl:reframe-toggle")
        }

        function test_it_is_visible_while_choosing_a_window() {
            // The reported state: Window mode, nothing picked yet.
            fakeApp.mode = 1
            fakeApp.sel = null
            var c = control()
            verify(c !== null, "the re-frame control should exist in the bar")
            verify(c.visible, "it must be visible before a window is picked")
        }

        function test_it_is_visible_while_choosing_an_area() {
            fakeApp.mode = 2
            fakeApp.sel = null
            verify(control().visible)
        }

        function test_it_stays_visible_once_something_is_picked() {
            fakeApp.mode = 1
            fakeApp.sel = { kind: "window", address: "0xabc" }
            verify(control().visible)
        }

        function test_a_display_capture_is_not_offered_the_choice() {
            // Already the whole display; the choice would be between a thing and itself.
            fakeApp.mode = 0
            fakeApp.sel = { kind: "monitor" }
            verify(!control().visible)
        }

        function test_its_hit_area_does_not_spill_onto_its_neighbour() {
            // Shipped as a bug: a MouseArea with anchors.fill inside a Row is a child
            // the Row also positions, so the anchor and the layout fight and the hit
            // area covers the control beside it -- the Script toggle stopped being
            // clickable and the checkbox visibly collided with it.
            fakeApp.mode = 1
            var c = control()
            verify(c.width > 0, "the hit area must have a width of its own")
            verify(c.width <= bar.width, "and it must not exceed the bar")
            // The row it belongs to is the bound it may cover, nothing wider.
            var row = c.parent
            compare(c.width, row.width, "hit area wider than its control spills over")
            compare(c.height, row.height)
        }

        function test_the_two_answers_are_exclusive() {
            // Re-frame needs surrounding pixels; isolating means the stream IS the
            // window and there are none. Both true at once describes nothing.
            fakeApp.mode = 1
            fakeApp.windowOnly = false
            fakeApp.fullMonitor = false
            var only = findChild(bar, "ctl:windowonly-toggle")
            verify(only !== null, "the isolate control should exist for a window pick")
            mouseClick(only)
            compare(fakeApp.windowOnly, true)
            compare(fakeApp.fullMonitor, false)
            verify(!control().visible, "re-frame is meaningless once the stream is the window")
        }

        function test_audio_is_disabled_where_it_cannot_be_recorded() {
            // The single-window recorder is video-only. Offering a mic switch that
            // silently records nothing is the kind of thing found in the editor,
            // after the take, when it cannot be redone.
            fakeApp.mode = 1
            fakeApp.windowOnly = true
            verify(!findChild(bar, "ctl:mic-toggle").enabled)
            verify(!findChild(bar, "ctl:audio-toggle").enabled)
            fakeApp.windowOnly = false
            verify(findChild(bar, "ctl:audio-toggle").enabled,
                   "system audio comes back when the mode can record it")
        }

        function test_isolating_is_not_offered_for_an_area() {
            fakeApp.mode = 2
            verify(!findChild(bar, "ctl:windowonly-toggle").visible,
                   "an area has no toplevel to export")
        }

        function test_clicking_it_flips_the_answer() {
            fakeApp.windowOnly = false
            fakeApp.mode = 1
            fakeApp.fullMonitor = false
            mouseClick(control())
            compare(fakeApp.fullMonitor, true)
            mouseClick(control())
            compare(fakeApp.fullMonitor, false)
        }
    }
}
