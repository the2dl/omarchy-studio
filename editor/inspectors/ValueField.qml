// One inline value chip: tiny prefix label + editable value on a fillSubtle plate.
// Mock 2a/2b, verbatim: padding 8px 10px, radius 9, label 10px text6, value 12px text2.
//
// The value is pushed, not bound: a TextInput assigns to its own `text` on every
// keystroke, so a binding to the model would be destroyed by the first edit. External
// state refreshes re-push only while the field is not focused, for the same reason a
// LabelledSlider does not yank the thumb mid-drag.
import QtQuick
import ".."

Rectangle {
    id: root

    property string label: ""
    property string value: ""
    property bool editable: true
    signal committed(string text)

    implicitHeight: 30
    radius: 9
    color: Theme.fillSubtle
    opacity: editable ? 1.0 : 0.45

    onValueChanged: if (!input.activeFocus) input.text = value

    Text {
        id: prefix
        x: 10
        anchors.verticalCenter: parent.verticalCenter
        text: root.label
        color: Theme.text6
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fsHint
    }

    TextInput {
        id: input
        x: prefix.x + prefix.width + 7
        width: parent.width - x - 8
        anchors.verticalCenter: parent.verticalCenter
        text: root.value
        color: Theme.text2
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fsRow
        selectionColor: Theme.accentWash
        selectedTextColor: Theme.text
        enabled: root.editable
        clip: true
        // The accent caret from the mock's text inspector ("Start here|").
        cursorDelegate: Rectangle {
            width: 1.5
            color: Theme.accent
            visible: input.activeFocus
        }
        onEditingFinished: {
            focus = false
            if (text !== root.value)
                root.committed(text)
            // Snap back to the model either way: if the commit is accepted the reply
            // re-pushes the new value, and if it is refused (or unparseable) the field
            // must not keep showing an input the project never took.
            text = root.value
        }
        Keys.onEscapePressed: {
            text = root.value
            focus = false
        }
    }
}
