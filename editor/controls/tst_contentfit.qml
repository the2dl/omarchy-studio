// Two cards in the layer list were sized by constants, and both outgrew them the
// moment their content changed: the drop panel's `height: 86` was chosen for a glyph
// and two lines, then a third line was added; the add menu's `width: 150` was chosen
// before its captions row started reporting WHY captions are unavailable, which is
// text decided at runtime.
//
// This pins the rule rather than the numbers: a card sized from its content contains
// it, and still does when the content grows.
//
//   /usr/lib/qt6/bin/qmltestrunner -input editor/controls/tst_contentfit.qml
import QtQuick
import QtTest

Item {
    id: harness
    width: 400
    height: 400

    // -- the drop panel's shape ------------------------------------------
    Rectangle {
        id: card
        width: 220
        height: col.implicitHeight + 2 * 15
        Column {
            id: col
            anchors.centerIn: parent
            spacing: 7
            Text { id: l1; text: "" ; font.pixelSize: 17 }
            Text { id: l2; text: "Drop an image"; font.pixelSize: 11 }
            Text { id: l3; text: "or click to browse"; font.pixelSize: 10 }
            Text { id: l4; text: "lands at the playhead"; font.pixelSize: 10 }
        }
    }

    // -- the add menu's shape --------------------------------------------
    Column {
        id: menu
        y: 200
        property var items: [
            { label: "Image…" }, { label: "Text" }, { label: "Shape" },
            { label: "Redact" }, { label: "Captions — transcribe first" }
        ]
        Repeater {
            model: menu.items
            Rectangle {
                required property var modelData
                width: Math.max(sizer.implicitWidth + 20, 140)
                height: 28
                Text { x: 10; text: parent.modelData.label; font.pixelSize: 12 }
            }
        }
    }

    // Hidden, and OUTSIDE the menu it measures: a sizer inside the column would be a
    // child whose width the column measures. Replaces a single TextMetrics reassigned
    // in a loop, which Qt reported as a binding loop -- the value came out right and
    // the warning was real.
    Column {
        id: sizer
        visible: false
        Repeater {
            model: menu.items
            Text {
                required property var modelData
                text: modelData.label
                font.pixelSize: 12
            }
        }
    }

    TestCase {
        name: "ContentFit"
        when: windowShown

        function test_the_drop_card_contains_all_four_lines() {
            verify(col.implicitHeight > 0)
            verify(card.height >= col.implicitHeight + 20,
                   "the card is shorter than its content plus breathing room")
            // Every line sits inside the card, top and bottom.
            var lines = [l1, l2, l3, l4]
            for (var i = 0; i < lines.length; ++i) {
                var top = lines[i].mapToItem(card, 0, 0).y
                verify(top >= 0, "line " + i + " is clipped off the top")
                verify(top + lines[i].height <= card.height,
                       "line " + i + " is clipped off the bottom")
            }
        }

        function test_the_card_grows_when_a_line_is_added() {
            var before = card.height
            l4.text = "lands at the playhead\nand a second line"
            tryVerify(function () { return card.height > before }, 2000,
                      "the card did not grow with its content -- it is not content-sized")
            l4.text = "lands at the playhead"
        }

        function test_the_longest_menu_label_fits_its_row() {
            var longest = sizer.implicitWidth
            var row = menu.children[0]
            verify(row.width >= longest + 20,
                   "the longest label runs off the row: " + row.width + " < " + longest)
        }

        function test_the_menu_widens_for_a_longer_label() {
            var before = menu.width
            menu.items = [{ label: "Captions — no transcription engine installed at all" }]
            tryVerify(function () { return menu.width > before }, 2000,
                      "the menu did not widen for a longer label")
        }
    }
}
