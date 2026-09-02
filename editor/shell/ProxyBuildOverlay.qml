// 2e -- building the proxy. The first thing a user sees after a real recording: the
// editor opens immediately, the shell is real, only playback is not (spec §2e).
//
// Laid over the canvas area by main.qml. The dimmed first frame behind this block is
// Preview.qml's job (it owns the viewport geometry); this component draws only the
// 320px progress block from the mock: label + percent (accent) + estimate, a 4px bar,
// the one-line explanation, then Trim / Cancel.
//
// Progress honesty: Bridge.proxyStatus.progress is real when the bridge has it, but
// today omarchy_studio.proxy.ensure_proxy takes no progress callback, so on the normal
// path the fraction sits at 0 for the whole encode (ProxyBuilder only feeds per-frame
// progress from its fallback transcoder). While the fraction is 0 the bar sweeps as
// indeterminate and no percent is shown -- a number would be invented. The moment the
// bridge reports a real fraction, percent and the time estimate light up, so this file
// needs no change when proxy.py grows the callback.
import QtQuick
import ".."
import "../controls" as C

Item {
    id: root

    readonly property var status: Bridge.proxyStatus
    readonly property bool building: status.state === "building"
    readonly property bool failed: status.state === "error"
    visible: building || failed

    // "Trim while it builds" is the one edit that works without seeking. The timeline
    // below stays live the whole time; what this button does is get the block out of
    // the way so the tray is what the eye lands on -- the build itself continues in the
    // compact pill.
    property bool minimized: false
    property string cancelNote: ""

    // The estimate is extrapolated from the real fraction (elapsed * remaining/done),
    // never from a guess about encode speed.
    property double startedAt: 0
    property real etaS: -1

    onBuildingChanged: {
        if (building) {
            startedAt = Date.now()
            etaS = -1
            minimized = false
            cancelNote = ""
        }
    }

    Timer {
        interval: 1000
        repeat: true
        running: root.building
        onTriggered: {
            var p = root.status.progress || 0
            if (p < 0.02) {              // too little signal to extrapolate from
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
        if (etaS < 90)
            return "~" + Math.max(1, Math.round(etaS)) + "s"
        var m = Math.floor(etaS / 60)
        var s = Math.round(etaS % 60)
        return "~" + m + ":" + (s < 10 ? "0" : "") + s
    }

    // The mock's copy names the resolutions; recordings that are not 4K say what they
    // are instead (same rule as the top bar's format label).
    function sourceLabel() {
        var c = Bridge.state.canvas
        if (!c)
            return "original"
        return c.height >= 2100 ? "4K" : (c.height + "p")
    }

    readonly property real frac: status.progress || 0
    readonly property bool indeterminate: building && frac <= 0

    // --- the 320px progress block (mock 2e, verbatim widths) -----------------
    Column {
        visible: !root.minimized
        width: 320
        x: (parent.width - width) / 2
        y: (parent.height - height) / 2
        spacing: 10

        Item {
            width: parent.width
            height: headline.implicitHeight

            Text {
                id: headline
                text: root.failed ? "Preview proxy failed" : "Preparing for playback"
                color: Theme.text2
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsRow
            }
            Text {
                id: etaText
                anchors.right: parent.right
                anchors.baseline: headline.baseline
                text: root.etaLabel()
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
            Text {
                anchors.right: etaText.text !== "" ? etaText.left : parent.right
                anchors.rightMargin: etaText.text !== "" ? 9 : 0
                anchors.baseline: headline.baseline
                visible: root.building && root.frac > 0
                text: Math.round(root.frac * 100) + "%"
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
        }

        // 4px bar. Determinate when the bridge reports a fraction; a sweeping segment
        // when all it can say is "building".
        Item {
            id: bar
            width: parent.width
            height: 4
            visible: !root.failed
            clip: true    // the sweep enters and leaves through the bar's own edges

            Rectangle {
                width: parent.width; height: 4; radius: 2
                color: Theme.track
            }
            Rectangle {
                visible: !root.indeterminate
                width: parent.width * root.frac
                height: 4; radius: 2
                color: Theme.accent
            }
            Rectangle {
                id: sweep
                visible: root.indeterminate
                width: bar.width * 0.3
                height: 4; radius: 2
                color: Theme.accent
                XAnimator on x {
                    running: root.indeterminate
                    loops: Animation.Infinite
                    from: -0.3 * 320   // one sweep-width off the left edge
                    to: 320
                    duration: 1400
                }
            }
        }

        Text {
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            lineHeight: 1.6
            text: root.failed
                  ? (root.status.message || "The working copy could not be built.")
                  : "Building a 1080p working copy. The " + root.sourceLabel()
                    + " original is untouched and is what exports."
            color: Theme.text5
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsHint
        }

        Item { width: 1; height: 4 }   // mock: 17px group gap vs the block's 10px

        Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: 10
            visible: root.building

            // Subtle-fill pill (mock: rgba(255,255,255,0.06), text-2) -- not a
            // PrimaryButton, because the screen's primary action is waiting.
            Rectangle {
                width: trimLabel.implicitWidth + 26
                height: 26
                radius: Theme.radiusRow - 1
                color: trimMa.containsMouse ? Theme.fillHover : Theme.fillSubtle
                Behavior on color { ColorAnimation { duration: Theme.durFast } }
                Text {
                    id: trimLabel
                    anchors.centerIn: parent
                    text: "Trim while it builds"
                    color: Theme.text2
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsCaption
                }
                MouseArea {
                    id: trimMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.minimized = true
                }
            }

            C.GhostButton {
                text: "Cancel"
                onClicked: root.cancel()
            }
        }

        Text {
            visible: root.cancelNote !== ""
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            text: root.cancelNote
            color: Theme.text5
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsHint
        }
    }

    // Minimized: a compact pill at the bottom of the canvas so the timeline below has
    // the room. Clicking it brings the block back.
    Rectangle {
        visible: root.minimized && root.building
        width: miniRow.implicitWidth + 28
        height: 26
        radius: Theme.radiusRow
        x: (parent.width - width) / 2
        y: parent.height - height - 18
        color: Theme.bgFloat

        Row {
            id: miniRow
            anchors.centerIn: parent
            spacing: 8
            Text {
                text: "preparing for playback"
                color: Theme.text4
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
                font.letterSpacing: Theme.fsHint * Theme.capsSpacing
                font.capitalization: Font.AllUppercase
            }
            Text {
                visible: root.frac > 0
                text: Math.round(root.frac * 100) + "%"
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
            }
        }
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.minimized = false
        }
    }

    // POST /proxy/cancel does not exist yet (reported); the UI is stubbed against it so
    // the wiring is done the day the bridge grows the route. Until then the refusal is
    // shown here rather than left in the footer as a bare "not found".
    function cancel() {
        Bridge.send("POST", "/proxy/cancel", {}, function (reply, ok) {
            if (!ok) {
                Bridge.lastError = ""
                root.cancelNote = "this bridge cannot cancel a build yet"
            }
        })
    }
}
