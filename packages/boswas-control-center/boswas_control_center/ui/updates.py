"""Boswas Update Center: automatic updates, pending updates, check and install now.

Pending updates are simulated by APT as the desktop user from the package
lists already on the device (an APT simulation, read-only). Checking
for updates (boswas-sysops apt-refresh) and installing them (apt-upgrade,
which never removes packages) are administrator operations: each starts only
after a click and a confirmation, and polkit asks for authorization.
"""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QPushButton, QTreeWidget, QTreeWidgetItem

from .. import commands, jobs
from .. import viewmodel as vm
from .base import Page
from .common import SP, Card, InfoGrid, SummaryCard, icon, muted_label, plain_label
from .jobs import JobPanel

MAX_ROWS = 500


def plan_text(plan, error: str | None, loading: bool) -> str:
    if plan is None:
        if error:
            return f"Pending updates could not be determined: {vm.one_line(error, 300)}"
        return "Looking for pending updates…" if loading else "Pending updates are not known yet."
    count = len(plan.upgrades)
    if not count:
        return ("No updates are pending in the package lists on this device. Choose Check for Updates to download "
                "the latest lists.")
    security = plan.security_count
    text = f"{count} update{'s' if count != 1 else ''} pending"
    if security:
        text += f", {security} of them security update{'s' if security != 1 else ''}"
    new = sum(1 for u in plan.upgrades if u.new_install)
    if new:
        text += f"; {new} new package{'s' if new != 1 else ''} will be installed as dependencies"
    if plan.held_back:
        text += f"; {plan.held_back} kept back (they would remove other packages)"
    return text + "."


class UpdatesPage(Page):
    data_keys = ("updates", "agent_config", "live", "upgradable", "programs")

    def build(self) -> None:
        jobs_ = self.window_.jobs
        self.summary = SummaryCard(self.content)
        self.check_button = QPushButton("Check for Updates", self.summary)
        self.check_button.setIcon(icon("view-refresh", fallback=SP.SP_BrowserReload))
        self.check_button.setAccessibleDescription("Downloads the latest package lists (administrator rights)")
        self.check_button.clicked.connect(self.check)
        self.install_button = QPushButton("Install Updates…", self.summary)
        self.install_button.setIcon(icon("system-software-update", "download", fallback=SP.SP_ArrowDown))
        self.install_button.setAccessibleDescription("Installs the pending updates (administrator rights)")
        self.install_button.clicked.connect(self.install)
        self.summary.actions.addWidget(self.check_button)
        self.summary.actions.addWidget(self.install_button)
        self.body.addWidget(self.summary)
        self.panel = JobPanel(self.info.key, jobs_, self.content)
        self.body.addWidget(self.panel)

        self.pending = Card("Pending updates", self.content)
        self.pending_text = plain_label("", wrap=True, parent=self.pending)
        self.tree = QTreeWidget(self.pending)
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Package", "Installed", "New version", "Type"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.setAccessibleName("Pending updates")
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setMinimumHeight(160)
        self.pending.addWidget(self.pending_text)
        self.pending.addWidget(self.tree)
        self.body.addWidget(self.pending)

        self.details = Card("Details", self.content)
        self.grid = InfoGrid(self.details)
        self.details.addWidget(self.grid)
        self.body.addWidget(self.details)
        self.log_card = Card("Recent automatic updates", self.content)
        self.log = plain_label("", wrap=True, selectable=True, parent=self.log_card)
        self.log_note = muted_label("", parent=self.log_card)
        self.log_card.addWidget(self.log)
        self.log_card.addWidget(self.log_note)
        self.body.addWidget(self.log_card)
        jobs_.changed.connect(self._buttons)

    # --- data ---
    def plan(self):
        return self.store.value("upgradable")

    def render(self) -> None:
        result = self.store.get("updates")
        if result is None:
            self.summary.set_summary(vm.Summary(vm.CHECKING, "Reading the update settings…", ""))
        else:
            settings = vm.device_settings(self.store.value("agent_config"))
            view = vm.updates_view(result.value if result.ok else None, settings.get("UPDATE_POLICY"),
                                   self.store.value("live") is True)
            self.summary.set_summary(view.summary)
            self.grid.set_rows(view.rows)
            self.log.setText("\n".join(view.log_lines))
            self.log.setVisible(bool(view.log_lines))
            self.log_note.setText(view.log_note)
            self.log_note.setVisible(bool(view.log_note))
        plan = self.plan()
        self.pending_text.setText(plan_text(plan, self.store.error("upgradable"), self.store.loading("upgradable")))
        self.tree.clear()
        for upgrade in (plan.upgrades if plan is not None else ())[:MAX_ROWS]:
            item = QTreeWidgetItem([upgrade.name, upgrade.old or "Not installed", upgrade.new,
                                    "Security" if upgrade.security else ("New dependency" if upgrade.new_install
                                                                         else "Update")])
            item.setToolTip(2, upgrade.origins)
            self.tree.addTopLevelItem(item)
        self.tree.setVisible(plan is not None and bool(plan.upgrades))
        self._buttons()

    def _buttons(self) -> None:
        jobs_ = self.window_.jobs
        busy = jobs_.busy()
        available = (self.store.value("programs") or {}).get("admin", True)
        plan = self.plan()
        self.check_button.setEnabled(not busy and available)
        self.install_button.setEnabled(not busy and available and plan is not None and bool(plan.upgrades))
        reason = jobs_.busy_reason() if busy else ("" if available else
                                                   commands.NO_ADMIN)
        self.check_button.setToolTip(reason or "Download the latest package lists")
        if not reason and (plan is None or not plan.upgrades):
            self.install_button.setToolTip("There are no pending updates to install.")
        else:
            self.install_button.setToolTip(reason or "Install the pending updates")

    # --- actions ---
    def check(self) -> None:
        if not self.window_.ask("Check for updates?",
                                "Boswas OS downloads the latest package lists from its signed repositories. Nothing "
                                "is installed. You are asked for an administrator password.", "Check Now"):
            return
        self.window_.jobs.start(jobs.JobSpec(jobs.SYSOPS, ("apt-refresh",), "Checking for updates", self.info.key),
                                self._job_done)

    def install(self) -> None:
        plan = self.plan()
        if plan is None or not plan.upgrades:
            return
        count = len(plan.upgrades)
        text = (f"{count} package{'s are' if count != 1 else ' is'} updated"
                + (f", including {plan.security_count} security update{'s' if plan.security_count != 1 else ''}"
                   if plan.security_count else "") + ". Packages are only upgraded or added, never removed. "
                "You are asked for an administrator password.")
        if self.store.value("live") is True:
            text += " In this live session the updates are lost when you shut down."
        if not self.window_.ask("Install updates?", text, "Install Updates"):
            return
        self.window_.jobs.start(jobs.JobSpec(jobs.SYSOPS, ("apt-upgrade",), "Installing updates", self.info.key),
                                self._job_done)

    def _job_done(self, outcome: jobs.Outcome) -> None:
        self.store.request("upgradable", force=True)
        self.store.request("updates", force=True)
