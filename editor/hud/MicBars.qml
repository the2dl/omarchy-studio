// The 6-bar mic meter (spec §1c). Six discrete bars, not a continuous meter: the
// question during a take is "is it hearing me", which a handful of lit segments answers
// at a glance, where a smooth bar asks you to read a length.
//
// The top two segments are accent, so clipping-adjacent level looks different from
// speaking level without introducing a second colour for it.
import QtQuick
import ".."

Item {
    id: root
    property real level: 0        // 0..1, from micmeter.MicMeter
    property bool alive: true

    implicitWidth: 6 * 3 + 5 * 2
    implicitHeight: 14

    Row {
        anchors.centerIn: parent
        spacing: 2
        Repeater {
            model: 6
            delegate: Rectangle {
                required property int index
                width: 3
                height: 5 + index * 1.8
                anchors.verticalCenter: parent.verticalCenter
                radius: 1.5
                readonly property bool lit: root.alive && root.level * 6 > index
                color: !lit ? Theme.fillSubtle
                     : index >= 4 ? Theme.accent : Theme.text2
                Behavior on color { ColorAnimation { duration: 90 } }
            }
        }
    }
}
