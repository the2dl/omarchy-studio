// 1f as a pane, not a modal (spec §4 "Decisions"): this card is the right inspector's
// content in the same 268px column, because renders are the one thing you want to keep
// working alongside. main.qml swaps it in for SettingsPanel while it is open.
//
// What the mock shows and this pane does not draw, and why (the shared rule: dead
// chrome reads as broken, so a control with no behaviour is left out):
//   - MOV / GIF / WebM chips: render.py writes H.264 MP4 only, so MP4 is the one chip.
//   - resolution / frame-rate dropdowns: the renderer exports at the master's own
//     resolution and timebase, no override exists. The real values are shown as facts.
//   - quality slider + size estimate: CRF 20 is fixed in render._output_args and
//     nothing measures a size ahead of the encode; the fixed setting is stated instead.
// The report names the bridge/render additions that would make each of these live.
import QtQuick
import QtQuick.Dialogs
import ".."
import "../controls" as C

Item {
    id: root

    signal closed()

    readonly property var st: Bridge.state
    readonly property var ex: Bridge.exportStatus
    readonly property bool running: ex.state === "running"

    // "" means the renderer's own default: <bundle>/<name>.mp4.
    property string output: ""
    readonly property string effectiveOutput:
        output !== "" ? output
                      : (Bridge.bundle !== "" ? Bridge.bundle + "/" + (st.name || "export") + ".mp4" : "")

    // Time remaining, extrapolated from the renderer's real frame counter -- the same
    // arithmetic the proxy overlay uses, never a guess about encode speed.
    property double startedAt: 0
    property real etaS: -1
    onRunningChanged: {
        if (running) {
            startedAt = Date.now()
            etaS = -1
        }
    }
    Timer {
        interval: 1000
        repeat: true
        running: root.running
        onTriggered: {
            var p = root.ex.progress || 0
            if (p < 0.02) {
                root.etaS = -1
                return
            }
            var elapsed = (Date.now() - root.startedAt) / 1000
            root.etaS = elapsed * (1 - p) / p
        }
    }
    function etaLabel() {
        if (etaS < 0)
            return ""
        var m = Math.floor(etaS / 60)
        var s = Math.round(etaS % 60)
        return m + ":" + (s < 10 ? "0" : "") + s + " left"
    }

    function fpsLabel() {
        if (!st.timebase)
            return ""
        var f = st.timebase.fps
        return (Math.abs(f - Math.round(f)) < 0.005 ? Math.round(f) : f.toFixed(2)) + " fps"
    }

    Column {
        x: 0
        y: 0
        width: parent.width
        spacing: 0

        // -- header (mock 1f: title + close on the card's own bar) ------------
        Item {
            width: parent.width
            height: 48

            Text {
                x: Style.pad
                anchors.verticalCenter: parent.verticalCenter
                text: "Export"
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsTitle
            }
            Text {   // nf-fa-times
                id: closeGlyph
                anchors.right: parent.right
                anchors.rightMargin: Style.pad
                anchors.verticalCenter: parent.verticalCenter
                text: ""
                color: closeMa.containsMouse ? Theme.text2 : Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: 15
                Behavior on color { ColorAnimation { duration: Theme.durFast } }
                MouseArea {
                    id: closeMa
                    anchors.fill: parent
                    anchors.margins: -8
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.closed()
                }
            }
        }
        Rectangle { width: parent.width; height: 1; color: Theme.hairline }

        Item { width: 1; height: 16 }

        Column {
            x: Style.pad
            width: parent.width - 2 * Style.pad
            spacing: 16

            // -- format --------------------------------------------------------
            Column {
                width: parent.width
                spacing: 9
                C.Caption { text: "format" }
                Row {
                    spacing: 5
                    // Selected treatment per spec §1 "Selected card": accent wash +
                    // ring. One chip, because MP4 is the only format the renderer
                    // writes -- see the header comment.
                    Rectangle {
                        width: mp4Label.implicitWidth + 28
                        height: 28
                        radius: Theme.radiusRow - 1
                        color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.11)
                        border.width: 1
                        border.color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.32)
                        Text {
                            id: mp4Label
                            anchors.centerIn: parent
                            text: "MP4"
                            color: Theme.accent
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsRow
                        }
                    }
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: "h.264"
                        color: Theme.text6
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsHint
                    }
                }
            }

            // -- resolution / frame rate, two-up -------------------------------
            // Value tiles, not selects: the export always matches the master, so
            // these are statements of fact and draw no chevron promising a menu.
            Row {
                width: parent.width
                spacing: 13

                Column {
                    width: (parent.width - 13) / 2
                    spacing: 7
                    C.Caption { text: "resolution" }
                    Rectangle {
                        width: parent.width
                        height: 32
                        radius: Theme.radiusRow - 1
                        color: Theme.fillSubtle
                        Text {
                            x: 12
                            anchors.verticalCenter: parent.verticalCenter
                            text: st.canvas ? st.canvas.width + "×" + st.canvas.height : ""
                            color: Theme.text2
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsRow
                        }
                    }
                }
                Column {
                    width: (parent.width - 13) / 2
                    spacing: 7
                    C.Caption { text: "frame rate" }
                    Rectangle {
                        width: parent.width
                        height: 32
                        radius: Theme.radiusRow - 1
                        color: Theme.fillSubtle
                        Text {
                            x: 12
                            anchors.verticalCenter: parent.verticalCenter
                            text: root.fpsLabel()
                            color: Theme.text2
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fsRow
                        }
                    }
                }
            }

            // -- quality -------------------------------------------------------
            Item {
                width: parent.width
                height: qualityCap.implicitHeight
                C.Caption { id: qualityCap; text: "quality" }
                Text {
                    anchors.right: parent.right
                    anchors.baseline: qualityCap.baseline
                    // render._output_args: -preset medium -crf 20, not adjustable.
                    text: "high · crf 20"
                    color: Theme.text3
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsCaption
                }
            }

            // -- destination ---------------------------------------------------
            Column {
                width: parent.width
                spacing: 7
                C.Caption { text: "destination" }
                Rectangle {
                    width: parent.width
                    height: 32
                    radius: Theme.radiusRow - 1
                    color: destMa.containsMouse ? Theme.fillHover : Theme.fillSubtle
                    Behavior on color { ColorAnimation { duration: Theme.durFast } }
                    Text {
                        x: 12
                        anchors.verticalCenter: parent.verticalCenter
                        width: parent.width - 40
                        text: root.effectiveOutput.split("/").pop()
                        elide: Text.ElideMiddle
                        color: Theme.text2
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsRow
                    }
                    Text {   // nf-fa-folder_open
                        anchors.right: parent.right
                        anchors.rightMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        text: ""
                        color: Theme.text5
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsRow
                    }
                    MouseArea {
                        id: destMa
                        anchors.fill: parent
                        hoverEnabled: true
                        enabled: !root.running
                        cursorShape: Qt.PointingHandCursor
                        onClicked: destDialog.open()
                    }
                }
            }
        }

        Item { width: 1; height: 16 }
        Rectangle {
            width: parent.width; height: 1; color: Theme.hairline
            visible: root.ex.state !== "idle"
        }
        Item { width: 1; height: 15; visible: root.ex.state !== "idle" }

        // -- render progress (mock 1f bottom section; exists only once a render
        //    has something to say) ---------------------------------------------
        Column {
            x: Style.pad
            width: parent.width - 2 * Style.pad
            spacing: 12
            visible: root.ex.state !== "idle"

            Item {
                width: parent.width
                height: renderCap.implicitHeight

                Text {
                    id: renderCap
                    text: root.ex.state === "running" ? "Rendering"
                        : root.ex.state === "done" ? "Rendered"
                        : root.ex.state === "cancelled" ? "Render cancelled"
                        : "Render stopped"
                    color: Theme.text2
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                }
                Text {
                    id: etaTag
                    anchors.right: parent.right
                    anchors.baseline: renderCap.baseline
                    visible: root.running
                    text: root.etaLabel()
                    color: Theme.text5
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsCaption
                }
                Text {
                    anchors.right: root.running && etaTag.text !== "" ? etaTag.left : parent.right
                    anchors.rightMargin: root.running && etaTag.text !== "" ? 10 : 0
                    anchors.baseline: renderCap.baseline
                    visible: root.running || root.ex.state === "done"
                    text: Math.round((root.ex.progress || 0) * 100) + "%"
                    color: Theme.accent
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsCaption
                }
            }

            Item {
                width: parent.width
                height: 4
                visible: root.running || root.ex.state === "done"
                Rectangle { width: parent.width; height: 4; radius: 2; color: Theme.track }
                Rectangle {
                    width: parent.width * (root.ex.progress || 0)
                    height: 4; radius: 2
                    color: Theme.accent
                }
            }

            // What happened, in words: frame counter while running, the output path
            // when done, the renderer's last line when it stopped. Per 2g the error
            // GLYPH is live red; the words stay text-3.
            Row {
                width: parent.width
                spacing: 7
                visible: (root.ex.message || "") !== ""
                Text {   // nf-fa-exclamation_circle
                    visible: root.ex.state === "error"
                    text: ""
                    color: Theme.live
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsCaption
                }
                Text {
                    width: parent.width - x
                    text: root.ex.message || ""
                    wrapMode: Text.WrapAnywhere
                    maximumLineCount: 3
                    elide: Text.ElideRight
                    color: root.ex.state === "error" ? Theme.text3 : Theme.text6
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsHint
                }
            }
        }

        Item { width: 1; height: 16 }

        // -- footer action -----------------------------------------------------
        Item {
            x: Style.pad
            width: parent.width - 2 * Style.pad
            height: 32

            C.PrimaryButton {
                anchors.right: parent.right
                visible: !root.running
                text: root.ex.state === "error" ? "Retry" : "Export"
                onClicked: Bridge.startExport(root.output !== "" ? root.output : null)
            }
            C.GhostButton {
                anchors.right: parent.right
                visible: root.running
                text: "Cancel render"
                onClicked: Bridge.cancelExport()
            }
        }
    }

    FileDialog {
        id: destDialog
        title: "Export to"
        fileMode: FileDialog.SaveFile
        nameFilters: ["MP4 video (*.mp4)"]
        currentFolder: "file://" + (Bridge.bundle || "")
        onAccepted: root.output =
            decodeURIComponent(selectedFile.toString().replace("file://", ""))
    }
}
