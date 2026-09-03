// The teleprompter's control strip: transport, pace, type size, dim, edit, close.
//
// It sits INSIDE the prompter window rather than floating beside it, for the reason
// editor/setup/SetupBar.qml gives: two windows cannot be kept in a workable stacking
// order under focus-follows-mouse, and the second one ends up either covered or deaf.
//
// The strip fades out while the script is running and comes back on hover. Reading is
// the whole point of the window; chrome that stays lit is chrome you read by accident.
import QtQuick
import QtQuick.Layouts
import ".."
import "../controls" as C
import "../shell" as S

Item {
    id: root

    property bool running: false
    property real wpm: 130
    property int fontSize: 26
    property real dim: 1.0
    property bool editing: false
    // Time left at the current pace. The prompter is the only place that knows both the
    // script and the speed, so it is the only place that can answer "how long is this
    // take" -- which is usually why the script was written in the first place.
    property string remaining: "0:00"

    signal toggleRun()
    signal toggleEdit()
    signal restart()
    signal closeRequested()

    implicitHeight: 40

    // The strip has to survive a narrow window: the prompter is routinely dragged small
    // so it covers less of what is being demonstrated, and a RowLayout that overflows
    // does not wrap or clip -- it pushes its last children off the end, which silently
    // took the close button away at 720px. Labels and the readout go first; the
    // transport and the two buttons that have no other route are the last to go.
    readonly property bool roomy: width > 700
    readonly property bool wide: width > 560

    Rectangle {
        anchors.fill: parent
        color: Qt.rgba(0, 0, 0, 0.35)
        Rectangle {
            width: parent.width
            height: 1
            anchors.bottom: parent.bottom
            color: Theme.hairline
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 10
        anchors.rightMargin: 10
        spacing: 10

        S.GlyphButton {
            objectName: "ctl:prompter-play"
            glyph: root.running ? "\uf04c" : "\uf04b"   // nf-fa-pause / nf-fa-play
            tip: root.running ? "Pause  (Space)" : "Play  (Space)"
            accented: root.running
            onClicked: root.toggleRun()
        }
        S.GlyphButton {
            objectName: "ctl:prompter-restart"
            glyph: "\uf01e"                       // nf-fa-repeat
            tip: "Back to the top  (Home)"
            onClicked: root.restart()
        }

        Rectangle { width: 1; Layout.preferredHeight: 20; color: Theme.hairline }

        // Pace in words per minute, because that is the unit a person can rehearse
        // against -- pixels per second is meaningless until you have already read it
        // once. The scroll rate is derived from it and the measured line height.
        Text {
            visible: root.wide
            text: "pace"
            color: Theme.text5
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsHint
            font.letterSpacing: Theme.capsSpacing * Theme.fsHint
        }
        C.Slider {
            id: paceSlider
            Layout.preferredWidth: root.wide ? 110 : 70
            from: 60
            to: 240
            value: root.wpm
            onMoved: root.wpm = value
        }
        Text {
            text: Math.round(root.wpm) + (root.wide ? " wpm" : "")
            color: Theme.text2
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsRow
            Layout.preferredWidth: root.wide ? 62 : 30
        }

        Rectangle { width: 1; Layout.preferredHeight: 20; color: Theme.hairline }

        S.GlyphButton {
            glyph: "\uf010"                       // nf-fa-search_minus
            tip: "Smaller type"
            onClicked: root.fontSize = Math.max(14, root.fontSize - 2)
        }
        Text {
            visible: root.wide
            text: root.fontSize + "px"
            color: Theme.text3
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsRow
            Layout.preferredWidth: visible ? 34 : 0
            horizontalAlignment: Text.AlignHCenter
        }
        S.GlyphButton {
            glyph: "\uf00e"                       // nf-fa-search_plus
            tip: "Bigger type"
            onClicked: root.fontSize = Math.min(72, root.fontSize + 2)
        }

        Rectangle { width: 1; Layout.preferredHeight: 20; color: Theme.hairline }

        Text {
            visible: root.wide
            text: "dim"
            color: Theme.text5
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsHint
            font.letterSpacing: Theme.capsSpacing * Theme.fsHint
        }
        C.Slider {
            id: dimSlider
            Layout.preferredWidth: root.wide ? 90 : 60
            from: 0.2
            to: 1.0
            value: root.dim
            onMoved: root.dim = value
        }

        Item { Layout.fillWidth: true }

        Text {
            visible: root.roomy
            text: root.remaining + " left"
            color: Theme.text4
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsRow
        }

        Rectangle { width: 1; Layout.preferredHeight: 20; color: Theme.hairline }

        S.GlyphButton {
            objectName: "ctl:prompter-edit"
            glyph: "\uf040"                       // nf-fa-pencil
            tip: root.editing ? "Done  (Ctrl+E)" : "Edit or paste your script  (Ctrl+E)"
            accented: root.editing
            onClicked: root.toggleEdit()
        }
        S.GlyphButton {
            objectName: "ctl:prompter-close"
            glyph: "\uf00d"                       // nf-fa-times
            tip: "Hide the prompter  (Esc)"
            onClicked: root.closeRequested()
        }
    }
}
