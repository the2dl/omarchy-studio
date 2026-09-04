// The left rail is glyph-only, so the tooltip IS the label. `tip` was a declared
// property that nothing rendered, and every call site was already passing one.
//
//   /usr/lib/qt6/bin/qmltestrunner -input editor/controls/tst_railbutton.qml
//
// (Qt6's runner by absolute path -- the one on PATH is qt5-declarative's and exits 1
// having printed nothing.)
import QtQuick
import QtTest
import "." as C

Item {
    id: harness
    width: 400
    height: 200

    C.RailButton {
        id: button
        x: 9
        y: 10
        glyph: ""
        tip: "Layers"
    }

    C.RailButton {
        id: untipped
        x: 9
        y: 60
        glyph: ""
        tip: ""
    }

    TestCase {
        name: "RailButton"
        when: windowShown

        // findChild, not a walk over `children`: a ToolTip is a Popup and popups are
        // not in the visual children list, so the obvious walk finds nothing and the
        // test reports "no tooltip" for a tooltip that is right there.
        function findTip(target) {
            return findChild(target, "railTip")
        }

        function test_the_tip_reaches_the_popup() {
            var tip = findTip(button)
            verify(tip !== null, "RailButton draws no tooltip at all")
            compare(tip.text, "Layers")
        }

        function test_hovering_shows_it_and_leaving_hides_it() {
            var tip = findTip(button)
            verify(!tip.visible, "the tooltip is up before anything hovered")
            mouseMove(button, button.width / 2, button.height / 2)
            tryVerify(function () { return tip.visible }, 3000,
                      "hovering the rail button never showed its tooltip")
            // Off the button entirely, not merely to another point on it.
            mouseMove(harness, 300, 180)
            tryVerify(function () { return !tip.visible }, 3000,
                      "the tooltip stayed up after the pointer left")
        }

        function test_a_button_with_no_tip_shows_nothing() {
            // The rail grows as tools do; a tool added without a tip must draw no empty
            // box rather than a box with nothing in it.
            var tip = findTip(untipped)
            mouseMove(untipped, untipped.width / 2, untipped.height / 2)
            wait(700)
            verify(tip === null || !tip.visible)
        }

        function test_it_sits_clear_of_the_rail() {
            // Positioned past the button's own width: the rail is 56px and the label is
            // wider, so a tooltip overlapping its own button would cover the glyph.
            var tip = findTip(button)
            verify(tip.x >= button.width, "the tooltip overlaps the glyph it labels")
        }
    }
}
