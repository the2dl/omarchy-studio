// omarchy-studio editor: window, layout, and the two things that leave the process
// (save and export).
//
// Launched by bin/omarchy-studio, which starts the Python bridge first and passes
// --port/--token. Run it with /usr/bin/qml6: plain `qml` on PATH is Qt 5.15 and fails
// with "Did not load any objects, exiting" and no other explanation.
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "controls" as C
import "shell" as S

ApplicationWindow {
    id: app

    width: 1560
    height: 880    // the mock's editor frame (§1d: 1560×880)
    visible: true
    color: Theme.bg
    title: "Omarchy Studio — " + (Bridge.state.name || Bridge.bundle)

    readonly property var st: Bridge.state

    // 1f is a pane, not a modal (spec §4): while open it replaces the inspector's
    // content in the same 268px column, so a render can keep going beside live work.
    property bool exportOpen: false

    // Preview mode (spec §1d region 1, "Preview"): show exactly what will export.
    // Every piece of editing chrome -- rings, handles, chips, the redaction hatch and
    // label, the zoom-region scrim, the rubber band -- binds to this and disappears;
    // the composited pixels (including the real redaction blur) do not change at all.
    // Scrubbing and playback stay live, because checking timing is the main reason to
    // be in this mode. `P` toggles, Escape leaves.
    property bool previewMode: false
    // The layer list's disclosure. Kept on the app rather than inside the rail so a
    // shortcut and the rail button drive one value.
    property bool layersOpen: false

    // --select <layer id> [--layers]: open on a layer already selected, and optionally
    // with the list out. A verification hook of the same class as --selftest-op and
    // --grab, and for the same reason those exist -- selection is UI state with no
    // route through the bridge, so it is the only way to put a layer inspector on
    // screen for a check that cannot click.
    property bool startupSelectionDone: false
    function applyStartupSelection() {
        if (startupSelectionDone)
            return
        var want = Bridge.arg("--select", "")
        if (want === "")
            return
        var layers = Bridge.state.layers || []
        for (var i = 0; i < layers.length; ++i) {
            if (layers[i].id === want) {
                startupSelectionDone = true
                preview.selectedId = want
                preview.webcamSelected = false
                if (Bridge.arg("--layers", "") !== "")
                    app.layersOpen = true
                return
            }
        }
    }
    Connections {
        target: Bridge
        function onStateChanged() { app.applyStartupSelection() }
    }

    // 2e: the proxy build. The timeline (another agent's file) binds the same
    // Bridge.proxyStatus singleton -- .state === "building" and .progress (0..1) -- to
    // fill its thumbnail cells left to right; nothing here needs to be passed down.
    readonly property bool proxyBuilding: Bridge.proxyStatus.state === "building"

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

    // The undo stack lives in the bridge (one snapshot per op, drags coalesced to a
    // single step server-side); these routes return the whole new state, which apply()
    // then fans out to every binding. Not guarded on canUndo here: the server answers
    // an empty stack with the unchanged state, so posting is always safe and the reply
    // re-syncs a client whose state has gone stale. canUndo/canRedo drive only the
    // buttons' disabled look.
    function undo() {
        Bridge.send("POST", "/undo", {}, function (s, ok) { if (ok) Bridge.apply(s) })
    }
    function redo() {
        Bridge.send("POST", "/redo", {}, function (s, ok) { if (ok) Bridge.apply(s) })
    }

    // True while an editable text item owns focus -- the layer inspectors have text
    // fields, and Ctrl+Z / P there must edit the field, not the project. cursorPosition
    // exists on TextInput/TextEdit and nothing else in this chrome.
    function typing() {
        var f = app.activeFocusItem
        // qmllint disable missing-property
        // Deliberate duck-typing, and the ONLY missing-property this project allows:
        // activeFocusItem is an Item, cursorPosition exists on TextInput/TextEdit and
        // nothing else in this chrome, and the check IS "does this member exist". The
        // suppression is here rather than in an allowlist beside the linter so it
        // cannot outlive the reason for it.
        return f !== null && f !== undefined && f.cursorPosition !== undefined
               && f.readOnly !== true
        // qmllint enable missing-property
    }

    // Top bar, 46px (spec §1d region 1): file glyph, name, duration/format in text6,
    // then the actions -- undo/redo glyphs, Preview, Export (primary), per the mock,
    // plus Save/Reset which the mock leaves implicit.
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

            // Once a render starts the pane can be dismissed; progress collapses into
            // this chip (spec §4, drawn in mock 2a). Clicking it reopens the pane.
            S.RenderChip {
                visible: Bridge.exportStatus.state === "running" && !app.exportOpen
                progress: Bridge.exportStatus.progress || 0
                onClicked: app.exportOpen = true
            }

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

            // Undo/redo, mock order: glyphs, divider, Preview, Export. Disabled rather
            // than hidden when the stack is empty -- a control that vanishes moves
            // everything beside it (the mock draws empty redo at text6, still there).
            S.GlyphButton {
                glyph: ""        // nf-fa-undo
                enabled: app.st.canUndo === true
                onClicked: app.undo()
            }
            S.GlyphButton {
                glyph: ""        // nf-fa-repeat, the mock's redo arrow
                enabled: app.st.canRedo === true
                onClicked: app.redo()
            }
            Rectangle { width: 1; height: 16; color: Theme.hairline }

            S.GhostToggle {
                text: "Preview"
                active: app.previewMode
                // Spec §2e: while the proxy builds, Preview renders text-6, no fill.
                dim: app.proxyBuilding
                onClicked: app.previewMode = !app.previewMode
            }

            // While the proxy builds, Export demotes to text-6 with no fill (spec §2e:
            // the shell is real, only playback is not -- nothing on this bar should
            // outshine the build). It still opens the pane: the render reads the
            // master, so exporting during the build is legal, just not invited.
            Item {
                visible: app.proxyBuilding
                width: dimExport.implicitWidth + 20
                height: 28
                Text {
                    id: dimExport
                    anchors.centerIn: parent
                    text: "Export"
                    color: Theme.text6
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: app.exportOpen = !app.exportOpen
                }
            }
            C.PrimaryButton {
                visible: !app.proxyBuilding
                text: "Export"
                onClicked: app.exportOpen = !app.exportOpen
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
                            { id: "layers",   glyph: "\uf5fd", tip: "Layers" },        // nf-fa-layer_group
                            { id: "blur",     glyph: "", tip: "Blur box" },      // nf-fa-eye_slash
                            { id: "pixelate", glyph: "", tip: "Pixelate" },      // nf-fa-th
                            { id: "text",     glyph: "", tip: "Text" }           // nf-fa-font
                        ]
                        C.RailButton {
                            glyph: modelData.glyph
                            tip: modelData.tip
                            // `layers` is a panel, not a canvas tool: it toggles the
                            // list open and leaves the pointer alone, so it lights from
                            // its own state rather than from preview.tool.
                            active: modelData.id === "layers" ? app.layersOpen
                                                              : preview.tool === modelData.id
                            onClicked: {
                                if (modelData.id === "layers") {
                                    app.layersOpen = !app.layersOpen
                                    return
                                }
                                // Picking a tool is an editing gesture; in preview mode
                                // the rubber band is inert, so leave the mode rather
                                // than light a tool that cannot draw.
                                app.previewMode = false
                                preview.tool = modelData.id
                            }
                        }
                    }
                }
            }

            // Spec §2a: the layers tool slides a 236px list out beside the rail, and
            // the canvas and inspector shrink to fit. The component was written and
            // never mounted -- there was no rail tool to open it, so the entire layer
            // list was unreachable from the running app.
            LayerList {
                id: layerList
                Layout.preferredWidth: app.layersOpen ? implicitWidth : 0
                Layout.maximumWidth: Layout.preferredWidth
                Layout.fillHeight: true
                visible: Layout.preferredWidth > 0
                clip: true
                preview: preview
                selectedId: preview.selectedId
                onSelectLayer: function (id) {
                    preview.selectedId = id
                    preview.webcamSelected = false
                }
                Behavior on Layout.preferredWidth {
                    NumberAnimation { duration: Theme.durSlow; easing.type: Easing.OutCubic }
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
                    previewMode: app.previewMode
                }

                // 2e -- the proxy build. Not a scrim: the shell is real and the canvas
                // stays what it is (Preview draws the first frame at 35% behind this);
                // the overlay adds only the 320px progress block from the mock.
                S.ProxyBuildOverlay {
                    x: 0
                    y: 0
                    width: parent.width
                    height: parent.height
                }
            }

            // The inspector slot: one 268px column (spec §1d region 4) whose content
            // swaps between the contextual inspector and the export pane (spec §4:
            // "1f's card becomes the right inspector's content in the same column").
            // A plain Item rather than Layout props on SettingsPanel itself so the
            // swap never rebuilds the panel and its scroll position survives.
            Item {
                id: inspectorSlot
                // Capped as well as preferred: without a maximum, one over-wide control
                // inside pushes the panel past the window edge and clips itself.
                Layout.preferredWidth: Theme.inspectorWidth
                Layout.maximumWidth: Theme.inspectorWidth
                Layout.fillHeight: true

                SettingsPanel {
                    id: settings
                    // 268px in the editor; the 320px width is for standalone panels.
                    x: 0
                    y: 0
                    width: parent.width
                    height: parent.height
                    visible: !app.exportOpen
                    selectedId: preview.selectedId
                    preview: preview
                    onSelectLayer: function (id) {
                        preview.selectedId = id
                        preview.webcamSelected = false
                    }
                }

                S.ExportPane {
                    x: 0
                    y: 0
                    width: parent.width
                    height: parent.height
                    visible: app.exportOpen
                    onClosed: app.exportOpen = false
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
    // Live render progress moved to the export pane and the top-bar chip (spec §4),
    // so the strip no longer duplicates it; it keeps errors and the completion line,
    // which need to surface even with the pane dismissed.
    footer: Rectangle {
        visible: Bridge.lastError !== "" || Bridge.exportStatus.state === "done"
                 || Bridge.exportStatus.state === "error"
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
            Text {
                visible: Bridge.exportStatus.state === "done"
                text: "export complete"
                color: Theme.accentDim
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
        }
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
        // Guarded by previewMode: with the ring hidden there is no visible selection,
        // and deleting an invisible selection is how work gets lost.
        onActivated: if (!app.previewMode && preview.selectedId !== "")
                         Bridge.op("delete_layer", { id: preview.selectedId })
    }
    // Split the camera segment under the playhead, which is how a head that is on for
    // the whole take becomes one that goes away. Deleting the right-hand half is then
    // the second half of the gesture, and Delete above already does that.
    //
    // Not guarded by previewMode the way Delete is: a split adds a seam rather than
    // removing anything, so doing it with no visible selection loses nothing.
    Shortcut {
        sequence: "S"
        onActivated: if (!app.typing())
                         Bridge.op("split_webcam", { at_ms: preview.frame * preview.msPerFrame })
    }
    // The typing() guards: a plain letter or Ctrl+Z in an inspector text field belongs
    // to the field. Qt's ShortcutOverride usually arbitrates this, but the guard makes
    // it a rule rather than platform behaviour.
    Shortcut {
        sequence: "P"
        onActivated: if (!app.typing()) app.previewMode = !app.previewMode
    }
    Shortcut {
        sequence: "L"
        onActivated: if (!app.typing()) app.layersOpen = !app.layersOpen
    }
    Shortcut {
        sequence: "Escape"
        // An armed draw tool first: it is the more modal of the two states and the one
        // a person is more likely to want out of, and leaving preview mode while a
        // rubber band is armed would drop them into a canvas that draws boxes.
        onActivated: {
            if (preview.tool !== "select")
                preview.tool = "select"
            else if (app.previewMode)
                app.previewMode = false
        }
    }
    Shortcut {
        sequences: [StandardKey.Undo]     // Ctrl+Z
        onActivated: if (!app.typing()) app.undo()
    }
    Shortcut {
        sequences: [StandardKey.Redo]     // Ctrl+Shift+Z on this platform
        onActivated: if (!app.typing()) app.redo()
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
            // Grabs are taken in preview mode: the parity harness compares the grab
            // against an ffmpeg render, and preview mode is by definition the frame
            // with no editing chrome -- the same frame the user checks by pressing P.
            // This replaces the old selftestMs gate on the redaction marker, so the
            // test measures a state a user can actually see.
            app.previewMode = true
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
            previewMode: app.previewMode,
            previewFit: preview.fit,
            frame: preview.frame,
            outFrame: preview.outFrame,
            timelineFrame: preview.timelineFrame,
            outputFrames: preview.outputFrames,
            inPad: preview.inPad,
            padNow: preview.padNow,
            padFrame: preview.padFrame,
            videoVisible: zoomedVisible(),
            zoomScale: preview.appliedZoomScale,
            zoomX: preview.appliedZoomX,
            zoomY: preview.appliedZoomY
        }))
        Bridge.quit()
        Qt.exit(Bridge.connected ? 0 : 3)
    }

    // The self-test needs a specific frame without depending on a decoder having
    // delivered one, so it drives the scrub position directly.
    // Whether the RECORDED picture is on screen. In a pad it must not be: the export
    // shows the pad ground there, and a preview showing frame 0 held under a title card
    // is the editor disagreeing with the video again.
    function zoomedVisible() {
        return !preview.inPad
    }

    Connections {
        target: Bridge
        function onStateUpdated() {
            // OUTPUT frames, so a test can park inside a pad -- the whole point of
            // the pads being addressable at all. preview.frame is the SOURCE frame and
            // is held at 0 through a head pad, so comparing against it would re-seek
            // forever at output frame 0..head.
            var f = parseInt(Bridge.arg("--frame", "-1"))
            if (f >= 0 && preview.outFrame !== f)
                preview.seekFrame(f)
        }
    }
}
