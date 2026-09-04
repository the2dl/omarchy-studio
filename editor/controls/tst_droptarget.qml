// The layer list's drop panel takes BOTH a drag and a click: dragging a file between
// windows is awkward on a tiling compositor, so the panel that says "drop an image"
// also opens the file chooser when clicked.
//
// This pins the one assumption that arrangement rests on -- that a DropArea stacked
// OVER a MouseArea does not swallow the click. DropArea is declared last so it wins
// for drags; if it also consumed mouse events, the click half would be silently dead
// and nothing about the file would look wrong.
//
//   /usr/lib/qt6/bin/qmltestrunner -input editor/controls/tst_droptarget.qml
import QtQuick
import QtTest

Item {
    id: harness
    width: 300
    height: 200

    property int clicks: 0

    Rectangle {
        id: panel
        anchors.centerIn: parent
        width: 200
        height: 86
        color: "#202020"

        // Same order as LayerList: MouseArea first, DropArea over it.
        MouseArea {
            id: ma
            anchors.fill: parent
            hoverEnabled: true
            onClicked: harness.clicks++
        }
        DropArea {
            id: da
            anchors.fill: parent
        }
    }

    TestCase {
        name: "DropTarget"
        when: windowShown

        function test_a_click_reaches_the_mouse_area_under_the_drop_area() {
            harness.clicks = 0
            mouseClick(panel, panel.width / 2, panel.height / 2)
            compare(harness.clicks, 1,
                    "the DropArea swallowed the click, so 'click to browse' is dead")
        }

        function test_hover_still_reaches_it() {
            // The panel swaps its label on hover, so containsMouse has to work through
            // the DropArea as well.
            mouseMove(panel, panel.width / 2, panel.height / 2)
            tryVerify(function () { return ma.containsMouse }, 2000,
                      "hover never reached the MouseArea, so the label cannot change")
        }
    }
}
