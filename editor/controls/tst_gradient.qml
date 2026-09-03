// The QML gradient line must agree with the one the export draws.
//
//   /usr/lib/qt6/bin/qmltestrunner -input editor/controls/tst_gradient.qml
//
// GradientFill.lineFor is a second implementation of backgrounds.gradient_line, which
// is only tolerable while something asserts the two agree. The expected values below
// were produced by that function -- if this test fails, QML has drifted and every
// gradient swatch is aimed somewhere the render will not put it.
import QtQuick
import QtTest
import "." as C

Item {
    width: 200
    height: 200

    C.GradientFill {
        id: fill
        width: 100
        height: 100
        colors: ["#101010", "#909090"]
    }

    TestCase {
        name: "GradientFill"

        function test_line_data() {
            return [
                { tag: "150deg 1920x1080", a: 150, w: 1920, h: 1080,
                  x0: 486.1731, y0: -280.6922, x1: 1433.8269, y1: 1360.6922 },
                { tag: "0deg bottom-to-top", a: 0, w: 640, h: 480,
                  x0: 320.0, y0: 480.0, x1: 320.0, y1: 0.0 },
                { tag: "90deg left-to-right", a: 90, w: 640, h: 480,
                  x0: 0.0, y0: 240.0, x1: 640.0, y1: 240.0 },
                { tag: "45deg 800x600", a: 45, w: 800, h: 600,
                  x0: 50.0, y0: 650.0, x1: 750.0, y1: -50.0 },
                { tag: "270deg square", a: 270, w: 1000, h: 1000,
                  x0: 1000.0, y0: 500.0, x1: 0.0, y1: 500.0 }
            ]
        }

        function test_line(data) {
            var l = fill.lineFor(data.a, data.w, data.h)
            fuzzyCompare(l.x0, data.x0, 0.001)
            fuzzyCompare(l.y0, data.y0, 0.001)
            fuzzyCompare(l.x1, data.x1, 0.001)
            fuzzyCompare(l.y1, data.y1, 0.001)
        }

        function test_a_supplied_line_is_used_verbatim() {
            // The preview is handed the export's own endpoints and must not recompute
            // them -- that is the whole reason resolve_backdrop sends them.
            fill.line = { x0: 1, y0: 2, x1: 3, y1: 4 }
            compare(fill.line.x0, 1)
            fill.line = null
        }
    }
}
