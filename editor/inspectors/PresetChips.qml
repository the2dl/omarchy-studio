// The redaction strength control: chips, deliberately NOT the Segmented control.
//
// Segmented marks its active chip with a plain fill; here the active chip carries the
// accent wash + ring (mock 2a: rgba(accent,0.13) + inset 1px ring at 34% + accent
// label), because the strength preset is a *safety* choice and the mock promotes it a
// register above an ordinary mode switch. Same emit-only rule as Segmented: a rejected
// preset snaps the chips back to the model's truth.
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
                color: index === root.currentIndex
                       ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.13)
                       : "transparent"
                border.width: index === root.currentIndex ? 1 : 0
                border.color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.34)
                Behavior on color { ColorAnimation { duration: Theme.durFast } }

                Text {
                    anchors.centerIn: parent
                    text: modelData
                    color: index === root.currentIndex ? Theme.accent : Theme.text4
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsCaption
                    Behavior on color { ColorAnimation { duration: Theme.durFast } }
                }
                MouseArea {
                    anchors.fill: parent
                    enabled: root.enabled
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.activated(parent.index)
                }
            }
        }
    }
}
