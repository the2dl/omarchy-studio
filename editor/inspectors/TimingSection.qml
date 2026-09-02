// The shared timing pattern (spec §2b): in/out fields two-up, a "Whole recording"
// toggle that disables them, and the transition segmented.
//
// Values display as m:ss.d because that is what the mock's fields carry ("0:12.4") and
// a frame index means nothing at a glance. Frames -> ms uses the bridge's ms_per_frame
// and the committed intent goes back as milliseconds -- Timebase.to_frame on the Python
// side is the only sanctioned seconds -> frame path, so the boundary snaps there.
import QtQuick
import QtQuick.Layouts
import ".."
import "../controls" as C

ColumnLayout {
    id: root

    property var spec: ({})
    readonly property var st: Bridge.state
    readonly property real msPerFrame: st.timebase ? st.timebase.ms_per_frame : 1000 / 60
    readonly property real durationMs: st.duration_ms || 0
    readonly property bool whole: !spec.t

    spacing: 9

    function fmt(ms) {
        var s = Math.max(0, ms) / 1000
        var m = Math.floor(s / 60)
        var rest = s - m * 60
        var tenths = Math.round(rest * 10) / 10
        return m + ":" + (tenths < 10 ? "0" : "") + tenths.toFixed(1)
    }

    // Accepts "m:ss.d" or bare seconds; NaN on anything else so the caller can refuse.
    function parseMs(text) {
        var t = String(text).trim()
        var m = t.match(/^(\d+):(\d+(?:\.\d+)?)$/)
        if (m)
            return (parseInt(m[1]) * 60 + parseFloat(m[2])) * 1000
        var s = parseFloat(t)
        return isNaN(s) ? NaN : s * 1000
    }

    readonly property real inMs: spec.t ? spec.t.start * msPerFrame : 0
    readonly property real outMs: spec.t ? spec.t.end * msPerFrame : durationMs

    function commitRange(a, b) {
        if (isNaN(a) || isNaN(b))
            return
        Bridge.op("update_layer", { id: spec.id, start_ms: a, end_ms: b })
    }

    C.Caption { text: "timing" }

    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        ValueField {
            Layout.fillWidth: true
            label: "in"
            editable: !root.whole
            value: root.fmt(root.inMs)
            onCommitted: function (t) { root.commitRange(root.parseMs(t), root.outMs) }
        }
        ValueField {
            Layout.fillWidth: true
            label: "out"
            editable: !root.whole
            value: root.fmt(root.outMs)
            onCommitted: function (t) { root.commitRange(root.inMs, root.parseMs(t)) }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: 11
        C.Toggle {
            checked: root.whole
            onToggled: function (v) {
                // On: t = null, the "always on" read (a full-width bar in the timeline).
                // Off: the same span made explicit, so nothing visibly jumps and the
                // fields become editable.
                if (v)
                    Bridge.op("update_layer", { id: root.spec.id, start_ms: null, end_ms: null })
                else
                    root.commitRange(0, root.durationMs)
            }
        }
        Text {
            text: "Whole recording"
            color: root.whole ? Theme.text3 : Theme.text4
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsRow
            Layout.fillWidth: true
        }
    }

    // Cut / Fade only. The renderer has a real fade (layers.py fade_filters) but no
    // slide, and dead chrome reads as broken -- so Slide from the mock is left out
    // until a slide transition exists. The intent below names the op the bridge is
    // missing: update_layer does not yet accept fade_ms, so today the reply comes back
    // unchanged and the chips snap to the truth.
    C.Segmented {
        Layout.fillWidth: true
        model: ["Cut", "Fade"]
        currentIndex: (root.spec.fade_frames || 0) > 0 ? 1 : 0
        onActivated: function (i) {
            Bridge.op("update_layer", { id: root.spec.id, fade_ms: i === 1 ? 300 : 0 })
        }
    }
}
