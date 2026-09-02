// 3px track, 10px round thumb, value right-aligned on the caption line.
//
// The value never sits under the track. In the mock every row reads as
// "CAPTION ............ value" with the track beneath, so the eye finds all values in
// one column regardless of how long the captions are.
//
// `subject` is the spec's accent rule: the filled portion is accent when the property is
// what the panel is ABOUT, and text3 when it is secondary. That is how the Zoom & cursor
// inspector signals which half of it you are working in.

import QtQuick
import ".."

Item {
    id: root

    property string caption: ""
    property string valueText: ""
    property real from: 0
    property real to: 1
    property real value: 0
    property bool subject: true
    // No `property bool enabled` here: Item already declares it, and redeclaring
    // shadows the base property. Qt warns on it and newer builds make it a hard
    // "Cannot override FINAL property" error, which fails the whole QML load with
    // no symptom but a window that never appears. Item.enabled already does what
    // is wanted -- it blocks input and propagates to children.
    signal moved(real value)
    // Fired once, on release. Callers whose write-back is real work (the zoom amount
    // rebuilds the whole track) commit on this instead of on every moved().
    signal committed(real value)
    readonly property alias dragging: dragArea.pressed

    implicitHeight: caption ? 34 : 14
    opacity: enabled ? 1.0 : 0.45

    readonly property real _t: to > from ? (value - from) / (to - from) : 0

    Text {
        id: cap
        visible: root.caption !== ""
        text: root.caption
        color: Theme.text5
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fsCaption
        font.letterSpacing: Theme.fsCaption * Theme.capsSpacing
        font.capitalization: Font.AllUppercase
        anchors.left: parent.left
        anchors.top: parent.top
    }

    Text {
        visible: root.valueText !== ""
        text: root.valueText
        color: Theme.text3
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fsRow
        anchors.right: parent.right
        anchors.baseline: cap.visible ? cap.baseline : undefined
        anchors.top: cap.visible ? undefined : parent.top
    }

    Item {
        id: bar
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 12

        Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width
            height: 3
            radius: 1
            color: Theme.track
        }
        Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width * root._t
            height: 3
            radius: 1
            color: root.subject ? Theme.accent : Theme.text3
        }
        Rectangle {
            width: 10
            height: 10
            radius: 5
            anchors.verticalCenter: parent.verticalCenter
            x: Math.max(0, Math.min(parent.width - width, parent.width * root._t - width / 2))
            color: root.subject ? Theme.accent : Theme.text3
        }

        MouseArea {
            id: dragArea
            anchors.fill: parent
            anchors.margins: -8
            enabled: root.enabled
            cursorShape: Qt.PointingHandCursor
            function place(mx) {
                var t = Math.max(0, Math.min(1, (mx + 8) / bar.width))
                root.value = root.from + t * (root.to - root.from)
                root.moved(root.value)
            }
            onPressed: function (m) { place(m.x) }
            onPositionChanged: function (m) { if (pressed) place(m.x) }
            onReleased: root.committed(root.value)
        }
    }
}
