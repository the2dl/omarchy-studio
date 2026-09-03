// Unit tests for the prompter's pace maths and its control strip.
//
//   /usr/lib/qt6/bin/qmltestrunner -input editor/teleprompter/tst_prompter.qml
//
// Qt6's runner, by absolute path: the `qmltestrunner` on PATH here belongs to
// qt5-declarative and loads the Qt 5 QML engine, where every one of these files fails to
// import and the runner exits 1 having printed nothing at all.
import QtQuick
import QtTest
import "." as T

Item {
    width: 600
    height: 200

    T.Pace {
        id: pace
        wordCount: 120
        contentHeight: 2400
        wpm: 120
    }

    T.PrompterBar {
        id: bar
        width: 600
        height: 40
    }

    TestCase {
        name: "Prompter"
        when: windowShown

        function test_the_rate_carries_one_word_per_word_time() {
            // 120 words at 120 wpm is 60 seconds, over 2400px, so 40 px/s.
            compare(pace.pxPerSecond, 40)
            compare(pace.totalSeconds, 60)
        }

        function test_rewrapping_narrower_does_not_read_faster() {
            // The same script laid out taller (a narrower window) must take the SAME
            // time, which means proportionally more pixels per second -- not the same
            // rate over more pixels, which is how a resize would silently change pace.
            var before = pace.totalSeconds
            pace.contentHeight = 4800
            compare(pace.pxPerSecond, 80)
            compare(pace.totalSeconds, before)
            pace.contentHeight = 2400
        }

        function test_pace_scales_with_wpm() {
            pace.wpm = 240
            compare(pace.pxPerSecond, 80)
            compare(pace.totalSeconds, 30)
            pace.wpm = 120
        }

        function test_an_empty_script_does_not_scroll() {
            pace.wordCount = 0
            compare(pace.pxPerSecond, 0)
            pace.wordCount = 120
        }

        function test_clock_formats_minutes_and_seconds() {
            compare(pace.clock(0), "0:00")
            compare(pace.clock(9), "0:09")
            compare(pace.clock(75), "1:15")
            compare(pace.clock(600), "10:00")
        }

        function test_the_play_button_asks_the_window_to_run() {
            var fired = 0
            function count() { fired++ }
            bar.toggleRun.connect(count)
            var play = findChild(bar, "ctl:prompter-play")
            verify(play !== null, "the transport button must exist and be findable")
            mouseClick(play)
            bar.toggleRun.disconnect(count)
            compare(fired, 1)
        }

        function test_the_edit_button_asks_for_the_paste_box() {
            var fired = 0
            function count() { fired++ }
            bar.toggleEdit.connect(count)
            mouseClick(findChild(bar, "ctl:prompter-edit"))
            bar.toggleEdit.disconnect(count)
            compare(fired, 1)
        }
    }
}
