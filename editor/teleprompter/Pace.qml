// The scroll rate, derived from words per minute.
//
// A component rather than a few expressions inside main.qml because this is the one bit
// of the prompter that can be WRONG rather than merely ugly, and a window is difficult
// to instantiate in a test. Everything here is pure: numbers in, numbers out.
//
// WHY WORDS PER MINUTE. Pixels per second is the natural unit for a scroller and the
// wrong unit for a prompter: it changes meaning with every type size and every window
// width, so a rate that felt right once is wrong the next time the window is resized.
// Words per minute is a property of the speaker, and it survives all of that.
//
// THE RATE COMES FROM THE LAID-OUT HEIGHT, not from a line count or a character count.
// Rewrap the window narrower and there are more lines and more pixels -- and
// proportionally more time, because the word count did not change. So the words still
// arrive under the reading band on the beat. Deriving from lines would make a narrow
// window read faster than a wide one at the same nominal pace.
import QtQuick

QtObject {
    id: root

    property int wordCount: 0
    property real contentHeight: 0
    property real wpm: 130

    // Pixels the content must travel per second. Zero for an empty script, which is
    // also what stops the scroll timer -- there is nothing to scroll past.
    readonly property real pxPerSecond:
        (wordCount <= 0 || contentHeight <= 0 || wpm <= 0)
            ? 0
            : (contentHeight / wordCount) * (wpm / 60.0)

    // How long the whole script takes at this pace. Shown in the bar because "how long
    // is this take going to be" is the question a script is usually written to answer,
    // and the prompter is the only place that knows both halves of it.
    readonly property real totalSeconds: wpm > 0 ? (wordCount / wpm) * 60.0 : 0

    function clock(seconds) {
        if (!(seconds > 0))
            return "0:00"
        var s = Math.round(seconds)
        return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2)
    }
}
