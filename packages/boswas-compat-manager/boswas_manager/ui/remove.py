"""Confirmation before removing an application: what is deleted and what is kept."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QVBoxLayout, QWidget

from .. import viewmodel as vm
from .common import SP, heading, icon, icon_label, plain_label


class RemoveDialog(QDialog):
    def __init__(self, plan: vm.RemovalPlan, parent: QWidget | None = None):
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("Remove Application")
        self.setModal(True)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(icon_label(icon("dialog-warning", fallback=SP.SP_MessageBoxWarning), 48), 0,
                      Qt.AlignmentFlag.AlignTop)
        body = QVBoxLayout()
        body.addWidget(heading(plan.title, 1.2))
        body.addWidget(plain_label(plan.intro, wrap=True))
        self.items = plain_label("\n".join(f"• {item}" for item in plan.items), wrap=True)
        body.addWidget(self.items)
        self.warning = plain_label(plan.warning, wrap=True)
        body.addWidget(self.warning)
        top.addLayout(body, 1)
        layout.addLayout(top)

        self.buttons = QDialogButtonBox(self)
        self.cancel_button = self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.remove_button = self.buttons.addButton(plan.confirm_label, QDialogButtonBox.ButtonRole.DestructiveRole)
        self.remove_button.setIcon(icon("edit-delete", "list-remove", fallback=SP.SP_TrashIcon))
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        self.remove_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.buttons)

    def text(self) -> str:
        """Everything the dialog says (for tests and accessibility checks)."""
        return "\n".join((self.plan.title, self.plan.intro, self.items.text(), self.warning.text()))
