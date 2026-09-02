// One project layer, drawn exactly where the export will draw it.
//
// Every number here -- x, y, width, height, the blur strength, the text centre -- comes
// from the resolved payload the bridge built out of geometry.py. Nothing on this side
// multiplies a normalized coordinate by a canvas dimension; that mapping exists once,
// in Placement.resolve, and this file is the reason it must stay that way.
//
// Note there is no `anchors.fill` anywhere in here. Anchors silently override explicit
// x/y with no warning, and this item lives inside a transformed stage.
import QtQuick
import QtQuick.Effects

Item {
    id: root

    // Named `spec`, not `layer`: Item.layer is FINAL and shadowing it makes the whole
    // type fail to load with "Cannot override FINAL property".
    property var spec: ({})
    property int frame: 0
    property bool selected: false
    // What a redaction reads its pixels from: the composited content beneath, captured
    // after the zoom transform, because the export's redact also crops the post-zoom
    // stream.
    property Item contentSource: null
    property bool interactive: true

    signal clicked()
    signal moved(var rect)      // canvas pixels, on release

    readonly property var rect: spec.rect || ({ x: 0, y: 0, width: 0, height: 0 })
    // Gates are half-open frame ranges: `t.start <= f < t.end`, the same interval the
    // export writes as gte(n,A)*lt(n,B). between() is inclusive at both ends and lights
    // one extra frame.
    readonly property bool inRange: !spec.t || (frame >= spec.t.start && frame < spec.t.end)

    // Never assigned to directly: a JS assignment to x would destroy the binding and
    // the item would stop tracking the model for the rest of the session. A live drag
    // switches the binding to the local values instead.
    property bool dragging: false
    property real liveX: 0
    property real liveY: 0
    property real liveW: 0
    property real liveH: 0

    x: dragging ? liveX : rect.x
    y: dragging ? liveY : rect.y
    width: dragging ? liveW : rect.width
    height: dragging ? liveH : rect.height
    visible: spec.enabled !== false && inRange
    opacity: spec.opacity === undefined ? 1 : spec.opacity

    Loader {
        id: body
        x: 0
        y: 0
        width: root.width
        height: root.height
        sourceComponent: {
            switch (root.spec.type) {
            case "image": return imageComp
            case "text": return textComp
            case "shape": return shapeComp
            case "blur": return blurComp
            case "pixelate": return pixelateComp
            default: return unknownComp
            }
        }
    }

    Component {
        id: imageComp
        Image {
            source: root.spec.source || ""
            fillMode: Image.Stretch
            smooth: true
            asynchronous: true
        }
    }

    Component {
        id: shapeComp
        Rectangle {
            // The colour and its alpha arrive split, because the project stores one
            // 'colour@alpha' property and Qt cannot parse that spelling -- handed the
            // raw string it silently drew white.
            color: root.spec.shape ? root.spec.shape.color : "#ff3b30"
            opacity: root.spec.shape && root.spec.shape.opacity !== undefined
                     ? root.spec.shape.opacity : 1
            radius: root.spec.shape ? root.spec.shape.radius : 0
        }
    }

    Component {
        id: textComp
        Item {
            Rectangle {
                x: 0; y: 0
                width: parent.width; height: parent.height
                color: root.spec.text.box_color
                opacity: root.spec.text.box_opacity
                radius: root.spec.text.radius
            }
            Text {
                // Centre-anchored on purpose: left-anchored text drifted ~7% of the
                // string width against drawtext, because the two engines' glyph
                // advances differ even on the same font file.
                x: (root.spec.text.cx - root.rect.x) - width / 2
                y: (root.spec.text.cy - root.rect.y) - height / 2
                text: root.spec.text.text
                color: root.spec.text.color
                font.pixelSize: root.spec.text.pixelSize
                // The font FILE the bridge names, which is the file drawtext is given.
                // Resolving a family name instead lets fontconfig hand Qt and libavfilter
                // different faces, and the metrics diverge before the glyphs visibly do.
                // This is the layer's font, not the editor's chrome font, so it is
                // deliberately not Theme.fontFamily.
                font.family: layerFont.status === FontLoader.Ready
                             ? layerFont.name : "monospace"
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }
    }

    Component {
        id: blurComp
        Item {
            clip: true
            ShaderEffectSource {
                id: src
                x: 0; y: 0
                width: parent.width; height: parent.height
                sourceItem: root.contentSource
                sourceRect: Qt.rect(root.rect.x, root.rect.y, root.rect.width, root.rect.height)
                live: true
                hideSource: false
                visible: false
            }
            MultiEffect {
                x: 0; y: 0
                width: parent.width; height: parent.height
                source: src
                // Straight from geometry.qml_blur(): biased stronger than the export's
                // gblur, because a redaction that looks sufficient here and renders
                // weaker is a data leak.
                blurEnabled: root.spec.blur ? root.spec.blur.blurEnabled : true
                blurMax: root.spec.blur ? root.spec.blur.blurMax : 48
                blur: root.spec.blur ? root.spec.blur.blur : 1.0
                autoPaddingEnabled: false
            }
        }
    }

    Component {
        id: pixelateComp
        Item {
            clip: true
            ShaderEffectSource {
                id: psrc
                x: 0; y: 0
                width: parent.width; height: parent.height
                sourceItem: root.contentSource
                sourceRect: Qt.rect(root.rect.x, root.rect.y, root.rect.width, root.rect.height)
                // Downsample to one texel per block and let the nearest-neighbour
                // upscale do the pixelation; Qt has no pixelate effect, and this is the
                // same operation ffmpeg's pixelize performs.
                textureSize: Qt.size(Math.max(1, root.rect.width / block),
                                     Math.max(1, root.rect.height / block))
                readonly property real block: root.spec.pixelate ? root.spec.pixelate.block : 16
                smooth: false
                live: true
                hideSource: false
                visible: false
            }
            ShaderEffectSource {
                x: 0; y: 0
                width: parent.width; height: parent.height
                sourceItem: psrc
                smooth: false
            }
        }
    }

    FontLoader {
        id: layerFont
        source: "file://" + (Bridge.state.font_file || "")
    }

    Component {
        id: unknownComp
        Rectangle {
            color: "transparent"
            border.color: Theme.accent
            border.width: 2
            Text {
                x: 6; y: 4
                text: "unsupported layer: " + root.spec.type
                color: Theme.accent
                font.pixelSize: 16
            }
        }
    }

    // 1.5px ring, not a heavy border: rings mean selected everywhere in this design
    // (spec §1 "Selected card / keyframe").
    Rectangle {
        x: 0; y: 0
        width: root.width; height: root.height
        visible: root.selected
        color: "transparent"
        border.color: Theme.accent
        border.width: 1.5
    }

    MouseArea {
        id: dragArea
        x: 0; y: 0
        width: root.width; height: root.height
        enabled: root.interactive
        cursorShape: Qt.OpenHandCursor
        property real ox: 0
        property real oy: 0
        onPressed: function (m) {
            root.clicked()
            root.beginDrag()
            ox = m.x
            oy = m.y
        }
        onPositionChanged: function (m) {
            if (!pressed)
                return
            // The item follows the pointer while the drag is live; on release the
            // bridge's clamped answer replaces it, so the model never learns a
            // coordinate that geometry.py did not produce.
            root.liveX += m.x - ox
            root.liveY += m.y - oy
        }
        onReleased: root.endDrag()
    }

    // Bottom-right resize grip. Square-ish handles at canvas scale would be invisible
    // on a scaled-down stage, so it is sized in canvas pixels against the stage scale.
    // The grip reads as a small corner handle; the transparent rectangle around it is
    // the hit area, kept at gripSize because a 7px target is not draggable.
    Rectangle {
        id: grip
        visible: root.selected && root.interactive
        width: root.gripSize
        height: root.gripSize
        x: root.width - width / 2
        y: root.height - height / 2
        color: "transparent"
        Rectangle {
            anchors.centerIn: parent
            width: 7; height: 7; radius: 3.5
            color: Theme.accent
        }
        MouseArea {
            x: 0; y: 0
            width: parent.width; height: parent.height
            cursorShape: Qt.SizeFDiagCursor
            onPressed: root.beginDrag()
            onPositionChanged: function (m) {
                if (!pressed)
                    return
                root.liveW = Math.max(root.gripSize, root.liveW + m.x - grip.width / 2)
                root.liveH = Math.max(root.gripSize, root.liveH + m.y - grip.height / 2)
            }
            onReleased: root.endDrag()
        }
    }

    property real gripSize: 18

    function beginDrag() {
        liveX = rect.x
        liveY = rect.y
        liveW = rect.width
        liveH = rect.height
        dragging = true
    }

    function endDrag() {
        if (!dragging)
            return
        dragging = false
        moved({ x: liveX, y: liveY, width: liveW, height: liveH })
    }
}
