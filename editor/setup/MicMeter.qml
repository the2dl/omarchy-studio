// The 12-segment mic level meter (spec §1b): 2px gaps, per-segment heights from the
// mock, hot segments `accent`, the two below the top `accent-dim`, and an unlit tail
// shading #4a423a → #2a2825. The tail hexes are literal because the spec names them
// literally and the token set has no rung between `track` and `bg` -- same reasoning
// as Toggle.qml's fixed off-knob grey.
import QtQuick
import ".."

Item {
    id: root

    property real level: 0.0    // 0..1, already perceptually mapped by the bridge
    property bool active: true  // false = mic off; the whole meter rests dark

    implicitHeight: 16

    // Heights measured off the mock's twelve spans (§1b markup).
    readonly property var heights: [12, 16, 9, 14, 7, 11, 5, 8, 4, 3, 5, 3]
    readonly property int lit: active ? Math.round(Math.min(1, Math.max(0, level)) * 12) : 0

    Row {
        anchors.fill: parent
        spacing: 2

        Repeater {
            model: 12
            delegate: Item {
                required property int index
                width: (root.width - 2 * 11) / 12
                height: root.height

                Rectangle {
                    width: parent.width
                    height: root.heights[parent.index]
                    anchors.verticalCenter: parent.verticalCenter
                    radius: 1
                    color: {
                        var i = parent.index
                        if (i < root.lit)
                            // The top two lit segments step down to accent-dim, as
                            // the mock does, so a loud level reads as a gradient
                            // rather than a solid amber bar.
                            return i >= root.lit - 2 && root.lit > 2
                                ? Theme.accentDim : Theme.accent
                        // Unlit tail: fade across the row from #4a423a to #2a2825.
                        var t = i / 11
                        return Qt.rgba(0.290 - t * 0.125, 0.259 - t * 0.102,
                                       0.227 - t * 0.082, 1)
                    }
                    Behavior on color { ColorAnimation { duration: 60 } }
                }
            }
        }
    }
}
