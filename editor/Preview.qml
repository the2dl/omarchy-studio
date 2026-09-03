// The composited stage: what the export will look like, one frame at a time.
//
// The structure is load-bearing and mirrors the export's compile order:
//
//   stage          canvas-sized, scaled by one Scale transform to fit the panel
//     content      clip box; ShaderEffectSource reads THIS for redactions, so a blur
//                  sees the composited pixels the way ffmpeg's redact crops the
//                  composited stream
//       backdrop   the coloured ground, when one is enabled
//       insetBox   the rounded inset the video sits in (the whole canvas when there is
//                  no backdrop), masked
//         zoomed   transformOrigin TopLeft, scale/x/y straight from Zoom.to_qml
//           screen VideoOutput, explicit width/height
//     layers       overlaid on the output canvas, i.e. NOT inside the zoom or the inset,
//                  because render.py's stage order is cut -> zoom -> backdrop -> layers
//     webcam       likewise
//
// Two rules that have already cost a day between them: transformOrigin must be TopLeft
// (any other origin makes the translation depend on the item's size, giving a zoom that
// scales but never pans), and nothing transformed may use anchors.fill (anchors silently
// override explicit x/y with no warning).
import QtQuick
import QtQuick.Controls.Basic
import QtMultimedia
import QtQuick.Effects
import "controls" as C

