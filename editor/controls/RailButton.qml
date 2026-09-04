// One glyph in the 56px left rail. Active gets an accent tile with an accentOn glyph --
// the only place in the chrome where an accent FILL marks state rather than an action.
import QtQuick
import QtQuick.Controls.Basic as QC
import ".."

Item {
    id: root
    property string glyph: ""
    property bool active: false
    // Rendered by the ToolTip below. The rail is glyph-only, so without this the tools
    // are guessable at best -- and every call site was already passing a tip that
    // nothing drew.
    property string tip: ""
    signal clicked()

    implicitWidth: 38
    implicitHeight: 38

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusRow
        color: root.active ? Theme.accent : (ma.containsMouse ? Theme.fillHover : "transparent")
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }
    Text {
        anchors.centerIn: parent
        text: root.glyph
        color: root.active ? Theme.accentOn : (ma.containsMouse ? Theme.text2 : Theme.text4)
        font.family: Theme.fontFamily
        font.pixelSize: 19
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }
    MouseArea {
        id: ma
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }

    // A Popup, not a child Rectangle. The rail is 56px and the label is wider than
    // that, so a plain child would be painted OVER by the canvas that follows it in the
    // row -- overflow is allowed in QML, but sibling order still decides who wins. A
    // popup renders in the window overlay and cannot lose that fight.
    QC.ToolTip {
        id: tipPopup
        // A Popup is not in the visual children list, so a test cannot walk to it the
        // way it would to a Rectangle. Named so findChild can.
        objectName: "railTip"
        parent: root
        visible: root.tip !== "" && ma.containsMouse
        text: root.tip
        // Long enough not to flash while the pointer crosses the rail on its way
        // somewhere else, short enough to feel like an answer to hovering.
        delay: 450
        timeout: -1
        padding: 0
        margins: 0
        x: root.width + 8
        y: (root.height - height) / 2
        background: Rectangle {
            color: Theme.bgFloat
            radius: Theme.radiusRow - 2
            border.width: 1
            border.color: Theme.hairline
        }
        contentItem: Text {
            text: tipPopup.text
            color: Theme.text2
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsRow
            leftPadding: 9
            rightPadding: 9
            topPadding: 6
            bottomPadding: 6
        }
    }
}
