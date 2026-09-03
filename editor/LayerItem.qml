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
    // Preview mode: the export's frame only. The ring, handles, type chip and the
    // redaction marker hide; the layer's PIXELS -- the blur included -- never change,
    // because the components above already render export strength in every mode.
    property bool previewMode: false

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
    // Held from the drop until the model actually comes back. Releasing the local
    // override the moment the mouse came up rendered a frame at the OLD rect -- the new
    // one is an intent in flight, not applied yet -- so the object visibly snapped back
    // to where it started and then jumped forward. Reported as a "double bounce", and it
    // is one: two position changes for one gesture.
    property bool committing: false
    readonly property bool showingLive: dragging || committing

    // Called by whoever posted the intent, when the reply lands. The timer is only a
    // backstop: an op that errors never calls back, and the override must not stick.
    function commitDone() {
        committing = false
        commitTimeout.stop()
    }
    Timer {
        id: commitTimeout
        interval: 1200
        onTriggered: root.committing = false
    }

    property bool dragging: false
    property real liveX: 0
    property real liveY: 0
    property real liveW: 0
    property real liveH: 0

    x: showingLive ? liveX : rect.x
    y: showingLive ? liveY : rect.y
    width: showingLive ? liveW : rect.width
    height: showingLive ? liveH : rect.height
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

    // -- redaction marker (spec §2a, "the one hard rule") ---------------------
    //
    // The 45° accent hatch at 14% plus a small uppercase "redacted" label mark a
    // redaction as an EDITING OBJECT without changing what it obscures. The pixels
    // underneath are the components above: the real blur/pixelate/fill at export
    // strength, always. There is deliberately no hover, select or scrub state that
    // lightens or removes the obscuring -- a preview that ever shows the un-blurred
    // content is a data leak, whatever it looks like.
    //
    // Suppressed in preview mode, like every other piece of chrome: the parity harness
    // grabs the stage in preview mode and compares it against the export frame at a
    // half-peak threshold, and the accent label measures 165/255 grey -- bright enough
    // to register as geometry (it widened the measured blur silhouette by 257px).
    // Hiding the marker changes nothing about what the redaction renders, which is the
    // thing both the harness and a user pressing P are checking.
    readonly property bool isRedaction: spec.type === "blur" || spec.type === "pixelate"
        || (spec.type === "shape" && spec.props && spec.props.redact === true)
    readonly property bool decorate: isRedaction && !previewMode

    Canvas {
        id: hatch
        x: 0; y: 0
        width: root.width
        height: root.height
        visible: root.decorate
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        property real step: root.px(10)   // mock: 5px stripe, 5px gap at screen scale
        onStepChanged: requestPaint()
        onPaint: {
            var ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)
            ctx.strokeStyle = Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.14)
            ctx.lineWidth = step / 2
            ctx.beginPath()
            for (var d = -height; d < width; d += step) {
                ctx.moveTo(d, height)
                ctx.lineTo(d + height, 0)
            }
            ctx.stroke()
        }
    }

    Text {
        visible: root.decorate
        x: root.px(8)
        y: (root.height - height) / 2
        text: "redacted"
        color: Theme.accent
        font.family: Theme.fontFamily
        font.pixelSize: root.px(10)
        font.letterSpacing: root.px(10) * Theme.capsSpacing
        font.capitalization: Font.AllUppercase
    }

    // 1.5px ring, not a heavy border: rings mean selected everywhere in this design
    // (spec §1 "Selected card / keyframe").
    Rectangle {
        x: 0; y: 0
        width: root.width; height: root.height
        visible: root.selected && !root.previewMode
        color: "transparent"
        border.color: Theme.accent
        border.width: Math.max(1, root.px(1.5))
    }

    MouseArea {
        id: dragArea
        x: 0; y: 0
        width: root.width; height: root.height
        // previewMode spelled out even though Preview.qml already folds it into
        // `interactive`: a standalone LayerItem must go inert on its own.
        enabled: root.interactive && !root.previewMode
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

    // -- selection dressing (spec §2a): 9px accent handles at the four corners plus a
    // type chip above the top-left corner. Everything is sized through px() because
    // this item lives on a scaled stage: a 9px handle authored in canvas pixels would
    // render at 4px on a half-scale stage and be untouchable.
    Repeater {
        model: root.selected && root.interactive && !root.previewMode ? 4 : 0
        delegate: Item {
            required property int index
            // `left`/`top` are FINAL Item anchor properties; shadowing them fails the whole type.
            readonly property bool onLeft: index % 2 === 0
            readonly property bool onTop: index < 2
            x: (onLeft ? 0 : root.width) - width / 2
            y: (onTop ? 0 : root.height) - height / 2
            width: root.px(18)   // the hit target; the visible dot sits inside it
            height: width

            Rectangle {
                anchors.centerIn: parent
                width: root.px(9)
                height: width
                radius: width / 2
                color: Theme.accent
            }
            MouseArea {
                anchors.fill: parent
                cursorShape: (parent.onLeft === parent.onTop) ? Qt.SizeFDiagCursor
                                                          : Qt.SizeBDiagCursor
                onPressed: root.beginDrag()
                onPositionChanged: function (m) {
                    if (!pressed)
                        return
                    var min = root.px(18)
                    var dx = m.x - width / 2
                    var dy = m.y - height / 2
                    // Each corner moves its own two edges; the opposite corner stays
                    // put. Deltas converge because the handle rides the live rect.
                    if (parent.onLeft) {
                        var nw = Math.max(min, root.liveW - dx)
                        root.liveX += root.liveW - nw
                        root.liveW = nw
                    } else {
                        root.liveW = Math.max(min, root.liveW + dx)
                    }
                    if (parent.onTop) {
                        var nh = Math.max(min, root.liveH - dy)
                        root.liveY += root.liveH - nh
                        root.liveH = nh
                    } else {
                        root.liveH = Math.max(min, root.liveH + dy)
                    }
                }
                onReleased: root.endDrag()
            }
        }
    }

    // The type chip: glyph + lowercase type (and the redaction's preset, so the
    // strength is readable at the box itself). Mock 2a: 4px/8px padding, radius 6,
    // bgFloat plate, accent 10px uppercase at 0.06em.
    Rectangle {
        visible: root.selected && !root.previewMode
        x: 0
        // Above the top-left corner, unless the layer touches the canvas top -- the
        // viewport clips, and a chip pushed off the edge is a label nobody can read.
        y: root.y > height + root.px(8) ? -height - root.px(8) : root.px(8)
        width: chipRow.width + root.px(16)
        height: chipRow.height + root.px(9)
        radius: root.px(6)
        color: Theme.bgFloat

        Row {
            id: chipRow
            x: root.px(8)
            anchors.verticalCenter: parent.verticalCenter
            spacing: root.px(7)
            Text {
                text: root.spec.type === "image" ? ""
                    : root.spec.type === "text" ? ""
                    : root.spec.type === "pixelate" ? ""
                    : root.spec.type === "shape" && !root.isRedaction ? ""
                    : ""
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: root.px(11)
            }
            Text {
                text: {
                    if (root.isRedaction) {
                        var p = root.spec.props && root.spec.props.preset ? root.spec.props.preset : ""
                        var how = root.spec.type === "shape" ? "fill" : p
                        return how !== "" ? ("redact · " + how) : "redact"
                    }
                    return root.spec.type
                }
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: root.px(10)
                font.letterSpacing: root.px(10) * 0.06
                font.capitalization: Font.AllUppercase
            }
        }
    }

    // Screen pixels -> stage (canvas) pixels. The stage is one uniform Scale inside a
    // viewport sized canvas*fit, so the factor is read off those two widths; the
    // fallback keeps a LayerItem usable outside that structure (tests, harnesses).
    readonly property real viewScale: parent && parent.parent && parent.width > 0
                                      && parent.parent.width > 0
                                      && parent.parent.width < parent.width
                                      ? parent.parent.width / parent.width : 1
    function px(n) { return n / viewScale }

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
        committing = true
        commitTimeout.restart()
        moved({ x: liveX, y: liveY, width: liveW, height: liveH })
    }
}
