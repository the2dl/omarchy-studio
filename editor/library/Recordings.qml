// The recordings library (spec §2d): header with count + total size, search and the
// primary New; filter chips; then one row per recording, newest first, with the newest
// unedited one carrying the accent treatment.
//
// Launched by bin/omarchy-recordings, which scans the recordings directory through
// lib/omarchy_studio/library.py and serves the resolved answer over loopback. This
// file renders that answer and decides nothing about the filesystem: whether a row is
// editable, missing or "plays only" arrived pre-classified, so the row can never
// promise a verb the backend will refuse.
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Dialogs
import QtQuick.Effects
import ".."

ApplicationWindow {
    id: app

    width: 720                 // spec §2d: the panel is 720px
    height: 520
    // Equal min and max: the panel's size is not negotiable, which is also the signal
    // that makes a tiling compositor float it instead of stretching it into a tile.
    minimumWidth: 720
    maximumWidth: 720
    minimumHeight: 520
    maximumHeight: 520
    visible: true
    color: Theme.bg
    title: "Recordings"

    // Nerd Font codepoints for the mock's Material glyph names; MDI glyphs live above
    // the BMP, so \u escapes cannot express them and fromCodePoint is the readable form.
    readonly property string gLibrary: String.fromCodePoint(0xf0d18)  // filmstrip-box-multiple ~ video_library
    readonly property string gSearch: String.fromCodePoint(0xf0349)   // magnify ~ search
    readonly property string gRecord: String.fromCodePoint(0xf043e)   // radiobox-marked ~ radio_button_checked
    readonly property string gMore: String.fromCodePoint(0xf01d8)     // dots-horizontal ~ more_horiz
    readonly property string gLinkOff: String.fromCodePoint(0xf0338)  // link-off

    // -- transport: same shape as Bridge.qml, pointed at library.py's server ---------
    property int port: 0
    property string token: ""
    property int selftestMs: 0
    property bool connected: false
    property var library: ({ count: 0, total_label: "", recordings: [] })
    property string shortcut: ""

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
                app.connected = true
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

    function reload() {
        send("GET", "/library", null, function (lib, ok) { if (ok) app.library = lib })
        // Only for the shortcut hint on the empty card; the row data never needs it.
        send("GET", "/menubar", null, function (m, ok) { if (ok) app.shortcut = m.shortcut || "" })
    }

    Component.onCompleted: {
        port = parseInt(arg("--port", "0"))
        token = arg("--token", "")
        selftestMs = parseInt(arg("--selftest", "0"))
        send("GET", "/theme", null, function (t, ok) { if (ok) Theme.load(t) })
        reload()
    }
    onClosing: send("POST", "/quit", {}, null)

    // -- view state -------------------------------------------------------------------
    property int filter: 0            // 0 All · 1 Unedited · 2 Exported
    property string query: ""

    // Filtering a delivered list is view logic; classification stayed in Python.
    readonly property var rows: {
        var out = []
        var list = library.recordings || []
        for (var i = 0; i < list.length; ++i) {
            var e = list[i]
            if (filter === 1 && !e.unedited)
                continue
            if (filter === 2 && !e.exported)
                continue
            if (query !== "" && e.name.toLowerCase().indexOf(query.toLowerCase()) < 0)
                continue
            out.push(e)
        }
        return out
    }

    // Header, 46px like the editor's top bar: identity left, search + New right.
    header: Rectangle {
        height: 46
        color: Theme.bg

        Row {
            x: 16
            height: parent.height
            spacing: 14
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: app.gLibrary
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: 17
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Recordings"
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsBody
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: app.library.count > 0
                      ? app.library.count + " · " + app.library.total_label : ""
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
        }
        Row {
            anchors.right: parent.right
            anchors.rightMargin: 16
            height: parent.height
            spacing: 12
            Row {
                anchors.verticalCenter: parent.verticalCenter
                spacing: 7
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: app.gSearch
                    color: Theme.text3
                    font.family: Theme.fontFamily
                    font.pixelSize: 16
                }
                TextInput {
                    id: searchInput
                    anchors.verticalCenter: parent.verticalCenter
                    width: 110
                    color: Theme.text2
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                    clip: true
                    onTextChanged: app.query = text
                    Text {
                        visible: searchInput.text === "" && !searchInput.activeFocus
                        text: "search"
                        color: Theme.text6
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsRow
                    }
                }
            }
            ErrorCard.ActionChip {
                anchors.verticalCenter: parent.verticalCenter
                kind: "accent"
                glyph: app.gRecord
                text: "New"
                implicitHeight: 28
                onClicked: app.op("record", { mode: "screen" })
            }
        }
        Rectangle {
            x: 0; y: parent.height - 1
            width: parent.width; height: 1
            color: Theme.hairline
        }
    }

    // Filter chips: sized to their labels like the mock, not equal-width -- Segmented
    // is for property values; these are views over one list.
    Row {
        id: chips
        x: 14
        y: 11
        spacing: 4
        visible: (app.library.recordings || []).length > 0
        Repeater {
            model: ["All", "Unedited", "Exported"]
            delegate: Rectangle {
                required property int index
                required property string modelData
                width: chipLabel.implicitWidth + 24
                height: 25
                radius: Theme.radiusChip
                color: index === app.filter ? Theme.fillHover : "transparent"
                Behavior on color { ColorAnimation { duration: Theme.durFast } }
                Text {
                    id: chipLabel
                    anchors.centerIn: parent
                    text: modelData
                    color: index === app.filter ? Theme.text2 : Theme.text4
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsCaption
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: app.filter = parent.index
                }
            }
        }
    }

    ListView {
        id: list
        x: 8
        y: chips.visible ? chips.y + chips.height + 6 : 12
        width: parent.width - 16
        height: parent.height - y - 12
        spacing: 2
        clip: true
        model: app.rows

        delegate: Rectangle {
            required property var modelData
            readonly property var rec: modelData

            width: list.width
            height: 64
            radius: Theme.radiusRow
            // The accent wash marks exactly one row: the newest unedited recording,
            // i.e. the one you just made (spec §2d). Hover elsewhere is the usual fill.
            color: rec.fresh ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.09)
                             : rowMa.containsMouse ? Theme.fillSubtle : "transparent"
            Behavior on color { ColorAnimation { duration: Theme.durFast } }

            MouseArea {
                id: rowMa
                anchors.fill: parent
                hoverEnabled: true
                onDoubleClicked: if (rec.editable) app.op("edit", { path: rec.path })
            }

            Row {
                x: 11
                anchors.verticalCenter: parent.verticalCenter
                spacing: 13

                // 78x46 thumbnail with the duration chip (spec §2d). A recording with
                // no extractable frame keeps the dark well -- an honest placeholder
                // beats an invented image.
                Rectangle {
                    width: 78; height: 46
                    radius: Theme.radiusChip
                    color: Theme.canvasB
                    anchors.verticalCenter: parent.verticalCenter
                    Image {
                        id: thumbImg
                        anchors.fill: parent
                        source: rec.thumb
                        fillMode: Image.PreserveAspectCrop
                        asynchronous: true
                        visible: false   // drawn through the mask below
                    }
                    // Rounded clip for the frame: plain `clip` is rectangular, so the
                    // image is drawn through a mask shaped like the well itself.
                    MultiEffect {
                        anchors.fill: parent
                        source: thumbImg
                        visible: rec.thumb !== ""
                        maskEnabled: true
                        maskSource: thumbMask
                    }
                    Item {
                        id: thumbMask
                        width: 78; height: 46
                        visible: false
                        layer.enabled: true
                        Rectangle { anchors.fill: parent; radius: Theme.radiusChip }
                    }
                    Rectangle {
                        visible: rec.duration !== ""
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.margins: 4
                        width: durText.implicitWidth + 8
                        height: 14
                        radius: 4
                        color: Qt.rgba(Theme.bgDeep.r, Theme.bgDeep.g, Theme.bgDeep.b, 0.85)
                        Text {
                            id: durText
                            anchors.centerIn: parent
                            text: rec.duration
                            color: rec.fresh ? Theme.text2 : Theme.text3
                            font.family: Theme.fontFamily
                            font.pixelSize: 9
                        }
                    }
                }

                Column {
                    width: list.width - 78 - 13 - 30 - 13 - 90 - 22 - 13
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 3
                    Row {
                        spacing: 9
                        Text {
                            text: rec.name
                            color: rec.fresh ? Theme.text : Theme.text3
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsBody
                            elide: Text.ElideRight
                        }
                        Text {
                            visible: rec.missing === true
                            anchors.verticalCenter: parent.verticalCenter
                            text: app.gLinkOff
                            color: Theme.text4
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                        }
                        Rectangle {
                            visible: rec.fresh === true
                            anchors.verticalCenter: parent.verticalCenter
                            width: newText.implicitWidth + 12
                            height: 15
                            radius: 5
                            color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.18)
                            Text {
                                id: newText
                                anchors.centerIn: parent
                                text: "NEW"
                                color: Theme.accent
                                font.family: Theme.fontFamily
                                font.pixelSize: 9
                                font.letterSpacing: 0.54
                            }
                        }
                    }
                    Text {
                        text: rec.meta
                        color: rec.fresh ? Theme.text5 : Theme.text6
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsHint
                        elide: Text.ElideRight
                        width: parent.width
                    }
                }

                Text {
                    id: moreGlyph
                    anchors.verticalCenter: parent.verticalCenter
                    width: 30
                    horizontalAlignment: Text.AlignHCenter
                    text: app.gMore
                    color: moreMa.containsMouse ? Theme.text3 : Theme.text6
                    font.family: Theme.fontFamily
                    font.pixelSize: 16
                    MouseArea {
                        id: moreMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            moreMenu.rec = rec
                            moreMenu.popup()
                        }
                    }
                }

                // One action per row, resolved by the backend's classification:
                // Edit (filled for the fresh row), Play for clips, Locate… when the
                // source is gone.
                Item {
                    width: 90
                    height: 28
                    anchors.verticalCenter: parent.verticalCenter
                    ErrorCard.ActionChip {
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        kind: rec.missing ? "ghost" : rec.fresh ? "accent" : "neutral"
                        text: rec.missing ? "Locate…" : rec.editable ? "Edit" : "Play"
                        onClicked: {
                            if (rec.missing) {
                                locateDialog.bundlePath = rec.path
                                locateDialog.open()
                            } else if (rec.editable) {
                                app.op("edit", { path: rec.path })
                            } else {
                                app.op("play", { path: rec.path })
                            }
                        }
                    }
                }
            }
        }
    }

    // Empty states. The full empty library is §2g's centered card; a filter or search
    // with no matches says which view is empty rather than pretending the library is.
    ErrorCard {
        visible: (app.library.recordings || []).length === 0 && app.connected
        anchors.centerIn: parent
        width: 420
        centered: true
        glyph: app.gLibrary
        title: "Nothing recorded yet"
        body: (app.shortcut !== "" ? app.shortcut + ", or the button below." : "Use the button below.")
        primaryText: "Record something"
        primaryGlyph: app.gRecord
        primaryAccent: true
        onPrimaryClicked: app.op("record", { mode: "screen" })
    }
    Text {
        visible: app.rows.length === 0 && (app.library.recordings || []).length > 0
        anchors.centerIn: parent
        text: app.query !== "" ? "No recording matches."
              : app.filter === 1 ? "Everything here has been edited."
              : "Nothing exported yet."
        color: Theme.text5
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fsRow
    }

    // The two verbs every row supports regardless of state; the main action column
    // stays a single button per the mock.
    component LibMenuItem: MenuItem {
        id: mi
        implicitHeight: 30
        contentItem: Text {
            text: mi.text
            color: !mi.enabled ? Theme.text6 : mi.hovered ? Theme.text2 : Theme.text3
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsRow
            leftPadding: 6
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: Theme.radiusChip
            color: mi.hovered ? Theme.fillHover : "transparent"
        }
    }

    Menu {
        id: moreMenu
        property var rec: null
        width: 170
        background: Rectangle {
            radius: Theme.radiusRow
            color: Theme.bgFloat
        }
        LibMenuItem {
            text: "Play"
            enabled: moreMenu.rec !== null && !!moreMenu.rec.playable
            onTriggered: app.op("play", { path: moreMenu.rec.path })
        }
        LibMenuItem {
            text: "Show in folder"
            onTriggered: app.op("reveal", { path: moreMenu.rec.path })
        }
    }

    FileDialog {
        id: locateDialog
        property string bundlePath: ""
        title: "Locate the moved recording"
        nameFilters: ["Video (*.mp4 *.mkv *.mov *.webm)", "All files (*)"]
        onAccepted: {
            var src = decodeURIComponent(selectedFile.toString().replace("file://", ""))
            app.op("locate", { path: bundlePath, source: src }, function (lib, ok) {
                if (ok)
                    app.library = lib
            })
        }
    }

    // --selftest N: report what loaded, then exit -- the launcher watches for this line
    // because qml6 exits 0 on some load failures.
    Timer {
        interval: Math.max(1, app.selftestMs)
        running: app.selftestMs > 0
        repeat: false
        onTriggered: {
            console.log("SELFTEST " + JSON.stringify({
                connected: app.connected,
                count: app.library.count,
                rows: app.rows.length,
                fresh: app.rows.filter(function (r) { return r.fresh }).map(function (r) { return r.name }),
                missing: app.rows.filter(function (r) { return r.missing }).length,
                clips: app.rows.filter(function (r) { return r.kind === "clip" }).length,
                theme: Theme.mode,
                font: Theme.fontFamily
            }))
            app.send("POST", "/quit", {}, null)
            Qt.exit(app.connected ? 0 : 3)
        }
    }
}
