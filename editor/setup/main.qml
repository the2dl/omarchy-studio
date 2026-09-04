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

    // Never shown. The bar is drawn inside the sheet (see SetupBar.qml), so this
    // window exists only to own the state, the bridge and the shortcuts -- one
    // object every sheet can reach. A second mapped window is exactly what made the
    // bar unclickable, so there is deliberately only one kind of window now.
    visible: false
    width: 1
    height: 1

    // --- bridge plumbing (the same shape as editor/Bridge.qml, minus the model) ---

    property int port: 0
    property string token: ""
    property int selftestMs: 0
    property int countdown: 3
    // --probe-input: report each control's screen rect once, then every pointer
    // position this window receives. tests/input_audit.py warps the cursor over
    // each rect and checks the report arrives, which is how "can this control be
    // clicked at all" is answered without injecting a click. Off by default -- it
    // logs on every mouse move.
    property bool probeInput: false
    // --open-picker mic|camera: open a device list at startup. A verification hook
    // like --mode, not UX -- it is the only way to put the panel on screen for a
    // test that cannot click.
    property string openPickerArg: ""
    // --camera-shape circle|corner: bring the self-view up at startup. Same class of
    // hook as --mode: a way to photograph a state that otherwise needs a click.
    property string cameraShapeArg: ""

    // The token arrives in a FILE whose path is on the command line, not as the value.
    // /proc/<pid>/cmdline is world-readable on Linux, so a token on argv is legible to
    // every local user; the file is 0600 inside the 0700 XDG_RUNTIME_DIR.
    function readTokenFile(path) {
        if (!path)
            return ""
        var xhr = new XMLHttpRequest()
        try {
            xhr.open("GET", "file://" + path, false)
            xhr.send()
            return (xhr.responseText || "").trim()
        } catch (e) {
            return ""
        }
    }

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

    // Camera is NOT a mode. It was one, and picking it built a camera:/dev/videoN
    // target that the recorder rejects outright ("camera-only recording is not
    // supported by this recorder yet") -- a dead end you could only leave by
    // picking another mode. The camera belongs where it actually acts: an overlay
    // on a screen recording, picked by device in the camera section below.
    readonly property var modeNames: ["Display", "Window", "Area"]
    readonly property var cameraModes: ["off", "circle", "rounded", "rect"]

    // The teleprompter runs as its own process (bin/omarchy-teleprompter) because it has
    // to outlive these surfaces -- they are destroyed the moment Record is pressed and
    // the script has to still be on screen after that. The bar only asks for it.
    property bool prompterOn: false
    // Where the prompter's window is, so the sheet can draw a drag proxy over it. Polled
    // rather than pushed: it changes when the user drags it or when one is raised, and a
    // 700ms poll is cheaper than a notification channel for a rect nobody watches closely.
    property var prompterRect: ({ running: false })
    function refreshPrompterRect() {
        send("GET", "/prompter", null, function (r, ok) {
            if (ok && r)
                prompterRect = r
        })
    }
    function movePrompter(x, y) {
        send("POST", "/prompter", { move: { x: x, y: y } }, function (r, ok) {
            if (ok && r)
                prompterRect = r
        })
    }
    Timer {
        interval: 700
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: win.refreshPrompterRect()
    }

    function setPrompter(on) {
        prompterOn = on
        send("POST", "/prompter", { on: on }, function (reply, ok) {
            // The launcher answers with what actually happened -- a prompter that could
            // not start, or one this compositor cannot keep out of the recording, must
            // not leave the toggle lit as if it had worked.
            if (!ok || !reply || reply.on !== on)
                prompterOn = !!(reply && reply.on)
            refreshPrompterRect()
        })
    }

    // Which device list is open on the overlay: "" | "mic" | "camera". The panel is
    // drawn there, not here, because this window is 70px tall (see DeviceChip).
    property string pickerOpen: ""
    // Left edge of the open chip in MONITOR coordinates, so the overlay can anchor
    // the panel under it. A Wayland client is not told where it sits, so this is
    // rebuilt from the same centring the launcher used to place the bar.
    property real pickerAnchor: 0

    // Chosen devices. Empty means "whatever the system default is", which is what
    // every run did before the pickers existed.
    property string micDevice: ""
    property string cameraDevice: ""

    // Where the self-view was left, in absolute logical desktop pixels. Written by the
    // SelfView on the sheet the capture is on (SelfView.report), read by record(). Null
    // until one reports, which is the "no camera" case and means the editor keeps
    // WebcamSettings' defaults.
    property var cameraRect: null

    readonly property var micEntry: entryFor(sources.mics, micDevice, "name")
    readonly property var cameraEntry: entryFor(sources.cameras, cameraDevice, "device")

    function entryFor(list, key, field) {
        if (!list || list.length === 0)
            return null
        for (var i = 0; i < list.length; ++i)
            if (list[i][field] === key)
                return list[i]
        return list[0]        // nothing picked yet, or the device vanished
    }

    function togglePicker(kind, item) {
        if (pickerOpen === kind) {
            pickerOpen = ""
            return
        }
        // Ask before showing: the heartbeat is up to two seconds stale, and the one
        // moment the user cares about the list being right is when they open it.
        refreshSources()
        // Sheet-local: the chip and the panel now live in the same window.
        pickerAnchor = item.mapToItem(null, 0, 0).x
        pickerOpen = kind
    }

    function pickMic(name) {
        micDevice = name
        pickerOpen = ""
        // Retarget the meter: a level from the old device would answer "is THIS mic
        // working?" with another mic's signal.
        send("POST", "/mic-device", { device: name }, null)
    }

    // The lists are re-fetched on a timer (see the rescan Timer below), so this has to
    // tell a real change from the heartbeat. Reassigning `sources` rebuilds every
    // Repeater bound to it, which would drop the hover ring off a card the moment the
    // user aimed at it; comparing the serialized payload keeps the UI still until
    // something actually appeared or went away. Cheap: the payload is small, flat, and
    // built in a fixed key order by the same function every time.
    property string sourcesFingerprint: ""
    property bool sourcesLoaded: false

    function refreshSources() {
        send("GET", "/sources", null, function (s, ok) {
            if (!ok)
                return
            var fp = JSON.stringify(s)
            if (fp === sourcesFingerprint)
                return
            var first = !sourcesLoaded
            sourcesFingerprint = fp
            sourcesLoaded = true
            sources = s
            // A camera unplugged while the bar is up leaves the shape armed with no
            // device to fill it, and /done refuses that config -- Start would then do
            // nothing at all, silently, because the rejection is deliberately dropped
            // (`if (!ok) return`, record()). Fall back to Off; the chip next to it
            // already reads "no camera", so the state stays legible.
            if (cameraMode > 0 && (!s.cameras || s.cameras.length === 0))
                cameraMode = 0
            // Sources arrive after the first setMode ran (the fetch is async), so
            // an empty selection is re-derived here: one display auto-selects
            // (the shipping product ships that as a fix), camera mode takes the
            // first device. viaClick=false, so this can never surprise-launch
            // the area picker. Re-derived on later passes too, which is how a
            // display or camera that appears late becomes selectable.
            if (sel === null)
                setMode(mode, false)
            // First pass only, both of them: probeReady is a one-shot handshake for
            // the input audit, and a selftest that re-pinned its window every rescan
            // would photograph a moving target.
            if (first && (probeInput || openPickerArg !== ""))
                probeDump.restart()      // rects are only real once laid out
            // Selftests cannot hover or click; give them the first window so the
            // picked state (ring, chip, Start) is photographable.
            if (first && selftestMs > 0 && mode === 1 && sel === null && s.windows.length > 0)
                sel = s.windows[0]
        })
    }

    // Hot-plug. A camera or a USB/Bluetooth mic connected AFTER this bar opened used to
    // be invisible for the life of the process -- the chip read "no camera" and the
    // shape control sat disabled beside it, because enumeration happened once, before
    // qml6 even spawned. Paused while the Area picker owns the screen (every window of
    // ours is unmapped) and once the countdown starts (the lists stop mattering, and
    // nothing should be spawning subprocesses into the take).
    Timer {
        interval: 2000
        repeat: true
        running: !win.counting && !win.picking && !win.surfacesGone
        onTriggered: win.refreshSources()
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

    // Every sheet's self-view, so record() can release the camera before capture opens
    // it. A binding on `counting` would do it too, but capture starts inside the
    // countdown and "probably evaluated in time" is not a thing to bet a take on.
    signal releaseCameras()

    function record() {
        if (sel === null || counting)
            return
        releaseCameras()
        send("POST", "/done", {
            target: sel.target,
            mic: micOn,
            mic_device: micEntry !== null ? micEntry.name : null,
            desktop_audio: desktopAudio,
            camera: cameraModes[cameraMode],
            camera_device: cameraEntry !== null ? cameraEntry.device : null,
            // Carried so the editor opens the camera where it was placed rather than
            // at the defaults. Null is legal and means "use the defaults".
            camera_rect: cameraMode > 0 ? cameraRect : null
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

    // The sheets watch this: it fires once layout has settled, which is when a
    // control's rect is real and the picker can be opened on a named chip.
    signal probeReady()

    Timer {
        id: probeDump
        interval: 400
        onTriggered: win.probeReady()
    }

    Timer { id: tick; interval: 1000; repeat: true
            onTriggered: win.countLeft = Math.max(1, win.countLeft - 1) }
    Timer { id: teardown; onTriggered: win.surfacesGone = true }
    Timer { id: quitTimer; onTriggered: Qt.quit() }

    // This bar is the recording entry point -- hotkey in, Enter out -- so the whole
    // flow must work without a mouse. Application-scoped: focus may sit on any of
    // our windows (the overlays take clicks) and the keys must land regardless.
    Shortcut {
        context: Qt.ApplicationShortcut
        sequence: "Escape"
        onActivated: if (win.pickerOpen !== "") win.pickerOpen = ""; else win.cancel()
    }
    Shortcut { context: Qt.ApplicationShortcut; sequence: "Return"; onActivated: win.record() }
    Shortcut { context: Qt.ApplicationShortcut; sequence: "Tab"
               onActivated: win.setMode((win.mode + 1) % 3, false) }
    Shortcut { context: Qt.ApplicationShortcut; sequence: "Backtab"
               onActivated: win.setMode((win.mode + 2) % 3, false) }
    Shortcut { context: Qt.ApplicationShortcut; sequence: "Right"; onActivated: win.cycleSource(1) }
    Shortcut { context: Qt.ApplicationShortcut; sequence: "Left"; onActivated: win.cycleSource(-1) }

    function cycleSource(step) {
        var list = mode === 0 ? sources.monitors
                 : mode === 1 ? sources.windows
                 : (area !== null ? [area] : [])
        if (list.length === 0)
            return
        var current = -1
        for (var i = 0; i < list.length; ++i)
            if (sel !== null && sel.target === list[i].target)
                current = i
        var next = list[(current + step + list.length) % list.length]
        sel = mode === 0 ? monitorSel(next) : next
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
        token = readTokenFile(arg("--token-file", "")) || arg("--token", "")
        selftestMs = parseInt(arg("--selftest", "0"))
        probeInput = arg("--probe-input", "") !== ""
        openPickerArg = arg("--open-picker", "")
        cameraShapeArg = arg("--camera-shape", "")
        if (cameraShapeArg !== "")
            cameraMode = Math.max(1, cameraModes.indexOf(cameraShapeArg))
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
}
