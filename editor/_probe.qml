import QtQuick
import QtQuick.Window
import "controls"
Window {
    width: 520; height: 150; visible: true; title: "flatprobe"
    color: Theme.bg
    Component.onCompleted: {
        var x = new XMLHttpRequest()
        x.open("GET", Qt.resolvedUrl("_probe_theme.json"), false); x.send(null)
        Theme.load(x.responseText)
    }
    Row {
        anchors.centerIn: parent; spacing: 18
        GhostButton { text: "Preview" }
        PrimaryButton { text: "Export" }
    }
}
