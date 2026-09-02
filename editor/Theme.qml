pragma Singleton
import QtQuick

// The design tokens, resolved by lib/omarchy_studio/theme.py and handed over as JSON.
//
// QML never parses colors.toml and never derives a colour itself. Every mix, alpha and
// contrast decision lives in theme.py so there is exactly one implementation -- the same
// rule the geometry seam follows, and for the same reason: two implementations of the
// same derivation drift, and nobody notices until the two are put side by side.
//
// Values below are the spec's own palette, used verbatim until load() replaces them.
// That means a bare checkout still renders as the mock rather than as black-on-black.

QtObject {
    id: root

    property string mode: "dark"
    readonly property bool light: mode === "light"

    property string fontFamily: "JetBrainsMono Nerd Font"

    // Surfaces
    property color bg:         "#0d0e10"
    property color bgDeep:     "#0a0b0d"
    property color bgFloat:    "#eb0d0e10"
    property color panel:      "#1d1a17"
    // The canvas is always dark, on light themes too -- exposure cannot be judged
    // against white. See theme.py's docstring.
    property color canvasA:    "#1d1a17"
    property color canvasB:    "#121316"

    // Accent
    property color accent:     "#f2a25a"
    property color accentDim:  "#c9854a"
    property color accentOn:   "#1a1006"
    property color accentWash: "#2ef2a25a"
    property color accentGlow: "#38f2a25a"

    // The only red in the product.
    property color live:       "#e0523f"

    // Text ramp, brightest to faintest
    property color text:       "#f6f4f2"
    property color text2:      "#eceae7"
    property color text3:      "#b3aea8"
    property color text4:      "#837e79"
    property color text5:      "#625d58"
    property color text6:      "#55514d"

    // Fills. Nothing in the UI uses a border to mean "container" -- containers are
    // fills and shadows. A ring means selected.
    property color hairline:   "#12ffffff"
    property color fillSubtle: "#0dffffff"
    property color fillHover:  "#12ffffff"
    property color track:      "#1affffff"
    property color selected:   "#2a2a2a"

    // Geometry, derived from Hyprland's live decoration:rounding. A square-cornered
    // theme squares the whole app.
    property int radiusWindow: 16
    property int radiusPanel:  16
    property int radiusRow:    10
    property int radiusChip:   7

    // Type scale from the spec.
    readonly property int fsTitle:   15
    readonly property int fsBody:    13
    readonly property int fsRow:     12
    readonly property int fsCaption: 11
    readonly property int fsHint:    10
    readonly property real capsSpacing: 0.08

    // Motion. Hover and active are quick; selection changes are given room to read.
    readonly property int durFast: 120
    readonly property int durSlow: 200

    readonly property int panelWidth: 320
    readonly property int inspectorWidth: 268

    function load(json) {
        if (!json)
            return
        var t = (typeof json === "string") ? JSON.parse(json) : json
        for (var key in t) {
            if (key === "radii" || t[key] === undefined)
                continue
            if (root.hasOwnProperty(key))
                root[key] = t[key]
        }
        if (t.radii) {
            radiusWindow = t.radii.window
            radiusPanel = t.radii.panel
            radiusRow = t.radii.row
            radiusChip = t.radii.chip
        }
        if (t.font)
            fontFamily = t.font
    }
}
