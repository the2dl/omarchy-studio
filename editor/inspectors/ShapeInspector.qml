// The plain shape inspector. The spec details image, text and redact (§2b); a shape
// that is NOT a redaction still needs somewhere honest to live, so it gets the shared
// pieces that have real behaviour behind them -- opacity, position, timing -- and
// nothing invented beyond them. (A shape carrying props.redact routes to
// RedactInspector instead; see SettingsPanel.)
import QtQuick
import QtQuick.Layouts
import ".."
import "../controls" as C

ColumnLayout {
    id: root

    property var spec: ({})
    signal selectLayer(string id)

    readonly property var canvas: Bridge.state.canvas || ({ width: 1920, height: 1080 })
    readonly property var rect: spec.rect || ({ x: 0, y: 0, width: 1, height: 1 })

    spacing: 14

    ModelSlider {
        Layout.fillWidth: true
        label: "opacity"
        from: 0
        to: 1
        modelValue: root.spec.opacity === undefined ? 1 : root.spec.opacity
        display: Math.round(liveValue * 100) + "%"
        onCommitted: function (v) { Bridge.op("update_layer", { id: root.spec.id, opacity: v }) }
    }

    C.Caption { text: "position" }

    PlacementGrid {
        Layout.fillWidth: true
        currentCell: {
            var cx = root.rect.x + root.rect.width / 2
            var cy = root.rect.y + root.rect.height / 2
            var col = Math.min(2, Math.max(0, Math.floor(cx / (root.canvas.width / 3))))
            var row = Math.min(2, Math.max(0, Math.floor(cy / (root.canvas.height / 3))))
            return row * 3 + col
        }
        onPlaced: function (col, row) {
            var m = 0.04 * Math.min(root.canvas.width, root.canvas.height)
            var w = root.rect.width
            var h = root.rect.height
            var x = col === 0 ? m : col === 1 ? (root.canvas.width - w) / 2 : root.canvas.width - w - m
            var y = row === 0 ? m : row === 1 ? (root.canvas.height - h) / 2 : root.canvas.height - h - m
            Bridge.op("update_layer", { id: root.spec.id, rect: { x: x, y: y, width: w, height: h } })
        }
    }

    TimingSection {
        Layout.fillWidth: true
        spec: root.spec
    }
}
