// The top-bar render chip (spec §4 "Decisions", drawn in mock 2a): once a render
// starts the export pane can be dismissed, and progress collapses into this --
// a 13px conic ring + "Rendering 64%" in accent on an 11% accent wash. Clicking it
// reopens the pane.
//
// The ring is a Canvas arc rather than a ConicalGradient: the gradient effect lives in
// Qt5Compat, and pulling that module in for one 13px circle is how version drift gets
// imported. An arc stroked at the ring's thickness reads identically at this size.
import QtQuick
import ".."

Item {
    id: root

    property real progress: 0
    signal clicked()

    implicitWidth: row.implicitWidth + 22
    implicitHeight: 23

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusRow - 1   // mock: 9px on a 10px control radius register
        // 11% accent wash, from the mock's rgba(242,162,90,0.11) -- derived from the
        // live accent so a cyan theme gets a cyan chip.
        color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.11)
    }

    Row {
        id: row
        anchors.centerIn: parent
        spacing: 9

        Canvas {
            id: ring
            width: 13
            height: 13
            anchors.verticalCenter: parent.verticalCenter
            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                // 13px circle with a 3.5px inset hole (mock 2a) = a 3.2px ring stroke.
                var c = width / 2
                var r = (width - 3.2) / 2
                ctx.lineWidth = 3.2
                ctx.strokeStyle = Theme.track
                ctx.beginPath()
                ctx.arc(c, c, r, 0, 2 * Math.PI)
                ctx.stroke()
                ctx.strokeStyle = Theme.accent
                ctx.beginPath()
                ctx.arc(c, c, r, -Math.PI / 2,
                        -Math.PI / 2 + 2 * Math.PI * Math.max(0, Math.min(1, root.progress)))
                ctx.stroke()
            }
        }

        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "Rendering " + Math.round(root.progress * 100) + "%"
            color: Theme.accent
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsCaption
        }
    }

    onProgressChanged: ring.requestPaint()

    MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
