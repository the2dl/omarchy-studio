// editor/LabelledSlider.qml, re-declared for this directory.
//
// Needed because editor/'s qmldir names only the three singletons: an explicit ".."
// import exposes exactly what that qmldir lists, so LabelledSlider is reachable from
// main.qml's implicit import but not from here -- and editor/qmldir belongs to another
// seam. Same rules as the original: the model value is pushed, never bound, and only
// re-pushed while idle so a state refresh cannot yank the thumb mid-drag.
import QtQuick
import "../controls" as C

C.Slider {
    id: root

    property string label: ""
    property string display: ""
    property real modelValue: 0
    readonly property alias liveValue: root.value

    caption: label
    valueText: display

    onModelValueChanged: if (!dragging) value = modelValue
    Component.onCompleted: value = modelValue
}
