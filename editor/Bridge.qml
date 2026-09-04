// The editor's connection to lib/omarchy_studio/qmlbridge.py.
//
// This file is deliberately dumb: it moves JSON, and it never computes anything about
// the project. Every rectangle, every zoom transform and every frame index in the UI
// arrives already resolved by geometry.py, because a second implementation in
// JavaScript would drift from the export and the drift is invisible until someone
// diffs rendered frames.
//
// Measured: 100 sequential round trips in 27ms (0.27ms each), so a drag can push an
// intent per frame and read back the authoritative answer without any local guessing.
pragma Singleton
import QtQuick

QtObject {
    id: bridge

    property int port: 0
    property string token: ""
    property string bundle: ""
    property int selftestMs: 0

    property var state: ({})
    property var proxyStatus: ({ state: "unknown", progress: 0, message: "" })
    property var exportStatus: ({ state: "idle", progress: 0, message: "" })
    property bool connected: false
    property string lastError: ""

    signal stateUpdated()

    function arg(name, fallback) {
        var a = Qt.application.arguments
        for (var i = 0; i < a.length - 1; ++i)
            if (a[i] === name)
                return a[i + 1]
        return fallback
    }

    // The token arrives in a FILE whose path is on the command line, not as the value
    // itself. /proc/<pid>/cmdline is world-readable on Linux -- verified on this
    // machine, no hidepid -- so a token on argv is legible to every local user, and the
    // bridges it guards can stop a recording, rewrite the teleprompter mid-take, and
    // (the HUD's `discard`) rmtree the bundle out of ~/Videos. The file is 0600 inside
    // the 0700 XDG_RUNTIME_DIR, so its contents cross no user boundary; the path being
    // public costs nothing.
    function readTokenFile(path) {
        if (!path)
            return ""
        var xhr = new XMLHttpRequest()
        try {
            xhr.open("GET", "file://" + path, false)   // sync: nothing may run before it
            xhr.send()
            return (xhr.responseText || "").trim()
        } catch (e) {
            return ""
        }
    }

    function url(path) {
        return "http://127.0.0.1:" + port + path
    }

    function send(method, path, body, cb) {
        var x = new XMLHttpRequest()
        x.onreadystatechange = function () {
            if (x.readyState !== XMLHttpRequest.DONE)
                return
            var reply = null
            try {
                reply = JSON.parse(x.responseText)
            } catch (e) {
                reply = null
            }
            if (x.status === 200) {
                lastError = ""
                connected = true
                if (cb)
                    cb(reply, true)
                return
            }
            // A rejected intent is normal -- a burned-in webcam, a cut past the end --
            // and the bridge sends the unchanged state back with it so the UI can snap
            // back to the truth instead of keeping the value the user just tried.
            lastError = reply && reply.error ? reply.error : ("bridge error " + x.status)
            if (reply && reply.state)
                apply(reply.state)
            // The callback runs either way: a caller waiting on a reply must not be left
            // waiting forever because the intent was refused.
            if (cb)
                cb(reply, false)
        }
        x.open(method, url(path))
        x.setRequestHeader("X-Studio-Token", token)
        x.setRequestHeader("Content-Type", "application/json")
        x.send(body === undefined || body === null ? "" : JSON.stringify(body))
    }

    function apply(s) {
        // An omitted zoom_track means "unchanged": drag replies leave it out so the
        // socket does not carry tens of thousands of samples per second.
        if (s.zoom_track === undefined && state.zoom_track !== undefined)
            s.zoom_track = state.zoom_track
        state = s
        if (s.proxy)
            proxyStatus = s.proxy
        if (s["export"])
            exportStatus = s["export"]   // "export" is a JS keyword; index it
        stateUpdated()
    }

    function refresh() {
        send("GET", "/state", null, function (s, ok) { if (ok) apply(s) })
    }

    // theme.py resolves every mix, alpha and contrast decision; QML applies the answer
    // and derives nothing, which is the same rule the geometry seam follows.
    function loadTheme() {
        send("GET", "/theme", null, function (t, ok) { if (ok) Theme.load(t) })
    }

    // The backdrop catalogue. Fetched once and held, on its own route rather than in
    // /state, because /state is re-serialized on every drag frame and the catalogue
    // never changes for the life of the process.
    property var backgrounds: ({ custom: "custom", entries: [] })
    function loadBackgrounds() {
        send("GET", "/backgrounds", null, function (b, ok) { if (ok) backgrounds = b })
    }

    function op(name, args, cb) {
        send("POST", "/op", { op: name, args: args || {} }, function (s, ok) {
            if (ok)
                apply(s)
            if (cb)
                cb(s, ok)
        })
    }

    function save(cb) {
        send("POST", "/save", {}, function (s, ok) {
            if (ok)
                apply(s)
            if (cb)
                cb(s, ok)
        })
    }

    function startExport(output) {
        send("POST", "/export", { output: output || null }, function (s, ok) {
            if (ok)
                exportStatus = s
        })
    }

    function cancelExport() {
        send("POST", "/export/cancel", {}, function (s, ok) {
            if (ok)
                exportStatus = s
        })
    }

    function quit() {
        send("POST", "/quit", {}, null)
    }

    readonly property bool busy: proxyStatus.state === "building"
                                 || exportStatus.state === "running"

    // Polls only the two small status endpoints. Polling /state instead would drag the
    // whole resolved project (and its zoom track) across twice a second for a number.
    property Timer poller: Timer {
        interval: 400
        repeat: true
        running: bridge.busy
        onTriggered: {
            bridge.send("GET", "/proxy", null, function (s, ok) {
                if (!ok)
                    return
                var wasBuilding = bridge.proxyStatus.state === "building"
                bridge.proxyStatus = s
                if (wasBuilding && s.state === "ready")
                    bridge.refresh()   // the proxy URLs only exist once the files do
            })
            if (bridge.exportStatus.state === "running")
                bridge.send("GET", "/export", null, function (s, ok) { if (ok) bridge.exportStatus = s })
        }
    }

    Component.onCompleted: {
        port = parseInt(arg("--port", "0"))
        // --token is still read as a fallback so an older launcher keeps working, but
        // nothing in this tree passes it any more.
        token = readTokenFile(arg("--token-file", "")) || arg("--token", "")
        bundle = arg("--bundle", "")
        selftestMs = parseInt(arg("--selftest", "0"))
        loadTheme()
        loadBackgrounds()
        refresh()
    }
}
