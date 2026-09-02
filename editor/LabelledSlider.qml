// A slider that shows the model's value and only writes back on release.
//
// The value is pushed into the control rather than bound to it: Qt's Slider assigns to
// its own `value` while dragging, which would silently destroy a binding to the model
// and leave the control showing a number the project no longer has. Committing on
// release also keeps a drag from posting an intent per pixel -- the zoom amount rebuilds
// the whole zoom track, so that is real work.
import QtQuick
import QtQuick.Controls.Basic

Item {
    id: root

    property string label: ""
    property real from: 0
    property real to: 1
    property real value: 0
    property string display: ""
    readonly property alias liveValue: slider.value

    signal committed(real v)

    implicitHeight: 40

    onValueChanged: if (!slider.pressed) slider.value = value
    Component.onCompleted: slider.value = value

    Text {
        id: caption
        x: 0
        y: 0
        text: root.label
        color: Theme.dim
        font.pixelSize: 12
    }

    Text {
        x: root.width - width
        y: 0
        text: root.display
        color: Theme.foreground
        font.pixelSize: 12
    }

    Slider {
        id: slider
        x: 0
        y: caption.height
        width: root.width
        from: root.from
        to: root.to
        onPressedChanged: if (!pressed) root.committed(value)
    }
}
