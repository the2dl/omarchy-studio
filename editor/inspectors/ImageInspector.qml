// The image layer inspector (spec §2b): thumbnail well -> provenance -> size ->
// opacity -> placement grid -> timing.
//
// The thumbnail is the real asset, never a stand-in -- the bridge serves the copied
// file's URI, and the meta line reads its true pixel size off the decoded image. The
// one hardcoded phrase, "copied into the project", is a provenance statement the spec
// requires verbatim: add_image copies the file into the bundle's assets/, so deleting
// the original cannot break the project, and the inspector is where the user learns
// that.
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

    // -- thumbnail well, 74px (mock 2b) ----------------------------------
    Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: 74
        radius: Theme.radiusRow
        color: Qt.rgba(1, 1, 1, 0.04)
        clip: true

        Image {
            id: thumb
            anchors.fill: parent
            anchors.margins: 4
            source: root.spec.source || ""
            fillMode: Image.PreserveAspectFit
            smooth: true
            asynchronous: true
        }
        Text {   // nf-fa-image, the well's honest empty state while the asset loads
            anchors.centerIn: parent
            visible: thumb.status !== Image.Ready
            text: ""
            color: Theme.text6
            font.family: Theme.fontFamily
            font.pixelSize: 24
        }
    }

    Text {
        Layout.fillWidth: true
        text: {
            var name = (root.spec.props && root.spec.props.asset) ? root.spec.props.asset : ""
            var ext = name.indexOf(".") >= 0 ? name.split(".").pop().toUpperCase() : ""
            var dims = thumb.status === Image.Ready
                       ? thumb.sourceSize.width + "×" + thumb.sourceSize.height : ""
            return [dims, ext, "copied into the project"].filter(function (s) { return s !== "" }).join(" · ")
        }
        color: Theme.text6
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fsHint
        wrapMode: Text.WordWrap
    }

    // -- size: the layer's width as a fraction of the canvas, resized about its own
    // centre so growing a logo does not walk it across the screen. The rect intent is
    // canvas pixels, the same currency a drag posts; geometry.py clamps and normalizes.
    ModelSlider {
        Layout.fillWidth: true
        label: "size"
        from: 2
        to: 100
        modelValue: root.canvas.width > 0 ? root.rect.width / root.canvas.width * 100 : 0
        display: Math.round(liveValue) + "%"
        onCommitted: function (pct) {
            var w = Math.max(8, pct / 100 * root.canvas.width)
            var h = root.rect.width > 0 ? w * root.rect.height / root.rect.width : w
            Bridge.op("update_layer", { id: root.spec.id, rect: {
                x: root.rect.x + root.rect.width / 2 - w / 2,
                y: root.rect.y + root.rect.height / 2 - h / 2,
                width: w, height: h } })
        }
    }

    ModelSlider {
        Layout.fillWidth: true
        label: "opacity"
        subject: false   // secondary per the mock: the text3 fill, not accent
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
            // 4% of the short side as the edge margin -- the same breathing room the
            // webcam's default corner placement uses.
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