Item {
    id: root

    readonly property var st: Bridge.state
    readonly property var canvas: st.canvas || ({ width: 1920, height: 1080 })
    readonly property real msPerFrame: st.timebase ? st.timebase.ms_per_frame : 1000 / 60
    readonly property int sourceFrames: st.source_frames || 0
    readonly property var screenMedia: st.media ? st.media.screen : null
    readonly property var cameraMedia: st.media ? st.media.camera : null
    readonly property bool hasScreen: screenMedia !== null && screenMedia !== undefined && screenMedia.ready === true
    readonly property bool hasCamera: cameraMedia !== null && cameraMedia !== undefined && cameraMedia.ready === true

    // 2e -- while the proxy builds, the canvas shows the recording's FIRST FRAME at
    // 35% opacity behind the progress block (spec §2e). There is no still in the
    // bundle, so the frame comes from the master -- the one bounded use the master
    // gets: primed once, paused, never seeked. The master's pathology is seeking
    // (517-651ms with half the seeks delivering nothing); decoding frame 0 forward is
    // the case that works, and the player unloads the moment the proxy is ready.
    readonly property bool proxyPending: !hasScreen
        && (Bridge.proxyStatus.state === "building" || Bridge.proxyStatus.state === "error")
    readonly property string masterUrl:
        screenMedia && screenMedia.master ? "file://" + screenMedia.master : ""
    readonly property real cameraOffsetMs: st.media ? st.media.camera_offset_ms : 0
    // The sign lives in CameraSync.qml, next to the reasoning for it.
    readonly property real cameraWarmupMs: st.media && st.media.camera_warmup_ms
                                          ? st.media.camera_warmup_ms : 0
    CameraSync {
        id: cameraSync
        offsetMs: root.cameraOffsetMs
        warmupMs: root.cameraWarmupMs
    }

    property string tool: "select"
    property string selectedId: ""
    property bool webcamSelected: false

    // Preview mode (spec §1d "Preview"): the export's frame and nothing else. Chrome
    // -- rings, handles, chips, the redaction marker, the zoom scrim, the rubber band
    // -- hides, and chrome-dependent input goes inert. Selection state is KEPT, only
    // undrawn, so leaving the mode restores exactly what was selected. Playback and
    // scrubbing stay live: timing is the main thing this mode exists to check.
    property bool previewMode: false

    // Scrubbing has to work before the proxy exists, so the frame falls back to a local
    // value when there is nothing to play.
    property int scrubFrame: 0
    readonly property int frame: hasScreen ? Math.round(screenPlayer.position / msPerFrame) : scrubFrame
    readonly property bool playing: screenPlayer.playbackState === MediaPlayer.PlayingState

    // Fit inside the canvas panel with the spec's 46px inset (§1d region 3), not the
    // whole item: the recording floats on the gradient rather than touching the edges.
    readonly property real fit: Math.max(0.01, Math.min(
        (canvasPanel.width - 2 * Style.canvasPad) / canvas.width,
        (canvasPanel.height - 2 * Style.canvasPad) / canvas.height))

    // Zoom events, derived from the resolved track the bridge sends: a run of
    // consecutive non-identity samples is one event, represented by its peak sample.
    // Derived here for DISPLAY only -- the transform applied to the stage still comes
    // one sample at a time from zoomAt(), never from these.
    readonly property var zoomSegments: computeZoomSegments(st.zoom_track)
    property int selectedZoomIndex: -1

    function computeZoomSegments(tr) {
        var out = []
        if (!tr || !tr.frames || tr.frames.length === 0)
            return out
        var start = 0
        var peak = 0
        for (var i = 1; i <= tr.frames.length; ++i) {
            if (i < tr.frames.length && tr.frames[i] === tr.frames[i - 1] + 1) {
                if (tr.scale[i] > tr.scale[peak])
                    peak = i
                continue
            }
            if (tr.scale[peak] > 1.0001)
                out.push({ start: tr.frames[start], end: tr.frames[i - 1] + 1,
                           scale: tr.scale[peak], x: tr.x[peak], y: tr.y[peak] })
            start = i
            peak = i
        }
        return out
    }

    // What the scene graph actually ended up with, read back off the items. The
    // difference between these and the payload is the whole class of bugs where a
    // binding is silently overridden -- anchors.fill on a transformed item does exactly
    // that, with no warning -- so the self-test asserts on these, not on the payload.
    readonly property var appliedWebcamRect: ({
        x: webcam.x, y: webcam.y, width: webcam.width, height: webcam.height,
        visible: webcam.visible, shape: webcam.cam.shape || ""
    })
    readonly property real appliedZoomScale: zoomed.scale
    readonly property real appliedZoomX: zoomed.x
    readonly property real appliedZoomY: zoomed.y

    // Grabs the stage at full canvas resolution, independent of the window size, the
    // display and its scale factor. The geometry test compares this against
    // Placement.resolve.
    function grabStage(path, done) {
        stage.grabToImage(function (result) {
            result.saveToFile(path)
            if (done)
                done()
        }, Qt.size(canvas.width, canvas.height))
    }

    // A seek asked for before the proxy has finished loading is remembered rather than
    // dropped: the editor opens on a bundle whose proxy may still be building, and a
    // click on the timeline in that window must not be silently ignored.
    property real pendingSeekMs: -1

    function seekFrame(f) {
        var clamped = Math.max(0, sourceFrames > 0 ? Math.min(f, sourceFrames - 1) : f)
        scrubFrame = clamped
        var ms = clamped * msPerFrame
        if (hasScreen && screenPlayer.seekable)
            screenPlayer.setPosition(ms)
        else
            pendingSeekMs = ms
        if (hasCamera && cameraPlayer.seekable)
            cameraPlayer.setPosition(cameraSync.cameraMsFor(ms))
    }

    function togglePlay() {
        if (!hasScreen)
            return
        if (playing) {
            screenPlayer.pause()
            cameraPlayer.pause()
        } else {
            screenPlayer.play()
            if (hasCamera)
                cameraPlayer.play()
        }
    }

    function step(delta) {
        screenPlayer.pause()
        cameraPlayer.pause()
        seekFrame(frame + delta)
    }

    // The zoom sample for this frame. The track is sampled per frame by the bridge and
    // holds identity everywhere else; interpolating here would be a second copy of the
    // export's easing, which is the one thing that must not diverge.
    readonly property var zoomNow: zoomAt(frame)

    function zoomAt(f) {
        var identity = { scale: 1, x: 0, y: 0 }
        var tr = st.zoom_track
        if (!tr || !tr.frames || tr.frames.length === 0)
            return identity
        var lo = 0
        var hi = tr.frames.length - 1
        while (lo <= hi) {
            var mid = (lo + hi) >> 1
            if (tr.frames[mid] === f)
                return { scale: tr.scale[mid], x: tr.x[mid], y: tr.y[mid] }
            if (tr.frames[mid] < f)
                lo = mid + 1
            else
                hi = mid - 1
        }
        return identity
    }

    function cutContaining(f) {
        var cuts = st.cuts || []
        for (var i = 0; i < cuts.length; ++i)
            if (f >= cuts[i].start && f < cuts[i].end)
                return cuts[i]
        return null
    }

    clip: true

    Rectangle {
        x: 0; y: 0
        width: root.width
        height: root.height
        color: Theme.bg
    }

    // The canvas ground: canvasA -> canvasB at 150deg (spec §1 "canvas"). Rectangle
    // gradients are axis-aligned only, so a vertical gradient is rotated -30deg inside
    // a masked panel -- same wash, and the mask gives the panel its rounded corners.
    Item {
        id: canvasPanel
        x: 6
        y: 14
        width: root.width - 12
        height: root.height - 28
        layer.enabled: true
        layer.effect: MultiEffect {
            maskEnabled: true
            maskSource: canvasPanelMask
        }
        Rectangle {
            width: Math.max(canvasPanel.width, canvasPanel.height) * 2
            height: width
            x: (canvasPanel.width - width) / 2
            y: (canvasPanel.height - height) / 2
            rotation: -30
            gradient: Gradient {
                GradientStop { position: 0; color: Theme.canvasA }
                GradientStop { position: 1; color: Theme.canvasB }
            }
        }
    }
    Item {
        id: canvasPanelMask
        x: canvasPanel.x; y: canvasPanel.y
        width: canvasPanel.width; height: canvasPanel.height
        visible: false
        layer.enabled: true
        Rectangle {
            x: 0; y: 0
            width: canvasPanelMask.width; height: canvasPanelMask.height
            radius: Theme.radiusPanel
            color: "black"
        }
    }

    // The recording's drop shadow -- 0 28px 64px rgba(0,0,0,0.6) per spec §1d. Drawn
    // from a stand-in rectangle so the video pixels themselves never re-render for it.
    Rectangle {
        id: recordingGround
        x: viewport.x; y: viewport.y
        width: viewport.width; height: viewport.height
        radius: Theme.radiusRow
        color: Theme.bgDeep
        visible: false
    }
    MultiEffect {
        source: recordingGround
        x: recordingGround.x; y: recordingGround.y
        width: recordingGround.width; height: recordingGround.height
        shadowEnabled: true
        shadowColor: Qt.rgba(0, 0, 0, 0.6)
        shadowVerticalOffset: 28
        shadowBlur: 1.0
        blurMax: 64
    }

    // Picking a redaction or text tool arms a rubber band and changes nothing you can
    // see, so the rail read as three buttons that do nothing. They always worked; there
    // was simply no way to find out that the canvas was now waiting for a drag.
    Rectangle {
        id: toolHint
        z: 60
        visible: root.tool !== "select" && !root.previewMode
        x: canvasPanel.x + (canvasPanel.width - width) / 2
        y: canvasPanel.y + 14
        width: hintRow.width + 22
        height: 30
        radius: 15
        color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.14)
        border.width: 1
        border.color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.45)
        opacity: visible ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.durFast } }

        Row {
            id: hintRow
            anchors.centerIn: parent
            spacing: 8
            Text {
                text: "\uf05b"   // nf-fa-crosshairs
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: 13
                anchors.verticalCenter: parent.verticalCenter
            }
            Text {
                text: root.tool === "blur" ? "Drag a box over what to blur"
                      : root.tool === "pixelate" ? "Drag a box over what to pixelate"
                      : root.tool === "text" ? "Drag a box where the text should sit"
                      : "Drag a box on the recording"
                color: Theme.text2
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsRow
                anchors.verticalCenter: parent.verticalCenter
            }
            Text {
                text: "Esc to cancel"
                color: Theme.text5
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsCaption
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    Item {
        id: viewport
        x: canvasPanel.x + (canvasPanel.width - width) / 2
        y: canvasPanel.y + (canvasPanel.height - height) / 2
        width: root.canvas.width * root.fit
        height: root.canvas.height * root.fit
        clip: true
        // The recording carries its own 10px radius on the canvas (spec §1d region 3).
        layer.enabled: true
        layer.effect: MultiEffect {
            maskEnabled: true
            maskSource: viewportMask
        }

        // The 2e first frame, under the stage so real content always wins. Sized to
        // the viewport, not the stage: nothing here is transformed, so no zoom or
        // placement maths applies -- it is a still, not a preview.
        VideoOutput {
            id: firstFrameOut
            x: 0
            y: 0
            width: viewport.width
            height: viewport.height
            visible: root.proxyPending
            opacity: 0.35            // spec §2e, verbatim
            fillMode: VideoOutput.Stretch
        }

        Item {
            id: stage
            x: 0
            y: 0
            width: root.canvas.width
            height: root.canvas.height
            // One Scale to fit the panel. Scale defaults to origin (0,0), so this is a
            // plain multiply and the stage's coordinate system stays canvas pixels --
            // which is what every MouseArea inside it then reports.
            transform: Scale {
                xScale: root.fit
                yScale: root.fit
            }

            Item {
                id: content
                x: 0
                y: 0
                width: stage.width
                height: stage.height
                clip: true

                Rectangle {
                    id: backdrop
                    x: 0; y: 0
                    width: content.width
                    height: content.height
                    visible: root.st.backdrop ? root.st.backdrop.enabled : false
                    // The resolved colour, which for a gradient ground is its first
                    // stop -- so a canvas that cannot draw the gradient still shows a
                    // member of it rather than a stale default.
                    color: root.st.backdrop ? root.st.backdrop.color : "#1b1d24"
                    clip: true

                    readonly property var ground: root.st.backdrop ? root.st.backdrop.ground : null
                    readonly property bool isGradient: ground && ground.kind !== "solid"
                                                       && ground.colors && ground.colors.length > 1

                    // Painted from the export's OWN gradient line, handed over by
                    // resolve_backdrop in canvas pixels -- see controls/GradientFill.qml.
                    C.GradientFill {
                        visible: backdrop.isGradient
                        anchors.fill: parent
                        colors: backdrop.isGradient ? backdrop.ground.colors : []
                        line: backdrop.isGradient ? backdrop.ground.line : null
                    }
                }

                // The inset the screen sits in when a backdrop is on. The export
                // zooms the FULL frame and then scales the result into this rectangle,
                // so the box stays put on the canvas while its contents zoom -- putting
                // the inset inside the zoom instead would pan the box itself.
                Item {
                    id: insetBox
                    x: backdrop.visible ? root.st.backdrop.rect.x : 0
                    y: backdrop.visible ? root.st.backdrop.rect.y : 0
                    width: backdrop.visible ? root.st.backdrop.rect.width : content.width
                    height: backdrop.visible ? root.st.backdrop.rect.height : content.height
                    layer.enabled: backdrop.visible
                    layer.effect: MultiEffect {
                        maskEnabled: true
                        maskSource: screenMask
                    }

                    Item {
                        id: insetScale
                        x: 0
                        y: 0
                        width: content.width
                        height: content.height
                        // Both factors come from the bridge; dividing the inset by the
                        // canvas here would be the same mapping written twice.
                        transform: Scale {
                            xScale: backdrop.visible ? root.st.backdrop.content_scale.x : 1
                            yScale: backdrop.visible ? root.st.backdrop.content_scale.y : 1
                        }

                        Item {
                            id: zoomed
                            // Straight from Zoom.to_qml. transformOrigin is carried in
                            // the payload rather than assumed here.
                            transformOrigin: Item.TopLeft
                            scale: root.zoomNow.scale
                            x: root.zoomNow.x
                            y: root.zoomNow.y
                            width: content.width
                            height: content.height

                            VideoOutput {
                                id: screenOut
                                x: 0; y: 0
                                width: zoomed.width
                                height: zoomed.height
                                fillMode: VideoOutput.Stretch
                            }
                        }
                    }
                }

                Item {
                    id: screenMask
                    x: insetBox.x
                    y: insetBox.y
                    width: insetBox.width
                    height: insetBox.height
                    visible: false
                    layer.enabled: true
                    Rectangle {
                        x: 0; y: 0
                        width: screenMask.width
                        height: screenMask.height
                        radius: root.st.backdrop ? root.st.backdrop.radius : 0
                        color: "black"
                    }
                }
            }

            Repeater {
                id: layerRepeater
                model: root.st.layers || []
                LayerItem {
                    id: item
                    spec: modelData
                    frame: root.frame
                    contentSource: content
                    selected: root.selectedId === modelData.id
                    interactive: root.tool === "select" && !root.previewMode
                    previewMode: root.previewMode
                    onClicked: {
                        root.selectedId = modelData.id
                        root.webcamSelected = false
                    }
                    onMoved: function (r) {
                        // The callback is what releases the item's local position: until
                        // the reply lands, the model still holds the OLD rect, and
                        // showing that for a frame is the "double bounce".
                        Bridge.op("update_layer", { id: modelData.id, rect: r },
                                  function () { if (item) item.commitDone() })
                    }
                }
            }

            WebcamOverlay {
                id: webcam
                cam: root.st.webcam || ({})
                // Undo the stage's fit so the handles are a constant size on screen.
                uiScale: root.fit > 0 ? 1 / root.fit : 1
                selected: root.webcamSelected
                previewMode: root.previewMode
                onClicked: {
                    root.webcamSelected = true
                    root.selectedId = ""
                }
                onMoved: function (r) {
                    Bridge.op("set_webcam", { rect: r },
                              function () { webcam.commitDone() })
                }
            }

            // Rubber band for the redaction tools. Coordinates are canvas pixels because
            // this MouseArea is a child of the scaled stage, so nothing here has to
            // divide by the fit scale.
            MouseArea {
                id: bandArea
                x: 0; y: 0
                width: stage.width
                height: stage.height
                enabled: root.tool !== "select" && !root.previewMode
                z: 50
                cursorShape: Qt.CrossCursor
                property real ax: 0
                property real ay: 0
                property rect band: Qt.rect(0, 0, 0, 0)
                onPressed: function (m) {
                    ax = m.x
                    ay = m.y
                    band = Qt.rect(m.x, m.y, 0, 0)
                }
                onPositionChanged: function (m) {
                    band = Qt.rect(Math.min(ax, m.x), Math.min(ay, m.y),
                                   Math.abs(m.x - ax), Math.abs(m.y - ay))
                }
                onReleased: {
                    if (band.width < 8 || band.height < 8) {
                        band = Qt.rect(0, 0, 0, 0)
                        return
                    }
                    var op = root.tool === "pixelate" ? "add_pixelate"
                           : root.tool === "text" ? "add_text" : "add_blur"
                    Bridge.op(op, { rect: { x: band.x, y: band.y, width: band.width, height: band.height } },
                              function () { root.tool = "select" })
                    band = Qt.rect(0, 0, 0, 0)
                }
                Rectangle {
                    visible: bandArea.band.width > 0
                    x: bandArea.band.x
                    y: bandArea.band.y
                    width: bandArea.band.width
                    height: bandArea.band.height
                    color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.25)
                    border.color: Theme.accent
                    border.width: 2
                }
            }

            MouseArea {
                x: 0; y: 0
                width: stage.width
                height: stage.height
                z: -1
                // Off in preview mode: with rings hidden, changing the selection is an
                // invisible state change the user cannot see happen.
                enabled: root.tool === "select" && !root.previewMode
                onClicked: {
                    root.selectedId = ""
                    root.webcamSelected = false
                    root.selectedZoomIndex = -1
                }
            }

            DropArea {
                x: 0; y: 0
                width: stage.width
                height: stage.height
                enabled: !root.previewMode   // dropping an image is an edit
                onDropped: function (drop) {
                    if (!drop.hasUrls)
                        return
                    var path = decodeURIComponent(drop.urls[0].toString().replace("file://", ""))
                    var w = stage.width * 0.3
                    var h = stage.height * 0.3
                    Bridge.op("add_image", {
                        path: path,
                        rect: { x: drop.x - w / 2, y: drop.y - h / 2, width: w, height: h }
                    })
                    drop.accept()
                }
            }
        }

        // -- selected zoom region, in viewport pixels ------------------------
        //
        // The region a zoom event magnifies, mapped through the CURRENT frame's
        // transform: at identity it sits where the source region is (the mock's state),
        // and once the playhead is inside the event it grows to fill the viewport,
        // which is exactly what "you are now looking at this region" should read as.
        // Nulled in preview mode, which takes the scrim, the hairline rect and the
        // label chip down in one place -- the zoom itself still applies (it is content,
        // resolved by the bridge), only the "you are editing this region" dressing goes.
        readonly property var selSeg:
            !root.previewMode
            && root.selectedZoomIndex >= 0 && root.selectedZoomIndex < root.zoomSegments.length
            ? root.zoomSegments[root.selectedZoomIndex] : null
        readonly property real segX: selSeg ? root.fit * (root.zoomNow.scale * (-selSeg.x / selSeg.scale) + root.zoomNow.x) : 0
        readonly property real segY: selSeg ? root.fit * (root.zoomNow.scale * (-selSeg.y / selSeg.scale) + root.zoomNow.y) : 0
        readonly property real segW: selSeg ? root.fit * root.zoomNow.scale * root.canvas.width / selSeg.scale : 0
        readonly property real segH: selSeg ? root.fit * root.zoomNow.scale * root.canvas.height / selSeg.scale : 0

        // One dim overlay with a hole punched in it -- the spec's 0 0 0 9999px scrim
        // (§3), kept as a single surface so the four sides can never seam or double up.
        Canvas {
            id: dimOverlay
            x: 0; y: 0
            width: viewport.width
            height: viewport.height
            visible: viewport.selSeg !== null
            property real hx: viewport.segX
            property real hy: viewport.segY
            property real hw: viewport.segW
            property real hh: viewport.segH
            onHxChanged: requestPaint()
            onHyChanged: requestPaint()
            onHwChanged: requestPaint()
            onHhChanged: requestPaint()
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                ctx.fillStyle = Qt.rgba(0, 0, 0, 0.28)   // spec §1d region 3, verbatim
                ctx.fillRect(0, 0, width, height)
                ctx.clearRect(hx, hy, hw, hh)
            }
        }

        Rectangle {
            visible: viewport.selSeg !== null
            x: viewport.segX
            y: viewport.segY
            width: viewport.segW
            height: viewport.segH
            radius: Theme.radiusChip
            color: "transparent"
            border.color: Theme.accent
            border.width: 1.5
        }

        // The label chip above the region: uppercase, accent on a floating fill.
        Rectangle {
            visible: viewport.selSeg !== null
            x: viewport.segX
            y: Math.max(4, viewport.segY - height - 8)
            width: chipLabel.implicitWidth + 16
            height: chipLabel.implicitHeight + 8
            radius: Theme.radiusChip
            color: Theme.bgFloat
            Text {
                id: chipLabel
                anchors.centerIn: parent
                text: viewport.selSeg
                      ? ("zoom " + (root.selectedZoomIndex + 1) + " · "
                         + viewport.selSeg.scale.toFixed(1) + "×")
                      : ""
                color: Theme.accent
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fsHint
                font.letterSpacing: Theme.fsHint * Theme.capsSpacing
                font.capitalization: Font.AllUppercase
            }
        }
    }

    // The rounded-rect mask the viewport's layer samples; hidden, like screenMask.
    Item {
        id: viewportMask
        x: viewport.x; y: viewport.y
        width: viewport.width; height: viewport.height
        visible: false
        layer.enabled: true
        Rectangle {
            x: 0; y: 0
            width: viewportMask.width; height: viewportMask.height
            radius: Theme.radiusRow
            color: "black"
        }
    }

    MediaPlayer {
        id: screenPlayer
        // The proxy, never the master: the master's seeks took 517-651ms and half of
        // them never delivered a frame. The bridge sends an empty url until the proxy
        // exists, which is why this is guarded rather than falling back.
        source: root.hasScreen ? root.screenMedia.url : ""
        videoOutput: screenOut
        audioOutput: AudioOutput { id: screenAudio }
        // A paused player that has never played delivers no frame at all, so the stage
        // would open black on a recording that is perfectly fine. Priming means starting
        // and immediately pausing, which is the only way to get frame 0 on screen.
        property bool primed: false
        onMediaStatusChanged: {
            if (mediaStatus !== MediaPlayer.LoadedMedia && mediaStatus !== MediaPlayer.BufferedMedia)
                return
            if (!primed) {
                primed = true
                play()
                if (root.hasCamera)
                    cameraPlayer.play()
                primeTimer.restart()
            } else if (root.pendingSeekMs >= 0 && seekable) {
                setPosition(root.pendingSeekMs)
                root.pendingSeekMs = -1
            }
        }
        // A formal parameter, not the injected one: injected signal parameters are
        // deprecated and warn on every load.
        onSeekableChanged: function (canSeek) {
            if (canSeek && root.pendingSeekMs >= 0) {
                setPosition(root.pendingSeekMs)
                root.pendingSeekMs = -1
            }
        }
        onPositionChanged: {
            if (playbackState !== MediaPlayer.PlayingState)
                return
            // A cut is not removed from the proxy; playback simply steps over it, which
            // is what the exported file will do.
            var cut = root.cutContaining(root.frame)
            if (cut)
                root.seekFrame(cut.end)
        }
    }

    MediaPlayer {
        id: cameraPlayer
        source: root.hasCamera ? root.cameraMedia.url : ""
        videoOutput: webcam.videoOutput
    }

    // Delivers frame 0 of the master for the 2e backdrop. Primed the same way as
    // screenPlayer (a paused player that never played shows nothing), then left
    // paused; no audioOutput, so it can never be heard, and no seek ever happens.
    MediaPlayer {
        id: firstFramePlayer
        source: root.proxyPending ? root.masterUrl : ""
        videoOutput: firstFrameOut
        property bool primed: false
        onSourceChanged: primed = false
        onMediaStatusChanged: {
            if (!primed && (mediaStatus === MediaPlayer.LoadedMedia
                            || mediaStatus === MediaPlayer.BufferedMedia)) {
                primed = true
                play()
                firstFrameTimer.restart()
            }
        }
    }
    Timer {
        id: firstFrameTimer
        interval: 120
        repeat: false
        onTriggered: firstFramePlayer.pause()
    }

    Timer {
        id: primeTimer
        interval: 120
        repeat: false
        onTriggered: {
            screenPlayer.pause()
            cameraPlayer.pause()
            root.seekFrame(root.scrubFrame)
        }
    }

    // Two players are frame-exact after a seek but drift over long playback, so the
    // camera is nudged back whenever it is more than one frame out.
    Timer {
        interval: 250
        repeat: true
        running: root.playing && root.hasCamera
        onTriggered: {
            var want = cameraSync.cameraMsFor(screenPlayer.position)
            if (Math.abs(cameraPlayer.position - want) > root.msPerFrame)
                cameraPlayer.setPosition(want)
        }
    }

    // The tool strip lives in main.qml's left rail (spec §1d region 2); this item only
    // carries the `tool` property the rail drives.

    Text {
        x: viewport.x + (viewport.width - width) / 2
        y: viewport.y + (viewport.height - height) / 2
        // The build itself is narrated by the 2e overlay (main.qml); this only covers
        // the state where there is nothing to play and nothing being built either.
        text: root.hasScreen || root.proxyPending ? "" : "no preview media"
        color: Theme.text5
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fsRow
    }
}
