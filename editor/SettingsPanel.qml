// Webcam, zoom, backdrop and the layer list.
//
// Every control posts an intent and then displays whatever comes back: none of them
// hold their own value. That is what makes a rejected change (a burned-in webcam, a
// clamped zoom amount) visible instead of leaving the UI showing something the project
// does not contain.
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ScrollView {
    id: root

    readonly property var st: Bridge.state
    readonly property var cam: st.webcam || ({})
    readonly property var zoom: st.edit ? st.edit.zoom : ({})
    readonly property var backdrop: st.backdrop || ({})
    readonly property real msPerFrame: st.timebase ? st.timebase.ms_per_frame : 1000 / 60

    property string selectedId: ""
    signal selectLayer(string id)

    contentWidth: availableWidth
    clip: true

    ColumnLayout {
        width: root.availableWidth
        spacing: Theme.pad

        // -- webcam ----------------------------------------------------------
        GroupBox {
            Layout.fillWidth: true
            Layout.margins: Theme.pad
            label: Text { text: "Webcam"; color: Theme.foreground; font.bold: true }
            background: Rectangle {
                color: Theme.surface
                radius: Theme.radius
                border.color: Theme.muted
            }

            ColumnLayout {
                width: parent.width
                spacing: 6

                // Not merely inert: a burned-in recording genuinely cannot be edited
                // this way, and the editor has to say which recording made that true.
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: reasonText.implicitHeight + 12
                    visible: root.cam.editable === false
                    color: Qt.rgba(Theme.warning.r, Theme.warning.g, Theme.warning.b, 0.15)
                    border.color: Theme.warning
                    radius: Theme.radius
                    Text {
                        id: reasonText
                        x: 6
                        y: 6
                        width: parent.width - 12
                        text: root.cam.disabled_reason || ""
                        color: Theme.warning
                        wrapMode: Text.WordWrap
                        font.pixelSize: 12
                    }
                }

                CheckBox {
                    text: "Show webcam"
                    enabled: root.cam.editable === true
                    checked: root.cam.enabled === true
                    onToggled: Bridge.op("set_webcam", { enabled: checked })
                    contentItem: Text {
                        text: parent.text
                        color: parent.enabled ? Theme.foreground : Theme.dim
                        leftPadding: parent.indicator.width + 6
                        verticalAlignment: Text.AlignVCenter
                    }
                }

                CheckBox {
                    text: "Mirror"
                    enabled: root.cam.editable === true
                    checked: root.cam.mirror === true
                    onToggled: Bridge.op("set_webcam", { mirror: checked })
                    contentItem: Text {
                        text: parent.text
                        color: parent.enabled ? Theme.foreground : Theme.dim
                        leftPadding: parent.indicator.width + 6
                        verticalAlignment: Text.AlignVCenter
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "Shape"; color: Theme.dim; font.pixelSize: 12 }
                    Repeater {
                        model: ["circle", "rounded", "rect"]
                        Button {
                            text: modelData
                            enabled: root.cam.editable === true
                            checkable: true
                            checked: root.cam.shape === modelData
                            onClicked: Bridge.op("set_webcam", { shape: modelData })
                        }
                    }
                }

                Text {
                    text: "Drag the webcam in the preview to move it; the corner grip resizes."
                    color: Theme.dim
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
            }
        }

        // -- zoom ------------------------------------------------------------
        GroupBox {
            Layout.fillWidth: true
            Layout.margins: Theme.pad
            label: Text { text: "Auto-zoom"; color: Theme.foreground; font.bold: true }
            background: Rectangle {
                color: Theme.surface
                radius: Theme.radius
                border.color: Theme.muted
            }

            ColumnLayout {
                width: parent.width
                spacing: 6

                CheckBox {
                    text: "Zoom on clicks (" + (st.clicks ? st.clicks.length : 0) + " recorded)"
                    checked: root.zoom.enabled === true
                    onToggled: Bridge.op("set_zoom", { enabled: checked })
                    contentItem: Text {
                        text: parent.text
                        color: Theme.foreground
                        leftPadding: parent.indicator.width + 6
                        verticalAlignment: Text.AlignVCenter
                    }
                }

                LabelledSlider {
                    Layout.fillWidth: true
                    label: "Amount"
                    from: 1.0
                    to: 3.0
                    value: root.zoom.amount === undefined ? 1.8 : root.zoom.amount
                    display: liveValue.toFixed(2) + "×"
                    onCommitted: function (v) { Bridge.op("set_zoom", { amount: v }) }
                }

                LabelledSlider {
                    Layout.fillWidth: true
                    label: "Hold"
                    from: 100
                    to: 4000
                    value: (root.zoom.hold_frames || 0) * root.msPerFrame
                    display: (liveValue / 1000).toFixed(2) + "s"
                    onCommitted: function (v) { Bridge.op("set_zoom", { hold_ms: v }) }
                }

                LabelledSlider {
                    Layout.fillWidth: true
                    label: "Ease"
                    from: 30
                    to: 1200
                    value: (root.zoom.ease_frames || 0) * root.msPerFrame
                    display: (liveValue / 1000).toFixed(2) + "s"
                    onCommitted: function (v) { Bridge.op("set_zoom", { ease_ms: v }) }
                }
            }
        }

        // -- backdrop --------------------------------------------------------
        GroupBox {
            Layout.fillWidth: true
            Layout.margins: Theme.pad
            label: Text { text: "Backdrop"; color: Theme.foreground; font.bold: true }
            background: Rectangle {
                color: Theme.surface
                radius: Theme.radius
                border.color: Theme.muted
            }

            ColumnLayout {
                width: parent.width
                spacing: 6

                CheckBox {
                    text: "Inset the screen on a backdrop"
                    checked: root.backdrop.enabled === true
                    onToggled: Bridge.op("set_backdrop", { enabled: checked })
                    contentItem: Text {
                        text: parent.text
                        color: Theme.foreground
                        leftPadding: parent.indicator.width + 6
                        verticalAlignment: Text.AlignVCenter
                    }
                }

                LabelledSlider {
                    Layout.fillWidth: true
                    label: "Padding"
                    from: 0.0
                    to: 0.2
                    value: root.backdrop.padding === undefined ? 0.04 : root.backdrop.padding
                    display: (liveValue * 100).toFixed(1) + "%"
                    onCommitted: function (v) { Bridge.op("set_backdrop", { padding: v }) }
                }

                LabelledSlider {
                    Layout.fillWidth: true
                    label: "Corner"
                    from: 0.0
                    to: 0.05
                    value: root.st.edit ? root.st.edit.backdrop.corner_radius : 0.015
                    display: (liveValue * 100).toFixed(2) + "%"
                    onCommitted: function (v) { Bridge.op("set_backdrop", { corner_radius: v }) }
                }
            }
        }

        // -- layers ----------------------------------------------------------
        GroupBox {
            Layout.fillWidth: true
            Layout.margins: Theme.pad
            label: Text { text: "Layers"; color: Theme.foreground; font.bold: true }
            background: Rectangle {
                color: Theme.surface
                radius: Theme.radius
                border.color: Theme.muted
            }

            ColumnLayout {
                width: parent.width
                spacing: 4

                Text {
                    visible: !st.layers || st.layers.length === 0
                    text: "No layers yet. Drop an image on the preview, or drag out a blur box."
                    color: Theme.dim
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }

                Repeater {
                    model: st.layers || []
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 58
                        color: root.selectedId === modelData.id ? Theme.selection : "transparent"
                        radius: Theme.radius
                        border.color: modelData.supported === false ? Theme.warning : Theme.muted

                        MouseArea {
                            x: 0; y: 0
                            width: parent.width
                            height: parent.height
                            onClicked: root.selectLayer(modelData.id)
                        }

                        Text {
                            x: 8
                            y: 6
                            text: modelData.type + "  " + modelData.id
                            color: Theme.foreground
                            font.pixelSize: 13
                        }
                        Text {
                            x: 8
                            y: 24
                            text: modelData.t
                                  ? ("frames " + modelData.t.start + "–" + modelData.t.end)
                                  : "whole recording"
                            color: Theme.dim
                            font.pixelSize: 11
                        }
                        Row {
                            x: parent.width - width - 8
                            y: 14
                            spacing: 4
                            Button {
                                text: modelData.enabled === false ? "Show" : "Hide"
                                onClicked: Bridge.op("update_layer", {
                                    id: modelData.id, enabled: modelData.enabled === false })
                            }
                            Button {
                                text: "Delete"
                                onClicked: Bridge.op("delete_layer", { id: modelData.id })
                            }
                        }
                    }
                }
            }
        }

        Item { Layout.preferredHeight: Theme.pad }
    }
}
