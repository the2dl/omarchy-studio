// accent fill, accentOn label, plus the accent glow. The only button that carries a
// shadow -- everything else in the chrome is a flat fill, so the glow is what marks the
// one action the screen is for.
import QtQuick
import QtQuick.Effects
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

    // The spec's shadow is `0 6px 18px rgba(accent, 0.22)` -- an 18px blur under a 30px
    // button, so it reads as the button sitting slightly proud of the panel. MultiEffect
    // measures blur against blurMax rather than in pixels, so blurMax is pinned to 18 and
    // shadowBlur left at 1.0 to mean exactly that radius. Scaling shadowBlur against the
    // default blurMax of 32 instead spreads the halo past the button's own width, which
    // stops reading as depth and starts reading as fuzz.
    MultiEffect {
        source: fill
        anchors.fill: fill
        shadowEnabled: true
        shadowColor: Theme.accentGlow
        shadowVerticalOffset: 6
        shadowBlur: 1.0
        blurMax: 18
        shadowScale: 1.0
        opacity: root.enabled ? 1 : 0
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
