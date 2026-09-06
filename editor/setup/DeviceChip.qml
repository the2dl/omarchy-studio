// A chip that names the currently picked device and opens its list. The list is NOT
// drawn here: the bar is a 70px window sized to its own row, so anything taller is
// clipped away by the compositor. The overlay is a monitor-sized sheet and already
// draws the right-click window list, so it draws these too -- this chip only reports
// where it sits so the panel can be anchored under it.
import QtQuick
import ".."

Item {
    id: root

    property string label: ""
    property bool open: false
    signal toggled()

    implicitWidth: Math.min(Style.dp(190), row.implicitWidth + Style.dp(20))
    implicitHeight: Style.dp(27)

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusChip
        color: root.open ? Theme.fillHover
               : ma.containsMouse ? Theme.fillSubtle : "transparent"
        border.width: 1
        border.color: root.open ? Theme.accent : Theme.hairline
        Behavior on color { ColorAnimation { duration: Theme.durFast } }

        Row {
            id: row
            anchors.centerIn: parent
            spacing: Style.dp(6)

            Text {
                width: Math.min(implicitWidth, root.width - Style.dp(32))
                elide: Text.ElideRight
                text: root.label
                color: root.enabled ? Theme.text2 : Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Style.dp(Theme.fsRow)
            }
            Text {           // nf-fa-angle_up: the list opens upward, off this bar
                text: ""
                color: Theme.text4
                font.family: Theme.fontFamily
                font.pixelSize: Style.dp(Theme.fsRow)
            }
        }
    }

    MouseArea {
        id: ma
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.toggled()
    }
}
