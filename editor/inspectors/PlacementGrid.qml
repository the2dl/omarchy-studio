// The 3x3 placement grid (spec §1g / §2b): cells on a faint plate, the occupied ninth
// accent-ringed. Mock 2b: 3px gap, 4px plate padding, 9px plate radius, 5px cell
// radius, active cell rgba(accent,0.22) + 1.5px accent ring.
//
// Emit-only, like every control here: the active cell is derived from the layer's
// resolved rect, so a rejected placement never leaves a cell lit on a lie.
import QtQuick
import ".."

Rectangle {
    id: root

    // 0..8, row-major; -1 lights nothing (the layer sits between ninths).
    property int currentCell: -1
    property bool enabled: true
    signal placed(int col, int row)

    implicitHeight: 3 * 20 + 2 * 3 + 2 * 4
    radius: 9
    color: Qt.rgba(1, 1, 1, 0.035)
    opacity: enabled ? 1.0 : 0.45

    Grid {
        x: 4
        y: 4
        columns: 3
        spacing: 3
        Repeater {
            model: 9
            delegate: Rectangle {
                required property int index
                width: (root.width - 8 - 6) / 3
                height: 20
                radius: 5
                color: index === root.currentCell
                       ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.22)
                       : ma.containsMouse ? Theme.fillSubtle : "transparent"
                border.width: index === root.currentCell ? 1.5 : 0
                border.color: Theme.accent
                Behavior on color { ColorAnimation { duration: Theme.durFast } }
                MouseArea {
                    id: ma
                    anchors.fill: parent
                    hoverEnabled: true
                    enabled: root.enabled
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.placed(parent.index % 3, Math.floor(parent.index / 3))
                }
            }
        }
    }
}
