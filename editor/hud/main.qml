// The recording HUD (spec §1c): a floating pill, bottom centre, 26px from the edge.
//
// WHY IT EXISTS. Before this, a recording started from a terminal could only be stopped
// from that terminal, a global hotkey, or the bar indicator -- and the terminal is the
// one of those you cannot see, because the thing you are recording is usually on top of
// it. Worse, the launcher leaves the shell's foreground process group pointing at its
// own dead pgid, so the terminal it was started from stops accepting input entirely.
// A HUD makes the stop button a thing on screen, which is what every recorder that is
// pleasant to use has.
//
// IT MUST NOT BE IN THE RECORDING. bin/omarchy-recording-hud marks it `no_screen_share`,
// which on the patched compositor (contrib/hyprland-screenshare-exclude) means absent
// from the frames rather than a black rectangle over them. That is the same mechanism
// the camera self-view and the teleprompter use, and the HUD checks it landed rather
// than assuming, because a stop button burned into every take is not recoverable.
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import ".."
import "../controls" as C
import "../shell" as S

ApplicationWindow {
    id: win

    property int port: 0
    property string token: ""

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
            if (a[i] === "--" + name)
                return a[i + 1]
        return fallback
    }

    visible: true
    flags: Qt.Window | Qt.FramelessWindowHint
    color: "transparent"
    title: "omarchy-studio-recording-hud"
    // Bound AND floored: a frameless window under a floating window rule takes the size
    // it asks for at map time, and the pill grew after that -- the mic meter appears
    // when parec connects, a second or so in, which pushed Stop off the right edge of a
    // window that had already been sized without it.
    width: pill.implicitWidth + 4
    minimumWidth: pill.implicitWidth + 4
    height: 56

    property real elapsed: 0
    property real micLevel: 0
    property bool micAlive: false
    property bool paused: false
    property bool stopping: false
    property bool confirmingDiscard: false

    function clock(s) {
        var t = Math.max(0, Math.floor(s))
        var m = Math.floor(t / 60)
        return (m < 10 ? "0" : "") + m + ":" + ("0" + (t % 60)).slice(-2)
    }

    function act(name) {
        var xhr = new XMLHttpRequest()
        xhr.open("POST", "http://127.0.0.1:" + win.port + "/action")
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.setRequestHeader("X-Studio-Token", win.token)
        xhr.send(JSON.stringify({ action: name }))
    }

    // Polled rather than pushed: the elapsed time and the mic level both live in the
    // launcher, and a 10Hz poll of two numbers costs less than keeping a socket open
    // through a recording that may run for an hour.
    Timer {
        interval: 100
        repeat: true
        running: true
        onTriggered: {
            var xhr = new XMLHttpRequest()
            xhr.onreadystatechange = function () {
                if (xhr.readyState !== XMLHttpRequest.DONE || xhr.status !== 200)
                    return
                try {
                    var s = JSON.parse(xhr.responseText)
                    win.elapsed = s.elapsed || 0
                    win.micLevel = s.mic_level || 0
                    win.micAlive = !!s.mic_alive
                    win.paused = !!s.paused
                    win.stopping = !!s.stopping
                } catch (e) {}
            }
            xhr.open("GET", "http://127.0.0.1:" + win.port + "/state")
            xhr.setRequestHeader("X-Studio-Token", win.token)
            xhr.send()
        }
    }

    Component.onCompleted: {
        win.port = parseInt(arg("port", "0"))
        win.token = readTokenFile(arg("token-file", "")) || arg("token", "")
        var tokens = arg("theme", "")
        if (tokens !== "")
            Theme.load(tokens)
    }

    Rectangle {
        id: pill
        anchors.centerIn: parent
        implicitWidth: row.implicitWidth + 32
        height: 44
        radius: height / 2
        color: Theme.bgFloat
        border.width: 1
        border.color: Theme.hairline

        HoverHandler { id: pillHover }

        // Dragging the pill moves it, in case it lands over the one thing being
        // demonstrated. startSystemMove because the compositor owns a frameless
        // window's position.
        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton
            onPressed: win.startSystemMove()
        }

        RowLayout {
            id: row
            anchors.centerIn: parent
            spacing: 12

            // Hollow once stopping: the pulse is what says "recording, right now", and
            // it must not keep pulsing through the wind-down.
            LiveDot { paused: win.paused || win.stopping }

            Text {
                text: win.clock(win.elapsed)
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsBody
                // Tabular width: the pill must not twitch every time a digit changes.
                font.features: ({ "tnum": 1 })
            }

            Rectangle { width: 1; Layout.preferredHeight: 20; color: Theme.hairline }

            // Dimmed rather than hidden when there is no mic: an unlit meter answers
            // "is it hearing me" with a no, where an absent one answers nothing at all.
            // It also keeps the pill one width for the whole take, instead of jumping
            // wider the moment parec connects.
            MicBars {
                level: win.micLevel
                alive: win.micAlive
                opacity: win.micAlive ? 1.0 : 0.35
            }

            Rectangle { width: 1; Layout.preferredHeight: 20; color: Theme.hairline }

            S.GlyphButton {
                objectName: "ctl:hud-pause"
                glyph: win.paused ? "" : ""     // nf-fa-play / nf-fa-pause
                tip: win.paused ? "Resume" : "Pause"
                accented: win.paused
                onClicked: win.act(win.paused ? "resume" : "pause")
            }
            S.GlyphButton {
                objectName: "ctl:hud-discard"
                glyph: ""                              // nf-fa-trash
                tip: "Discard this take"
                accented: win.confirmingDiscard
                // Two presses, because there is no undo for a discarded take and the
                // button sits next to Stop. The arming lapses on its own so a stray
                // click cannot leave it armed for the rest of the recording.
                enabled: !win.stopping
                onClicked: {
                    if (win.confirmingDiscard) {
                        win.confirmingDiscard = false
                        win.stopping = true
                        win.act("discard")
                    } else {
                        win.confirmingDiscard = true
                        disarm.restart()
                    }
                }
            }
            Timer { id: disarm; interval: 3000; onTriggered: win.confirmingDiscard = false }

            Text {
                visible: win.confirmingDiscard
                text: "again to discard"
                color: Theme.live
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }

            Rectangle { width: 1; Layout.preferredHeight: 20; color: Theme.hairline }

            C.PrimaryButton {
                objectName: "ctl:hud-stop"
                // Finalizing the file takes about three seconds and the HUD stays up for
                // all of it. With no acknowledgement the button read as dead, and the
                // obvious response -- press it again -- used to start a NEW recording.
                // The label is the acknowledgement; the latch is the actual fix.
                text: win.stopping ? "Stopping…" : "Stop"
                enabled: !win.stopping
                onClicked: {
                    win.stopping = true      // instant, rather than waiting for the poll
                    win.act("stop")
                }
            }
        }
    }

    Shortcut {
        sequence: "Escape"
        onActivated: win.confirmingDiscard = false
    }
}
