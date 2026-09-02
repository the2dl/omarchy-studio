// A label that lights on hover. No fill at rest, no border ever -- in this design a
// border means "selected", so a bordered button would read as a stuck toggle.
import QtQuick
import ".."

Item {
    id: root
    property string text: ""
    // No `property bool enabled` here: Item already declares it, and redeclaring
    // shadows the base property. Qt warns on it and newer builds make it a hard
    // "Cannot override FINAL property" error, which fails the whole QML load with
    // no symptom but a window that never appears. Item.enabled already does what
    // is wanted -- it blocks input and propagates to children.
    signal clicked()

    implicitWidth: label.implicitWidth + 20
    implicitHeight: 28
    opacity: enabled ? 1.0 : 0.4

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusRow
        color: ma.containsMouse ? Theme.fillHover : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }
    Text {
        id: label
        anchors.centerIn: parent
        text: root.text
        color: ma.containsMouse ? Theme.text2 : Theme.text3
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fsRow
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
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
