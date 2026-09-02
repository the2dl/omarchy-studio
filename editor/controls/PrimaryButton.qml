// accent fill, accentOn label. Flat.
//
// The spec asks for `0 6px 18px rgba(accent, 0.22)` under this button, and it is dropped
// on the user's call: at any radius it reads as fuzz rather than depth, and the accent
// fill is already the loudest thing on the screen, so it does not need help being found.
// Everything else in the chrome is a flat fill and this now matches.
import QtQuick
import ".."

Item {
    id: root
    property string text: ""
    property bool enabled: true
    signal clicked()

    implicitWidth: label.implicitWidth + 26
    implicitHeight: 30
    opacity: enabled ? 1.0 : 0.4

    Rectangle {
        id: fill
        anchors.fill: parent
        radius: Theme.radiusRow
        color: ma.containsMouse && root.enabled ? Qt.lighter(Theme.accent, 1.08) : Theme.accent
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }

    Text {
        id: label
        anchors.centerIn: parent
        text: root.text
        color: Theme.accentOn
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fsRow
        font.weight: Font.Medium
    }

    MouseArea {
        id: ma
        anchors.fill: parent
        hoverEnabled: true
        enabled: root.enabled
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
