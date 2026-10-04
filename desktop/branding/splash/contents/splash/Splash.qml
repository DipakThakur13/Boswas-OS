/*
    Boswas OS login splash (KSplash), shown while the Plasma session starts.
    Installed by boswas-branding as the look-and-feel package com.boswas.splash;
    the Boswas presets select it with [KSplash] Theme=com.boswas.splash.

    Plasma sets `stage` from 1 to 6 while the session starts; the content fades
    in at stage 2 and the spinner fades out at stage 5. Animations follow the
    user's animation speed setting and stop entirely when animations are off.
*/

import QtQuick
import org.kde.kirigami 2 as Kirigami

Rectangle {
    id: root
    color: "#080C16"

    property int stage

    onStageChanged: {
        if (stage == 2) {
            fadeIn.running = true;
        } else if (stage == 5) {
            spinnerFade.running = true;
        }
    }

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#0B1220" }
            GradientStop { position: 1.0; color: "#080C16" }
        }
    }

    Item {
        id: content
        anchors.fill: parent
        opacity: 0

        Image {
            id: lockup
            readonly property real size: Kirigami.Units.gridUnit * 10
            anchors.centerIn: parent
            anchors.verticalCenterOffset: -Kirigami.Units.gridUnit * 2
            asynchronous: true
            source: "images/lockup.svg"
            sourceSize.width: size * 280 / 190
            sourceSize.height: size
            Accessible.name: "Boswas OS"
            Accessible.role: Accessible.Graphic
        }

        Image {
            id: spinner
            y: lockup.y + lockup.height + Kirigami.Units.gridUnit * 3
            anchors.horizontalCenter: parent.horizontalCenter
            asynchronous: true
            source: "images/spinner.svg"
            sourceSize.width: Kirigami.Units.gridUnit * 2
            sourceSize.height: Kirigami.Units.gridUnit * 2
            RotationAnimator on rotation {
                from: 0
                to: 360
                duration: 1600
                loops: Animation.Infinite
                running: Kirigami.Units.longDuration > 1
            }
        }
    }

    OpacityAnimator {
        id: fadeIn
        running: false
        target: content
        from: 0
        to: 1
        duration: Kirigami.Units.veryLongDuration * 2
        easing.type: Easing.InOutQuad
    }

    OpacityAnimator {
        id: spinnerFade
        running: false
        target: spinner
        from: 1
        to: 0
        duration: Kirigami.Units.longDuration
    }
}
