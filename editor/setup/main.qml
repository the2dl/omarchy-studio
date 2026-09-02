// The pre-record setup bar: a single-row pill, bottom-centre, holding source MODE
// (Display / Window / Area / Camera), the mic check, system audio, and the camera
// overlay shape. Record does NOT live here -- the reference design moved it
// onto the picked target itself in 2023, so Start is drawn by Overlay.qml in the
// centre of whatever is selected, where it doubles as confirmation you picked the
// right thing. This window plus one Overlay per monitor are one qml6 process.
//
// Launched by bin/omarchy-capture-setup, which passes --port/--token for the
// loopback bridge and positions all windows via Hyprland dispatches keyed on their
// titles. Pure view: sources and mic levels arrive resolved from Python, and the
// only things sent back are the user's choices. Nothing here records -- Start POSTs
// the configuration; the launcher prints it and the recorder takes over.
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import ".."             // Theme/Style singletons via editor/qmldir, same as controls/
import "../controls" as C

ApplicationWindow {
    id: win

    // The pill sizes itself from its row; ~70px tall per the shipping product's
    // bar. Fixed min=max keeps Hyprland treating it as a floating fixed panel.
    // min/max bind to barW, not to `width` itself -- that binding loops through
    // the compositor's clamp and Qt gives up, collapsing the window to nothing.
    readonly property int barW: Math.ceil(barRow.implicitWidth) + 36
    width: barW
    height: 70
    minimumWidth: barW
    maximumWidth: barW
    minimumHeight: 70
    maximumHeight: 70
    visible: !counting && !picking
    title: "omarchy-setup-bar"     // the launcher's positioning key; not user-visible
    flags: Qt.FramelessWindowHint
    color: "transparent"

    // --- bridge plumbing (the same shape as editor/Bridge.qml, minus the model) ---

    property int port: 0
    property string token: ""
    property int selftestMs: 0
    property int countdown: 3

    function arg(name, fallback) {
        var a = Qt.application.arguments
        for (var i = 0; i < a.length - 1; ++i)
            if (a[i] === name)
                return a[i + 1]
        return fallback
    }

    function send(method, path, body, cb) {
        var x = new XMLHttpRequest()
        x.onreadystatechange = function () {
            if (x.readyState !== XMLHttpRequest.DONE)
                return
            var reply = null
            try { reply = JSON.parse(x.responseText) } catch (e) {}
            if (cb)
                cb(reply, x.status === 200)
        }
        x.open(method, "http://127.0.0.1:" + port + path)
        x.setRequestHeader("X-Studio-Token", token)
        x.setRequestHeader("Content-Type", "application/json")
        x.send(body === undefined || body === null ? "" : JSON.stringify(body))
    }

    // --- state -------------------------------------------------------------------

    property var sources: ({ monitors: [], windows: [], cameras: [], mic: null, focused: "" })
    property int mode: 0                      // Display / Window / Area / Camera
    property var sel: null                    // the chosen source object (kind:...)
    property var area: null                   // the Area mode's picked region
    property bool picking: false              // all windows hidden, slurp on screen
    property bool micOn: true
    property bool desktopAudio: false
    property int cameraMode: 0                // Off / Circle / Corner
    property real micLevel: 0
    property real micDb: -120
    property bool counting: false             // Start pressed; countdown running
    property int countLeft: 0
    property bool surfacesGone: false         // last pre-capture teardown fired

    readonly property var modeNames: ["Display", "Window", "Area", "Camera"]
    readonly property var cameraModes: ["off", "circle", "corner"]
    readonly property string cameraDevice:
        sources.cameras.length > 0
            ? (sel !== null && sel.kind === "camera" ? sel.device : sources.cameras[0].device)
            : ""
    readonly property bool cameraTarget: sel !== null && sel.kind === "camera"

    function refreshSources() {
        send("GET", "/sources", null, function (s, ok) {
            if (!ok)
                return
            sources = s
            // Sources arrive after the first setMode ran (the fetch is async), so
            // an empty selection is re-derived here: one display auto-selects
            // (the shipping product ships that as a fix), camera mode takes the
            // first device. viaClick=false, so this can never surprise-launch
            // the area picker.
            if (sel === null)
                setMode(mode, false)
            // Selftests cannot hover or click; give them the first window so the
            // picked state (ring, chip, Start) is photographable.
            if (selftestMs > 0 && mode === 1 && sel === null && s.windows.length > 0)
                sel = s.windows[0]
        })
    }

    function monitorSel(m) {
        return { kind: "monitor", name: m.name, target: m.target,
                 x: m.x, y: m.y, width: m.width, height: m.height,
                 refresh: m.refresh, label: "Display " + (sources.monitors.indexOf(m) + 1) }
    }

    // Mode switches reset the selection to that mode's sensible default, so Enter
    // always acts on what the overlays are showing.
    function setMode(m, viaClick) {
        mode = m
        if (m === 0)
            sel = sources.monitors.length === 1 ? monitorSel(sources.monitors[0]) : null
        else if (m === 1)
            sel = null
        else if (m === 2) {
            sel = area
            // Clicking Area goes straight into the picker -- one less click on the
            // main path. Tab-cycling must NOT: surprise-freezing the screen while
            // walking modes with the keyboard would read as a crash.
            if (viaClick && area === null && !picking)
                pickArea()
        } else {
            sel = sources.cameras.length > 0
                ? { kind: "camera", name: sources.cameras[0].name,
                    device: sources.cameras[0].device,
                    target: "camera:" + sources.cameras[0].device }
                : null
        }
    }

    function pickArea() {
        // The picker freezes whatever is on screen, so every window of ours must
        // be gone first; the bridge waits 300ms after the POST before slurp starts.
        picking = true
        send("POST", "/area", {}, function (r, ok) {
            picking = false
            if (!ok || !r || r.cancelled)
                return
            if (r.target.indexOf("monitor:") === 0) {
                // A bare click on a monitor in the picker IS a display pick; show
                // it as one so the overlay rings the right thing.
                var name = r.target.substring(8)
                for (var i = 0; i < sources.monitors.length; ++i)
                    if (sources.monitors[i].name === name) {
                        mode = 0
                        sel = monitorSel(sources.monitors[i])
                        return
                    }
                return
            }
            area = { kind: "area", target: r.target, label: r.width + "×" + r.height,
                     x: r.x, y: r.y, width: r.width, height: r.height }
            sel = area
        })
    }

    function record() {
        if (sel === null || counting)
            return
        send("POST", "/done", {
            target: sel.target,
            mic: micOn,
            desktop_audio: desktopAudio,
            camera: cameraTarget ? "off" : cameraModes[cameraMode],
            camera_device: cameraDevice !== "" ? cameraDevice : null
        }, function (r, ok) {
            if (!ok)
                return          // rejected config; everything stays up
            // The launcher prints the contract line NOW (countdown > 0), so the
            // recorder is already initialising while we count. Everything except
            // the count itself disappears immediately; the count dies 400ms
            // before the countdown ends so no setup pixel can reach a kept frame.
            if (win.countdown <= 0) {
                surfacesGone = true
                counting = true      // hides the bar and every overlay
                quitTimer.interval = 400
                quitTimer.start()
                return
            }
            counting = true
            countLeft = win.countdown
            tick.start()
            teardown.interval = win.countdown * 1000 - 400
            teardown.start()
            quitTimer.interval = win.countdown * 1000
            quitTimer.start()
        })
    }

    function cancel() {
        if (counting)
            return              // the handoff already happened; too late to unwind
        send("POST", "/cancel", {}, function () { Qt.quit() })
    }

    Timer { id: tick; interval: 1000; repeat: true
            onTriggered: win.countLeft = Math.max(1, win.countLeft - 1) }
    Timer { id: teardown; onTriggered: win.surfacesGone = true }
    Timer { id: quitTimer; onTriggered: Qt.quit() }

    // This bar is the recording entry point -- hotkey in, Enter out -- so the whole
    // flow must work without a mouse. Application-scoped: focus may sit on any of
    // our windows (the overlays take clicks) and the keys must land regardless.
    Shortcut { context: Qt.ApplicationShortcut; sequence: "Escape"; onActivated: win.cancel() }
    Shortcut { context: Qt.ApplicationShortcut; sequence: "Return"; onActivated: win.record() }
    Shortcut { context: Qt.ApplicationShortcut; sequence: "Tab"
               onActivated: win.setMode((win.mode + 1) % 4, false) }
    Shortcut { context: Qt.ApplicationShortcut; sequence: "Backtab"
               onActivated: win.setMode((win.mode + 3) % 4, false) }
    Shortcut { context: Qt.ApplicationShortcut; sequence: "Right"; onActivated: win.cycleSource(1) }
    Shortcut { context: Qt.ApplicationShortcut; sequence: "Left"; onActivated: win.cycleSource(-1) }

    function cycleSource(step) {
        var list = mode === 0 ? sources.monitors
                 : mode === 1 ? sources.windows
                 : mode === 2 ? (area !== null ? [area] : [])
                 : sources.cameras
        if (list.length === 0)
            return
        var current = -1
        for (var i = 0; i < list.length; ++i) {
            var t = mode === 3 ? "camera:" + list[i].device : list[i].target
            if (sel !== null && sel.target === t)
                current = i
        }
        var next = list[(current + step + list.length) % list.length]
        if (mode === 0)
            sel = monitorSel(next)
        else if (mode === 3)
            sel = { kind: "camera", name: next.name, device: next.device,
                    target: "camera:" + next.device }
        else
            sel = next
    }

    Timer {   // the "is my mic actually working" check; 8 polls/s is plenty
        interval: 120
        repeat: true
        running: !win.counting && win.micOn && win.sources.mic !== null
        onTriggered: win.send("GET", "/mic", null, function (r, ok) {
            if (ok && r) { win.micLevel = r.level; win.micDb = r.db }
        })
    }

    Timer {   // --selftest MS: open, render, exit as cancelled; used by CI eyes
        interval: win.selftestMs > 0 ? win.selftestMs : 1
        running: win.selftestMs > 0
        onTriggered: Qt.quit()
    }

    property int selftestRecordMs: 0
    Timer {   // --selftest-record MS: press Start for real, countdown included
        interval: win.selftestRecordMs > 0 ? win.selftestRecordMs : 1
        running: win.selftestRecordMs > 0
        onTriggered: win.record()
    }

    Component.onCompleted: {
        port = parseInt(arg("--port", "0"))
        token = arg("--token", "")
        selftestMs = parseInt(arg("--selftest", "0"))
        selftestRecordMs = parseInt(arg("--selftest-record", "0"))
        countdown = parseInt(arg("--countdown", "3"))
        send("GET", "/theme", null, function (t, ok) { if (ok) Theme.load(t) })
        refreshSources()
        var m = parseInt(arg("--mode", "0"))
        if (m > 0)
            setMode(m, false)
    }

    // One transparent, monitor-sized overlay per output: the in-place picker and
    // the home of the Start button and the countdown.
    Instantiator {
        model: win.sources.monitors
        delegate: MonitorOverlay { app: win; mon: modelData; monIndex: index }
    }

    // --- the bar -----------------------------------------------------------------

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusPanel
        // bg-float without its blur would bleed the desktop into the controls
        // (verified in a grab of the earlier modal); solid bg is the honest form.
        color: Theme.bg

        RowLayout {
            id: barRow
            anchors.centerIn: parent
            spacing: 14

            Row {   // mode chips (register of the mock's tabs: 7x13 padding chips)
                spacing: 4
                Repeater {
                    model: win.modeNames
                    delegate: Rectangle {
                        required property int index
                        required property var modelData
                        width: modeLabel.implicitWidth + 26
                        height: 27
                        radius: Theme.radiusRow
                        color: index === win.mode ? Theme.fillHover : "transparent"
                        Behavior on color { ColorAnimation { duration: Theme.durFast } }
                        Text {
                            id: modeLabel
                            anchors.centerIn: parent
                            text: modelData
                            color: index === win.mode ? Theme.text : Theme.text4
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsRow
                            Behavior on color { ColorAnimation { duration: Theme.durFast } }
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: win.setMode(parent.index, true)
                        }
                    }
                }
            }

            Rectangle { width: 1; height: 26; color: Theme.hairline }

            Text {   // mic glyph doubles as the state light
                text: "\uf130"
                color: win.micOn && win.sources.mic !== null ? Theme.accent : Theme.text4
                font.family: Theme.fontFamily
                font.pixelSize: 17
            }
            MicMeter {
                Layout.preferredWidth: 130
                level: win.micLevel
                active: win.micOn && win.sources.mic !== null
            }
            Text {
                text: win.sources.mic === null ? "no mic"
                      : win.micOn ? (Math.round(Math.max(-60, win.micDb)) + " dB")
                      : "muted"
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
                Layout.preferredWidth: 44
                horizontalAlignment: Text.AlignRight
            }
            C.Toggle {
                checked: win.micOn
                enabled: win.sources.mic !== null
                onToggled: function (v) { win.micOn = v }
            }

            Rectangle { width: 1; height: 26; color: Theme.hairline }

            Text {   // system audio
                text: "\uf028"
                color: win.desktopAudio ? Theme.accent : Theme.text4
                font.family: Theme.fontFamily
                font.pixelSize: 16
            }
            C.Toggle {
                checked: win.desktopAudio
                onToggled: function (v) { win.desktopAudio = v }
            }

            Rectangle { width: 1; height: 26; color: Theme.hairline }

            Text {   // camera overlay shape
                text: "\uf03d"
                color: !win.cameraTarget && win.cameraMode > 0 ? Theme.accent : Theme.text4
                font.family: Theme.fontFamily
                font.pixelSize: 16
            }
            C.Segmented {
                Layout.preferredWidth: 186
                model: ["Off", "Circle", "Corner"]
                currentIndex: win.cameraTarget ? 0 : win.cameraMode
                enabled: win.sources.cameras.length > 0 && !win.cameraTarget
                onActivated: function (i) { win.cameraMode = i }
            }
        }
    }
}
