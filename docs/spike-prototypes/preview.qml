// SPIKE 7 -- QML-side preview of the SAME layer set the ffmpeg generator exports.
// Run:  qml6 preview.qml
// Renders the 1920x1080 stage with grabToImage() at exactly 1920x1080, so the
// screenshot is independent of the display, its scale factor and the compositor.
//
// The point being tested: every layer in the project maps to ONE QML Item with
// the same x/y/w/h and the same opacity gate as the ffmpeg overlay, so preview
// and export agree geometrically.
import QtQuick
import QtQuick.Window
import QtQuick.Shapes
import QtMultimedia
import QtQuick.Effects

Window {
    id: win
    width: 960; height: 540      // displayed at half size; the grab is full size
    visible: true
    color: "#202020"

    property string dir: "file:///tmp/claude-1000/-home-dan/60c17442-ebb5-4cc3-aa4c-5e583aa2ad8c/scratchpad/spikes/layers/"
    property real tSec: 8.0
    property int  grabbed: 0

    Item {
        id: stage
        width: 1920; height: 1080
        transform: Scale { xScale: 0.5; yScale: 0.5 }
        clip: true

        // ---- base video -----------------------------------------------------
        VideoOutput {
            id: baseOut
            anchors.fill: parent
            fillMode: VideoOutput.Stretch
        }

        // ---- redact: blur a sub-rectangle of everything beneath --------------
        // ShaderEffectSource copies the stage sub-rect; MultiEffect blurs it.
        Item {
            id: blurBox
            x: 180; y: 140; width: 520; height: 300
            clip: true
            ShaderEffectSource {
                id: blurSrc
                anchors.fill: parent
                sourceItem: baseOut
                sourceRect: Qt.rect(blurBox.x, blurBox.y, blurBox.width, blurBox.height)
                live: true
                hideSource: false
                visible: false
            }
            MultiEffect {
                anchors.fill: parent
                source: blurSrc
                blurEnabled: true
                blur: 1.0
                blurMax: 48          // ~ ffmpeg boxblur radius 22, 2 passes
                autoPaddingEnabled: false
            }
        }

        // ---- image layer (PNG with alpha), picture-in-picture ---------------
        Image {
            id: logo
            source: win.dir + "logo.png"
            x: 1400; y: 80; width: 320; height: 320
            smooth: true
        }

        // ---- text callout with a rounded background box ----------------------
        Rectangle {
            id: callout
            x: 200; y: 760; width: 640; height: 160
            color: "#101820"; opacity: 0.85; radius: 24
        }
        Text {
            anchors.centerIn: callout
            text: "Callout box"; color: "white"
            font.pixelSize: 56
        }

        // ---- solid shape -----------------------------------------------------
        Rectangle {
            id: shapeRect
            x: 1200; y: 600; width: 300; height: 120
            color: "#ff3b30"; radius: 16
        }

        // ---- arrow -----------------------------------------------------------
        Shape {
            x: 700; y: 300; width: 400; height: 300
            ShapePath {
                strokeColor: "#ffd60a"; strokeWidth: 18
                fillColor: "transparent"; capStyle: ShapePath.FlatCap
                startX: 8; startY: 292
                PathLine { x: 392 - 0.804 * 54; y: 8 + 0.5946 * 54 }
            }
            ShapePath {
                strokeColor: "transparent"; fillColor: "#ffd60a"
                startX: 392; startY: 8
                PathLine { x: 392 - 0.804*54 - 0.5946*28.8; y: 8 + 0.5946*54 - 0.804*28.8 }
                PathLine { x: 392 - 0.804*54 + 0.5946*28.8; y: 8 + 0.5946*54 + 0.804*28.8 }
                PathLine { x: 392; y: 8 }
            }
        }

        // ---- webcam, circular ------------------------------------------------
        Item {
            id: camBox
            x: 1520; y: 700; width: 300; height: 300
            layer.enabled: true
            layer.effect: MultiEffect { maskEnabled: true; maskSource: camMask }
            VideoOutput {
                id: camOut
                anchors.fill: parent
                fillMode: VideoOutput.PreserveAspectCrop
            }
        }
        Item {
            id: camMask
            width: 300; height: 300; visible: false; layer.enabled: true
            Rectangle { anchors.fill: parent; radius: 150; color: "black" }
        }
    }

    MediaPlayer { id: mpBase; source: win.dir + "base.mp4"; videoOutput: baseOut }
    MediaPlayer { id: mpCam;  source: win.dir + "cam.mp4";  videoOutput: camOut }

    Component.onCompleted: { mpBase.play(); mpCam.play() }

    Timer {                       // let both decoders spin up, then seek + pause
        interval: 1200; running: true; repeat: false
        onTriggered: {
            mpBase.pause(); mpCam.pause()
            mpBase.position = win.tSec * 1000
            mpCam.position  = win.tSec * 1000
        }
    }
    Timer {                       // then grab
        interval: 2600; running: true; repeat: false
        onTriggered: {
            console.log("BASE pos=" + mpBase.position + " CAM pos=" + mpCam.position)
            stage.grabToImage(function (res) {
                res.saveToFile("/tmp/claude-1000/-home-dan/60c17442-ebb5-4cc3-aa4c-5e583aa2ad8c/scratchpad/spikes/layers/qml_stage.png")
                console.log("GRABBED " + res.image.width + "x" + res.image.height)
                Qt.callLater(Qt.quit)
            }, Qt.size(1920, 1080))
        }
    }
    Timer { interval: 20000; running: true; repeat: false; onTriggered: Qt.quit() }
}
