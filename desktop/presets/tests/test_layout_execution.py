"""Run the layout scripts in a JavaScript engine against a mock of the Plasma
6.3 desktop scripting API.

Optional: needs the py_mini_racer module (V8), which Boswas OS does not ship;
the test is skipped without it. The mock behaves like a live Plasma 6 panel:
a new widget goes to the end of the panel. It runs KDE's own default panel
template when it is installed, else a stand-in with the same widgets.
"""

import json
import unittest
from pathlib import Path

import support

try:
    from py_mini_racer import MiniRacer
except ImportError:  # pragma: no cover - optional
    MiniRacer = None

TEMPLATES = [
    Path("/usr/share/plasma/layout-templates/org.kde.plasma.desktop.defaultPanel/contents/layout.js"),
    Path("/tmp/audit/root/usr/share/plasma/layout-templates/org.kde.plasma.desktop.defaultPanel/contents/layout.js"),
]
STAND_IN_TEMPLATE = """
var panel = new Panel;
panel.height = 2 * Math.floor(gridUnit * 2.5 / 2);
panel.addWidget("org.kde.plasma.kickoff");
panel.addWidget("org.kde.plasma.pager");
panel.addWidget("org.kde.plasma.icontasks");
panel.addWidget("org.kde.plasma.marginsseparator");
panel.addWidget("org.kde.plasma.systemtray");
panel.addWidget("org.kde.plasma.digitalclock");
panel.addWidget("org.kde.plasma.showdesktop");
"""
MOCK = """
var __nextId = 1;
var __panels = [];
var __desktops = [{wallpaperPlugin: "org.kde.desktopcontainment-default"}];
var gridUnit = 18;
var languageId = "en";
var knownWidgetTypes = __KNOWN__;
function screenGeometry(s) { return {x: 0, y: 0, width: 1920, height: 1080}; }
function currentActivity() { return "activity-1"; }
function desktopsForActivity(a) { return __desktops; }
function panels() { return __panels.slice(0); }
function Widget(type) {
    this.id = __nextId++; this.type = type; this.config = {}; this.removed = false;
    this.currentConfigGroup = [];
}
Widget.prototype.writeConfig = function (key, value) {
    this.config[this.currentConfigGroup.join("/") + "/" + key] = value;
};
Widget.prototype.remove = function () { this.removed = true; };
function Panel() {
    this.screen = 0; this.formFactor = "horizontal"; this.height = 0; this.floating = true;
    this.lengthMode = "custom"; this._widgets = []; this.id = __nextId++;
    __panels.push(this);
}
Object.defineProperty(Panel.prototype, "widgetIds", {get: function () {
    return this._widgets.filter(function (w) { return !w.removed; }).map(function (w) { return w.id; });
}});
Panel.prototype.widgetById = function (id) {
    return this._widgets.filter(function (w) { return w.id === id && !w.removed; })[0];
};
Panel.prototype.addWidget = function (type) {
    if (knownWidgetTypes.indexOf(type) < 0) { return null; }
    var w = new Widget(type); this._widgets.push(w); return w;
};
Panel.prototype.widgets = function (type) {
    return this._widgets.filter(function (w) { return !w.removed && (!type || w.type === type); });
};
function loadTemplate(name) {
    if (name !== "org.kde.plasma.desktop.defaultPanel") { throw new Error("unknown template " + name); }
    __TEMPLATE__
    return true;
}
"""
STOCK = ["org.kde.plasma.kickoff", "org.kde.plasma.pager", "org.kde.plasma.icontasks",
         "org.kde.plasma.marginsseparator", "org.kde.plasma.kimpanel", "org.kde.plasma.systemtray",
         "org.kde.plasma.digitalclock", "org.kde.plasma.showdesktop", "org.kde.plasma.taskmanager"]


def template_code() -> str:
    for path in TEMPLATES:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return STAND_IN_TEMPLATE


@unittest.skipIf(MiniRacer is None, "py_mini_racer (V8) not installed")
class LayoutExecutionTest(unittest.TestCase):
    def run_layout(self, preset: dict, launcher_installed: bool) -> dict:
        known = STOCK + (["com.boswas.launcher"] if launcher_installed else [])
        ctx = MiniRacer()
        ctx.eval(MOCK.replace("__KNOWN__", json.dumps(known)).replace("__TEMPLATE__", template_code()))
        path = (support.PRESETS_DIR / "look-and-feel" / f"com.boswas.{preset['id']}" / "contents" / "layouts"
                / "org.kde.plasma.desktop-layout.js")
        ctx.eval(path.read_text(encoding="utf-8"))
        return json.loads(ctx.eval("""JSON.stringify({desktops: __desktops, panels: __panels.map(function (p) {
            return {floating: p.floating, lengthMode: p.lengthMode, height: p.height,
                    widgets: p.widgets().map(function (w) { return {type: w.type, config: w.config}; })};
        })})"""))

    def test_layouts(self):
        for preset in support.presets():
            classic = preset["panel"]["style"] == "full-width"
            for installed in (True, False):
                with self.subTest(preset=preset["id"], launcher_installed=installed):
                    out = self.run_layout(preset, installed)
                    self.assertEqual(out["desktops"], [{"wallpaperPlugin": "org.kde.image"}])
                    self.assertEqual(len(out["panels"]), 1)
                    panel = out["panels"][0]
                    types = [w["type"] for w in panel["widgets"]]
                    launcher = "com.boswas.launcher" if installed else "org.kde.plasma.kickoff"
                    tasks = preset["panel"]["task_manager"]
                    self.assertEqual(types, [launcher, "org.kde.plasma.pager", tasks,
                                             "org.kde.plasma.marginsseparator", "org.kde.plasma.systemtray",
                                             "org.kde.plasma.digitalclock", "org.kde.plasma.showdesktop"])
                    launcher_config = panel["widgets"][0]["config"]
                    self.assertEqual(launcher_config["General/icon"], "boswas-logo")
                    self.assertEqual(launcher_config["General/favorites"][0], "com.boswas.ControlCenter.desktop")
                    self.assertEqual(len(launcher_config["General/systemApplications"]), 3)
                    self.assertEqual(len(panel["widgets"][2]["config"]["General/launchers"]), 5)
                    self.assertEqual(panel["floating"], not classic)
                    if classic:
                        self.assertEqual(panel["lengthMode"], "fill")


if __name__ == "__main__":
    unittest.main()
