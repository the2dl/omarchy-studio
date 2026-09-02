// The right inspector: contextual to the selection (spec §1d region 4, §2a/§2b).
//
// A selected layer swaps the whole column for that layer's inspector (image, text,
// redact, shape); otherwise the recording's own panels show -- zoom, webcam, backdrop.
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
import "inspectors" as I

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

    // The selected layer's resolved spec, or null. Everything below routes on this.
    readonly property var selLayer: {
        var layers = st.layers || []
        for (var i = 0; i < layers.length; ++i)
            if (layers[i].id === selectedId)
                return layers[i]
        return null
    }
    readonly property bool selIsRedact: selLayer !== null
        && (selLayer.type === "blur" || selLayer.type === "pixelate"
            || (selLayer.type === "shape" && selLayer.props && selLayer.props.redact === true))

    function shortTime(f) {
        var fps = Math.max(1, st.timebase ? st.timebase.fps : 60)
        var secs = Math.floor(f / fps)
        return Math.floor(secs / 60) + ":" + (secs % 60 < 10 ? "0" : "") + (secs % 60)
    }

    function layerName(l) {
        if (!l)
            return ""
        if (l.type === "image")
            return (l.props && l.props.asset) ? l.props.asset : l.id
        if (l.type === "text")
            return "\"" + ((l.props && l.props.text) ? l.props.text : "") + "\""
        return selIsRedact ? ("Redact · " + l.id) : l.id
    }

    function layerGlyph(l) {
        switch (l ? l.type : "") {
        case "image": return ""
        case "text": return ""
        case "shape": return ""
        case "pixelate": return ""
        default: return ""
        }
    }

    // Whether webcam edits are possible, and if not, which of the two distinct
    // impossibilities applies -- they get different words because they are different
    // facts: burned-in means the camera is IN the frames; no-camera means there is no
    // camera anywhere.
    readonly property bool camEditable: cam.editable === true
    readonly property bool camBurnedIn: st.capture ? st.capture.camera_burned_in === true : false

    contentWidth: availableWidth
    clip: true

    ColumnLayout {
        width: root.availableWidth - 2 * Style.pad
        x: Style.pad
        spacing: 12

        Item { Layout.preferredHeight: 4 }

        // -- contextual header ------------------------------------------------
        // Names the selected object: a layer, a zoom event, or the recording.
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            Text {   // layer glyph, nf-fa-search_plus for zoom, nf-fa-film otherwise
                text: root.selLayer ? root.layerGlyph(root.selLayer)
                    : root.selSeg ? "" : ""
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: 16
            }
            Text {
                Layout.fillWidth: true
                text: root.selLayer ? root.layerName(root.selLayer)
                    : root.selSeg ? ("Zoom " + (root.selZoom + 1))
                    : (st.name || "Recording")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsRow
                elide: Text.ElideRight
            }
            Text {
                text: root.selLayer && root.selLayer.t
                      ? (root.shortTime(root.selLayer.t.start) + " – " + root.shortTime(root.selLayer.t.end))
                    : root.selSeg
                      ? (root.shortTime(root.selSeg.start) + " – " + root.shortTime(root.selSeg.end))
                    : ""
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
            }
        }

        // ====================================================================
        // Layer inspectors (spec §2b). One visible at a time; ColumnLayout
        // skips the hidden ones entirely.
        // ====================================================================
        I.ImageInspector {
            Layout.fillWidth: true
            visible: root.selLayer !== null && root.selLayer.type === "image"
            spec: visible ? root.selLayer : ({})
            onSelectLayer: function (id) { root.selectLayer(id) }
        }
        I.TextInspector {
            Layout.fillWidth: true
            visible: root.selLayer !== null && root.selLayer.type === "text"
            spec: visible ? root.selLayer : ({})
            onSelectLayer: function (id) { root.selectLayer(id) }
        }
        I.RedactInspector {
            Layout.fillWidth: true
            visible: root.selIsRedact
            spec: visible ? root.selLayer : ({})
            onSelectLayer: function (id) { root.selectLayer(id) }
        }
        I.ShapeInspector {
            Layout.fillWidth: true
            visible: root.selLayer !== null && root.selLayer.type === "shape" && !root.selIsRedact
            spec: visible ? root.selLayer : ({})
            onSelectLayer: function (id) { root.selectLayer(id) }
        }

        // ====================================================================
        // Recording panels -- only while no layer is selected.
        // ====================================================================
        ColumnLayout {
            Layout.fillWidth: true
            visible: root.selLayer === null
            spacing: 12

            // -- zoom --------------------------------------------------------
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

            // -- webcam (1g live; 2f when the camera cannot be edited) -------
            // The 2f pattern, verbatim from the spec: the panel STAYS, everything
            // below the header drops to text6, the header toggle is replaced by a
            // badge, and an info panel says why. Two distinct reasons get two
            // distinct badges and explanations -- burned-in (the camera is part of
            // the frames) versus no camera stream at all.
            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                C.Caption { text: "webcam"; Layout.fillWidth: true }
                Rectangle {
                    visible: !root.camEditable
                    width: badgeLabel.implicitWidth + 14
                    height: 17
                    radius: 5
                    color: Theme.fillSubtle
                    Text {
                        id: badgeLabel
                        anchors.centerIn: parent
                        text: root.camBurnedIn ? "burned in" : "no camera"
                        color: Theme.text5
                        font.family: Theme.fontFamily
                        font.pixelSize: 9
                        font.letterSpacing: 9 * 0.06
                        font.capitalization: Font.AllUppercase
                    }
                }
                C.Toggle {
                    visible: root.camEditable
                    checked: root.cam.enabled === true
                    onToggled: function (v) { Bridge.op("set_webcam", { enabled: v }) }
                }
            }

            // Not merely inert: the editor has to say WHICH impossibility applies.
            Rectangle {
                visible: !root.camEditable
                Layout.fillWidth: true
                Layout.preferredHeight: camInfoRow.implicitHeight + 20
                radius: Theme.radiusRow
                color: Qt.rgba(1, 1, 1, 0.025)
                RowLayout {
                    id: camInfoRow
                    x: 12
                    y: 10
                    width: parent.width - 24
                    spacing: 9
                    Text {   // nf-fa-info_circle
                        text: ""
                        color: Theme.text3
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                        Layout.alignment: Qt.AlignTop
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.camBurnedIn
                              ? "This take was recorded fullscreen with self-view on, so "
                                + "the camera is part of the frames. Nothing here can move "
                                + "or remove it."
                              : "This recording has no camera stream. Nothing was captured, "
                                + "so there is nothing to show, move or remove."
                        color: Theme.text3
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsCaption
                        lineHeight: 1.4
                        wrapMode: Text.WordWrap
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                C.Toggle {
                    checked: root.cam.mirror === true
                    enabled: root.camEditable
                    onToggled: function (v) { Bridge.op("set_webcam", { mirror: v }) }
                }
                Text {
                    text: "Mirror"
                    // The disabled ramp from 1h: labels drop to text6, not text3-dim.
                    color: root.camEditable ? Theme.text3 : Theme.text6
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                }
            }

            C.Caption { text: "shape"; opacity: root.camEditable ? 1.0 : 0.5 }

            C.Segmented {
                Layout.fillWidth: true
                model: ["Circle", "Rounded", "Rect"]
                enabled: root.camEditable
                // Bound to the model and never assigned locally (Segmented only
                // emits), so a rejected shape change snaps the chips back.
                currentIndex: ["circle", "rounded", "rect"].indexOf(root.cam.shape)
                onActivated: function (i) {
                    Bridge.op("set_webcam", { shape: ["circle", "rounded", "rect"][i] })
                }
            }

            Text {
                visible: root.camEditable
                text: "Drag the webcam on the canvas to move it; the corner grip resizes."
                color: Theme.text6
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // The one live control on a burned-in panel, because it is the only thing
            // that actually helps: the camera cannot be moved, but it can be covered.
            // Placed over the webcam's resolved box, at export strength.
            Rectangle {
                visible: root.camBurnedIn
                Layout.fillWidth: true
                Layout.preferredHeight: 34
                radius: Theme.radiusRow
                color: coverMa.containsMouse ? Theme.fillHover : Theme.fillSubtle
                Behavior on color { ColorAnimation { duration: Theme.durFast } }
                RowLayout {
                    x: 12
                    width: parent.width - 24
                    height: parent.height
                    spacing: 10
                    Text {   // nf-fa-eye_slash, the redact register
                        text: ""
                        color: Theme.text3
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                    }
                    Text {
                        Layout.fillWidth: true
                        text: "Cover it with a redact layer"
                        color: Theme.text2
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsCaption
                    }
                }
                MouseArea {
                    id: coverMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        var r = root.cam.rect || { x: 0, y: 0, width: 0, height: 0 }
                        var before = {}
                        var layers = root.st.layers || []
                        for (var i = 0; i < layers.length; ++i)
                            before[layers[i].id] = true
                        Bridge.op("add_blur", { rect: {
                            x: r.x, y: r.y, width: r.width, height: r.height } },
                            function (s, ok) {
                                if (!ok || !s.layers)
                                    return
                                for (var j = 0; j < s.layers.length; ++j)
                                    if (!before[s.layers[j].id])
                                        root.selectLayer(s.layers[j].id)
                            })
                    }
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.hairline }

            // -- backdrop ----------------------------------------------------
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
        }

        Item { Layout.preferredHeight: Style.pad }
    }
}
