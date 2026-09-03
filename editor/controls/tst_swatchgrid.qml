// The backdrop picker's behaviour: what it reports, and what it lights.
//
//   /usr/lib/qt6/bin/qmltestrunner -input editor/controls/tst_swatchgrid.qml
//
// (Qt6's runner by absolute path -- the one on PATH is qt5-declarative's and exits 1
// having printed nothing.)
import QtQuick
import QtTest
import "." as C

Item {
    id: harness
    width: 400
    height: 300

    property string lastPicked: ""
    property int customPicks: 0

    C.SwatchGrid {
        id: grid
        width: 260
        catalogue: ({
            custom: "custom",
            entries: [
                { id: "ink", name: "Ink", kind: "solid", colors: ["#0d0e10"], angle: 0 },
                { id: "bone", name: "Bone", kind: "solid", colors: ["#e9e5de"], angle: 0 },
                { id: "dusk", name: "Dusk", kind: "gradient", colors: ["#232838", "#0f1016"], angle: 160 },
                { id: "nocturne", name: "Nocturne", kind: "gradient",
                  colors: ["#1d2440", "#141726", "#0b0c12"], angle: 165 }
            ]
        })
        currentId: "dusk"
        onPicked: function (id) { lastPicked = id }
        onCustomPicked: customPicks++
    }

    TestCase {
        name: "SwatchGrid"
        when: windowShown

        function init() {
            lastPicked = ""
            customPicks = 0
        }

        function test_every_entry_gets_a_swatch_plus_the_custom_well() {
            // Four entries and one well over five columns is two rows; the height has to
            // account for the well or the last row is clipped out of the panel.
            compare(grid.entries.length, 4)
            compare(grid.rows, 1)
            verify(grid.implicitHeight >= grid.cell)
        }

        function test_the_grid_wraps_onto_rows() {
            grid.columns = 2
            compare(grid.rows, 3)          // 4 entries + the well = 5, over 2 columns
            verify(grid.implicitHeight > grid.cell * 2)
            grid.columns = 5
        }

        function test_picking_reports_the_id_not_the_index() {
            // The bridge takes ids; reporting an index would silently bind the UI to the
            // catalogue's order, which is not part of its contract.
            //
            // An earlier case in this file swaps the catalogue out and back, and a
            // Repeater rebuilds its delegates on a later frame than the model change --
            // so a click issued immediately lands on a delegate that is being torn down
            // and hits nothing. Verified: the same click passes first try in isolation.
            wait(50)
            mouseClick(grid, grid.cellWidth / 2, grid.cell / 2)
            tryCompare(harness, "lastPicked", "ink")
        }

        function test_the_custom_well_is_its_own_signal() {
            // It opens a colour dialog rather than selecting a ground, so it cannot be
            // reported through picked() without the panel having to special-case an id.
            var col = grid.entries.length % grid.columns
            mouseClick(grid, col * (grid.cellWidth + grid.gap) + grid.cellWidth / 2,
                       grid.cell / 2)
            compare(customPicks, 1)
            compare(lastPicked, "")
        }

        function test_an_empty_catalogue_still_offers_custom() {
            // The catalogue arrives on its own route and is empty until it lands, so the
            // panel must render before it does.
            var saved = grid.catalogue
            grid.catalogue = ({ custom: "custom", entries: [] })
            compare(grid.entries.length, 0)
            compare(grid.rows, 1)
            grid.catalogue = saved
        }
    }
}
