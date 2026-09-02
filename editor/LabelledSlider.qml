// A controls/Slider that shows the model's value and only writes back on release.
//
// The value is pushed into the control rather than bound to it: the slider assigns to
// its own `value` while dragging, which would silently destroy a binding to the model
// and leave the control showing a number the project no longer has. Committing on
// release also keeps a drag from posting an intent per pixel -- the zoom amount rebuilds
// the whole zoom track, so that is real work.
import QtQuick
import "controls" as C

C.Slider {
    id: root

    property string label: ""
    property string display: ""
    property real modelValue: 0
    readonly property alias liveValue: root.value

    caption: label
    valueText: display

    // Re-push only while idle; a state refresh mid-drag must not yank the thumb.
    onModelValueChanged: if (!dragging) value = modelValue
    Component.onCompleted: value = modelValue
}
