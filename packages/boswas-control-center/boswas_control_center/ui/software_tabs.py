"""The Software Center's install tabs: Linux packages, Flatpak, AppImages, Install from file.

  Linux packages   Debian's signed repositories: search (apt-cache), details,
                   install and remove through boswas-sysops (administrator
                   rights, confirmation, progress)
  Flatpak          remotes the administrator enabled (Enable Flathub runs
                   boswas-sysops flatpak-remote-enable); applications are
                   installed for this user only
  AppImages        copied to ~/Applications with a menu entry; they run
                   without a sandbox, which the window says before adding one
  Install from file  routes a chosen file by its content: Debian package ->
                   boswas-sysops apt-install-local (origin not verified, said
                   so), Windows program -> Compatibility Manager, APK ->
                   Android page, AppImage -> AppImages
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
                               QWidget)

from .. import commands, filetypes, jobs, software
from .common import SP, Card, InfoGrid, MessageBanner, icon, muted_label, plain_label, title_label

USER = Qt.ItemDataRole.UserRole


def _button(text: str, description: str, slot, icon_name: str, parent) -> QPushButton:
    button = QPushButton(text, parent)
    button.setIcon(icon(icon_name, fallback=SP.SP_CommandLink))
    button.setAccessibleDescription(description)
    button.clicked.connect(slot)
    return button


def _enable(button: QPushButton, reason: str) -> None:
    button.setEnabled(not reason)
    button.setToolTip(reason or button.accessibleDescription())


class _Tab(QWidget):
    def __init__(self, page, parent: QWidget | None = None):
        super().__init__(parent)
        self.page = page
        self.window_ = page.window_
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(4, 10, 4, 4)
        self.layout_.setSpacing(10)
        self.message = MessageBanner(parent=self)
        self.layout_.addWidget(self.message)

    def busy_reason(self) -> str:
        jobs_ = self.window_.jobs
        return jobs_.busy_reason() if jobs_.busy() else ""

    def refresh(self) -> None:
        """Called when the tab is shown and after operations."""

    def update_buttons(self) -> None:
        """Called whenever the window's operation state changes."""


# --- Linux packages ----------------------------------------------------------------------------------------

