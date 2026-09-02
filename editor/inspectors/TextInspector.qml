// The text layer inspector (spec §2b): editable field -> style -> size -> color ->
// timing.
//
// Style is Plate / Plain only. The mock also shows Outline, but neither drawtext nor
// the QML tile draws an outlined face today, and a chip that changes nothing reads as
// broken -- so it waits for the renderer, like Slide does in TimingSection.
import QtQuick
import QtQuick.Layouts
import ".."
import "../controls" as C

ColumnLayout {
    id: root

    property var spec: ({})
    signal selectLayer(string id)

    readonly property var textProps: spec.text || ({})

    spacing: 14

    // -- content: the one field on this panel with a caret, and it is accent --------
    Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: 38
        radius: Theme.radiusRow
        color: Theme.fillSubtle

        TextInput {
            id: contentField
            x: 12
            width: parent.width - 24
            anchors.verticalCenter: parent.verticalCenter
            color: Theme.text2
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fsBody
            selectionColor: Theme.accentWash
            selectedTextColor: Theme.text
            clip: true
            cursorDelegate: Rectangle {
                width: 1.5
                color: Theme.accent
                visible: contentField.activeFocus
            }
            // Pushed, not bound: assigning to `text` while typing would sever a binding
            // to the model. Re-pushed only while unfocused so a state refresh cannot
            // eat keystrokes.
            property string model: root.textProps.text || ""
            onModelChanged: if (!activeFocus) text = model
            Component.onCompleted: text = model
            onEditingFinished: {
                focus = false
                if (text !== model)
                    Bridge.op("update_layer", { id: root.spec.id, props: { text: text } })
            }
        }
    }

    C.Caption { text: "style" }

    C.Segmented {
        Layout.fillWidth: true
        model: ["Plate", "Plain"]
        currentIndex: (root.textProps.box_opacity || 0) > 0 ? 0 : 1
        onActivated: function (i) {
            // box_color carries the alpha as one 'colour@alpha' property -- the exact
            // shape layers.split_color expects; a separate opacity key would draw a box
            // the export does not. The plate is the app background at 85%, the mock's
            // caption chip, and radius is normalized to the tile's short side.
            var plate = String(Theme.bg) + "@0.85"
            Bridge.op("update_layer", { id: root.spec.id, props: i === 0
                ? { box_color: plate, radius: 0.15 }
                : { box_color: "black@0.0" } })
        }
    }

    ModelSlider {
        Layout.fillWidth: true
        label: "size"
        from: 12
        to: 220
        modelValue: root.textProps.pixelSize || 32
        display: Math.round(liveValue) + " px"
        onCommitted: function (v) {
            Bridge.op("update_layer", { id: root.spec.id, props: { font_px: Math.round(v) } })
        }
    }

    C.Caption { text: "color" }

    // Four swatches from the mock, all theme-resolved: text / bg / accent / live.
    // 30px, radius 8; the active one gets the 1.5px accent ring, the dark one a faint
    // ring so it does not vanish into the panel.
    Row {
        spacing: 7
        Repeater {
            model: [String(Theme.text), String(Theme.bg), String(Theme.accent), String(Theme.live)]
            delegate: Rectangle {
                required property var modelData
                width: 30
                height: 30
                radius: 8
                color: modelData
                border.width: active ? 1.5 : 1
                border.color: active ? Theme.accent : Theme.hairline
                readonly property bool active:
                    String(root.textProps.color || "").toLowerCase() === modelData.toLowerCase()
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: Bridge.op("update_layer", { id: root.spec.id, props: { color: parent.modelData } })
                }
            }
        }
    }

    TimingSection {
        Layout.fillWidth: true
        spec: root.spec
    }
}
