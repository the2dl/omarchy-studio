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
import QtQuick.Controls.Basic as QC
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
            checked: app.micOn && app.audioAvailable
            // Two reasons to be off: no microphone exists, or this capture mode
            // cannot record one.
            enabled: app.sources.mic !== null && app.audioAvailable
            opacity: app.audioAvailable ? 1.0 : 0.4
            onToggled: function (v) { if (app.audioAvailable) app.micOn = v }
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
            checked: app.desktopAudio && app.audioAvailable
            enabled: app.audioAvailable
            opacity: app.audioAvailable ? 1.0 : 0.4
            onToggled: function (v) { if (app.audioAvailable) app.desktopAudio = v }
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

        // Re-framing. Only for a window or an area: a display capture is already the
        // whole display, so the choice would be between a thing and itself.
        //
        // OFF by default, and the default is the point. On means the whole screen is
        // recorded and the selection is carried as a crop -- which is what lets the
        // frame be moved afterwards, and the only way a window that moved can be
        // followed. It also means everything beside the selection lands on disk, and
        // picking one window is often precisely a decision NOT to record the rest.
        // That is not a default anyone should get without asking for it.
        // Two answers to one question, so they are drawn as a pair and only one can
        // be true. "Just this window" captures the window's own surface tree, so
        // nothing drawn over it is in the take and there is no surrounding pixel to
        // re-frame into -- which is exactly what "Re-frame later" needs. Ticking
        // either therefore unticks the other.
        Item {
            id: windowOnlyRow
            // Window picks only. An area is a rectangle by definition; there is no
            // toplevel behind it to export.
            visible: app.mode === 1
            implicitWidth: windowOnlyContent.implicitWidth
            implicitHeight: 26
            Layout.preferredWidth: implicitWidth
            Layout.preferredHeight: implicitHeight

            Row {
                id: windowOnlyContent
                anchors.verticalCenter: parent.verticalCenter
                spacing: 8
                Rectangle {
                    width: 16; height: 16
                    anchors.verticalCenter: parent.verticalCenter
                    radius: 4
                    color: app.windowOnly ? Theme.accent : "transparent"
                    border.width: 1
                    border.color: app.windowOnly ? Theme.accent
                                : windowOnlyMa.containsMouse ? Theme.text3 : Theme.text5
                    Behavior on color { ColorAnimation { duration: Theme.durFast } }
                    Text {
                        anchors.centerIn: parent
                        text: "\uf00c"
                        visible: app.windowOnly
                        color: Theme.bg
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                    }
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Just this window"
                    color: app.windowOnly ? Theme.text2
                         : windowOnlyMa.containsMouse ? Theme.text3 : Theme.text4
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                }
            }
            MouseArea {
                id: windowOnlyMa
                objectName: "ctl:windowonly-toggle"
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    app.windowOnly = !app.windowOnly
                    if (app.windowOnly)
                        app.fullMonitor = false
                }
            }
            QC.ToolTip {
                id: windowOnlyTip
                objectName: "windowOnlyTip"
                parent: windowOnlyRow
                visible: windowOnlyMa.containsMouse
                text: "Records this window's own pixels. Anything drawn over it \u2014 a "
                    + "dropdown, a notification \u2014 is not in the take, and the frame "
                    + "follows the window if it moves.\nOff records the rectangle it sits "
                    + "in, whatever ends up on top."
                delay: 350
                timeout: -1
                padding: 0
                margins: 0
                y: -height - 10
                x: -(width - windowOnlyRow.width) / 2
                background: Rectangle {
                    color: Theme.bgFloat
                    radius: Theme.radiusRow - 2
                    border.width: 1
                    border.color: Theme.hairline
                }
                contentItem: Text {
                    text: windowOnlyTip.text
                    color: Theme.text2
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                    leftPadding: 10; rightPadding: 10
                    topPadding: 7; bottomPadding: 7
                    lineHeight: 1.25
                }
            }
        }

        // A CHECKBOX, not another Toggle. The bar already carries three switches
        // (mic, system audio, Script) and they all mean "turn this input on". This
        // one means something else -- how much of the screen reaches the disk -- and
        // reading as a fourth switch is why it was missed entirely.
        //
        // An Item wrapping a Row, not a bare Row, and that distinction shipped as a
        // bug: a MouseArea with anchors.fill inside a Row is a child the Row also
        // POSITIONS, so the anchor and the layout fight, the hit area spills past its
        // own control, and the Script toggle beside it stops being clickable. The Row
        // positions the visuals; the Item owns the bounds the MouseArea fills.
        Item {
            id: reframeRow
            // The MODE decides visibility, not the selection. setMode(1) clears `sel`,
            // so gating on a selection hid this for the whole of choosing a window --
            // exactly when someone would look for it. It survived review because the
            // selftest auto-picks a window to make the picked state photographable, so
            // every screenshot had a selection the real flow never has.
            visible: app.mode !== 0 && !app.windowOnly
            implicitWidth: reframeContent.implicitWidth
            implicitHeight: 26
            Layout.preferredWidth: implicitWidth
            Layout.preferredHeight: implicitHeight

            Row {
                id: reframeContent
                anchors.verticalCenter: parent.verticalCenter
                spacing: 8

                Rectangle {
                    width: 16; height: 16
                    anchors.verticalCenter: parent.verticalCenter
                    radius: 4
                    color: app.fullMonitor ? Theme.accent : "transparent"
                    border.width: 1
                    border.color: app.fullMonitor ? Theme.accent
                                : reframeMa.containsMouse ? Theme.text3 : Theme.text5
                    Behavior on color { ColorAnimation { duration: Theme.durFast } }
                    Text {
                        anchors.centerIn: parent
                        text: "\uf00c"            // nf-fa-check
                        visible: app.fullMonitor
                        color: Theme.bg
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                    }
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Re-frame later"
                    color: app.fullMonitor ? Theme.text2
                         : reframeMa.containsMouse ? Theme.text3 : Theme.text4
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                }
            }

            MouseArea {
                id: reframeMa
                objectName: "ctl:reframe-toggle"
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    app.fullMonitor = !app.fullMonitor
                    if (app.fullMonitor)
                        app.windowOnly = false
                }
            }

            // The label cannot carry the trade-off, and the trade-off is the whole
            // decision: it costs the rest of the screen going to disk.
            QC.ToolTip {
                id: reframeTip
                objectName: "reframeTip"
                parent: reframeRow
                visible: reframeMa.containsMouse
                text: app.mode === 1
                    ? "Records the whole display so you can move the frame afterwards, "
                    + "and follow this window if it moves.\nOff records only the window "
                    + "you picked \u2014 nothing else reaches the disk."
                    : "Records the whole display so you can move the frame afterwards.\n"
                    + "Off records only the area you picked \u2014 nothing else reaches "
                    + "the disk."
                delay: 350
                timeout: -1
                padding: 0
                margins: 0
                y: -height - 10
                x: -(width - reframeRow.width) / 2
                background: Rectangle {
                    color: Theme.bgFloat
                    radius: Theme.radiusRow - 2
                    border.width: 1
                    border.color: Theme.hairline
                }
                contentItem: Text {
                    // reframeTip.text, not parent.text: contentItem's parent is a bare
                    // QQuickItem, so `parent.text` resolves to undefined and the tip
                    // renders empty -- silently. qmllint [missing-property] catches it.
                    text: reframeTip.text
                    color: Theme.text2
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                    leftPadding: 10; rightPadding: 10
                    topPadding: 7; bottomPadding: 7
                    lineHeight: 1.25
                }
            }
        }
        Rectangle {
            width: 1; height: 26; color: Theme.hairline
            visible: app.mode !== 0
        }

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
