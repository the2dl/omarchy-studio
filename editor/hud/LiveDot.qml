// The recording dot (spec §1c): 9px in `live`, with a halo that expands and fades on a
// 1.8s loop. The spec gives this as a CSS keyframe on box-shadow; a growing, fading ring
// behind the dot is the same animation with a shadow QML does not have.
//
// It is the only red in the product, and it is animated because a still red dot reads as
// a status light that might be stale -- the pulse is what says the recorder is alive
// right now.
import QtQuick
import ".."

Item {
    id: root
    property bool paused: false

    implicitWidth: 9
    implicitHeight: 9

    Rectangle {
        id: halo
        anchors.centerIn: parent
        width: 9
        height: 9
        radius: width / 2
        color: "transparent"
        border.color: Theme.live
        border.width: 1
        opacity: 0

        SequentialAnimation on opacity {
            running: !root.paused
            loops: Animation.Infinite
            NumberAnimation { from: 0.5; to: 0.0; duration: 1800; easing.type: Easing.InOutQuad }
        }
        ParallelAnimation {
            running: !root.paused
            loops: Animation.Infinite
            NumberAnimation { target: halo; property: "width"; from: 9; to: 19; duration: 1800; easing.type: Easing.InOutQuad }
            NumberAnimation { target: halo; property: "height"; from: 9; to: 19; duration: 1800; easing.type: Easing.InOutQuad }
        }
    }

    Rectangle {
        anchors.centerIn: parent
        width: 9
        height: 9
        radius: width / 2
        // Paused is hollow rather than a different colour: the palette has one red and
        // borrowing another would make "paused" look like a second kind of error.
        color: root.paused ? "transparent" : Theme.live
        border.color: Theme.live
        border.width: root.paused ? 1.5 : 0
    }
}
