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
}
