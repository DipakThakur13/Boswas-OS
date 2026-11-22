"""Apps: Installed apps and Optional features.

Page "installed-apps":
  Installed apps     every application with where it comes from, its version
                     and size where known (boswas_control_center.installed),
                     a search box, a filter by source and a sort order. Each
                     row removes the application the way it was installed:
                     Debian package -> sysops apt-remove (question,
                     administrator password, progress), the user's Flatpak
                     application -> `flatpak uninstall --user`, AppImage ->
                     its file and menu entry (userfiles), Windows
                     application -> the Compatibility Manager
Page "optional-features":
  Optional features  the features of boswas_control_center.features and
                     Firefox in the languages generated on this device, as
                     installed or not (`dpkg-query -W`), with Install / Remove
                     through sysops apt-install / apt-remove

Long operations run in the window's one job at a time; the JobPanel of the
page shows their progress.
"""

from __future__ import annotations

import boswas_fluent as fluent
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QVBoxLayout, QWidget

from ... import commands, features, installed, jobs, probes, software, timedate, userfiles
from ...catalog import COMPAT_MANAGER
from ...errors import Failed
from ..jobs import JobPanel
from . import context
from .system import READ_TIMEOUT, FilesSection, tool_failure
from .widgets import RowList, ask, window_of

FLATPAK_TIMEOUT = 3600.0
FLATPAK_QUERY_TIMEOUT = 90.0
SOURCE_ICONS = {installed.DEBIAN: "application-x-deb", installed.FLATPAK: "boswas-software-center",
                installed.APPIMAGE: "application-x-executable", installed.WINDOWS: "application-x-ms-dos-executable",
                installed.OTHER: "application-x-executable"}


class _JobSection(FilesSection):
    """A section that starts the window's long operations and shows them in a JobPanel of its own."""

    def __init__(self, parent: QWidget | None = None, *, environ=None, runner=None):
        super().__init__(parent, environ=environ)
        self.runner = runner or context.runner()
        self.panel_holder = self.add(QWidget(self))
        self.panel_holder.hide()
        self.panel: JobPanel | None = None

    def watched(self) -> list[str]:
        return []

    def activate(self) -> None:
        window = window_of(self)
        if window is not None and self.panel is None:
            self.panel = JobPanel(self.page, window.jobs, self.panel_holder)
            layout = QVBoxLayout(self.panel_holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.panel)
            self.panel_holder.show()
            window.jobs.changed.connect(self.jobs_changed)
        super().activate()
        self._poll.stop()

    def job_reason(self) -> str:
        """Why a long operation cannot start now ("" when it can)."""
        window = window_of(self)
        if window is None:
            return "Not available here."
        if window.jobs.busy():
            return window.jobs.busy_reason()
        return "" if self.runner.available(commands.EXECUTABLES[commands.ADMIN_TOOL]) else commands.NO_ADMIN

    def jobs_changed(self) -> None:
        """The window's operation state changed: buttons follow."""

    def start_job(self, spec: jobs.JobSpec) -> None:
        window = window_of(self)
        if window is not None:
            window.jobs.start(spec, lambda _outcome: self._after_job())

    def _after_job(self) -> None:
        window = window_of(self)
        if window is not None:
            window.store.request("desktop_apps", force=True)
        self.reload()


