// The menubar dropdown (spec §1a): status header (state caption + current source),
// the primary actions, then the input section. 300px, bgFloat, 15px radius.
//
// This is a panel, not an app window. Standalone it is launched by
// `omarchy-recordings --dropdown`, which serves the desktop state (focused monitor,
// default mic, recording flag, library count) from lib/omarchy_studio/library.py;
// wiring it under an actual bar widget is the bar's job, and amounts to running that
// command anchored to the widget.
//
// The mock's "Camera only" row is absent on purpose: the recorder has no camera-only
// capture path (bin/omarchy-capture-screenrecording records the screen, with the
// camera as an extra stream), and a row that does nothing reads as broken.
import QtQuick
import ".."

Window {
    id: win

    width: 300                       // spec §1a: the dropdown is 300px
    height: panel.implicitHeight
    // Equal min and max is the size-is-not-negotiable signal that makes a tiling
    // compositor float this; a dropdown stretched into a tile would be nonsense.
    minimumWidth: 300
    maximumWidth: 300
    minimumHeight: panel.implicitHeight
    maximumHeight: panel.implicitHeight
    visible: true
    color: "transparent"
    title: "Recording"
    flags: Qt.FramelessWindowHint | Qt.Dialog

    readonly property string gRecord: String.fromCodePoint(0xf043e)   // radiobox-marked
    readonly property string gStop: String.fromCodePoint(0xf04db)     // stop
    readonly property string gCrop: String.fromCodePoint(0xf019e)     // crop
    readonly property string gMic: String.fromCodePoint(0xf036c)      // microphone
    readonly property string gChevron: String.fromCodePoint(0xf0142)  // chevron-right
    readonly property string gFolder: String.fromCodePoint(0xf0770)   // folder-open

    // -- transport, same anatomy as Recordings.qml ------------------------------------
    property int port: 0
    property string token: ""
    property int selftestMs: 0
    property bool connected: false
    property var mb: ({ state: "ready", source: "", mic: "", mics: [], count: 0, shortcut: "" })
    property bool micsOpen: false

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
            if (x.status === 200)
                win.connected = true
            if (cb)
                cb(reply, x.status === 200)
        }
        x.open(method, "http://127.0.0.1:" + port + path)
        x.setRequestHeader("X-Studio-Token", token)
        x.setRequestHeader("Content-Type", "application/json")
        x.send(body ? JSON.stringify(body) : "")
    }

    function op(name, args, cb) {
        send("POST", "/op", { op: name, args: args || {} }, cb)
    }

    // A menu action closes the menu; the verb keeps running in its own process.
    function fire(name, args) {
        op(name, args, function () { win.quitPanel() })
    }

    function quitPanel() {
        send("POST", "/quit", {}, null)
        Qt.quit()
    }

    Component.onCompleted: {
        port = parseInt(arg("--port", "0"))
        token = arg("--token", "")
        selftestMs = parseInt(arg("--selftest", "0"))
        send("GET", "/theme", null, function (t, ok) { if (ok) Theme.load(t) })
        send("GET", "/menubar", null, function (m, ok) { if (ok) win.mb = m })
    }

    readonly property bool recording: mb.state === "recording"

    // One dropdown row: glyph, label, right-aligned hint in text-5. `Start recording`
    // is the only accented row on the panel (spec §1a).
    component MenuRow: Rectangle {
        id: row
        property string glyph: ""
        property string label: ""
        property string hint: ""
        property bool accent: false
        property bool chevron: false
        signal clicked()

        width: parent.width
        height: 38
        radius: Theme.radiusRow
        color: row.accent ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.10)
                          : rowMa.containsMouse ? Theme.fillHover : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.durFast } }

        Row {
            x: 12
            anchors.verticalCenter: parent.verticalCenter
            spacing: 12
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: row.glyph
                color: row.accent ? Theme.accent : Theme.text3
                font.family: Theme.fontFamily
                font.pixelSize: 18
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: row.label
                color: row.accent ? Theme.text : Theme.text3
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsBody
            }
        }
        Text {
            anchors.right: parent.right
            anchors.rightMargin: 12
            anchors.verticalCenter: parent.verticalCenter
            text: row.chevron ? win.gChevron : row.hint
            color: Theme.text5
            font.family: Theme.fontFamily
            font.pixelSize: row.chevron ? 15 : Theme.fsCaption
        }
        MouseArea {
            id: rowMa
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: row.clicked()
        }
    }

    Rectangle {
        id: panel
        width: parent.width
        implicitHeight: content.implicitHeight
        radius: 15
        color: Theme.bgFloat

        Column {
            id: content
            width: parent.width

            // Status header: what pressing the accent row will act on.
            Item {
                width: parent.width
                height: 58
                Column {
                    x: 17
                    y: 15
                    spacing: 3
                    Row {
                        spacing: 6
                        // The live dot appears only while actually recording -- the
                        // one red in the product (spec §1).
                        Rectangle {
                            visible: win.recording
                            anchors.verticalCenter: parent.verticalCenter
                            width: 7; height: 7; radius: 3.5
                            color: Theme.live
                        }
                        Text {
                            text: win.recording ? "recording" : "ready"
                            color: Theme.text5
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsCaption
                            font.letterSpacing: Theme.fsCaption * Theme.capsSpacing
                            font.capitalization: Font.AllUppercase
                        }
                    }
                    Text {
                        text: win.mb.source || "no display detected"
                        color: Theme.text2
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsBody
                    }
                }
            }
            Rectangle { width: parent.width; height: 1; color: Theme.hairline }

            Column {
                width: parent.width - 14
                x: 7
                topPadding: 7
                bottomPadding: 7
                MenuRow {
                    glyph: win.recording ? win.gStop : win.gRecord
                    label: win.recording ? "Stop recording" : "Start recording"
                    hint: win.mb.shortcut || ""
                    accent: true
                    onClicked: win.fire(win.recording ? "stop" : "record", { mode: "screen" })
                }
                MenuRow {
                    glyph: win.gCrop
                    label: "Record area…"
                    onClicked: win.fire("record", { mode: "area" })
                }
            }
            Rectangle { width: parent.width; height: 1; color: Theme.hairline }

            Column {
                width: parent.width - 14
                x: 7
                topPadding: 7
                bottomPadding: 7
                MenuRow {
                    glyph: win.gMic
                    label: win.mb.mic || "no microphone"
                    chevron: (win.mb.mics || []).length > 0
                    onClicked: if ((win.mb.mics || []).length > 0)
                                   win.micsOpen = !win.micsOpen
                }
                // The device list unfolds in place: a dropdown spawning a second
                // dropdown would race the bar for focus.
                Column {
                    width: parent.width
                    visible: win.micsOpen
                    Repeater {
                        model: win.micsOpen ? (win.mb.mics || []) : []
                        delegate: Rectangle {
                            required property var modelData
                            width: parent.width
                            height: 30
                            radius: Theme.radiusChip
                            color: micMa.containsMouse ? Theme.fillSubtle : "transparent"
                            Row {
                                x: 42
                                anchors.verticalCenter: parent.verticalCenter
                                spacing: 8
                                Rectangle {
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: 5; height: 5; radius: 2.5
                                    color: Theme.accent
                                    visible: modelData.current === true
                                }
                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: modelData.label
                                    color: modelData.current ? Theme.text2 : Theme.text4
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fsRow
                                    width: 220
                                    elide: Text.ElideRight
                                }
                            }
                            MouseArea {
                                id: micMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: win.op("set_mic", { name: modelData.name },
                                                  function (m, ok) {
                                                      if (ok) win.mb = m
                                                      win.micsOpen = false
                                                  })
                            }
                        }
                    }
                }
                MenuRow {
                    glyph: win.gFolder
                    label: "Recordings"
                    hint: win.mb.count > 0 ? String(win.mb.count) : ""
                    onClicked: win.fire("open_library", {})
                }
            }
            Item { width: 1; height: 3 }   // breathing room under the last row
        }
    }

    // A dropdown that loses focus is done -- same contract as any menu.
    onActiveChanged: if (!active && selftestMs === 0) quitPanel()

    Timer {
        interval: Math.max(1, win.selftestMs)
        running: win.selftestMs > 0
        repeat: false
        onTriggered: {
            console.log("SELFTEST " + JSON.stringify({
                connected: win.connected,
                state: win.mb.state,
                source: win.mb.source,
                mic: win.mb.mic,
                mics: (win.mb.mics || []).length,
                count: win.mb.count,
                shortcut: win.mb.shortcut,
                theme: Theme.mode
            }))
            win.quitPanel()
            Qt.exit(win.connected ? 0 : 3)
        }
    }
}
