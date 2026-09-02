// The composited stage: what the export will look like, one frame at a time.
//
// The structure is load-bearing and mirrors the export's compile order:
//
//   stage        canvas-sized, scaled by one Scale transform to fit the panel
//     content    clip box; ShaderEffectSource reads THIS for redactions, so a blur
//                sees the post-zoom pixels exactly as ffmpeg's redact crops the
//                post-zoom stream
//       zoomed   transformOrigin TopLeft, scale/x/y straight from Zoom.to_qml
//         screen VideoOutput, explicit width/height
//     layers     overlaid on the output canvas, i.e. NOT inside the zoom, because the
//                export overlays them after the crop+scale
//     webcam     likewise
//
// Two rules that have already cost a day between them: transformOrigin must be TopLeft
// (any other origin makes the translation depend on the item's size, giving a zoom that
// scales but never pans), and nothing transformed may use anchors.fill (anchors silently
// override explicit x/y with no warning).
import QtQuick
import QtQuick.Controls.Basic
import QtMultimedia
import QtQuick.Effects

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
    readonly property real cameraOffsetMs: st.media ? st.media.camera_offset_ms : 0

    property string tool: "select"
    property string selectedId: ""
    property bool webcamSelected: false

    // Scrubbing has to work before the proxy exists, so the frame falls back to a local
    // value when there is nothing to play.
    property int scrubFrame: 0
    readonly property int frame: hasScreen ? Math.round(screenPlayer.position / msPerFrame) : scrubFrame
    readonly property bool playing: screenPlayer.playbackState === MediaPlayer.PlayingState

    readonly property real fit: Math.min(width / canvas.width, height / canvas.height)

    // What the zoom transform actually ended up as, read back off the item. The
    // difference between this and `zoomNow` is the whole class of bugs where a binding
    // is silently overridden -- anchors.fill on a transformed item does exactly that,
    // with no warning -- so the self-test asserts on these rather than on the payload.
    // Read back off the items for the same reason as the zoom transform: what the
    // scene graph ended up with, not what the payload asked for.
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
            cameraPlayer.setPosition(ms + cameraOffsetMs)
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
        color: Theme.surfaceDeep
    }

    Item {
        id: viewport
        x: (root.width - width) / 2
        y: (root.height - height) / 2
        width: root.canvas.width * root.fit
        height: root.canvas.height * root.fit
        clip: true

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
                    color: root.st.backdrop ? root.st.backdrop.color : "#1b1d24"
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
                    spec: modelData
                    frame: root.frame
                    contentSource: content
                    selected: root.selectedId === modelData.id
                    interactive: root.tool === "select"
                    onClicked: {
                        root.selectedId = modelData.id
                        root.webcamSelected = false
                    }
                    onMoved: function (r) {
                        Bridge.op("update_layer", { id: modelData.id, rect: r })
                    }
                }
            }

            WebcamOverlay {
                id: webcam
                cam: root.st.webcam || ({})
                selected: root.webcamSelected
                onClicked: {
                    root.webcamSelected = true
                    root.selectedId = ""
                }
                onMoved: function (r) {
                    Bridge.op("set_webcam", { rect: r })
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
                enabled: root.tool !== "select"
                z: 50
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
                enabled: root.tool === "select"
                onClicked: {
                    root.selectedId = ""
                    root.webcamSelected = false
                }
            }

            DropArea {
                x: 0; y: 0
                width: stage.width
                height: stage.height
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
            var want = screenPlayer.position + root.cameraOffsetMs
            if (Math.abs(cameraPlayer.position - want) > root.msPerFrame)
                cameraPlayer.setPosition(want)
        }
    }

    // Tool strip. Sits outside the stage so it is not scaled with the canvas.
    Row {
        x: Theme.pad
        y: Theme.pad
        spacing: 6
        Repeater {
            model: [
                { id: "select", label: "Select" },
                { id: "blur", label: "Blur box" },
                { id: "pixelate", label: "Pixelate" },
                { id: "text", label: "Text" }
            ]
            Button {
                text: modelData.label
                checkable: true
                checked: root.tool === modelData.id
                onClicked: root.tool = modelData.id
                background: Rectangle {
                    radius: Theme.radius
                    color: root.tool === modelData.id ? Theme.accent : Theme.surface
                    opacity: 0.9
                }
                contentItem: Text {
                    text: parent.text
                    color: root.tool === modelData.id ? Theme.surfaceDeep : Theme.foreground
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }
    }

    Text {
        x: Theme.pad
        y: root.height - height - Theme.pad
        text: root.hasScreen ? "" : (Bridge.proxyStatus.state === "building"
                                     ? "building preview proxy…"
                                     : "no preview media")
        color: Theme.dim
        font.pixelSize: 14
    }
}
