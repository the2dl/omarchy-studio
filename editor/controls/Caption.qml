// The uppercase section caption: 11px, 0.08em tracking, text5.
import QtQuick
import ".."

Text {
    color: Theme.text5
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fsCaption
    font.letterSpacing: Theme.fsCaption * Theme.capsSpacing
    font.capitalization: Font.AllUppercase
}
