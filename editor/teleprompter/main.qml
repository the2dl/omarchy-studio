// The teleprompter: your script on screen while you record, and absent from the
// recording.
//
// WHY IT CAN EXIST HERE AT ALL. A prompter is only useful if it sits over the thing you
// are demonstrating, and that is exactly where a screen recorder cannot afford to have
// it. The locally patched Hyprland in contrib/hyprland-screenshare-exclude makes a
// `no_screen_share` window absent from the portal's frames rather than a black
// rectangle over them, which is what lets this window be read from and stay out of the
// take. bin/omarchy-teleprompter installs that rule and REFUSES TO SHOW THE WINDOW if
// the compositor did not apply it -- a prompter that lands in the video is worse than
// no prompter, because you find out afterwards.
//
// PACE IS WORDS PER MINUTE, NOT PIXELS PER SECOND. Pixels per second is a number nobody
// can rehearse against: it changes meaning with every type size and window width, so a
// setting found once is wrong the next time. Words per minute is a property of the
// speaker. The scroll rate is derived from it and the laid-out height of the actual
// script, so the same 130 wpm reads at the same speed whatever size the type is.
//
// Launched by bin/omarchy-teleprompter, which owns the window rule, the placement and
// the saved script. Same launch pattern as bin/omarchy-capture-setup: /usr/bin/qml6 by
// absolute path, QT_ASSUME_STDERR_HAS_CONSOLE=1, and a tokened loopback bridge.
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
    title: "omarchy-studio-teleprompter"
    width: 720
    height: 420
    minimumWidth: 360
    minimumHeight: 200

    // --- state ---------------------------------------------------------------

    property string script: ""
    property bool running: false
    property real wpm: 130
    property int fontSize: 26
    property real dim: 1.0
    property bool editing: false
    // Suppresses the save POST while the initial state is being applied, so restoring
    // a script does not immediately write it back and race the launcher's own read.
    property bool loading: true

    // --- the scroll ----------------------------------------------------------

    readonly property int wordCount: {
        var t = script.trim()
        return t === "" ? 0 : t.split(/\s+/).length
    }

    // The rate and the running time both come from here -- see Pace.qml for why the
    // unit is words per minute and why the derivation uses the laid-out height.
    Pace {
        id: pace
        wordCount: win.wordCount
        contentHeight: scriptText.contentHeight
        wpm: win.wpm
    }
    readonly property real pxPerSecond: pace.pxPerSecond

    readonly property real scrollEnd: Math.max(0, scriptText.contentHeight
                                               - scroller.height + scroller.height * 0.6)
    readonly property real progress: scrollEnd > 0 ? Math.min(1, scroller.contentY / scrollEnd) : 0

    // 60Hz and a fractional accumulator, not an integer step per tick: at 20 px/s a
    // whole-pixel step every frame is 3x too fast and a step every third frame visibly
    // stutters. Flickable takes a real contentY, so the fraction is simply kept.
    Timer {
        interval: 16
        repeat: true
        running: win.running && !win.editing && win.pxPerSecond > 0
        onTriggered: {
            var next = scroller.contentY + win.pxPerSecond * (interval / 1000.0)
            if (next >= win.scrollEnd) {
                scroller.contentY = win.scrollEnd
                win.running = false          // stop at the end rather than grind on it
            } else {
                scroller.contentY = next
            }
        }
    }

    function toggleRun() {
        if (win.editing)
            return
        if (!win.running && win.progress >= 1)
            scroller.contentY = 0            // play at the end means play again
        win.running = !win.running
    }
    function restart() {
        scroller.contentY = 0
    }
    function toggleEdit() {
        if (win.editing) {
            win.script = editor.text
            win.editing = false
            win.save()
        } else {
            win.running = false
            editor.text = win.script
            win.editing = true
            editor.forceActiveFocus()
        }
    }

    // --- persistence ---------------------------------------------------------

    Timer {
        id: saveDebounce
        interval: 400
        onTriggered: win.postState()
    }
    function save() {
        if (!win.loading)
            saveDebounce.restart()
    }
    onWpmChanged: save()
    onFontSizeChanged: save()
    onDimChanged: save()

    function postState() {
        if (win.port === 0)
            return
        var xhr = new XMLHttpRequest()
        xhr.open("POST", "http://127.0.0.1:" + win.port + "/state")
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.setRequestHeader("X-Studio-Token", win.token)
        xhr.send(JSON.stringify({
            script: win.script, wpm: win.wpm, font_size: win.fontSize,
            dim: win.dim, width: win.width, height: win.height,
            x: win.x, y: win.y
        }))
    }

    Component.onCompleted: {
        win.port = parseInt(arg("port", "0"))
        win.token = arg("token", "")
        var tokens = arg("theme", "")
        if (tokens !== "")
            Theme.load(tokens)
        var xhr = new XMLHttpRequest()
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== XMLHttpRequest.DONE)
                return
            try {
                var s = JSON.parse(xhr.responseText)
                win.script = s.script || ""
                win.wpm = s.wpm || 130
                win.fontSize = s.font_size || 26
                win.dim = s.dim === undefined ? 1.0 : s.dim
                if (s.width) win.width = s.width
                if (s.height) win.height = s.height
            } catch (e) {
                // A prompter that will not open because its saved state is malformed is
                // strictly worse than one that opens empty.
            }
            win.loading = false
            if (win.script === "")
                win.toggleEdit()      // nothing to read: open straight into the paste box
        }
        xhr.open("GET", "http://127.0.0.1:" + win.port + "/state")
        xhr.setRequestHeader("X-Studio-Token", win.token)
        xhr.send()
    }

    // --- the window ----------------------------------------------------------

    Rectangle {
        id: shell
        anchors.fill: parent
        radius: Theme.radiusWindow
        color: Theme.bg
        opacity: win.dim
        border.width: 1
        border.color: win.running ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.5)
                                  : Theme.hairline
        Behavior on border.color { ColorAnimation { duration: Theme.durSlow } }

        HoverHandler { id: shellHover }

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            // The strip fades while the script is running and returns on hover. Reading
            // is the point of the window; chrome that stays lit gets read by accident.
            PrompterBar {
                id: bar
                Layout.fillWidth: true
                Layout.preferredHeight: 40
                running: win.running
                wpm: win.wpm
                fontSize: win.fontSize
                dim: win.dim
                editing: win.editing
                remaining: pace.clock(pace.totalSeconds * (1 - win.progress))
                opacity: (!win.running || shellHover.hovered || win.editing) ? 1.0 : 0.0
                Behavior on opacity { NumberAnimation { duration: Theme.durSlow } }
                onWpmChanged: win.wpm = wpm
                onFontSizeChanged: win.fontSize = fontSize
                onDimChanged: win.dim = dim
                onToggleRun: win.toggleRun()
                onRestart: win.restart()
                onToggleEdit: win.toggleEdit()
                onCloseRequested: Qt.quit()

                // Dragging the strip moves the window. startSystemMove hands the drag to
                // the compositor, which is the only way a frameless Wayland window can be
                // moved without fighting Hyprland over the position.
                MouseArea {
                    anchors.fill: parent
                    z: -1
                    acceptedButtons: Qt.LeftButton
                    onPressed: win.startSystemMove()
                }
            }

            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true

                // The reading line. A band rather than a rule: a 1px line asks the eye to
                // land on a coordinate, a band asks it to land in a region, and the
                // second is what people can actually do while talking.
                Rectangle {
                    visible: !win.editing && win.script !== ""
                    x: 0
                    width: parent.width
                    y: parent.height * 0.30
                    height: win.fontSize * 1.8
                    color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.07)
                    Rectangle {
                        width: parent.width; height: 1
                        color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.22)
                    }
                    Rectangle {
                        width: parent.width; height: 1; y: parent.height - 1
                        color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.22)
                    }
                }

                Flickable {
                    id: scroller
                    anchors.fill: parent
                    anchors.leftMargin: 26
                    anchors.rightMargin: 26
                    visible: !win.editing
                    contentWidth: width
                    contentHeight: scriptText.contentHeight + height * 0.6
                    clip: true
                    // Wheel and drag still work while it is scrolling itself: catching up
                    // or skipping back is the most common thing a person does mid-take,
                    // and having to pause first loses the sentence.
                    boundsBehavior: Flickable.StopAtBounds
                    onMovementStarted: win.running = false

                    Text {
                        id: scriptText
                        width: scroller.width
                        // Padded by a fraction of the height so the first line starts at
                        // the reading band rather than at the top edge.
                        y: scroller.height * 0.30
                        text: win.script
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: win.fontSize
                        lineHeight: 1.55
                        wrapMode: Text.WordWrap
                    }
                }

                // Paste box. A plain TextArea and not a styled one: this is the only
                // surface in the product where the user's own text is the content, and
                // decoration around it makes the paste target harder to find.
                ScrollView {
                    anchors.fill: parent
                    anchors.margins: 18
                    visible: win.editing
                    TextArea {
                        id: editor
                        placeholderText: "Paste or type your script.\n\nBlank lines become pauses in the scroll."
                        color: Theme.text
                        placeholderTextColor: Theme.text5
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsBody
                        wrapMode: TextArea.Wrap
                        selectByMouse: true
                        background: Rectangle { color: "transparent" }
                    }
                }

                // The script fades out away from the reading band, top and bottom. This
                // is the difference between a scrolling text box and a prompter: with
                // every line equally lit the eye keeps re-finding its place, and the
                // failure mode is reading a line twice on camera. Scrims rather than
                // per-line opacity because the text is one wrapped Text item -- there
                // are no line objects to address, and splitting it into some would put
                // the wrap under our control instead of the layout engine's.
                Rectangle {
                    visible: !win.editing
                    x: 0
                    y: 0
                    width: parent.width
                    height: parent.height * 0.30
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: Theme.bg }
                        GradientStop { position: 0.55; color: Qt.rgba(Theme.bg.r, Theme.bg.g, Theme.bg.b, 0.82) }
                        GradientStop { position: 1.0; color: "transparent" }
                    }
                }
                Rectangle {
                    visible: !win.editing
                    x: 0
                    width: parent.width
                    y: parent.height * 0.30 + win.fontSize * 1.8
                    height: parent.height - y
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: "transparent" }
                        GradientStop { position: 0.45; color: Qt.rgba(Theme.bg.r, Theme.bg.g, Theme.bg.b, 0.55) }
                        GradientStop { position: 1.0; color: Qt.rgba(Theme.bg.r, Theme.bg.g, Theme.bg.b, 0.93) }
                    }
                }

                // Faint progress along the bottom edge: how much script is left, without
                // a number and without taking a row of its own.
                Rectangle {
                    visible: !win.editing && win.wordCount > 0
                    x: 0
                    y: parent.height - 2
                    height: 2
                    width: parent.width * win.progress
                    color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.55)
                }
            }
        }

        // Resize grip, bottom-right. startSystemResize for the same reason the move uses
        // startSystemMove: the compositor owns the geometry of a frameless window.
        Item {
            width: 18
            height: 18
            x: parent.width - width - 3
            y: parent.height - height - 3
            opacity: shellHover.hovered ? 0.8 : 0.0
            Behavior on opacity { NumberAnimation { duration: Theme.durFast } }
            Repeater {
                model: 2
                Rectangle {
                    width: 9 - index * 4
                    height: 1.5
                    radius: 0.75
                    color: Theme.text4
                    rotation: -45
                    x: (18 - width) / 2 + index * 3
                    y: (18 - height) / 2 + index * 3
                }
            }
            MouseArea {
                anchors.fill: parent
                anchors.margins: -6
                cursorShape: Qt.SizeFDiagCursor
                onPressed: win.startSystemResize(Qt.BottomEdge | Qt.RightEdge)
            }
        }
    }

    // --- keys ----------------------------------------------------------------
    //
    // Space is the transport, as it is everywhere else in this product. The guard is the
    // same one editor/main.qml uses: while the paste box has focus a plain key belongs
    // to the field, not to the window.

    Shortcut {
        sequence: "Space"
        onActivated: if (!win.editing) win.toggleRun()
    }
    Shortcut {
        sequence: "Home"
        onActivated: if (!win.editing) win.restart()
    }
    Shortcut {
        sequence: "Ctrl+E"
        onActivated: win.toggleEdit()
    }
    Shortcut {
        sequence: "Escape"
        onActivated: {
            if (win.editing)
                win.toggleEdit()
            else
                Qt.quit()
        }
    }
    Shortcut {
        sequence: "Up"
        onActivated: if (!win.editing) win.wpm = Math.min(240, win.wpm + 5)
    }
    Shortcut {
        sequence: "Down"
        onActivated: if (!win.editing) win.wpm = Math.max(60, win.wpm - 5)
    }
    Shortcut {
        sequences: ["Ctrl++", "Ctrl+="]
        onActivated: win.fontSize = Math.min(72, win.fontSize + 2)
    }
    Shortcut {
        sequence: "Ctrl+-"
        onActivated: win.fontSize = Math.max(14, win.fontSize - 2)
    }
}
