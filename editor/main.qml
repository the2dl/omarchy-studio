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

ApplicationWindow {
    id: app

    width: 1560
    height: 980
    visible: true
    color: Theme.background
    title: "Omarchy Studio — " + (Bridge.state.name || Bridge.bundle)

    readonly property var st: Bridge.state

    header: ToolBar {
        background: Rectangle { color: Theme.surface }
        RowLayout {
            x: Theme.pad
            y: 0
            width: parent.width - 2 * Theme.pad
            height: parent.height
            spacing: 8

            Text {
                text: app.st.name || "loading…"
                color: Theme.foreground
                font.bold: true
            }
            Text {
                text: app.st.canvas
                      ? (app.st.canvas.width + "×" + app.st.canvas.height + " · "
                         + (app.st.timebase ? app.st.timebase.fps.toFixed(2) : "?") + " fps · "
                         + app.st.source_frames + " frames")
                      : ""
                color: Theme.dim
                font.pixelSize: 12
            }
            Item { Layout.fillWidth: true }

            Rectangle {
                width: 8; height: 8; radius: 4
                color: app.st.dirty ? Theme.warning : Theme.ok
                visible: app.st.dirty !== undefined
            }
            Text {
                text: app.st.dirty ? "unsaved" : "saved"
                color: Theme.dim
                font.pixelSize: 12
            }
            Button {
                text: "Save"
                onClicked: Bridge.save()
            }
            Button {
                text: "Reset edits"
                onClicked: resetDialog.open()
            }
            Button {
                text: Bridge.exportStatus.state === "running" ? "Cancel export" : "Export…"
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
                            color: Theme.foreground
                            font.pixelSize: 18
                            Layout.alignment: Qt.AlignHCenter
                        }
                        Text {
                            text: Bridge.proxyStatus.message
                                  || "The editor never plays the 5K master: seeking it took 517-651ms "
                                   + "and half the seeks delivered no frame at all."
                            color: Theme.dim
                            font.pixelSize: 12
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                        }
                        ProgressBar {
                            Layout.fillWidth: true
                            visible: Bridge.proxyStatus.state === "building"
                            from: 0
                            to: 1
                            value: Bridge.proxyStatus.progress || 0
                        }
                    }
                }
            }

            SettingsPanel {
                id: settings
                Layout.preferredWidth: 360
                Layout.fillHeight: true
                selectedId: preview.selectedId
                onSelectLayer: function (id) {
                    preview.selectedId = id
                    preview.webcamSelected = false
                }
            }
        }

        Timeline {
            id: timeline
            Layout.fillWidth: true
            Layout.preferredHeight: 150
            preview: preview
        }
    }

    footer: ToolBar {
        background: Rectangle { color: Theme.surface }
        RowLayout {
            x: Theme.pad
            y: 0
            width: parent.width - 2 * Theme.pad
            height: parent.height
            spacing: 8

            Text {
                text: Bridge.lastError !== "" ? ("⚠ " + Bridge.lastError)
                                              : (Bridge.exportStatus.message || Theme.loadedFrom)
                color: Bridge.lastError !== "" ? Theme.danger : Theme.dim
                font.pixelSize: 12
                elide: Text.ElideMiddle
                Layout.fillWidth: true
            }
            ProgressBar {
                visible: Bridge.exportStatus.state === "running"
                Layout.preferredWidth: 240
                from: 0
                to: 1
                value: Bridge.exportStatus.progress || 0
            }
            Text {
                visible: Bridge.exportStatus.state === "done"
                text: "export complete"
                color: Theme.ok
                font.pixelSize: 12
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
            color: Theme.foreground
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
            theme: Theme.loadedFrom,
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
