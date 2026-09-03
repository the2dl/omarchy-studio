// A GhostButton that can be ON: the top bar's Preview control (mock 1d). The lit state
// is fillHover + text2 -- the same register as Segmented's active chip, because in this
// design state is a FILL and rings are reserved for a selected object (spec §1).
//
// `dim` is spec §2e verbatim: while the proxy builds, Preview renders text-6 with no
// fill. Still clickable -- the shell is real in that state, only playback is not.
import QtQuick
import ".."

Item {
    id: root
    property string text: ""
    property bool active: false
    property bool dim: false
    signal clicked()

    implicitWidth: label.implicitWidth + 20
    implicitHeight: 28

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusRow
        color: !root.dim && (root.active || ma.containsMouse) ? Theme.fillHover : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }
    Text {
        id: label
        anchors.centerIn: parent
        text: root.text
        color: root.dim ? Theme.text6
             : root.active || ma.containsMouse ? Theme.text2 : Theme.text3
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fsRow
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
