// The redaction inspector (spec §2a inspector / §2b): method -> strength -> the
// standing note -> timing -> geometry.
//
// Strength is three presets and never a slider. The presets live in
// geometry.REDACT_PRESETS and the weakest already exceeds what OCR can recover; a
// slider's whole purpose would be to find "looks about right", which is exactly the
// setting that leaks a password. The shield panel below carries that rationale
// verbatim, per the spec, so the constraint reads as a decision rather than a gap.
//
// Method changes swap the LAYER TYPE (blur / pixelate / shape are distinct types in
// the project format), so a change is add-new -> carry z and timing -> delete-old,
// with selection following the survivor.
import QtQuick
import QtQuick.Layouts
import ".."
import "../controls" as C

ColumnLayout {
    id: root

    property var spec: ({})
    signal selectLayer(string id)

    readonly property var st: Bridge.state
    readonly property var rect: spec.rect || ({ x: 0, y: 0, width: 0, height: 0 })
    readonly property real msPerFrame: st.timebase ? st.timebase.ms_per_frame : 1000 / 60
    readonly property string preset: (spec.props && spec.props.preset) ? spec.props.preset : "strong"
    readonly property int methodIndex: spec.type === "pixelate" ? 1 : spec.type === "shape" ? 2 : 0

    spacing: 14

    function switchMethod(i) {
        if (i === methodIndex)
            return
        var target = ["blur", "pixelate", "shape"][i]
        var old = spec
        var before = {}
        var layers = st.layers || []
        for (var k = 0; k < layers.length; ++k)
            before[layers[k].id] = true
        var args = { rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } }
        if (target === "blur")
            args.preset = root.preset
        Bridge.op("add_" + target, args, function (s, ok) {
            if (!ok || !s || !s.layers)
                return
            var id = ""
            for (var j = 0; j < s.layers.length; ++j)
                if (!before[s.layers[j].id])
                    id = s.layers[j].id
            if (id === "")
                return
            var up = { id: id, z: old.z }
            if (old.t) {
                up.start_ms = old.t.start * root.msPerFrame
                up.end_ms = old.t.end * root.msPerFrame
            }
            // Fill is a redaction, not a decoration: solid ink, marked so the router
            // and the canvas hatch keep treating it as one. Pixelate carries the preset
            // forward for the day the bridge honors it (see the report note on block).
            up.props = target === "shape"
                       ? { color: "#000000", redact: true }
                       : { preset: root.preset }
            Bridge.op("update_layer", up, function () {
                Bridge.op("delete_layer", { id: old.id }, function () {
                    root.selectLayer(id)
                })
            })
        })
    }

    C.Caption { text: "method" }

    C.Segmented {
        Layout.fillWidth: true
        model: ["Blur", "Pixelate", "Fill"]
        currentIndex: root.methodIndex
        onActivated: function (i) { root.switchMethod(i) }
    }

    // A solid fill has no strength to choose; the section applies to blur/pixelate.
    C.Caption { text: "strength"; visible: root.methodIndex !== 2 }

    PresetChips {
        Layout.fillWidth: true
        visible: root.methodIndex !== 2
        model: ["Strong", "Heavy", "Solid"]
        currentIndex: ["strong", "heavy", "solid"].indexOf(root.preset)
        onActivated: function (i) {
            Bridge.op("update_layer", { id: root.spec.id,
                                        props: { preset: ["strong", "heavy", "solid"][i] } })
        }
    }

    // The standing note, verbatim from the spec (§ "Redaction -- the one hard rule").
    Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: noteRow.implicitHeight + 18
        radius: Theme.radiusRow
        color: Qt.rgba(1, 1, 1, 0.035)

        RowLayout {
            id: noteRow
            x: 11
            y: 9
            width: parent.width - 22
            spacing: 8
            Text {   // nf-fa-shield
                text: ""
                color: Theme.text3
                font.family: Theme.fontFamily
                font.pixelSize: 13
                Layout.alignment: Qt.AlignTop
            }
            Text {
                Layout.fillWidth: true
                text: "Three presets, no slider. The weakest one already exceeds what "
                    + "OCR can recover, and the canvas shows exactly what exports — "
                    + "never a preview that looks safer than the file."
                color: Theme.text4
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
                lineHeight: 1.4
                wrapMode: Text.WordWrap
            }
        }
    }

    Rectangle { Layout.fillWidth: true; height: 1; color: Theme.hairline }

    TimingSection {
        Layout.fillWidth: true
        spec: root.spec
    }

    // "Follow the window" tracks the redaction to the window it covers, so a window
    // move mid-recording cannot slide the secret out from under the box. The tracking
    // does not exist yet: the intent below names the missing capability, the bridge
    // returns the state unchanged, and the toggle stays off -- it must never show ON
    // over a redaction that is actually static, because that is itself the leak.
    RowLayout {
        Layout.fillWidth: true
        spacing: 11
        C.Toggle {
            checked: root.spec.props ? root.spec.props.follow_window === true : false
            onToggled: function (v) {
                Bridge.op("update_layer", { id: root.spec.id, follow_window: v })
            }
        }
        Text {
            text: "Follow the window"
            color: Theme.text4
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsRow
            Layout.fillWidth: true
        }
    }

    Rectangle { Layout.fillWidth: true; height: 1; color: Theme.hairline }

    C.Caption { text: "geometry" }

    GridLayout {
        Layout.fillWidth: true
        columns: 2
        columnSpacing: 8
        rowSpacing: 8

        Repeater {
            model: [
                { k: "x", get: function (r) { return r.x } },
                { k: "y", get: function (r) { return r.y } },
                { k: "w", get: function (r) { return r.width } },
                { k: "h", get: function (r) { return r.height } }
            ]
            delegate: ValueField {
                required property var modelData
                Layout.fillWidth: true
                label: modelData.k
                value: String(Math.round(modelData.get(root.rect)))
                onCommitted: function (t) {
                    var v = parseFloat(t)
                    if (isNaN(v))
                        return
                    var r = { x: root.rect.x, y: root.rect.y,
                              width: root.rect.width, height: root.rect.height }
                    if (modelData.k === "x") r.x = v
                    else if (modelData.k === "y") r.y = v
                    else if (modelData.k === "w") r.width = Math.max(8, v)
                    else r.height = Math.max(8, v)
                    Bridge.op("update_layer", { id: root.spec.id, rect: r })
                }
            }
        }
    }
}
