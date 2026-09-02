// Equal-width chips, 4px gap, no borders. The active chip gets fillHover + text2; the
// rest sit at text4. Selection here is a fill, not a ring -- rings are reserved for a
// selected OBJECT (a card, a keyframe), and using one here would overload the signal.

import QtQuick
import ".."

Item {
    id: root

    property var model: []
    property int currentIndex: 0
    property bool enabled: true
    signal activated(int index)

    implicitHeight: 26
    opacity: enabled ? 1.0 : 0.45

    Row {
        anchors.fill: parent
        spacing: 4

        Repeater {
            model: root.model
            delegate: Rectangle {
                required property int index
                required property var modelData

                width: (root.width - 4 * (root.model.length - 1)) / root.model.length
                height: root.height
                radius: Theme.radiusChip
                color: index === root.currentIndex ? Theme.fillHover : "transparent"
                Behavior on color { ColorAnimation { duration: Theme.durFast } }

                Text {
                    anchors.centerIn: parent
                    text: modelData
                    color: index === parent.index ? Theme.text2 : Theme.text4
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                    Behavior on color { ColorAnimation { duration: Theme.durFast } }
                }

                MouseArea {
                    anchors.fill: parent
                    enabled: root.enabled
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        root.currentIndex = parent.index
                        root.activated(parent.index)
                    }
                }
            }
        }
    }
}
