// The backdrop picker: every ground in the catalogue as a swatch, plus a custom well.
//
// Swatches rather than a list of names, because a background is chosen by eye and a
// name like "Terracotta" tells you nothing about whether it sits well behind THIS
// recording. The grid is dense on purpose -- comparing two grounds means seeing both at
// once, and a scrolling list of large tiles turns that into a memory test.
import QtQuick
import ".."

Item {
    id: root

    // { custom: "custom", entries: [{ id, name, kind, colors, angle }] }
    property var catalogue: ({ custom: "custom", entries: [] })
    property string currentId: "custom"
    property color customColor: "#1b1d24"
    property int columns: 5
    property real cell: 34
    property real gap: 6

    signal picked(string id)
    signal customPicked()

    readonly property var entries: catalogue && catalogue.entries ? catalogue.entries : []
    readonly property int rows: Math.ceil((entries.length + 1) / columns)
    readonly property real cellWidth: (width - gap * (columns - 1)) / columns
    implicitHeight: rows * cell + Math.max(0, rows - 1) * gap

    Grid {
        width: parent.width
        columns: root.columns
        spacing: root.gap

        Repeater {
            model: root.entries
            delegate: Item {
                required property var modelData
                width: root.cellWidth
                height: root.cell

                Rectangle {
                    id: plate
                    anchors.fill: parent
                    radius: 7
                    clip: true
                    color: modelData.kind === "solid" ? modelData.colors[0] : "transparent"

                    // No `line`: catalogue entries carry only an angle, so the swatch
                    // recomputes one for its own box. GradientFill's construction is
                    // checked against backgrounds.gradient_line in tst_gradient.qml.
                    GradientFill {
                        visible: modelData.kind !== "solid"
                        anchors.fill: parent
                        colors: modelData.colors || []
                        angle: modelData.angle || 0
                    }
                }

                // The ring is a sibling of the clipped plate, not a child: a border on
                // the plate is drawn under its own gradient fill and disappears.
                Rectangle {
                    anchors.fill: parent
                    radius: 7
                    color: "transparent"
                    border.width: modelData.id === root.currentId ? 1.5 : 1
                    border.color: modelData.id === root.currentId
                                  ? Theme.accent
                                  : (ma.containsMouse ? Qt.rgba(1, 1, 1, 0.28)
                                                      : Qt.rgba(1, 1, 1, 0.10))
                    Behavior on border.color { ColorAnimation { duration: Theme.durFast } }
                }

                MouseArea {
                    id: ma
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.picked(modelData.id)
                }
            }
        }

        // The custom well, last: the catalogue is a set of good defaults, not a jail.
        Item {
            width: root.cellWidth
            height: root.cell

            Rectangle {
                anchors.fill: parent
                radius: 7
                color: root.customColor
                border.width: root.currentId === root.catalogue.custom ? 1.5 : 1
                border.color: root.currentId === root.catalogue.custom
                              ? Theme.accent
                              : (customMa.containsMouse ? Qt.rgba(1, 1, 1, 0.28)
                                                        : Qt.rgba(1, 1, 1, 0.10))
                Text {
                    anchors.centerIn: parent
                    text: ""          // nf-fa-paint_brush
                    color: Theme.text2
                    font.family: Theme.fontFamily
                    font.pixelSize: 13
                }
            }
            MouseArea {
                id: customMa
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.customPicked()
            }
        }
    }
}
