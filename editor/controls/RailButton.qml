// One glyph in the 56px left rail. Active gets an accent tile with an accentOn glyph --
// the only place in the chrome where an accent FILL marks state rather than an action.
import QtQuick
import ".."

Item {
    id: root
    property string glyph: ""
    property bool active: false
    property string tip: ""
    signal clicked()

    implicitWidth: 38
    implicitHeight: 38

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusRow
        color: root.active ? Theme.accent : (ma.containsMouse ? Theme.fillHover : "transparent")
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }
    Text {
        anchors.centerIn: parent
        text: root.glyph
        color: root.active ? Theme.accentOn : (ma.containsMouse ? Theme.text2 : Theme.text4)
        font.family: Theme.fontFamily
        font.pixelSize: 19
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }
    MouseArea {
        id: ma
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
