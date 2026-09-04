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
import QtQuick.Dialogs
import "controls" as C
import "inspectors" as I

ScrollView {
    id: root

    readonly property var st: Bridge.state
    // The segment's own values when one is selected, the whole-take setting otherwise.
    // `editable` and the badge reasons stay global: a burned-in recording is burned in
    // whichever segment is selected.
    readonly property var cam: {
        var g = st.webcam || ({})
        if (!camSeg)
            return g
        var m = {}
        for (var k in g)
            m[k] = g[k]
        m.rect = camSeg.rect
        m.shape = camSeg.shape
        m.mirror = camSeg.mirror
        m.corner_radius = camSeg.corner_radius
        m.size = camSeg.size
        m.enabled = camSeg.enabled
        return m
    }
    readonly property var zoom: st.edit ? st.edit.zoom : ({})
    readonly property var backdrop: st.backdrop || ({})
    readonly property real msPerFrame: st.timebase ? st.timebase.ms_per_frame : 1000 / 60

    property string selectedId: ""
    // Typed, not `Item`. As a bare Item every member reached through it --
    // preview.timelineFrame, preview.togglePlay(), preview.hasScreen -- was
    // unresolvable, so qmllint could not tell a real typo from a valid call and
    // reported nineteen of them as noise. Typed, they are checked.
    property Preview preview: null
    signal selectLayer(string id)

    // The selected CAMERA segment, if the selection is one. When it is, the webcam
    // controls edit that segment instead of the whole-take default -- same controls,
    // same field names, one extra id on the way out. Without this the panel would show
    // the global values while writing to the segment, which is the kind of quiet
    // disagreement that makes people distrust the whole inspector.
    readonly property var camTrack: st.webcam_track ? st.webcam_track.segments : []
    readonly property var camSeg: {
        for (var i = 0; i < camTrack.length; ++i)
            if (camTrack[i].id === selectedId)
                return camTrack[i]
        return null
    }
    readonly property string camSegId: camSeg ? camSeg.id : ""

    // One writer for every webcam control, so a segment can never be edited by some
    // controls and the global setting by others.
    function camOp(args) {
        if (root.camSegId !== "")
            args.id = root.camSegId
        Bridge.op("set_webcam", args)
    }

    readonly property int selZoom: preview ? preview.selectedZoomIndex : -1
    readonly property var selSeg: preview && selZoom >= 0 && selZoom < preview.zoomSegments.length
                                  ? preview.zoomSegments[selZoom] : null

    // The selected layer's resolved spec, or null. Everything below routes on this.
    //
    // Camera segments are excluded ON PURPOSE. They ARE layers, but this panel treats
    // "a layer is selected" as "hide the recording panels and show a layer inspector"
    // -- and the camera's inspector IS one of those recording panels. Selecting a
    // segment therefore hid the very controls meant to edit it: click a block on the
    // camera row and the whole webcam section vanished, with no layer inspector to
    // replace it because there is no `webcam` case among the type-specific sections.
    // Routing on camSeg instead keeps the webcam panel up and pointed at the segment.
    readonly property var selLayer: {
        var layers = st.layers || []
        for (var i = 0; i < layers.length; ++i)
            if (layers[i].id === selectedId && layers[i].type !== "webcam")
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
    // Canvas pixels are the currency every placement control posts in, the same one the
    // inspectors use (see inspectors/ImageInspector.qml).
    readonly property var canvas: Bridge.state.canvas || ({ width: 1920, height: 1080 })
    readonly property var cursor: st.cursor || ({})
    readonly property bool cursorEditable: cursor.editable !== false && cursor.samples > 0
    // Both gates, because the sliders below are meaningless without a track AND
    // pointless while the pointer is switched off.
    readonly property bool cursorOn: cursorEditable && cursor.enabled === true
    readonly property bool camEditable: cam.editable === true
    // The shape chips' values, positionally paired with their labels below. One list,
    // named once, so the two can no longer drift out of step with each other.
    readonly property var shapeValues: ["circle", "rounded", "rect"]
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
        // WHEN a layer plays. Above the per-type inspectors because it applies to
        // every one of them, and because "make this the start" is a thing people
        // reach for before they think about the layer's own settings.
        //
        // The recording is not the whole timeline any more: head and tail pads are
        // output-only time where nothing was recorded, so a card can precede or follow
        // the take rather than only cover it. The camera is excluded -- it is recorded
        // footage and there is none in a pad.
        // ====================================================================
        ColumnLayout {
            Layout.fillWidth: true
            visible: root.selLayer !== null && root.selLayer.type !== "webcam"
            spacing: 7

            C.Caption { text: "when" }

            C.Segmented {
                Layout.fillWidth: true
                model: ["Intro", "In video", "Outro"]
                readonly property var padValues: ["head", "", "tail"]
                // Bound to the model and never assigned locally, so a rejected change
                // snaps the chips back rather than lying about what was stored.
                currentIndex: root.selLayer
                              ? Math.max(0, padValues.indexOf(root.selLayer.pad || ""))
                              : 1
                onActivated: function (i) {
                    Bridge.op("update_layer",
                              { id: root.selectedId, pad: padValues[i] })
                }
            }

            Text {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                // Says the constraint rather than letting it be discovered in an
                // export: a pad is time that was never recorded, so there is no
                // screen, no camera and no sound in it.
                text: !root.selLayer || !root.selLayer.pad
                      ? "Plays over the recording."
                      : (root.selLayer.pad === "head"
                         ? "Plays before the recording starts — no video, camera or sound there."
                         : "Plays after the recording ends — no video, camera or sound there.")
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
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
        I.CaptionInspector {
            Layout.fillWidth: true
            visible: root.selLayer !== null && root.selLayer.type === "caption"
            spec: visible ? root.selLayer : ({})
            canvas: root.canvas
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

            // -- cursor (spec §1e, the second half of the zoom inspector) -----
            // Spec: "Zoom properties use accent sliders, cursor properties use text-3
            // sliders -- accent marks what the panel is ABOUT." So every slider here is
            // subject:false, and that is the whole visual difference between the two
            // halves of this panel.
            //
            // The capture runs with the hardware cursor off, so without these the export
            // has no pointer at all -- which is why `enabled` defaults on rather than
            // being something to discover.
            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                C.Caption { text: "cursor"; Layout.fillWidth: true }
                Rectangle {
                    visible: !root.cursorEditable
                    width: cursorBadge.implicitWidth + 14
                    height: 17
                    radius: 5
                    color: Theme.fillSubtle
                    Text {
                        id: cursorBadge
                        anchors.centerIn: parent
                        text: "no track"
                        color: Theme.text5
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fsHint
                    }
                }
                C.Toggle {
                    visible: root.cursorEditable
                    checked: root.cursor.enabled === true
                    onToggled: function (v) { Bridge.op("set_cursor", { enabled: v }) }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: cursorWhy.implicitHeight + 20
                visible: !root.cursorEditable
                radius: Theme.radiusRow
                color: Qt.rgba(1, 1, 1, 0.025)
                Text {
                    id: cursorWhy
                    x: 12
                    y: 10
                    width: parent.width - 24
                    text: root.cursor.disabled_reason
                          || "This recording has no cursor track, so there is no pointer "
                           + "to draw."
                    color: Theme.text3
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsCaption
                    lineHeight: 1.4
                    wrapMode: Text.WordWrap
                }
            }

            LabelledSlider {
                Layout.fillWidth: true
                label: "size"
                subject: false
                from: root.cursor.min_size === undefined ? 0.008 : root.cursor.min_size
                to: root.cursor.max_size === undefined ? 0.08 : root.cursor.max_size
                modelValue: root.cursor.size === undefined ? 0.022 : root.cursor.size
                // Pixels, not the normalized fraction: the fraction is the honest unit
                // for the model and a meaningless one to a person sizing a pointer.
                display: Math.round(liveValue * (root.canvas.height || 1080)) + " px"
                enabled: root.cursorOn
                onCommitted: function (v) { Bridge.op("set_cursor", { size: v }) }
            }

            LabelledSlider {
                Layout.fillWidth: true
                label: "smoothing"
                subject: false
                from: 0.0
                to: 1.0
                modelValue: root.cursor.smoothing === undefined ? 0.5 : root.cursor.smoothing
                display: liveValue < 0.02 ? "off" : Math.round(liveValue * 80) + " ms"
                enabled: root.cursorOn
                onCommitted: function (v) { Bridge.op("set_cursor", { smoothing: v }) }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                C.Toggle {
                    checked: root.cursor.click_ripple === true
                    enabled: root.cursorOn
                    onToggled: function (v) { Bridge.op("set_cursor", { click_ripple: v }) }
                }
                Text {
                    text: "Click ripple"
                    color: root.cursorOn ? Theme.text3 : Theme.text6
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fsRow
                    Layout.fillWidth: true
                }
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
                    onToggled: function (v) { root.camOp({ enabled: v }) }
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
                    onToggled: function (v) { root.camOp({ mirror: v }) }
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
                //
                // Labels and values are ONE list apart and must stay the same length.
                // They were not: three labels against a four-value list meant "Rounded"
                // wrote `squircle`, "Rect" wrote `rounded`, and a project already saved
                // as `rect` lit no chip at all.
                currentIndex: root.shapeValues.indexOf(root.cam.shape)
                onActivated: function (i) {
                    root.camOp({ shape: root.shapeValues[i] })
                }
            }

            C.Caption { text: "placement"; opacity: root.camEditable ? 1.0 : 0.5 }

            // Spec §1g: "The grid is the primary placement control; dragging the bubble
            // on canvas is the secondary one." Dragging was the only one that existed,
            // which made corner placement -- the thing every recording actually wants --
            // a freehand gesture you had to eyeball against the frame edge.
            I.PlacementGrid {
                Layout.fillWidth: true
                enabled: root.camEditable
                currentCell: {
                    var r = root.cam.rect
                    if (!r || !root.canvas)
                        return -1
                    var cx = r.x + r.width / 2
                    var cy = r.y + r.height / 2
                    var col = Math.min(2, Math.max(0, Math.floor(cx / (root.canvas.width / 3))))
                    var row = Math.min(2, Math.max(0, Math.floor(cy / (root.canvas.height / 3))))
                    return row * 3 + col
                }
                onPlaced: function (col, row) {
                    var m = 0.04 * Math.min(root.canvas.width, root.canvas.height)
                    var w = root.cam.rect.width
                    var h = root.cam.rect.height
                    var x = col === 0 ? m : col === 1 ? (root.canvas.width - w) / 2
                                                      : root.canvas.width - w - m
                    var y = row === 0 ? m : row === 1 ? (root.canvas.height - h) / 2
                                                      : root.canvas.height - h - m
                    root.camOp({ rect: { x: x, y: y, width: w, height: h } })
                }
            }

            C.Caption { text: "size"; opacity: root.camEditable ? 1.0 : 0.5 }

            // The camera could only ever be resized by finding an invisible 22px target
            // on a 7px dot, and only after clicking the camera to select it first. The
            // grip still works and is still the fast way; this is the one you can find.
            LabelledSlider {
                Layout.fillWidth: true
                label: "width"
                from: 0.04
                to: 0.60
                modelValue: root.cam.size === undefined ? 0.14 : root.cam.size
                display: Math.round(liveValue * 100) + "% of frame"
                enabled: root.camEditable
                onCommitted: function (v) { root.camOp({ size: v }) }
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

            // The grounds. Above the sliders because it is the choice that changes what
            // the recording looks like; padding and radius are adjustments to it.
            C.SwatchGrid {
                Layout.fillWidth: true
                enabled: root.backdrop.enabled === true
                opacity: enabled ? 1.0 : 0.45
                catalogue: Bridge.backgrounds
                currentId: root.backdrop.background || "custom"
                customColor: root.backdrop.color || "#1b1d24"
                onPicked: function (id) { Bridge.op("set_backdrop", { background: id }) }
                onCustomPicked: colorDialog.open()
            }

            ColorDialog {
                id: colorDialog
                selectedColor: root.backdrop.color || "#1b1d24"
                // Sending `color` is what flips the backdrop back to `custom`, so the
                // well cannot be outranked by a swatch that is still selected.
                onAccepted: Bridge.op("set_backdrop",
                                      { color: selectedColor.toString().substring(0, 7) })
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