class InstalledAppsSection(_JobSection):
    page = "installed-apps"
    title = "Installed apps"
    order = 10
    keywords = ("installed apps", "uninstall", "remove app", "programs", "applications", "app size", "sort")

    def __init__(self, parent: QWidget | None = None, *, environ=None, runner=None):
        super().__init__(parent, environ=environ, runner=runner)
        controls = QWidget(self)
        self.search = fluent.SearchBox(self, placeholder="Search apps")
        self.search.setAccessibleName("Search installed apps")
        self.source = QComboBox(controls)
        for key, label in installed.FILTERS:
            self.source.addItem(label, key)
        self.source.setAccessibleName("Show")
        self.order = QComboBox(controls)
        for key, label in installed.SORTS:
            self.order.addItem(label, key)
        self.order.setAccessibleName("Sort by")
        self.add(self.search)
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(fluent.Label("Show", "caption", controls, tone="secondary"))
        row.addWidget(self.source)
        row.addSpacing(12)
        row.addWidget(fluent.Label("Sort by", "caption", controls, tone="secondary"))
        row.addWidget(self.order)
        row.addStretch(1)
        self.add(controls)
        self.count = self.add(fluent.Label("Reading…", "caption", self, tone="secondary"))
        self.list = self.add(RowList(self))
        self.search.textChanged.connect(lambda _t: self.populate())
        self.source.activated.connect(lambda _i: self.populate())
        self.order.activated.connect(lambda _i: self.populate())
        self.apps: list[installed.App] = []
        self.windows_apps: list | None = None

    def activate(self) -> None:
        super().activate()
        window = window_of(self)
        if window is not None:
            window.store.changed.connect(self._store_changed)
            window.store.request("windows_apps")
            self.windows_apps = window.store.value("windows_apps")

    def deactivate(self) -> None:
        super().deactivate()
        window = window_of(self)
        if window is not None:
            try:
                window.store.changed.disconnect(self._store_changed)
            except (RuntimeError, TypeError):
                pass

    def _store_changed(self, key: str) -> None:
        if key == "windows_apps" and self.active:
            window = window_of(self)
            self.windows_apps = window.store.value("windows_apps") if window is not None else None
            self.reload()

    # --- reading ---
    def read(self) -> list[installed.App]:
        environ = self.environ
        entries = probes.desktop_entries(environ, self.locale())
        owners, status = {}, {}
        home = environ.get("HOME") or None
        paths = installed.debian_paths(entries, home)
        if paths and self.runner.available(commands.EXECUTABLES["dpkg-query"]):
            result = self.runner.run(commands.dpkg_query_argv("owners", *paths[:commands.MAX_DPKG_ARGS]),
                                     READ_TIMEOUT)
            owners = installed.parse_owners(result.stdout)
            packages = sorted(set(owners.values()))
            if packages:
                result = self.runner.run(commands.dpkg_query_argv("status", *packages[:commands.MAX_DPKG_ARGS]),
                                         READ_TIMEOUT)
                status = features.parse_status(result.stdout)
        flatpaks = []
        if self.runner.available(commands.EXECUTABLES["flatpak"]):
            for where in ("user", "system"):
                result = self.runner.run(commands.argv("flatpak", "list", f"--{where}", "--app",
                                                       "--columns=application,name,version,origin"),
                                         FLATPAK_QUERY_TIMEOUT)
                if result.code == 0:
                    flatpaks += software.parse_flatpak_list(result.stdout, where)
        try:
            images = userfiles.list_appimages(environ)
        except userfiles.UserFileError:
            images = []
        return installed.assemble(entries, owners, status, flatpaks, images, self.windows_apps or [], home)

    def show_state(self, apps: list[installed.App]) -> None:
        self.apps = apps
        self.show_notice("")
        self.populate()

    def populate(self) -> None:
        shown = installed.view(self.apps, self.search.text(), self.source.currentData(), self.order.currentData())
        by_key = {app.key: app for app in shown}
        self.list.sync(list(by_key), self._row, lambda key, row: self._update(row, by_key[key]))
        self.count.setText(f"{len(shown)} app{'s' if len(shown) != 1 else ''} found")

    def _row(self, key: str) -> fluent.SettingsCard:
        card = fluent.SettingsCard("", "", None, None, self.list)
        card.action = fluent.Button("Uninstall", card)
        card.action.clicked.connect(lambda _c=False, k=key: self.uninstall(k))
        card.set_control(card.action)
        return card

    def _update(self, card: fluent.SettingsCard, app: installed.App) -> None:
        card.set_title(app.name)
        card.set_description(app.detail + (f" · {app.why_not}" if app.why_not else ""))
        card.set_icon(fluent.icon(app.icon, SOURCE_ICONS[app.source]) if app.icon and "/" not in app.icon
                      else fluent.icon(SOURCE_ICONS[app.source]))
        windows = app.source == installed.WINDOWS
        card.action.setText("Manage" if windows else "Uninstall")
        card.action.setAccessibleName(f"{'Manage' if windows else 'Uninstall'} {app.name}")
        card.action.setVisible(app.removable)
        reason = self.job_reason() if app.source == installed.DEBIAN else ""
        card.action.setEnabled(not self.busy and not reason)
        card.action.setToolTip(reason or ("Opens the Compatibility Manager" if windows else ""))

    def jobs_changed(self) -> None:
        if self.active:
            self.populate()

    def set_enabled(self, enabled: bool) -> None:
        self.populate()

    # --- uninstalling ---
    def uninstall(self, key: str) -> None:
        app = next((a for a in self.apps if a.key == key), None)
        window = window_of(self)
        if app is None or not app.removable:
            return
        if app.source == installed.WINDOWS:
            if window is not None:
                window.open_program(COMPAT_MANAGER)
            return
        if app.source == installed.DEBIAN:
            if ask(self, f"Uninstall {app.name}?", f"The package {app.package} is removed. Packages that depend on "
                   "it are removed too; Boswas OS refuses if that would remove a part of the system. Your files are "
                   "kept. You are asked for an administrator password.", "Uninstall"):
                self.start_job(jobs.JobSpec(jobs.SYSOPS, ("apt-remove", app.package), f"Removing {app.name}",
                                            self.page))
            return
        if app.source == installed.FLATPAK:
            if not ask(self, f"Uninstall {app.name}?", f"{app.flatpak_id} is removed from your account. Its data in "
                       "~/.var/app is kept.", "Uninstall"):
                return

            def apply() -> None:
                result = self.runner.run(commands.argv("flatpak", "uninstall", "--user", "--noninteractive", "-y",
                                                       app.flatpak_id), FLATPAK_TIMEOUT)
                if result.code != 0:
                    raise tool_failure(f"Removing {app.name}", result)

            self.change(apply, f"{app.name} was uninstalled.")
            return
        if app.source == installed.APPIMAGE:
            if not ask(self, f"Uninstall {app.name}?", "The AppImage file and its menu entry are deleted. Settings it "
                       "saved in your home folder are kept.", "Uninstall"):
                return
            environ = self.environ

            def remove() -> None:
                try:
                    userfiles.remove_appimage(environ, app.appimage_id)
                except userfiles.UserFileError as exc:
                    raise Failed(str(exc)) from None

            self.change(remove, f"{app.name} was uninstalled.")


