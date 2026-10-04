// Boswas OS default Plasma desktop layout, applied once when a user first logs in.
//
// Uses KDE's stock default panel template (launcher, pager, task manager,
// system tray, clock) so Plasma upgrades keep working, then applies Boswas
// branding on top: the Boswas wallpaper and the Boswas launcher icon.

loadTemplate("org.kde.plasma.desktop.defaultPanel")

var desktopsArray = desktopsForActivity(currentActivity());
for (var j = 0; j < desktopsArray.length; j++) {
    desktopsArray[j].wallpaperPlugin = "org.kde.image";
}

var allPanels = panels();
for (var i = 0; i < allPanels.length; i++) {
    var launchers = allPanels[i].widgets("org.kde.plasma.kickoff");
    for (var k = 0; k < launchers.length; k++) {
        launchers[k].currentConfigGroup = ["General"];
        launchers[k].writeConfig("icon", "boswas-logo");
    }
}
