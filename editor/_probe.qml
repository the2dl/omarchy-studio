import QtQuick
import QtQuick.Window
import "controls"

Window {
    width: 560; height: 200; visible: true; title: "glowprobe"
    color: Theme.bg
    Component.onCompleted: {
        var x = new XMLHttpRequest()
        x.open("GET", Qt.resolvedUrl("_probe_theme.json"), false); x.send(null)
        Theme.load(x.responseText)
    }
    Column {
        anchors.centerIn: parent; spacing: 26
        Caption { text: "Export button — tightened glow" }
        Row { spacing: 16
            GhostButton { text: "Preview" }
            PrimaryButton { text: "Export" }
        }
    }
}