class LinuxTab(_Tab):
    def __init__(self, page, parent: QWidget | None = None):
        super().__init__(page, parent)
        self.layout_.addWidget(muted_label("Software from Debian's repositories. APT checks every package's "
                                           "signature before it is installed.", parent=self))
        row = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search Debian packages by name, for example vlc or gimp")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search Debian packages")
        self.search.returnPressed.connect(self.run_search)
        self.search_button = _button("Search", "Searches the package lists on this device", self.run_search,
                                     "edit-find", self)
        row.addWidget(self.search, 1)
        row.addWidget(self.search_button)
        self.layout_.addLayout(row)
        body = QHBoxLayout()
        self.results = QListWidget(self)
        self.results.setAccessibleName("Debian packages found")
        self.results.setMinimumHeight(260)
        self.results.currentItemChanged.connect(self._selected)
        self.detail = Card(parent=self, margins=14)
        self.detail.setFixedWidth(330)
        self.name = title_label("", 1.15, self.detail)
        self.summary = plain_label("", wrap=True, parent=self.detail)
        self.grid = InfoGrid(self.detail)
        self.description = muted_label("", selectable=True, parent=self.detail)
        buttons = QHBoxLayout()
        self.install_button = _button("Install…", "Installs the package and its dependencies (administrator "
                                      "rights)", self.install, "download", self.detail)
        self.remove_button = _button("Remove…", "Removes the package (administrator rights)", self.remove,
                                     "edit-delete", self.detail)
        buttons.addWidget(self.install_button)
        buttons.addWidget(self.remove_button)
        self.empty = muted_label("Search for a package, then select it to see its details.", parent=self.detail)
        for widget in (self.name, self.summary, self.grid, self.description):
            self.detail.addWidget(widget)
        self.detail.addLayout(buttons)
        self.detail.addWidget(self.empty)
        self.detail.layout_.addStretch(1)
        body.addWidget(self.results, 1)
        body.addWidget(self.detail, 0)
        self.layout_.addLayout(body)
        self.hits: list[software.PackageHit] = []
        self.details: dict | None = None
        self.searching = False
        self.last_term = ""
        self._show_detail(None)

    def run_search(self) -> None:
        term = self.search.text().strip()
        if not term:
            return
        if not software.valid_search(term):
            self.message.show_message("error", "Search with letters, digits, spaces and + . _ - only.")
            return
        self.searching = True
        self.last_term = term
        self.message.show_message("info", f"Searching for “{term}”…")
        self.update_buttons()
        self.window_.runner.submit(self.window_.backend.apt_search, term, key="apt-search",
                                   on_done=self._found, on_error=self._failed)

    def _found(self, hits: list[software.PackageHit]) -> None:
        self.searching = False
        self.message.hide()
        current = self.results.currentItem()
        selected = current.data(USER) if current is not None else None
        self.hits = hits
        self.results.clear()
        for hit in hits:
            state = f"installed {hit.installed}" if hit.is_installed else (hit.candidate or "not available")
            item = QListWidgetItem(f"{hit.name} — {hit.summary}  ({state})")
            item.setData(USER, hit.name)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, hit.name)
            self.results.addItem(item)
            if hit.name == selected:
                self.results.setCurrentItem(item)
        if not hits:
            self.message.show_message("info", f"No Debian package name matches “{self.last_term}”.")
        self.update_buttons()

    def _failed(self, exc) -> None:
        self.searching = False
        self.message.show_message("error", f"The search failed. {self.window_.error_text(exc)}")
        self.update_buttons()

    def _hit(self, name: str | None) -> software.PackageHit | None:
        return next((h for h in self.hits if h.name == name), None)

    def _selected(self, current, _previous) -> None:
        name = current.data(USER) if current is not None else None
        self.details = None
        self._show_detail(self._hit(name))
        if name:
            self.window_.runner.submit(self.window_.backend.apt_details, name, key=f"apt-details:{name}",
                                       on_done=lambda fields, n=name: self._details(n, fields),
                                       on_error=lambda exc: self.description.setText(
                                           f"Details could not be read: {self.window_.error_text(exc)}"))

    def _details(self, name: str, fields: dict) -> None:
        if self.selected_name() != name:
            return
        self.details = fields
        self._show_detail(self._hit(name))

    def selected_name(self) -> str | None:
        item = self.results.currentItem()
        return item.data(USER) if item is not None else None

    def _show_detail(self, hit: software.PackageHit | None) -> None:
        for widget in (self.name, self.summary, self.grid, self.description, self.install_button,
                       self.remove_button):
            widget.setVisible(hit is not None)
        self.empty.setVisible(hit is None)
        if hit is None:
            return
        fields = self.details or {}
        self.name.setText(hit.name)
        self.summary.setText(fields.get("Summary") or hit.summary)
        self.grid.set_rows([
            ("Installed", fields.get("Installed", hit.installed) or "No"),
            ("Available", fields.get("Candidate", hit.candidate) or "Not available"),
            ("Source", fields.get("Source", hit.source)),
            ("Size", software.size_text(fields) if fields else "…"),
            ("Section", fields.get("Section", "…") or "Unknown"),
        ])
        self.description.setText(fields.get("Description", "") if fields else "Reading details…")
        self.update_buttons()

    def update_buttons(self) -> None:
        reason = self.busy_reason()
        admin = (self.page.store.value("programs") or {}).get("admin", True)
        if not admin:
            reason = reason or commands.NO_ADMIN
        _enable(self.search_button, "Searching…" if self.searching else "")
        hit = self._hit(self.selected_name())
        installed = bool((self.details or {}).get("Installed", hit.installed if hit else ""))
        candidate = bool((self.details or {}).get("Candidate", hit.candidate if hit else ""))
        _enable(self.install_button, reason or ("Already installed." if installed else
                                                ("" if candidate else "No repository offers this package.")))
        _enable(self.remove_button, reason or ("" if installed else "This package is not installed."))

    def install(self) -> None:
        name = self.selected_name()
        if not name or not self.window_.ask(f"Install {name}?",
                                            f"{name} and the packages it needs are installed from Debian's signed "
                                            "repositories. You are asked for an administrator password.", "Install"):
            return
        self.window_.jobs.start(jobs.JobSpec(jobs.SYSOPS, ("apt-install", name), f"Installing {name}",
                                             self.page.info.key), self._changed)

    def remove(self) -> None:
        name = self.selected_name()
        if not name or not self.window_.ask(f"Remove {name}?",
                                            f"{name} is removed. Packages that depend on it are removed too; Boswas OS "
                                            "refuses if that would remove a part of the system. Your files are kept. "
                                            "You are asked for an administrator password.", "Remove"):
            return
        self.window_.jobs.start(jobs.JobSpec(jobs.SYSOPS, ("apt-remove", name), f"Removing {name}",
                                             self.page.info.key), self._changed)

    def _changed(self, outcome: jobs.Outcome) -> None:
        self.page.store.request("desktop_apps", force=True)
        if self.last_term:
            self.search.setText(self.last_term)
            self.run_search()


