"""Android Applications: the optional Waydroid runtime, Android apps and APK files.

Everything comes from boswas-android (docs/architecture/platform-contracts.md,
section 2). Reading the status changes nothing. Installing, initialising,
starting, stopping, repairing, updating or removing the runtime, installing
an APK or F-Droid are operations the user starts with a click and confirms;
boswas-android asks for administrator authorization where needed. Google
Play is shown exactly as the runtime manager assesses it: Control Center
never offers it on its own.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QWidget

from .. import android, filetypes, jobs
from .. import viewmodel as vm
from .base import Page
from .common import SP, Card, InfoGrid, MessageBanner, Separator, StatusLine, SummaryCard, icon, muted_label, plain_label
from .jobs import JobPanel

APK_FILTER = "Android apps (*.apk);;All files (*)"


class AndroidPage(Page):
    data_keys = ("android",)

    def build(self) -> None:
        jobs_ = self.window_.jobs
        self.summary = SummaryCard(self.content)
        self.install_button = self._button("Install Runtime…", "Installs Waydroid from its signed repository "
                                           "(administrator rights, network)", self.install_runtime,
                                           "download", self.summary)
        self.init_button = self._button("Initialize…", "Downloads the Android system images (about 1 GB)",
                                        self.initialize, "run-build", self.summary)
        self.summary.actions.addWidget(self.install_button)
        self.summary.actions.addWidget(self.init_button)
        self.body.addWidget(self.summary)
        self.message = MessageBanner(parent=self.content)
        self.body.addWidget(self.message)
        self.panel = JobPanel(self.info.key, jobs_, self.content)
        self.body.addWidget(self.panel)

        self.runtime = Card("Runtime", self.content)
        self.grid = InfoGrid(self.runtime)
        self.runtime.addWidget(self.grid)
        row = QHBoxLayout()
        self.start_button = self._button("Start", "Starts the Android container service", lambda: self.service("start"),
                                         "media-playback-start", self.runtime)
        self.stop_button = self._button("Stop", "Stops Android and its container service",
                                        lambda: self.service("stop"), "media-playback-stop", self.runtime)
        self.restart_button = self._button("Restart", "Restarts the Android container service",
                                           lambda: self.service("restart"), "view-refresh", self.runtime)
        self.repair_button = self._button("Repair…", "Re-applies the runtime configuration and restarts it",
                                          lambda: self.runtime_op("repair"), "tools-wizard", self.runtime)
        self.update_button = self._button("Update…", "Downloads a newer Android system if one is available",
                                          lambda: self.runtime_op("update"), "system-software-update", self.runtime)
        for button in (self.start_button, self.stop_button, self.restart_button, self.repair_button,
                       self.update_button):
            row.addWidget(button)
        row.addStretch(1)
        self.runtime.addLayout(row)
        remove_row = QHBoxLayout()
        self.remove_button = self._button("Remove Runtime…", "Removes Waydroid; your Android data is kept",
                                          lambda: self.remove(False), "edit-delete", self.runtime)
        self.purge_button = self._button("Remove Runtime and System Images…",
                                         "Removes Waydroid and the downloaded Android system images",
                                         lambda: self.remove(True), "edit-delete", self.runtime)
        remove_row.addWidget(self.remove_button)
        remove_row.addWidget(self.purge_button)
        remove_row.addStretch(1)
        self.runtime.addLayout(remove_row)
        self.body.addWidget(self.runtime)

        self.compat = Card("Compatibility of this device", self.content, spacing=0)
        self.compat_lines: list[QWidget] = []
        self.compat_empty = muted_label("", parent=self.compat)
        self.compat.addWidget(self.compat_empty)
        self.body.addWidget(self.compat)

        self.apps_card = Card("Android apps", self.content)
        self.apps_note = muted_label("", parent=self.apps_card)
        self.apps = QListWidget(self.apps_card)
        self.apps.setAccessibleName("Installed Android apps")
        self.apps.setMinimumHeight(120)
        app_row = QHBoxLayout()
        self.apk_button = self._button("Install APK File…", "Checks an APK file and installs it after you confirm",
                                       self.install_apk, "document-open", self.apps_card)
        self.launch_button = self._button("Open", "Opens the selected Android app", self.launch,
                                          "system-run", self.apps_card)
        self.app_remove_button = self._button("Uninstall…", "Uninstalls the selected Android app",
                                              self.remove_app, "edit-delete", self.apps_card)
        for button in (self.apk_button, self.launch_button, self.app_remove_button):
            app_row.addWidget(button)
        app_row.addStretch(1)
        self.apps_card.addWidget(self.apps_note)
        self.apps_card.addWidget(self.apps)
        self.apps_card.addLayout(app_row)
        self.body.addWidget(self.apps_card)

        self.play = Card("Google Play", self.content)
        self.play_text = plain_label("", wrap=True, selectable=True, parent=self.play)
        self.play_alternatives = muted_label("", parent=self.play)
        self.fdroid_button = self._button("Install F-Droid (open-source app store, not Google Play)…",
                                          "Downloads the F-Droid client from f-droid.org, checks its signature and "
                                          "installs it", self.install_fdroid, "download", self.play)
        self.play.addWidget(self.play_text)
        self.play.addWidget(Separator(self.play))
        self.play.addWidget(self.play_alternatives)
        self.play.addWidget(self.fdroid_button, 0)
        self.body.addWidget(self.play)
        self.view_: android.AndroidView = android.android_view(None, loading=True)
        self.apps.currentRowChanged.connect(lambda _row: self._buttons())
        jobs_.changed.connect(self._buttons)

    def _button(self, text: str, description: str, slot, icon_name: str, parent) -> QPushButton:
        button = QPushButton(text, parent)
        button.setIcon(icon(icon_name, fallback=SP.SP_CommandLink))
        button.setAccessibleDescription(description)
        button.clicked.connect(slot)
        return button

    def refresh(self) -> None:
        super().refresh()
        if self.view_.initialized:
            self.store.request("android_apps", force=True)

    def _changed(self, key: str) -> None:
        # The app list is requested only once the runtime is initialized, but it redraws the page too.
        if key == "android_apps":
            self._render_apps()
            self._buttons()
        else:
            super()._changed(key)

    # --- render ---
    def render(self) -> None:
        result = self.store.get("android")
        self.view_ = android.android_view(result.value if result is not None and result.ok else None,
                                          result.error if result is not None and not result.ok else None,
                                          bool(result is not None and result.unavailable),
                                          loading=result is None or self.store.loading("android"))
        view = self.view_
        self.summary.set_summary(view.summary)
        self.grid.set_rows(list(view.rows) if view.rows else [("Runtime", "Unknown")])
        for widget in self.compat_lines:
            widget.deleteLater()
        self.compat_lines = []
        for index, check in enumerate(view.checks):
            if index:
                separator = Separator(self.compat)
                self.compat.addWidget(separator)
                self.compat_lines.append(separator)
            line = StatusLine(check.title, self.compat)
            line.set_status(check.state, check.detail)
            self.compat.addWidget(line)
            self.compat_lines.append(line)
        self.compat_empty.setText("" if view.checks else ("Checking…" if result is None else
                                                          "No compatibility information is available."))
        self.compat_empty.setVisible(not view.checks)
        self.play_text.setText(view.google_play_reason or "Google Play status is not known.")
        alternatives = [android.ALTERNATIVE_LABELS[a] for a in view.alternatives]
        self.play_alternatives.setText(("Alternatives: " + "; ".join(alternatives) + ".") if alternatives else
                                       "No alternatives are reported.")
        if view.initialized:
            self.store.request("android_apps")
        self._render_apps()
        self._buttons()

    def _render_apps(self) -> None:
        apps_result = self.store.get("android_apps")
        current = self.apps.currentItem()
        current_package = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self.apps.clear()
        if not self.view_.initialized:
            self.apps_note.setText("Android apps can be installed once the runtime is installed and initialized.")
            self.apps.hide()
            return
        if apps_result is None:
            self.apps_note.setText("Listing Android apps…")
        elif not apps_result.ok:
            self.apps_note.setText(f"The Android apps could not be listed: {apps_result.error}")
        else:
            apps = apps_result.value or []
            self.apps_note.setText("No Android apps are installed." if not apps else
                                   f"{len(apps)} Android app{'s' if len(apps) != 1 else ''} installed. Apps also "
                                   "appear in the application menu.")
            for app in apps:
                item = QListWidgetItem(f"{app.name}  ({app.package}{', ' + app.version if app.version else ''})")
                item.setData(Qt.ItemDataRole.UserRole, app.package)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, app.name)
                self.apps.addItem(item)
                if app.package == current_package:
                    self.apps.setCurrentItem(item)
        self.apps.setVisible(apps_result is not None and apps_result.ok and bool(apps_result.value))

    def _set(self, button: QPushButton, enabled: bool, reason: str, visible: bool = True) -> None:
        button.setVisible(visible)
        button.setEnabled(enabled)
        button.setToolTip(button.accessibleDescription() if enabled else reason)

    def _buttons(self) -> None:
        view, jobs_ = self.view_, self.window_.jobs
        busy = jobs_.busy()
        result = self.store.get("android")
        manager = result is None or not result.unavailable
        reason = jobs_.busy_reason() if busy else ""
        if result is None:
            reason = "Checking the Android runtime…"
        elif not manager:
            reason = android.NOT_INSTALLED_MANAGER
        elif not result.ok:
            reason = "The Android runtime status could not be read; choose Refresh (F5) to try again."
        installed, initialized = view.installed is True, view.initialized is True
        unsupported = view.supported is False
        blocker = " ".join(view.blockers[:2])
        install_reason = reason or (blocker if unsupported else "") or ("Already installed." if installed else "")
        self._set(self.install_button, not install_reason and view.installed is False, install_reason or
                  "The runtime status is not known yet.", visible=not installed)
        init_reason = reason or ("Install the runtime first." if not installed else
                                 ("Already initialized." if initialized else ""))
        self._set(self.init_button, not init_reason and view.initialized is False, init_reason or
                  "The runtime status is not known yet.", visible=installed and not initialized)
        ready = reason or ("Install and initialize the runtime first." if not (installed and initialized) else "")
        service = view.service_active
        self._set(self.start_button, not ready and service is not True, ready or "The container service is running.")
        self._set(self.stop_button, not ready and service is not False, ready or "The container service is stopped.")
        self._set(self.restart_button, not ready, ready)
        self._set(self.repair_button, not ready, ready)
        self._set(self.update_button, not ready, ready)
        removable = reason or ("The runtime is not installed." if not installed else "")
        self._set(self.remove_button, not removable, removable)
        self._set(self.purge_button, not removable, removable)
        self._set(self.apk_button, not ready, ready)
        selected = self.apps.currentItem() is not None
        pick = ready or ("" if selected else "Select an app first.")
        self._set(self.launch_button, not pick, pick)
        self._set(self.app_remove_button, not pick, pick)
        self._set(self.fdroid_button, not ready, ready, visible="fdroid" in view.alternatives)
        self.runtime.setVisible(manager)
        self.apps_card.setVisible(manager)

    # --- actions ---
    def _start(self, words: tuple[str, ...], title: str) -> None:
        self.window_.jobs.start(jobs.JobSpec(jobs.ANDROID, words, title, self.info.key), self._job_done)

    def _job_done(self, outcome: jobs.Outcome) -> None:
        self.store.request("android", force=True)
        if self.view_.initialized or outcome.ok:
            self.store.request("android_apps", force=True)

    def install_runtime(self) -> None:
        if self.window_.ask("Install the Android runtime?",
                            "Boswas OS installs Waydroid from the Waydroid project's signed package repository and "
                            "enables the Android kernel driver (binder). This needs a network connection and an "
                            "administrator password. Nothing starts automatically at boot. Google Play is not "
                            "installed.", "Install Runtime"):
            self._start(("runtime", "install"), "Installing the Android runtime")

    def initialize(self) -> None:
        if self.window_.ask("Initialize Android?",
                            "Waydroid downloads the Android system images (about 1 GB, without Google apps) and "
                            "checks them. This needs a network connection and an administrator password.",
                            "Initialize"):
            self._start(("runtime", "init"), "Initializing Android")

    def service(self, verb: str) -> None:
        texts = {"start": ("Start Android?", "The Android container service starts.", "Start"),
                 "stop": ("Stop Android?", "Running Android apps close and the container service stops.", "Stop"),
                 "restart": ("Restart Android?", "Running Android apps close and Android starts again.", "Restart")}
        title, text, confirm = texts[verb]
        if self.window_.ask(title, text, confirm):
            self._start(("service", verb), {"start": "Starting Android", "stop": "Stopping Android",
                                            "restart": "Restarting Android"}[verb])

    def runtime_op(self, verb: str) -> None:
        texts = {"repair": ("Repair the Android runtime?", "Android stops, its configuration is applied again and it "
                            "restarts. Your apps and data are kept. An administrator password is needed.", "Repair"),
                 "update": ("Update Android?", "Waydroid downloads a newer Android system if one is available "
                            "(network needed). Your apps and data are kept. An administrator password is needed.",
                            "Update")}
        title, text, confirm = texts[verb]
        if self.window_.ask(title, text, confirm):
            self._start(("runtime", verb), {"repair": "Repairing Android", "update": "Updating Android"}[verb])

    def remove(self, purge: bool) -> None:
        text = ("Waydroid is removed. Your Android apps and data in your home folder are kept"
                + (", the downloaded Android system images are deleted." if purge else
                   " and so are the downloaded system images.") + " An administrator password is needed.")
        if self.window_.ask("Remove the Android runtime?", text, "Remove"):
            words = ("runtime", "remove", "--purge-data") if purge else ("runtime", "remove")
            self._start(words, "Removing the Android runtime")

    def install_apk(self) -> None:
        path = self.window_.choose_file("Choose an Android app (APK)", APK_FILTER)
        if not path:
            return
        self.open_apk(path)

    def open_apk(self, path: str) -> None:
        """Inspect an APK file and, after confirmation, install it (also used by the Software Center)."""
        try:
            kind = self.window_.backend.detect_file(path)
        except Exception as exc:        # noqa: BLE001 - shown to the user
            self.message.show_message("error", self.window_.error_text(exc))
            return
        if kind.kind != filetypes.APK:
            self.message.show_message("error", f"This file is not an Android app ({kind.description}).")
            return
        self.message.show_message("info", "Checking the Android app…")
        self.window_.runner.submit(self.window_.backend.android_inspect, kind.path,
                                   on_done=lambda view, p=kind.path: self._inspected(p, view),
                                   on_error=lambda exc: self.message.show_message(
                                       "error", f"The app could not be checked. {self.window_.error_text(exc)}"))

    def _inspected(self, path: str, view: android.ApkView) -> None:
        self.message.hide()
        if not view.compatible:
            self.message.show_message("error", f"{view.title} cannot be installed: " + " ".join(view.problems))
            return
        if self.window_.ask(f"Install {view.title}?", android.apk_confirmation(view), "Install"):
            self._start(("apk", "install", path), f"Installing {view.title}")

    def _selected_package(self) -> str | None:
        item = self.apps.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def launch(self) -> None:
        package = self._selected_package()
        if not package:
            return
        self.message.show_message("info", "Opening the app…")
        self.window_.runner.submit(self.window_.backend.android_launch, package,
                                   on_done=lambda _r: self.message.hide(),
                                   on_error=lambda exc: self.message.show_message(
                                       "error", f"The app could not be opened. {self.window_.error_text(exc)}"))

    def remove_app(self) -> None:
        package = self._selected_package()
        if package and self.window_.ask("Uninstall this Android app?",
                                        f"{package} and its data inside Android are removed.", "Uninstall"):
            self._start(("app", "remove", package), f"Uninstalling {package}")

    def install_fdroid(self) -> None:
        if self.window_.ask("Install F-Droid?",
                            "F-Droid is an open-source Android app store. It is not Google Play and offers different "
                            "apps. Boswas OS downloads the F-Droid client from f-droid.org, checks that it is signed "
                            "by F-Droid, and installs it.", "Install F-Droid"):
            self._start(("fdroid", "install"), "Installing F-Droid")
