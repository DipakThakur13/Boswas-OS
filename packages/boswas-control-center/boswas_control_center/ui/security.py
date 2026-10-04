"""Boswas Security Center.

Source of truth: `boswas --json status` (the local posture checks of
boswas-cli) plus the device agent's status and its runtime check. Each item
shows a state in words and as a distinct icon: Secure (PASS), Warning
(WARN), Attention (FAIL), Info and Not available (INFO, UNKNOWN), Error (the
status could not be read). Only checks that exist are shown.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import QPushButton

from .. import viewmodel as vm
from .base import Page
from .common import SP, Card, Separator, StatusLine, SummaryCard, icon, muted_label

KEYS = ("security", "agent", "agent_runtime")


class SecurityPage(Page):
    data_keys = KEYS

    def build(self) -> None:
        self.summary = SummaryCard(self.content)
        self.refresh_button = QPushButton("Refresh", self.summary)
        self.refresh_button.setIcon(icon("view-refresh", fallback=SP.SP_BrowserReload))
        self.refresh_button.setToolTip("Run the checks again (F5)")
        self.refresh_button.setAccessibleName("Refresh security status")
        self.refresh_button.clicked.connect(self.refresh)
        self.summary.actions.addWidget(self.refresh_button)
        self.body.addWidget(self.summary)

        self.protection = Card("Protection", self.content, spacing=0)
        self.lines: dict[str, StatusLine] = {}
        titles = {key: title for key, title, _check in vm.SECURITY_CHECK_ROWS}
        titles.update({"agent": "Device agent", "policy": "Device policy"})
        for index, key in enumerate(vm.SECURITY_ROW_KEYS):
            if index:
                self.protection.addWidget(Separator(self.protection))
            line = StatusLine(titles[key], self.protection)
            self.lines[key] = line
            self.protection.addWidget(line)
        self.body.addWidget(self.protection)

        self.other = Card("More checks", self.content, spacing=0)
        self.other_lines: list[StatusLine] = []
        self.other_separators: list[Separator] = []
        self.body.addWidget(self.other)
        self.other.hide()
        self.basis = muted_label("", parent=self.content)
        self.body.addWidget(self.basis)
        self.add_modules("Security settings")
        self.checked_at = ""
        self.checked_result = None

    def view(self) -> vm.SecurityView:
        status, agent, runtime = (self.store.get(k) for k in KEYS)
        return vm.security_view(
            status.value if status is not None and status.ok else None,
            status.error if status is not None and not status.ok else None,
            agent.value if agent is not None and agent.ok else None,
            agent.error if agent is not None and not agent.ok else None,
            runtime.value if runtime is not None and runtime.ok else None,
            loading=self.store.loading("security"), agent_loading=self.store.loading("agent"))

    def render(self) -> None:
        view = self.view()
        loading = self.store.loading("security")
        self.refresh_button.setEnabled(not loading)
        summary = view.summary
        if loading and summary.state != vm.CHECKING:
            summary = vm.Summary(summary.state, summary.headline, "Checking again…")
        self.summary.set_summary(summary)
        for row in view.rows:
            self.lines[row.key].set_status(row.state, row.detail)
        self._set_other(view.other_rows)
        result = self.store.get("security")
        if not loading and result is not None and result is not self.checked_result:
            self.checked_result = result                  # a new answer of `boswas --json status`
            self.checked_at = datetime.now().strftime("%H:%M")
        basis = view.basis or "A local self-assessment of this device."
        self.basis.setText(f"{basis} Last checked at {self.checked_at}." if self.checked_at else basis)

    def _set_other(self, rows: list[vm.SecurityRow]) -> None:
        while len(self.other_lines) < len(rows):
            if self.other_lines:
                separator = Separator(self.other)
                self.other_separators.append(separator)
                self.other.addWidget(separator)
            line = StatusLine("", self.other)
            self.other_lines.append(line)
            self.other.addWidget(line)
        for index, line in enumerate(self.other_lines):
            visible = index < len(rows)
            line.setVisible(visible)
            if index and index - 1 < len(self.other_separators):
                self.other_separators[index - 1].setVisible(visible)
            if visible:
                line.set_status(rows[index].state, rows[index].detail, rows[index].title)
        self.other.setVisible(bool(rows))

    def line_state(self, key: str) -> tuple[str, str]:
        line = self.lines[key]
        return line.state_label.text(), line.detail.text()
