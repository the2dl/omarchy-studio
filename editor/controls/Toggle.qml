// A 30x17 pill with a 13px knob, 2px inset.
//
// Qt's Switch is not restyled here, it is replaced: the native control carries an
// indicator, a background, a content item and its own implicit sizing, and pinning all
// four to these numbers takes more code than drawing two rectangles.

import QtQuick
import ".."

Item {
    id: root

    property bool checked: false
    // No `property bool enabled` here: Item already declares it, and redeclaring
    // shadows the base property. Qt warns on it and newer builds make it a hard
    // "Cannot override FINAL property" error, which fails the whole QML load with
    // no symptom but a window that never appears. Item.enabled already does what
    // is wanted -- it blocks input and propagates to children.
    signal toggled(bool value)

    implicitWidth: 30
    implicitHeight: 17

    opacity: enabled ? 1.0 : 0.45

    Rectangle {
        id: track
        anchors.fill: parent
        radius: height / 2
        color: root.checked ? Theme.accent : Theme.track
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }

    Rectangle {
        id: knob
        width: 13
        height: 13
        radius: height / 2
        y: 2
        x: root.checked ? parent.width - width - 2 : 2
        // Off-state knob is a fixed mid-grey in the spec rather than a token, because it
        // has to read against `track` on both light and dark grounds.
        color: root.checked ? Theme.accentOn : "#6d6863"
        Behavior on x { NumberAnimation { duration: Theme.durFast; easing.type: Easing.OutCubic } }
        Behavior on color { ColorAnimation { duration: Theme.durFast } }
    }

    MouseArea {
        anchors.fill: parent
        anchors.margins: -6   // the pill is small; the hit target should not be
        enabled: root.enabled
        cursorShape: Qt.PointingHandCursor
        // Emit only, never self-assign: `checked` is bound to the model, and a local
        // assignment would destroy that binding -- the control would keep a value the
        // project may have rejected. The bridge round-trips in 0.27ms, so the knob
        // still answers the click instantly.
        onClicked: root.toggled(!root.checked)
    }
}
