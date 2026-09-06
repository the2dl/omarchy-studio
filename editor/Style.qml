// Spacing constants for the editor's chrome.
//
// Separate from Theme on purpose: Theme carries what the user's Omarchy theme decides
// (colour, corner radius, font), resolved by theme.py. These are layout numbers that no
// theme has an opinion about, and putting them in the theme token set would imply
// colors.toml could change them. Every value cites spec §1d or the mockup's inline
// styles, which are the same numbers rendered.
pragma Singleton
import QtQuick

QtObject {
    readonly property int pad: 16          // tray and bar side padding (mock: padding 0 16px)
    readonly property int gap: 8

    readonly property int topBarHeight: 46 // spec §1d region 1
    readonly property int railWidth: 56    // spec §1d region 2
    readonly property int canvasPad: 46    // spec §1d region 3: recording inset on the gradient

    // Timeline tray, bottom-up: transport 38 + ruler 18 + rows 40/26/16/30 with 7px
    // gaps (mock: gap 7px) + 16px bottom padding + the top hairline.
    readonly property int gutterWidth: 74  // spec §1d region 5: uppercase row labels
    readonly property int rowGap: 7
    readonly property int screenRowH: 40
    readonly property int zoomRowH: 26
    readonly property int clicksRowH: 16
    readonly property int audioRowH: 30
    readonly property int timelineHeight: 38 + 18 + 40 + 26 + 16 + 30 + 3 * 7 + 16 + 1

    // --- responsive scale ---------------------------------------------------------
    //
    // Every number above is drawn for a surface about 1600 logical px wide, which is
    // what the desktop this was built on gives. A 13" laptop at scale 2 gives 1440, and
    // the setup bar -- one row that sizes itself to its own contents -- simply ran off
    // BOTH edges there, clipped by the compositor with no warning and no error: the
    // mode chips lost their first letter on the left while the shape picker lost its
    // last on the right.
    //
    // So: one factor, and one dp() that applies it. Widths, heights, spacings, paddings
    // and font sizes go through dp(). 1px hairlines and the corner radii deliberately do
    // NOT -- a 0.9px hairline is a blurry hairline, and the radii already read fine at
    // every width in this range.
    readonly property int refWidth: 1600

    // Never above 1: a wider screen earns more room, not bigger chrome. Never below
    // 0.8: past that the labels stop being legible and the honest answer is a different
    // layout, not a smaller one.
    property real uiScale: 1.0

    // Called by each surface as it learns its own width. The NARROWEST one wins, so a
    // bar that has to fit on two different monitors fits on both of them.
    function noteAvailableWidth(w) {
        if (w > 0) {
            var s = Math.max(0.8, Math.min(1.0, w / refWidth))
            if (s < uiScale)
                uiScale = s
        }
    }

    function dp(v) { return Math.round(v * uiScale) }
}
