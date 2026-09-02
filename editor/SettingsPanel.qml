// The right inspector: zoom, webcam, backdrop and the layer list (spec §1d region 4).
//
// Every control posts an intent and then displays whatever comes back: none of them
// hold their own value. That is what makes a rejected change (a burned-in webcam, a
// clamped zoom amount) visible instead of leaving the UI showing something the project
// does not contain.
//
// Visual register per the spec: uppercase captions, hairline dividers, no boxes --
// "nothing uses a border to mean container". Accent sliders mark what the panel is
// about (the zoom); webcam and backdrop sliders are secondary and run text3.
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "controls" as C

ScrollView {
    id: root

    readonly property var st: Bridge.state
    readonly property var cam: st.webcam || ({})
    readonly property var zoom: st.edit ? st.edit.zoom : ({})
    readonly property var backdrop: st.backdrop || ({})
    readonly property real msPerFrame: st.timebase ? st.timebase.ms_per_frame : 1000 / 60

    property string selectedId: ""
    property Item preview: null
    signal selectLayer(string id)

    readonly property int selZoom: preview ? preview.selectedZoomIndex : -1
    readonly property var selSeg: preview && selZoom >= 0 && selZoom < preview.zoomSegments.length
                                  ? preview.zoomSegments[selZoom] : null

    function shortTime(f) {
        var fps = Math.max(1, st.timebase ? st.timebase.fps : 60)
        var secs = Math.floor(f / fps)
        return Math.floor(secs / 60) + ":" + (secs % 60 < 10 ? "0" : "") + (secs % 60)
    }

    contentWidth: availableWidth
    clip: true

    ColumnLayout {
        width: root.availableWidth - 2 * Style.pad
        x: Style.pad
        spacing: 12

        Item { Layout.preferredHeight: 4 }

        // -- contextual header ------------------------------------------------
        // Names the selected object: a zoom event when one is picked on the timeline,
        // a layer when one is picked on the canvas, the recording otherwise.
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            Text {   // nf-fa-search_plus for zoom, nf-fa-film otherwise
                text: root.selSeg ? "" : ""
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: 16
            }
            Text {
                Layout.fillWidth: true
                text: root.selSeg ? ("Zoom " + (root.selZoom + 1))
                    : root.selectedId !== "" ? root.selectedId
                    : (st.name || "Recording")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsRow
                elide: Text.ElideRight
            }
            Text {
                text: root.selSeg
                      ? (root.shortTime(root.selSeg.start) + " – " + root.shortTime(root.selSeg.end))
                      : ""
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
        }

        // -- zoom ------------------------------------------------------------
        RowLayout {
            Layout.fillWidth: true
            C.Caption { text: "zoom"; Layout.fillWidth: true }
            Text {
                text: (st.clicks ? st.clicks.length : 0) + " clicks"
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
            }
            C.Toggle {
                checked: root.zoom.enabled === true
                onToggled: function (v) { Bridge.op("set_zoom", { enabled: v }) }
            }
        }

        LabelledSlider {
            Layout.fillWidth: true
            label: "scale"
            from: 1.0
            to: 3.0
            modelValue: root.zoom.amount === undefined ? 1.8 : root.zoom.amount
            display: liveValue.toFixed(2) + "×"
            enabled: root.zoom.enabled === true
            onCommitted: function (v) { Bridge.op("set_zoom", { amount: v }) }
        }

        LabelledSlider {
            Layout.fillWidth: true
            label: "hold after click"
            from: 100
            to: 4000
            modelValue: (root.zoom.hold_frames || 0) * root.msPerFrame
            display: (liveValue / 1000).toFixed(2) + " s"
            enabled: root.zoom.enabled === true
            onCommitted: function (v) { Bridge.op("set_zoom", { hold_ms: v }) }
        }

        LabelledSlider {
            Layout.fillWidth: true
            label: "ease"
            from: 30
            to: 1200
            modelValue: (root.zoom.ease_frames || 0) * root.msPerFrame
            display: (liveValue / 1000).toFixed(2) + " s"
            enabled: root.zoom.enabled === true
            onCommitted: function (v) { Bridge.op("set_zoom", { ease_ms: v }) }
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: Theme.hairline }

        // -- webcam ----------------------------------------------------------
        // The toggle lives on the caption line (1g's pattern) so the whole section can
        // be switched off from one place.
        RowLayout {
            Layout.fillWidth: true
            C.Caption { text: "webcam"; Layout.fillWidth: true }
            C.Toggle {
                checked: root.cam.enabled === true
                enabled: root.cam.editable === true
                onToggled: function (v) { Bridge.op("set_webcam", { enabled: v }) }
            }
        }

        // Not merely inert: a burned-in recording genuinely cannot be edited this way,
        // and the editor has to say which recording made that true.
        Text {
            visible: root.cam.editable === false
            text: root.cam.disabled_reason || ""
            color: Theme.text4
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsCaption
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            enabled: root.cam.editable === true
            C.Toggle {
                checked: root.cam.mirror === true
                enabled: root.cam.editable === true
                onToggled: function (v) { Bridge.op("set_webcam", { mirror: v }) }
            }
            Text {
                text: "Mirror"
                color: root.cam.editable === true ? Theme.text3 : Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsRow
            }
        }

        C.Caption { text: "shape" }

        C.Segmented {
            Layout.fillWidth: true
            model: ["Circle", "Rounded", "Rect"]
            enabled: root.cam.editable === true
            // Bound to the model and never assigned locally (Segmented only emits), so
            // a rejected shape change snaps the chips back to the truth.
            currentIndex: ["circle", "rounded", "rect"].indexOf(root.cam.shape)
            onActivated: function (i) {
                Bridge.op("set_webcam", { shape: ["circle", "rounded", "rect"][i] })
            }
        }

        Text {
            text: "Drag the webcam on the canvas to move it; the corner grip resizes."
            color: Theme.text6
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsHint
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: Theme.hairline }

        // -- backdrop --------------------------------------------------------
        RowLayout {
            Layout.fillWidth: true
            C.Caption { text: "backdrop"; Layout.fillWidth: true }
            C.Toggle {
                checked: root.backdrop.enabled === true
                onToggled: function (v) { Bridge.op("set_backdrop", { enabled: v }) }
            }
        }

        LabelledSlider {
            Layout.fillWidth: true
            label: "padding"
            subject: false
            from: 0.0
            to: 0.2
            modelValue: root.backdrop.padding === undefined ? 0.04 : root.backdrop.padding
            display: (liveValue * 100).toFixed(1) + " %"
            enabled: root.backdrop.enabled === true
            onCommitted: function (v) { Bridge.op("set_backdrop", { padding: v }) }
        }

        LabelledSlider {
            Layout.fillWidth: true
            label: "corner radius"
            subject: false
            from: 0.0
            to: 0.05
            modelValue: root.st.edit ? root.st.edit.backdrop.corner_radius : 0.015
            display: (liveValue * 100).toFixed(2) + " %"
            enabled: root.backdrop.enabled === true
            onCommitted: function (v) { Bridge.op("set_backdrop", { corner_radius: v }) }
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: Theme.hairline }

        // -- layers ----------------------------------------------------------
        C.Caption { text: "layers" }

        Text {
            visible: !st.layers || st.layers.length === 0
            text: "Drop an image on the canvas, or drag out a blur box."
            color: Theme.text6
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsHint
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }

        Repeater {
            model: st.layers || []
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                radius: Theme.radiusRow
                // Ring + wash mean selected; rest state is a bare row, hover a fill.
                color: root.selectedId === modelData.id ? Theme.accentWash
                     : rowMa.containsMouse ? Theme.fillHover : "transparent"
                border.width: root.selectedId === modelData.id ? 1.5 : 0
                border.color: Theme.accent
                Behavior on color { ColorAnimation { duration: Theme.durFast } }

                MouseArea {
                    id: rowMa
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: root.selectLayer(modelData.id)
                }

                Text {
                    x: 10
                    y: 7
                    width: parent.width - rowBtns.width - 24
                    text: modelData.type + " · " + modelData.id
                    color: modelData.enabled === false ? Theme.text5 : Theme.text2
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                    elide: Text.ElideRight
                }
                Text {
                    x: 10
                    y: 24
                    width: parent.width - rowBtns.width - 24
                    text: modelData.t
                          ? ("frames " + modelData.t.start + "–" + modelData.t.end)
                          : "whole recording"
                    color: Theme.text6
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsHint
                    elide: Text.ElideRight
                }
                Row {
                    id: rowBtns
                    x: parent.width - width - 6
                    y: (parent.height - 28) / 2
                    spacing: 2
                    C.GhostButton {   // nf-fa-eye / nf-fa-eye_slash
                        text: modelData.enabled === false ? "" : ""
                        onClicked: Bridge.op("update_layer", {
                            id: modelData.id, enabled: modelData.enabled === false })
                    }
                    C.GhostButton {   // nf-fa-trash
                        text: ""
                        onClicked: Bridge.op("delete_layer", { id: modelData.id })
                    }
                }
            }
        }

        Item { Layout.preferredHeight: Style.pad }
    }
}
