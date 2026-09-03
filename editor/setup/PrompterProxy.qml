// A drag handle for the teleprompter, drawn on the setup sheet.
//
// WHY A PROXY AND NOT THE WINDOW ITSELF. The sheet is monitor-sized and takes the
// pointer, so the prompter underneath it cannot be dragged while the sheet is up.
// Raising the prompter does not fix that: with focus-follows-mouse the pointer has to
// CROSS the sheet to reach the prompter, which focuses and re-raises the sheet and drops
// the prompter back under -- measured with bring_to_top, the sheet stayed on top through
// it. This is the same failure that made the old floating setup bar unclickable, and it
// has the same answer: whatever the sheet covers, the sheet has to drive.
//
// So this draws an outline where the prompter is and moves the REAL window as you drag,
// exactly as SelfView reports the camera bubble's placement. Sheet coordinates are
// monitor-local; the prompter's are global logical desktop pixels, hence the origin.
import QtQuick
import ".."

Item {
    id: root

    property var app
    property real originX: 0
    property real originY: 0

    // {running, x, y, width, height} in global logical pixels, from the launcher.
    property var rect: ({ running: false })
    readonly property bool present: rect && rect.running === true

    // Live position while dragging, so the outline tracks the pointer even if a move
    // POST is still in flight.
    property real liveX: 0
    property real liveY: 0
    property bool dragging: false

    visible: present && !app.counting && !app.picking
    x: (dragging ? liveX : (present ? rect.x : 0)) - originX
    y: (dragging ? liveY : (present ? rect.y : 0)) - originY
    width: present ? rect.width : 0
    height: present ? rect.height : 0

    signal moveRequested(int x, int y)

    // Throttled: each move is an hyprctl dispatch, and posting one per mouse event would
    // spawn dozens of processes a second. 60ms is under the threshold where a dragged
    // window stops feeling attached to the pointer.
    Timer {
        id: throttle
        interval: 60
        onTriggered: root.moveRequested(Math.round(root.liveX), Math.round(root.liveY))
    }

    Rectangle {
        anchors.fill: parent
        color: dragArea.containsMouse || root.dragging
               ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.10)
               : "transparent"
        border.width: root.dragging ? 2 : 1.5
        border.color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b,
                              dragArea.containsMouse || root.dragging ? 0.9 : 0.45)
        radius: Theme.radiusWindow
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }

    // A label, because an outline with no name over a window you were not expecting to
    // be undraggable reads as a rendering artefact rather than an affordance.
    Rectangle {
        anchors.horizontalCenter: parent.horizontalCenter
        y: -14
        width: label.implicitWidth + 18
        height: 24
        radius: 12
        color: Theme.bgFloat
        border.width: 1
        border.color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.45)
        opacity: dragArea.containsMouse || root.dragging ? 1.0 : 0.75
        Text {
            id: label
            anchors.centerIn: parent
            text: "Script — drag to move"
            color: Theme.text2
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsCaption
        }
    }

    MouseArea {
        id: dragArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: root.dragging ? Qt.ClosedHandCursor : Qt.OpenHandCursor
        property real ox: 0
        property real oy: 0

        onPressed: function (m) {
            root.liveX = root.rect.x
            root.liveY = root.rect.y
            ox = m.x
            oy = m.y
            root.dragging = true
        }
        onPositionChanged: function (m) {
            if (!root.dragging)
                return
            root.liveX += m.x - ox
            root.liveY += m.y - oy
            if (!throttle.running)
                throttle.start()
        }
        onReleased: {
            throttle.stop()
            root.dragging = false
            root.moveRequested(Math.round(root.liveX), Math.round(root.liveY))
        }
    }
}
