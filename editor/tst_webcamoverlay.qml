// The editor's camera must be the export's twin, and for `rect` it was not.
//
// layers._tile_webcam crops the camera to the BOX's aspect and scales into it, for every
// shape. This overlay reproduces that rather than approximating it -- so when the
// renderer stopped special-casing `rect`, this had to stop too. It did not, and the
// result was an export that was correct and an editor that still showed a 4:3 sensor
// squeezed into a square box: a face stretched tall, in the one place you look before
// deciding the recording is fine.
//
// VideoOutput draws nothing offscreen, so the pixels cannot be checked here. The
// fillMode and the layout CAN be, and they are what decides the shape.
//
//   /usr/lib/qt6/bin/qmltestrunner -input editor/tst_webcamoverlay.qml
import QtQuick
import QtTest
import QtMultimedia
import "."

Item {
    id: harness
    width: 900
    height: 600

    function camFor(shape) {
        return {
            enabled: true, editable: true, shape: shape, mirror: true,
            rect: { x: 0, y: 0, width: 716, height: 716 }
        }
    }

    WebcamOverlay { id: overlay; cam: harness.camFor("rect") }

    TestCase {
        name: "WebcamOverlay"
        when: windowShown

        function test_every_shape_fills_rather_than_stretching_data() {
            return [{ tag: "rect", shape: "rect" },
                    { tag: "circle", shape: "circle" },
                    { tag: "rounded", shape: "rounded" }]
        }

        function test_every_shape_fills_rather_than_stretching(row) {
            overlay.cam = harness.camFor(row.shape)
            compare(overlay.videoOutput.fillMode, VideoOutput.PreserveAspectCrop,
                    row.shape + " stretches the camera into its box")
        }

        function test_the_video_is_laid_out_at_the_box_size() {
            // It used to be laid out as a SQUARE and squashed into the box by a second
            // Scale. Sizing it to the box and letting PreserveAspectCrop do the work is
            // what makes it the same operation the filtergraph performs -- and leaving
            // both in would scale it twice.
            overlay.cam = harness.camFor("rect")
            compare(overlay.videoOutput.width, overlay.width)
            compare(overlay.videoOutput.height, overlay.height)
        }

        // The shadow's numbers are the export's numbers: these constants are what
        // layers._shadow_margin(716, 716, depth) returns, including the floor and the
        // rounding to even. Offscreen draws none of it, so the geometry is the check.
        function test_the_shadow_geometry_is_the_exports_data() {
            return [{ tag: "shallow", depth: 0.0, margin: 80, drop: 30, alpha: 0.3 },
                    { tag: "rest", depth: 0.5, margin: 158, drop: 60, alpha: 0.6 },
                    { tag: "deep", depth: 1.0, margin: 236, drop: 90, alpha: 0.9 }]
        }

        function test_the_shadow_geometry_is_the_exports(row) {
            var c = harness.camFor("circle")
            c.shadow = true
            c.shadow_depth = row.depth
            overlay.cam = c
            compare(overlay.shadowMargin, row.margin, "margin at depth " + row.depth)
            compare(overlay.shadowDrop, row.drop, "drop at depth " + row.depth)
            fuzzyCompare(overlay.shadowOpacity, row.alpha, 0.0001)
            compare(overlay.shadowItem.offset.y, row.drop)
            fuzzyCompare(overlay.shadowItem.blur, row.margin * 8 / 9, 0.001)
        }

        function test_a_project_without_a_depth_is_at_rest() {
            overlay.cam = harness.camFor("circle")     // no shadow_depth key at all
            compare(overlay.shadowMargin, 158)
            verify(overlay.shadowItem.visible, "the shadow defaults on, like the model")
        }

        function test_the_shadow_follows_the_shape_data() {
            return [{ tag: "circle", shape: "circle", radius: 358 },
                    { tag: "rect", shape: "rect", radius: 0 },
                    { tag: "rounded", shape: "rounded", radius: 0.27 * 716 }]
        }

        function test_the_shadow_follows_the_shape(row) {
            overlay.cam = harness.camFor(row.shape)
            fuzzyCompare(overlay.shadowItem.radius, row.radius, 0.01)
        }

        function test_the_toggle_hides_the_shadow() {
            var c = harness.camFor("circle")
            c.shadow = false
            overlay.cam = c
            verify(!overlay.shadowItem.visible)
        }

        function test_the_mirror_is_the_only_transform_left() {
            // The export flips before the scale; a leftover squash transform here would
            // silently reintroduce the stretch this file exists to prevent.
            compare(overlay.videoOutput.transform.length, 1,
                    "an extra transform is scaling the camera a second time")
        }
    }
}
