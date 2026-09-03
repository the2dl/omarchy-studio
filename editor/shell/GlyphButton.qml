// A bare glyph action for the top bar -- the mock's undo/redo (1d: 17px glyphs, no
// plate at rest). Disabled DIMS to text6 instead of vanishing: the mock renders redo
// at #55514d when there is nothing to redo, and a control that disappears would shift
// everything beside it.
import QtQuick
import ".."

Item {
    id: root
    property string glyph: ""
    // Marks a toggle that is currently ON -- the teleprompter's play and edit buttons
    // are states, not actions. Same accent-fill rule as controls/RailButton.qml: an
    // accent FILL is reserved for "this is on", never for "this is clickable".
    property bool accented: false
    property string tip: ""
    // No `property bool enabled`: Item already declares it and redeclaring shadows a
    // FINAL property, which fails the whole QML load (see controls/GhostButton.qml).
    signal clicked()

    implicitWidth: 28
    implicitHeight: 28

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusRow
        color: root.accented ? Theme.accent
             : root.enabled && ma.containsMouse ? Theme.fillHover : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }
    Text {
        anchors.centerIn: parent
        text: root.glyph
        color: root.accented ? Theme.accentOn
             : !root.enabled ? Theme.text6
             : ma.containsMouse ? Theme.text2 : Theme.text3
        font.family: Theme.fontFamily
        font.pixelSize: 17
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }
    MouseArea {
        id: ma
        anchors.fill: parent
        hoverEnabled: true
        enabled: root.enabled
        cursorShape: root.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: root.clicked()
    }
}