# --- Flatpak ---------------------------------------------------------------------------------------------------

class FlatpakTab(_Tab):
    def __init__(self, page, parent: QWidget | None = None):
        super().__init__(page, parent)
        self.remotes = plain_label("", wrap=True, parent=self)
        self.layout_.addWidget(self.remotes)
        row = QHBoxLayout()
        self.enable_button = _button("Enable Flathub…", "Adds the Flathub remote with its pinned signing key "
                                     "(administrator rights)", self.enable_flathub, "list-add", self)
        self.update_button = _button("Update Flatpak Applications", "Updates your Flatpak applications",
                                     self.update_all, "system-software-update", self)
        row.addWidget(self.enable_button)
        row.addWidget(self.update_button)
        row.addStretch(1)
        self.layout_.addLayout(row)
        search_row = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search Flatpak applications")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search Flatpak applications")
        self.search.returnPressed.connect(self.run_search)
        self.search_button = _button("Search", "Searches the enabled Flatpak remotes", self.run_search, "edit-find",
                                     self)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.search_button)
        self.layout_.addLayout(search_row)
        self.found = QListWidget(self)
        self.found.setAccessibleName("Flatpak applications found")
        self.found.setMinimumHeight(140)
        self.found.currentItemChanged.connect(lambda *_: self.update_buttons())
        self.install_button = _button("Install for Me…", "Installs the selected application for your account only",
                                      self.install, "download", self)
        self.layout_.addWidget(self.found)
        self.layout_.addWidget(self.install_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.layout_.addWidget(title_label("Your Flatpak applications", 1.0, self))
        self.installed = QListWidget(self)
        self.installed.setAccessibleName("Installed Flatpak applications")
        self.installed.setMinimumHeight(100)
        self.installed.currentItemChanged.connect(lambda *_: self.update_buttons())
        self.uninstall_button = _button("Uninstall…", "Removes the selected Flatpak application", self.uninstall,
                                        "edit-delete", self)
        self.layout_.addWidget(self.installed)
        self.layout_.addWidget(self.uninstall_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.results: list[software.FlatpakApp] = []
        self.working = ""

    def state(self) -> software.FlatpakState | None:
        return self.page.store.value("flatpak")

    def refresh(self) -> None:
        result = self.page.store.get("flatpak")
        state = self.state()
        if result is None:
            self.remotes.setText("Reading Flatpak remotes…")
        elif not result.ok:
            self.remotes.setText("Flatpak is not available: " + (result.error or "unknown error"))
        else:
            enabled = state.enabled()
            if enabled:
                names = ", ".join(sorted({f"{r.name} ({'for everyone' if r.installation == 'system' else 'you'})"
                                          for r in enabled}))
                self.remotes.setText(f"Enabled remotes: {names}. Applications are installed for your account only "
                                     "and run sandboxed by Flatpak.")
            else:
                self.remotes.setText("No Flatpak remote is enabled on this device, so no Flatpak applications can be "
                                     "installed. Your administrator can enable Flathub.")
        current = self.installed.currentItem()
        selected = current.data(USER) if current is not None else None
        self.installed.clear()
        for app in (state.apps if state is not None else ()):
            item = QListWidgetItem(f"{app.name} ({app.app_id}{', ' + app.version if app.version else ''}) — "
                                   f"{'your account' if app.installation == 'user' else 'all users'}")
            item.setData(USER, (app.app_id, app.installation))
            item.setData(Qt.ItemDataRole.AccessibleTextRole, app.name)
            self.installed.addItem(item)
            if selected and selected[0] == app.app_id:
                self.installed.setCurrentItem(item)
        self.update_buttons()

    def update_buttons(self) -> None:
        state = self.state()
        reason = "Another Flatpak operation is running." if self.working else ""
        has_remote = bool(state and state.enabled())
        flathub_enabled = bool(state and any(r.name == "flathub" for r in state.enabled("system")))
        shipped = bool(state and "flathub" in state.shipped)
        self.enable_button.setVisible(state is not None and not flathub_enabled)
        _enable(self.enable_button, self.busy_reason() or ("" if shipped else
                                                           "This device has no approved Flathub configuration."))
        no_remote = "" if has_remote else "Enable a Flatpak remote first."
        _enable(self.search_button, reason or no_remote)
        _enable(self.update_button, reason or ("" if state and any(a.installation == "user" for a in state.apps)
                                               else "You have no Flatpak applications to update."))
        _enable(self.install_button, reason or no_remote or ("" if self.found.currentItem() else
                                                             "Select an application first."))
        current = self.installed.currentItem()
        mine = current is not None and current.data(USER)[1] == "user"
        _enable(self.uninstall_button, reason or ("" if mine else ("Applications installed for all users are "
                                                                   "managed by your administrator." if current
                                                                   else "Select an application first.")))

    def enable_flathub(self) -> None:
        if self.window_.ask("Enable Flathub?",
                            "Flathub, the main Flatpak application repository, is added for all users with the "
                            "signing key Boswas OS ships. Applications from Flathub are made by their developers, "
                            "not by Boswas or Debian. You are asked for an administrator password.", "Enable"):
            self.window_.jobs.start(jobs.JobSpec(jobs.SYSOPS, ("flatpak-remote-enable", "flathub"),
                                                 "Enabling Flathub", self.page.info.key),
                                    lambda _o: self.page.store.request("flatpak", force=True))

    def run_search(self) -> None:
        term = self.search.text().strip()
        if not software.valid_search(term):
            self.message.show_message("error", "Search with letters, digits, spaces and + . _ - only.")
            return
        self.message.show_message("info", f"Searching for “{term}”…")
        self.window_.runner.submit(self.window_.backend.flatpak_search, term, key="flatpak-search",
                                   on_done=self._found, on_error=lambda exc: self.message.show_message(
                                       "error", f"The search failed. {self.window_.error_text(exc)}"))

    def _found(self, apps: list[software.FlatpakApp]) -> None:
        self.message.hide()
        self.results = apps
        self.found.clear()
        for app in apps:
            item = QListWidgetItem(f"{app.name} — {app.description}  ({app.app_id}, {app.remote})")
            item.setData(USER, (app.app_id, app.remote))
            item.setData(Qt.ItemDataRole.AccessibleTextRole, app.name)
            self.found.addItem(item)
        if not apps:
            self.message.show_message("info", "No Flatpak application matches.")
        self.update_buttons()

    def _change(self, what: str, fn, *args) -> None:
        self.working = what
        self.message.show_message("info", f"{what}…")
        self.update_buttons()
        self.window_.runner.submit(fn, *args, key="flatpak-change", on_done=lambda _r: self._done(what, None),
                                   on_error=lambda exc: self._done(what, exc))

    def _done(self, what: str, exc) -> None:
        self.working = ""
        if exc is None:
            self.message.show_message("ok", f"{what}: done.")
        else:
            self.message.show_message("error", f"{what} failed. {self.window_.error_text(exc)}")
        self.page.store.request("flatpak", force=True)
        self.page.store.request("desktop_apps", force=True)
        self.update_buttons()

    def install(self) -> None:
        item = self.found.currentItem()
        state = self.state()
        if item is None or state is None:
            return
        app_id, remote = item.data(USER)
        if self.window_.ask(f"Install {app_id}?", f"{app_id} is installed from the {remote} remote for your account "
                            "only. It runs in Flatpak's sandbox with the permissions its developer requested.",
                            "Install"):
            self._change(f"Installing {app_id}", self.window_.backend.flatpak_install, remote, app_id, state)

    def uninstall(self) -> None:
        item = self.installed.currentItem()
        if item is None:
            return
        app_id = item.data(USER)[0]
        if self.window_.ask(f"Uninstall {app_id}?", f"{app_id} is removed from your account. Its data in "
                            "~/.var/app is kept.", "Uninstall"):
            self._change(f"Removing {app_id}", self.window_.backend.flatpak_uninstall, app_id)

    def update_all(self) -> None:
        if self.window_.ask("Update Flatpak applications?", "Your Flatpak applications and their runtimes are "
                            "updated from their remotes.", "Update"):
            self._change("Updating Flatpak applications", self.window_.backend.flatpak_update)


# --- AppImages ----------------------------------------------------------------------------------------------------

APPIMAGE_WARNING = ("AppImages run without a sandbox, with all the permissions of your user account, and Boswas OS "
                    "cannot verify where they come from. Add only AppImages from developers you trust.")


class AppImageTab(_Tab):
    def __init__(self, page, parent: QWidget | None = None):
        super().__init__(page, parent)
        warning = MessageBanner(closable=False, parent=self)
        warning.show_message("warning", APPIMAGE_WARNING)
        self.layout_.addWidget(warning)
        self.list = QListWidget(self)
        self.list.setAccessibleName("Your AppImages")
        self.list.setMinimumHeight(140)
        self.list.currentItemChanged.connect(lambda *_: self.update_buttons())
        row = QHBoxLayout()
        self.add_button = _button("Add AppImage…", "Copies an AppImage to your Applications folder and adds it to "
                                  "the application menu", self.add, "list-add", self)
        self.remove_button = _button("Remove…", "Deletes the selected AppImage and its menu entry", self.remove,
                                     "edit-delete", self)
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        self.empty = muted_label("", parent=self)
        self.layout_.addWidget(self.empty)
        self.layout_.addWidget(self.list)
        self.layout_.addLayout(row)
        self.working = False

    def refresh(self) -> None:
        result = self.page.store.get("appimages")
        self.list.clear()
        items = result.value if result is not None and result.ok else []
        for app in items:
            item = QListWidgetItem(f"{app.name} — {app.path}")
            item.setData(USER, app.ident)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, app.name)
            self.list.addItem(item)
        self.empty.setText("" if items else ("Reading…" if result is None else
                                             ("You have not added any AppImages." if result.ok else
                                              f"Your AppImages could not be listed: {result.error}")))
        self.empty.setVisible(not items)
        self.list.setVisible(bool(items))
        self.update_buttons()

    def update_buttons(self) -> None:
        _enable(self.add_button, "Adding…" if self.working else "")
        _enable(self.remove_button, "Adding…" if self.working else ("" if self.list.currentItem() else
                                                                    "Select an AppImage first."))

    def add(self) -> None:
        path = self.window_.choose_file("Choose an AppImage", "AppImages (*.AppImage *.appimage);;All files (*)")
        if path:
            self.add_path(path)

    def add_path(self, path: str) -> None:
        try:
            kind = self.window_.backend.detect_file(path)
        except Exception as exc:        # noqa: BLE001 - shown to the user
            self.message.show_message("error", self.window_.error_text(exc))
            return
        if kind.kind == filetypes.APPIMAGE_TYPE1:
            self.message.show_message("error", "This is an AppImage in the old type 1 format, which Boswas OS does not "
                                               "support. Ask the developer for a current AppImage.")
            return
        if kind.kind != filetypes.APPIMAGE:
            self.message.show_message("error", f"This file is not an AppImage ({kind.description}).")
            return
        if not self.window_.ask("Add this AppImage?", f"{kind.path} is copied to your Applications folder and added "
                                f"to the application menu.\n\n{APPIMAGE_WARNING}", "Add AppImage"):
            return
        self.working = True
        self.update_buttons()
        self.window_.runner.submit(self.window_.backend.add_appimage, kind.path, key="appimage",
                                   on_done=lambda app: self._done(f"{app.name} was added to the application menu.",
                                                                  None),
                                   on_error=lambda exc: self._done("The AppImage could not be added.", exc))

    def remove(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        ident = item.data(USER)
        if self.window_.ask("Remove this AppImage?", f"{item.text()}\n\nThe AppImage file and its menu entry are "
                            "deleted. Settings it saved in your home folder are kept.", "Remove"):
            self.window_.runner.submit(self.window_.backend.remove_appimage, ident, key="appimage",
                                       on_done=lambda _r: self._done("The AppImage was removed.", None),
                                       on_error=lambda exc: self._done("The AppImage could not be removed.", exc))

    def _done(self, text: str, exc) -> None:
        self.working = False
        if exc is None:
            self.message.show_message("ok", text)
        else:
            self.message.show_message("error", f"{text} {self.window_.error_text(exc)}")
        self.page.store.request("appimages", force=True)
        self.page.store.request("desktop_apps", force=True)
        self.update_buttons()


# --- Install from file -----------------------------------------------------------------------------------------

LOCAL_DEB_WARNING = ("This Debian package comes from a file, not from a signed repository: its origin was not "
                     "verified. Packages run their installation scripts with administrator rights. Install only "
                     "packages from sources you trust.")


class FileTab(_Tab):
    def __init__(self, page, parent: QWidget | None = None):
        super().__init__(page, parent)
        self.layout_.addWidget(plain_label(
            "Choose a file to install. Boswas OS recognises it by its content and uses the right tool:", wrap=True,
            parent=self))
        for line in ("Debian package (.deb): the package manager, after a warning that its origin is not verified",
                     "Windows program (.exe, .msi): the Compatibility Manager, in its own sandbox",
                     "Android app (.apk): the Android page, after the app is checked",
                     "AppImage: copied to your Applications folder and added to the menu"):
            self.layout_.addWidget(muted_label("•  " + line, parent=self))
        self.choose_button = _button("Choose File…", "Opens a file and installs it with the matching tool",
                                     self.choose, "document-open", self)
        self.layout_.addWidget(self.choose_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.layout_.addStretch(1)

    def update_buttons(self) -> None:
        _enable(self.choose_button, "")

    def choose(self) -> None:
        path = self.window_.choose_file("Choose a file to install", "All files (*)")
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        try:
            kind = self.window_.backend.detect_file(path)
        except Exception as exc:        # noqa: BLE001 - shown to the user
            self.message.show_message("error", self.window_.error_text(exc))
            return
        if kind.kind == filetypes.DEB:
            self.install_deb(kind)
        elif kind.kind in (filetypes.WINDOWS, filetypes.MSI):
            self.window_.runner.submit(self.window_.backend.open_windows_installer, kind.path,
                                       on_done=lambda _r: self.message.show_message(
                                           "info", "The Compatibility Manager opened to check and install the Windows "
                                                   "program."),
                                       on_error=lambda exc: self.message.show_message(
                                           "error", "The Compatibility Manager could not be opened. "
                                                    f"{self.window_.error_text(exc)}"))
        elif kind.kind == filetypes.APK:
            self.window_.show_page("android")
            self.window_.page("android").open_apk(kind.path)
        elif kind.kind in (filetypes.APPIMAGE, filetypes.APPIMAGE_TYPE1):
            self.page.show_tab("appimage")
            self.page.appimage_tab.add_path(kind.path)
        elif kind.kind == filetypes.ZIP:
            self.message.show_message("error", "This is a ZIP archive, not an application. Extract it first and look "
                                               "for installation instructions.")
        else:
            self.message.show_message("error", "Boswas OS does not recognise this file as something it can install "
                                               "(Debian package, Windows program, Android app or AppImage).")

    def install_deb(self, kind: filetypes.FileKind) -> None:
        if not kind.path.endswith(".deb"):
            self.message.show_message("error", "This is a Debian package, but its name does not end in .deb. Rename "
                                               "it to end in .deb, then try again.")
            return
        if not self.window_.ask("Install a package from a file?", f"{kind.path}\n\n{LOCAL_DEB_WARNING}\n\nYou are "
                                "asked for an administrator password.", "Install Anyway"):
            return
        self.window_.jobs.start(jobs.JobSpec(jobs.SYSOPS, ("apt-install-local", kind.path),
                                             "Installing a package from a file", self.page.info.key),
                                lambda _o: self.page.store.request("desktop_apps", force=True))
