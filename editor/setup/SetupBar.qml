// The setup bar: the single-row pill carrying source mode, the mic, system audio and
// the camera. It is an ITEM, not a window, and it is drawn inside the full-screen
// sheet on purpose.
//
// It used to be its own frameless window beside one sheet per monitor, and that shape
// is what made every control on it dead. The sheet is monitor-sized with a full-fill
// MouseArea and maps after the bar (it is instantiated from /sources, which arrives
// async), so Hyprland stacked it on top: the bar showed through the transparent sheet
// while every click aimed at it was delivered to the sheet. Raising the bar did not
// hold -- with focus-follows-mouse, crossing the sheet focused it, Hyprland raised the
// focused window, and the bar dropped back under, unrecoverably, because the pointer
// must cross the sheet to reach the bar. A no_focus rule on the sheet stopped that by
// making the sheet deaf to the pointer altogether, which killed Start recording, the
// window picker and the device lists instead.
//
// One surface has none of those failure modes. Z-order inside a window belongs to QML,
// not the compositor: this item is declared after the sheet's click layer, so it sits
// above it, and that is the whole of it.
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import ".."
import "../controls" as C

Item {
    id: bar

    property var app                     // the state holder (setup/main.qml)

    implicitWidth: barRow.implicitWidth + 36
    implicitHeight: 70
    width: implicitWidth
    height: implicitHeight

    // --open-picker: the verification hook opens a list on a named chip. The chip
    // has to be resolved here, where the ids live.
    function openPicker(kind) {
        app.togglePicker(kind, kind === "mic" ? micChip : camChip)
    }

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusPanel
        // bg-float without its blur would bleed the desktop into the controls
        // (verified in a grab of the earlier modal); solid bg is the honest form.
        color: Theme.bg
    }

    RowLayout {
        id: barRow
        anchors.centerIn: parent
        spacing: 14

        Row {   // mode chips (register of the mock's tabs: 7x13 padding chips)
            spacing: 4
            Repeater {
                model: app.modeNames
                delegate: Rectangle {
                    required property int index
                    required property var modelData
                    objectName: "ctl:mode-" + modelData
                    width: modeLabel.implicitWidth + 26
                    height: 27
                    radius: Theme.radiusRow
                    color: index === app.mode ? Theme.fillHover : "transparent"
                    Behavior on color { ColorAnimation { duration: Theme.durFast } }
                    Text {
                        id: modeLabel
                        anchors.centerIn: parent
                        text: modelData
                        color: index === app.mode ? Theme.text : Theme.text4
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsRow
                        Behavior on color { ColorAnimation { duration: Theme.durFast } }
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: app.setMode(parent.index, true)
                    }
                }
            }
        }

        Rectangle { width: 1; height: 26; color: Theme.hairline }

        Text {   // mic glyph doubles as the state light
            text: "\uf130"
            color: app.micOn && app.sources.mic !== null ? Theme.accent : Theme.text4
            font.family: Theme.fontFamily
            font.pixelSize: 17
        }
        DeviceChip {
            id: micChip
            objectName: "ctl:mic-device"
            label: app.micEntry !== null ? app.micEntry.label : "no mic"
            enabled: app.sources.mics !== undefined && app.sources.mics.length > 0
            open: app.pickerOpen === "mic"
            onToggled: app.togglePicker("mic", micChip)
        }
        MicMeter {
            Layout.preferredWidth: 130
            level: app.micLevel
            active: app.micOn && app.sources.mic !== null
        }
        Text {
            text: app.sources.mic === null ? "no mic"
                  : app.micOn ? (Math.round(Math.max(-60, app.micDb)) + " dB")
                  : "muted"
            color: Theme.text5
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsHint
            Layout.preferredWidth: 44
            horizontalAlignment: Text.AlignRight
        }
        C.Toggle {
            objectName: "ctl:mic-toggle"
            checked: app.micOn
            enabled: app.sources.mic !== null
            onToggled: function (v) { app.micOn = v }
        }

        Rectangle { width: 1; height: 26; color: Theme.hairline }

        Text {   // system audio
            text: "\uf028"
            color: app.desktopAudio ? Theme.accent : Theme.text4
            font.family: Theme.fontFamily
            font.pixelSize: 16
        }
        C.Toggle {
            objectName: "ctl:audio-toggle"
            checked: app.desktopAudio
            onToggled: function (v) { app.desktopAudio = v }
        }

        Rectangle { width: 1; height: 26; color: Theme.hairline }

        Text {   // camera overlay shape
            text: "\uf03d"
            color: app.cameraMode > 0 ? Theme.accent : Theme.text4
            font.family: Theme.fontFamily
            font.pixelSize: 16
        }
        DeviceChip {
            id: camChip
            objectName: "ctl:camera-device"
            label: app.cameraEntry !== null ? app.cameraEntry.name : "no camera"
            enabled: app.sources.cameras.length > 0
            open: app.pickerOpen === "camera"
            onToggled: app.togglePicker("camera", camChip)
        }
        Rectangle { width: 1; height: 26; color: Theme.hairline }

        // The teleprompter. It belongs here rather than in the editor because it is a
        // recording-time tool: the script is read WHILE the take happens, and by the
        // time the editor exists the reading is over. It is also the only surface in
        // the product that depends on the screenshare-exclude patch to be useful at
        // all, so the chip reports when the compositor cannot hide it.
        C.Toggle {
            objectName: "ctl:prompter-toggle"
            checked: app.prompterOn
            onToggled: function (v) { app.setPrompter(v) }
        }
        Text {
            text: "Script"
            color: app.prompterOn ? Theme.text2 : Theme.text4
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsRow
        }

        Rectangle { width: 1; height: 26; color: Theme.hairline }

        C.Segmented {
            objectName: "ctl:camera-shape"
            // No preferredWidth: Segmented sizes itself to its widest label now. The
            // fixed 186 here was four chips at 43px each, which "Squircle" overflowed
            // into both of its neighbours.
            model: ["Off", "Circle", "Rounded", "Rect"]
            currentIndex: app.cameraMode
            enabled: app.sources.cameras.length > 0
            onActivated: function (i) { app.cameraMode = i }
        }
    }
}
