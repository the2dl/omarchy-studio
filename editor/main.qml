// omarchy-studio editor: window, layout, and the two things that leave the process
// (save and export).
//
// Launched by bin/omarchy-studio, which starts the Python bridge first and passes
// --port/--token. Run it with /usr/bin/qml6: plain `qml` on PATH is Qt 5.15 and fails
// with "Did not load any objects, exiting" and no other explanation.
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs
import "controls" as C

ApplicationWindow {
    id: app

    width: 1560
    height: 880    // the mock's editor frame (§1d: 1560×880)
    visible: true
    color: Theme.bg
    title: "Omarchy Studio — " + (Bridge.state.name || Bridge.bundle)

    readonly property var st: Bridge.state

    // m:ss from source frames -- whole seconds; the frame-exact counter lives in the
    // timeline transport where scrubbing needs it.
    function durationLabel() {
        if (!st.timebase || !st.source_frames)
            return ""
        var secs = Math.floor(st.source_frames / st.timebase.fps)
        var m = Math.floor(secs / 60)
        var s = secs % 60
        return m + ":" + (s < 10 ? "0" : "") + s
    }

    // The mock says "4K"; recordings that are not 4K say what they are instead.
    function formatLabel() {
        if (!st.canvas)
            return ""
        return st.canvas.height >= 2100 ? "4K" : (st.canvas.height + "p")
    }

    // Top bar, 46px (spec §1d region 1): file glyph, name, duration/format in text6,
    // then the actions. Undo/redo and Preview from the mock are absent on purpose --
    // the model has no undo stack and no preview render, and dead chrome would read
    // as broken. Save/Reset take their place until those exist.
    header: Rectangle {
        height: Style.topBarHeight
        color: Theme.bg

        RowLayout {
            x: Style.pad
            y: 0
            width: parent.width - 2 * Style.pad
            height: parent.height
            spacing: 14

            Text {   // nf-fa-film
                text: ""
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: 17
            }
            Text {
                text: app.st.name || "loading…"
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsBody
            }
            Text {
                text: app.durationLabel() !== "" ? (app.durationLabel() + " · " + app.formatLabel()) : ""
                color: Theme.text6
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
            Item { Layout.fillWidth: true }

            // Unsaved state: an accent dot, nothing shouting. The Save button next to
            // it is the verb.
            Rectangle {
                width: 7; height: 7; radius: 3.5
                color: Theme.accent
                visible: app.st.dirty === true
            }
            C.GhostButton {
                text: "Save"
                onClicked: Bridge.save()
            }
            C.GhostButton {
                text: "Reset"
                onClicked: resetDialog.open()
            }
            Rectangle { width: 1; height: 16; color: Theme.hairline }
            C.PrimaryButton {
                text: Bridge.exportStatus.state === "running" ? "Cancel" : "Export"
                onClicked: {
                    if (Bridge.exportStatus.state === "running")
                        Bridge.cancelExport()
                    else
                        exportDialog.open()
                }
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent      // nothing here is transformed; the stage inside is
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            // Left rail, 56px (spec §1d region 2): the tools the editor actually has.
            // The mock shows six; the four below are the ones with behaviour behind
            // them, and the rail grows as tools do.
            Item {
                Layout.preferredWidth: Style.railWidth
                Layout.maximumWidth: Style.railWidth
                Layout.fillHeight: true

                Column {
                    x: (Style.railWidth - 38) / 2
                    y: 10
                    spacing: 6
                    Repeater {
                        model: [
                            { id: "select",   glyph: "", tip: "Select" },        // nf-fa-mouse_pointer
                            { id: "blur",     glyph: "", tip: "Blur box" },      // nf-fa-eye_slash
                            { id: "pixelate", glyph: "", tip: "Pixelate" },      // nf-fa-th
                            { id: "text",     glyph: "", tip: "Text" }           // nf-fa-font
                        ]
                        C.RailButton {
                            glyph: modelData.glyph
                            tip: modelData.tip
                            active: preview.tool === modelData.id
                            onClicked: preview.tool = modelData.id
                        }
                    }
                }
            }

            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true

                Preview {
                    id: preview
                    x: 0
                    y: 0
                    width: parent.width
                    height: parent.height
                }

                // The proxy is generated before anything is playable; showing the
                // progress here is the difference between "starting" and "hung".
                Rectangle {
                    x: 0
                    y: 0
                    width: parent.width
                    height: parent.height
                    visible: Bridge.proxyStatus.state === "building"
                             || Bridge.proxyStatus.state === "error"
                    color: Qt.rgba(0, 0, 0, 0.72)

                    ColumnLayout {
                        x: (parent.width - width) / 2
                        y: (parent.height - height) / 2
                        width: Math.min(460, parent.width - 80)
                        spacing: 10

                        Text {
                            text: Bridge.proxyStatus.state === "error"
                                  ? "Preview proxy failed"
                                  : "Building the preview proxy"
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsTitle
                            Layout.alignment: Qt.AlignHCenter
                        }
                        Text {
                            text: Bridge.proxyStatus.message
                                  || "The editor never plays the 5K master: seeking it took 517-651ms "
                                   + "and half the seeks delivered no frame at all."
                            color: Theme.text4
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsRow
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                        }
                        Item {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 3
                            visible: Bridge.proxyStatus.state === "building"
                            Rectangle {
                                width: parent.width; height: 3; radius: 1
                                color: Theme.track
                            }
                            Rectangle {
                                width: parent.width * (Bridge.proxyStatus.progress || 0)
                                height: 3; radius: 1
                                color: Theme.accent
                            }
                        }
                    }
                }
            }

            SettingsPanel {
                id: settings
                // 268px in the editor (spec §1d region 4); the 320px width is for the
                // standalone panels.
                Layout.preferredWidth: Theme.inspectorWidth
                // Capped as well as preferred: without a maximum, one over-wide control
                // inside pushes the panel past the window edge and clips itself.
                Layout.maximumWidth: Theme.inspectorWidth
                Layout.fillHeight: true
                selectedId: preview.selectedId
                preview: preview
                onSelectLayer: function (id) {
                    preview.selectedId = id
                    preview.webcamSelected = false
                }
            }
        }

        Timeline {
            id: timeline
            Layout.fillWidth: true
            Layout.preferredHeight: Style.timelineHeight
            preview: preview
        }
    }

    // Status strip: only exists while there is something to say (spec has no footer).
    // Errors and export progress surface here; the rest of the time the tray sits on
    // the window edge as in the mock.
    footer: Rectangle {
        visible: Bridge.lastError !== "" || Bridge.exportStatus.state === "running"
                 || Bridge.exportStatus.state === "done"
        height: visible ? 26 : 0
        color: Theme.bgDeep

        Rectangle { x: 0; y: 0; width: parent.width; height: 1; color: Theme.hairline }

        RowLayout {
            x: Style.pad
            y: 0
            width: parent.width - 2 * Style.pad
            height: parent.height
            spacing: 10

            Text {
                text: Bridge.lastError !== "" ? Bridge.lastError
                                              : Bridge.exportStatus.message
                color: Bridge.lastError !== "" ? Theme.live : Theme.text4
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
                elide: Text.ElideMiddle
                Layout.fillWidth: true
            }
            // 3px track, accent fill -- the slider anatomy without the thumb.
            Item {
                visible: Bridge.exportStatus.state === "running"
                Layout.preferredWidth: 240
                Layout.preferredHeight: 3
                Rectangle {
                    width: parent.width; height: 3; radius: 1
                    color: Theme.track
                }
                Rectangle {
                    width: parent.width * (Bridge.exportStatus.progress || 0)
                    height: 3; radius: 1
                    color: Theme.accent
                }
            }
            Text {
                visible: Bridge.exportStatus.state === "running"
                text: Math.round((Bridge.exportStatus.progress || 0) * 100) + "%"
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
            Text {
                visible: Bridge.exportStatus.state === "done"
                text: "export complete"
                color: Theme.accentDim
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
        }
    }

    FileDialog {
        id: exportDialog
        title: "Export to"
        fileMode: FileDialog.SaveFile
        nameFilters: ["MP4 video (*.mp4)"]
        currentFolder: "file://" + (Bridge.bundle || "")
        onAccepted: Bridge.startExport(decodeURIComponent(selectedFile.toString().replace("file://", "")))
    }

    Dialog {
        id: resetDialog
        title: "Reset all edits?"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: Bridge.op("reset", {})
        Text {
            text: "Deletes edit.json and returns to defaults.\nThe recording itself is untouched."
            color: Theme.text
        }
    }

    Shortcut { sequence: "Space"; onActivated: preview.togglePlay() }
    Shortcut { sequence: "Left"; onActivated: preview.step(-1) }
    Shortcut { sequence: "Right"; onActivated: preview.step(1) }
    // `sequences`, not `sequence`: several StandardKeys map to more than one binding
    // and Qt warns that it would otherwise register only the first.
    Shortcut { sequences: [StandardKey.Save]; onActivated: Bridge.save() }
    Shortcut {
        sequences: [StandardKey.Delete]
        onActivated: if (preview.selectedId !== "")
                         Bridge.op("delete_layer", { id: preview.selectedId })
    }

    onClosing: Bridge.quit()

    // --selftest N runs the whole app headless for N ms and reports what it managed to
    // load. The launcher uses it to verify the editor actually starts, because qml6
    // exits 0 on some load failures and the only other signal is stderr.
    Timer {
        interval: Math.max(1, Bridge.selftestMs)
        running: Bridge.selftestMs > 0
        repeat: false
        onTriggered: app.runSelftest()
    }

    property bool selftestOpDone: false
    property bool selftestGrabDone: false

    // Each step re-enters once its asynchronous half has finished, so the report is
    // written after the op has round-tripped rather than while it is still in flight.
    function runSelftest() {
        var opJson = Bridge.arg("--selftest-op", "")
        if (opJson !== "" && !selftestOpDone) {
            selftestOpDone = true
            var o = JSON.parse(opJson)
            Bridge.op(o.op, o.args, function () { app.runSelftest() })
            return
        }
        var grab = Bridge.arg("--grab", "")
        if (grab !== "" && !selftestGrabDone) {
            selftestGrabDone = true
            preview.grabStage(grab, function () {
                console.log("SELFTEST grabbed -> " + grab)
                app.runSelftest()
            })
            return
        }
        app.selftestReport(Bridge.state)
    }

    // One JSON line, because some of these values (an error message, a theme path)
    // contain spaces and a key=value line would be ambiguous to parse.
    function selftestReport(s) {
        console.log("SELFTEST " + JSON.stringify({
            connected: Bridge.connected,
            canvas: s.canvas ? (s.canvas.width + "x" + s.canvas.height) : "none",
            frames: s.source_frames,
            layers: s.layers ? s.layers.length : -1,
            clicks: s.clicks ? s.clicks.length : -1,
            webcamEditable: s.webcam ? s.webcam.editable : null,
            webcamShape: s.webcam ? s.webcam.shape : null,
            webcamRect: preview.appliedWebcamRect,
            dirty: s.dirty,
            error: Bridge.lastError,
            proxy: Bridge.proxyStatus.state,
            theme: Theme.mode,
            font: Theme.fontFamily,
            previewFit: preview.fit,
            frame: preview.frame,
            zoomScale: preview.appliedZoomScale,
            zoomX: preview.appliedZoomX,
            zoomY: preview.appliedZoomY
        }))
        Bridge.quit()
        Qt.exit(Bridge.connected ? 0 : 3)
    }

    // The self-test needs a specific frame without depending on a decoder having
    // delivered one, so it drives the scrub position directly.
    Connections {
        target: Bridge
        function onStateUpdated() {
            var f = parseInt(Bridge.arg("--frame", "-1"))
            if (f >= 0 && preview.frame !== f)
                preview.seekFrame(f)
        }
    }
}
