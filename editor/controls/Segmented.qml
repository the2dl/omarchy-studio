// Equal-width chips, 4px gap, no borders. The active chip gets fillHover + text2; the
// rest sit at text4. Selection here is a fill, not a ring -- rings are reserved for a
// selected OBJECT (a card, a keyframe), and using one here would overload the signal.

import QtQuick
import ".."

Item {
    id: root

    property var model: []
    property int currentIndex: 0
    // No `property bool enabled` here: Item already declares it, and redeclaring
    // shadows the base property. Qt warns on it and newer builds make it a hard
    // "Cannot override FINAL property" error, which fails the whole QML load with
    // no symptom but a window that never appears. Item.enabled already does what
    // is wanted -- it blocks input and propagates to children.
    signal activated(int index)

    implicitHeight: 26
    opacity: enabled ? 1.0 : 0.45

    // Chips are equal width, so the widest label has to set the width for all of them.
    // Without this the control simply took whatever width the layout handed it, and a
    // caller that guessed low got labels running into each other rather than a squeezed
    // chip -- the text is centred in the chip and nothing clips it. Four chips in a
    // fixed 186px did exactly that on the setup bar.
    readonly property real chipPadding: 18
    implicitWidth: {
        var widest = 0
        for (var i = 0; i < model.length; ++i)
            widest = Math.max(widest, metrics.advanceWidth(String(model[i])))
        return Math.ceil(model.length * (widest + chipPadding)
                         + 4 * Math.max(0, model.length - 1))
    }

    FontMetrics {
        id: metrics
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fsRow
    }

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
                    // Emit only -- `currentIndex` is bound to the model, and assigning
                    // it here would destroy that binding, leaving the chip lit on a
                    // value the project rejected (a burned-in webcam does exactly that).
                    onClicked: root.activated(parent.index)
                }
            }
        }
    }
}
