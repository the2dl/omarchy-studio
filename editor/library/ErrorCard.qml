// The error/empty card (spec §2g): glyph + title, one or two lines of text-3 giving
// state AND consequence, then two actions with the recoverable one first.
//
// One reusable component rather than five one-offs, because the spec's table is five
// rows of the same anatomy. The `severe` flag is the only place the live red is allowed
// to appear -- on the GLYPH, never on a button and never as a fill (spec §2g). Copy
// rule for callers: say what happened, then what survived, then what to press. No
// apologies, no exclamation marks.
import QtQuick
import ".."

Rectangle {
    id: root

    property string glyph: ""
    property bool severe: false          // live-red glyph: an error, not a state
    property string title: ""
    property string body: ""
    property string primaryText: ""
    property string secondaryText: ""
    property bool primaryAccent: false   // Retry / Record: the one accented recovery
    property string primaryGlyph: ""
    property bool centered: false        // the empty-library variant
    signal primaryClicked()
    signal secondaryClicked()

    // The action chip the cards share, exported for siblings (the Edit/Locate buttons
    // in Recordings.qml use it) so the register exists exactly once.
    component ActionChip: Item {
        id: chip
        property string text: ""
        property string glyph: ""
        // "neutral": subtle fill; "accent": the recoverable action when it deserves
        // the accent; "ghost": the action you only find when you look for it.
        property string kind: "neutral"
        signal clicked()

        implicitWidth: chipRow.implicitWidth + 2 * (chip.kind === "accent" && chip.glyph !== "" ? 15 : 12)
        implicitHeight: 26

        Rectangle {
            anchors.fill: parent
            radius: Theme.radiusChip
            color: chip.kind === "accent"
                   ? (chipMa.containsMouse ? Qt.lighter(Theme.accent, 1.08) : Theme.accent)
                   : chip.kind === "neutral"
                     ? (chipMa.containsMouse ? Theme.fillHover : Theme.fillSubtle)
                     : (chipMa.containsMouse ? Theme.fillSubtle : "transparent")
            Behavior on color { ColorAnimation { duration: Theme.durFast } }
        }
        Row {
            id: chipRow
            anchors.centerIn: parent
            spacing: 7
            Text {
                visible: chip.glyph !== ""
                anchors.verticalCenter: parent.verticalCenter
                text: chip.glyph
                color: chip.kind === "accent" ? Theme.accentOn : Theme.text3
                font.family: Theme.fontFamily
                font.pixelSize: 13
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: chip.text
                color: chip.kind === "accent" ? Theme.accentOn
                     : chip.kind === "neutral" ? Theme.text2
                     : (chipMa.containsMouse ? Theme.text3 : Theme.text4)
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
                font.weight: chip.kind === "accent" ? Font.Medium : Font.Normal
                Behavior on color { ColorAnimation { duration: Theme.durFast } }
            }
        }
        MouseArea {
            id: chipMa
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: chip.clicked()
        }
    }

    implicitWidth: 350
    implicitHeight: column.implicitHeight + (centered ? 60 : 34)   // 17px / 30px pads
    radius: 13
    // The card ground is the canvas's darker stop, one step above bg -- a card floats
    // on the panel without borrowing the accent for attention.
    color: Theme.canvasB

    Column {
        id: column
        x: 18
        y: root.centered ? 30 : 17
        width: parent.width - 36
        spacing: root.centered ? 12 : 11

        // Header: glyph + title in one row, or stacked and centered for the empty
        // state, where there is nothing to recover and the card IS the screen.
        Row {
            visible: !root.centered
            width: parent.width
            spacing: 10
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: root.glyph
                color: root.severe ? Theme.live : Theme.text3
                font.family: Theme.fontFamily
                font.pixelSize: 17
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: root.title
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsBody
            }
        }
        Text {
            visible: root.centered
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            text: root.glyph
            color: Theme.text6
            font.family: Theme.fontFamily
            font.pixelSize: 26
        }
        Text {
            visible: root.centered
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            text: root.title
            color: Theme.text3
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsBody
        }

        Text {
            visible: root.body !== ""
            width: parent.width
            horizontalAlignment: root.centered ? Text.AlignHCenter : Text.AlignLeft
            text: root.body
            color: root.centered ? Theme.text5 : Theme.text3
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsCaption
            wrapMode: Text.WordWrap
            lineHeight: 1.6
        }

        Row {
            anchors.horizontalCenter: root.centered ? parent.horizontalCenter : undefined
            spacing: 8
            ActionChip {
                visible: root.primaryText !== ""
                text: root.primaryText
                glyph: root.primaryGlyph
                kind: root.primaryAccent ? "accent" : "neutral"
                onClicked: root.primaryClicked()
            }
            ActionChip {
                visible: root.secondaryText !== ""
                text: root.secondaryText
                kind: "ghost"
                onClicked: root.secondaryClicked()
            }
        }
    }
}