class FeatureRow(fluent.SettingsCard):
    def __init__(self, feature: features.Feature, parent: QWidget | None = None):
        super().__init__(feature.title, feature.description, fluent.icon(feature.icon, "package-x-generic"), None,
                         parent)
        self.feature = feature
        self.state_label = fluent.Label("", "caption", self, tone="secondary")
        self.action = fluent.Button("Install", self)
        self.add_control(self.state_label)
        self.set_control(self.action)


class OptionalFeaturesSection(_JobSection):
    page = "optional-features"
    title = "Optional features"
    order = 10
    keywords = ("optional features", "codecs", "printing", "fonts", "language pack", "7zip", "emoji", "pdf printer",
                "developer tools", "add a feature")

    def __init__(self, parent: QWidget | None = None, *, environ=None, runner=None):
        super().__init__(parent, environ=environ, runner=runner)
        self.list = self.add(RowList(self))
        self.status: dict = {}
        self.offered: list[features.Feature] = []

    def read(self):
        locales = []
        try:
            result = self.runner.run(commands.argv("locale", "-a"), READ_TIMEOUT)
            locales = timedate.parse_locale_list(result.stdout) if result.code == 0 else []
        except Exception:                         # noqa: BLE001 - no language packs offered then
            locales = []
        offered = list(features.FEATURES) + features.language_features(locales)
        packages = sorted({package for feature in offered for package in feature.packages})
        if not self.runner.available(commands.EXECUTABLES["dpkg-query"]):
            raise Failed("dpkg-query is not available, so installed features cannot be told.")
        result = self.runner.run(commands.dpkg_query_argv("status", *packages), READ_TIMEOUT)
        # dpkg-query exits 1 when a package was never installed; the others are still listed.
        return offered, features.parse_status(result.stdout)

    def show_state(self, state) -> None:
        self.offered, self.status = state
        self.show_notice("")
        by_key = {feature.key: feature for feature in self.offered}
        self.list.sync(list(by_key), lambda key: self._row(by_key[key]), lambda key, row: self._update(row))

    def load_failed(self, exc: Exception) -> None:
        self.show_notice(f"Optional features are not available: {exc}", "warning")

    def _row(self, feature: features.Feature) -> FeatureRow:
        row = FeatureRow(feature, self.list)
        row.action.clicked.connect(lambda _c=False, k=feature.key: self.toggle(k))
        return row

    def _update(self, row: FeatureRow) -> None:
        state = features.feature_state(row.feature, self.status)
        row.state_label.setText({"installed": "Installed", "partly": "Partly installed", "missing": ""}[state])
        row.action.setText("Remove" if state == "installed" else "Install")
        row.action.setAccessibleName(f"{'Remove' if state == 'installed' else 'Install'} {row.feature.title}")
        reason = self.job_reason()
        row.action.setEnabled(not reason and not self.busy)
        row.action.setToolTip(reason or ", ".join(row.feature.packages))

    def jobs_changed(self) -> None:
        for key in self.list.keys():
            self._update(self.list.row(key))

    def toggle(self, key: str) -> None:
        feature = next((f for f in self.offered if f.key == key), None)
        if feature is None:
            return
        packages = ", ".join(feature.packages)
        if features.feature_state(feature, self.status) == "installed":
            if ask(self, f"Remove {feature.title}?", f"The package{'s' if len(feature.packages) > 1 else ''} "
                   f"{packages} {'are' if len(feature.packages) > 1 else 'is'} removed. Boswas OS refuses if that "
                   "would remove a part of the system. You are asked for an administrator password.", "Remove"):
                self.start_job(jobs.JobSpec(jobs.SYSOPS, ("apt-remove", *feature.packages),
                                            f"Removing {feature.title}", self.page))
            return
        missing = tuple(p for p in feature.packages if not features.installed(self.status, p))
        if ask(self, f"Install {feature.title}?", f"{', '.join(missing)} and the packages they need are installed "
               "from Debian's signed repositories. You are asked for an administrator password.", "Install"):
            self.start_job(jobs.JobSpec(jobs.SYSOPS, ("apt-install", *missing), f"Installing {feature.title}",
                                        self.page))


SECTIONS = (InstalledAppsSection, OptionalFeaturesSection)
