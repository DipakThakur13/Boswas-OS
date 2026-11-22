"""Looking after the device: its name, the troubleshooters, and advanced start-up.

Page "about":
  Device name         the static host name, and Rename: a dialog checks the
                      name as it is typed (1-63 letters, digits and hyphens)
                      and `hostnamectl --static set-hostname NAME` sets it;
                      systemd-hostnamed asks polkit, the shell's agent shows
                      the question
Page "troubleshoot":
  Troubleshooters     Internet connection, Sound, Bluetooth and Display: each
                      runs when its button is pressed, reads only, and shows
                      its steps as Fine, Problem or Note with what helps
                      (boswas_control_center.troubleshoot). The internet check
                      says before it runs that it contacts deb.debian.org
Page "recovery":
  Advanced start-up   "Restart now" into the firmware settings when logind
                      says the firmware supports it (CanRebootToFirmwareSetup),
                      and into the boot loader's menu when the boot loader
                      supports it (CanRebootToBootLoaderMenu); each after a
                      question, with `systemctl reboot --firmware-setup` or
                      `systemctl reboot --boot-loader-menu=0`. What the device
                      does not support is not offered.

Every blocking call runs on a worker thread; nothing is changed when a page
opens.
"""

from __future__ import annotations

import json

import boswas_fluent as fluent
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QWidget
from shiboken6 import isValid

from ... import commands, device, troubleshoot
from ... import servicebridge as sb
from ...errors import user_message
from .. import bridgeclient
from . import context
from .system import CHANGE_TIMEOUT, READ_TIMEOUT, ToolSection, tool_failure
from .widgets import Busy, Section, ask, go, step_card

REBOOT_TIMEOUT = 60.0
LOGIND_TIMEOUT = 15.0


# --- device name ---------------------------------------------------------------------------------------------

class RenameDialog(fluent.ContentDialog):
    """The new name, checked while it is typed."""

    def __init__(self, parent: QWidget, current: str):
        super().__init__(parent, "Rename this device",
                         "Other devices on the network and your organisation see this name. Use letters, digits "
                         "and hyphens. You may be asked for an administrator password.",
                         primary="Rename", close="Cancel")
        self.current = current
        self.edit = QLineEdit(self)
        self.edit.setMaxLength(device.MAX_NAME)
        self.edit.setText(current)
        self.edit.selectAll()
        self.edit.setAccessibleName("New device name")
        self.problem = fluent.Label("", "caption", self, tone="danger", wrap=True)
        self.content_layout.addWidget(self.edit)
        self.content_layout.addWidget(self.problem)
        self.edit.textChanged.connect(self._check)
        self._check(current)

    def name(self) -> str:
        return self.edit.text().strip()

    def _check(self, _text: str = "") -> None:
        name = self.name()
        problem = device.name_problem(name)
        if problem and name:
            suggestion = device.suggest_name(name)
            if suggestion and suggestion != name:
                problem += f" For example: {suggestion}"
        self.problem.setText(problem if name else "")
        self.problem.setVisible(bool(problem and name))
        self.primary_button.setEnabled(not problem and name != self.current)

    def open(self) -> None:
        super().open()
        self.edit.setFocus(Qt.FocusReason.PopupFocusReason)


class DeviceNameSection(ToolSection):
    page = "about"
    title = "Device name"
    order = 5
    keywords = ("rename", "device name", "computer name", "hostname", "pc name", "network name")

    def __init__(self, parent: QWidget | None = None, *, runner=None, root: str | None = None):
        super().__init__(parent)
        self.runner = runner or context.runner()
        self.root = context.root if root is None else root
        self.info: device.DeviceInfo | None = None
        self.rename_button = fluent.Button("Rename…", self)
        self.rename_button.setAccessibleName("Rename this device")
        self.rename_button.clicked.connect(self.rename_device)
        self.card = self.add(fluent.SettingsCard("Device name", "Reading…", fluent.icon("computer"),
                                                 self.rename_button, self))
        self.card.title_label.set_style("body-strong")
        self.dialog: RenameDialog | None = None
        self.rename_wanted = False             # Rename was asked for (from Home) before the name was read
        self.rename_button.setEnabled(False)

    def read(self) -> device.DeviceInfo:
        from ... import probes
        return device.read_device(probes.Root(self.root))

    def show_state(self, info: device.DeviceInfo) -> None:
        self.info = info
        self.card.set_title(info.name or "Unnamed device")
        text = info.model_text
        if info.pretty and info.pretty != info.name:
            text = f"{info.pretty} · {text}"
        if info.running_name and info.running_name != info.name:
            text += f" · the running system uses the name {info.running_name}"
        self.card.set_description(text)
        self.rename_button.setEnabled(not self.busy and self.runner.available(commands.EXECUTABLES["hostnamectl"]))
        if not self.runner.available(commands.EXECUTABLES["hostnamectl"]):
            self.rename_button.setToolTip("hostnamectl (package systemd) is not installed.")
        elif self.rename_wanted:
            self.rename_wanted = False
            self.rename_device()

    def set_enabled(self, enabled: bool) -> None:
        self.rename_button.setEnabled(enabled and self.info is not None)

    def deactivate(self) -> None:
        super().deactivate()
        if self.dialog is not None and self.dialog.is_open():
            self.dialog.reject()

    def rename_device(self) -> None:
        if self.info is None:
            self.rename_wanted = True          # the dialog opens once the current name is known
            return
        if self.busy or (self.dialog is not None and self.dialog.is_open()):
            return
        dialog = RenameDialog(self, self.info.name)
        self.dialog = dialog

        def finished(result) -> None:
            name = dialog.name()
            dialog.deleteLater()
            self.dialog = None
            if result == fluent.ContentDialog.Result.PRIMARY:
                self.set_name(name)

        dialog.finished.connect(finished)
        dialog.open()

    def set_name(self, name: str) -> None:
        if not device.valid_name(name):
            self.show_error(device.name_problem(name) or "This is not a device name.")
            return

        def apply() -> None:
            result = self.runner.run(commands.hostnamectl_argv(name), CHANGE_TIMEOUT)
            if result.code != 0:
                raise tool_failure("Renaming the device", result)

        self.change(apply, f"This device is now called {name}. Programs that are running may show the old name "
                           "until they are started again.")


# --- troubleshooters -----------------------------------------------------------------------------------------

class Troubleshooter(fluent.SettingsExpander):
    """One check: Run, then its steps under it."""

    def __init__(self, title: str, description: str, icon: str, parent: QWidget | None = None):
        self.run_button = fluent.Button("Run")
        super().__init__(title, description, fluent.icon(icon), self.run_button, parent)
        self.run_button.setAccessibleName(f"Run the {title.lower()} troubleshooter")
        self.busy = Busy("Checking…")
        self.header.add_control(self.busy)
        self.busy.set_busy(False)
        self.rows: list[QWidget] = [self.add_row("Not run yet", "Press Run: the check takes a few seconds and "
                                                 "changes nothing.")]
        self.report: troubleshoot.Report | None = None

    def set_running(self, running: bool) -> None:
        self.busy.set_busy(running)
        self.run_button.setEnabled(not running)

    def show_report(self, report: troubleshoot.Report, on_open) -> None:
        self.report = report
        for row in self.rows:
            row.hide()
            row.deleteLater()
        self.rows = []
        summary = step_card("ok" if report.ok else "problem", report.headline,
                            "" if not report.fix else f"What helps: {report.fix.fix}", self)
        if report.fix is not None and report.fix.page:
            target = report.fix.page
            open_button = fluent.Button(f"Open {_place_title(target)}", summary)
            open_button.clicked.connect(lambda _c=False, key=target: on_open(key))
            summary.add_control(open_button)
        self.add_card(summary)
        self.rows.append(summary)
        for step in report.steps:
            row = step_card(step.state, step.title, step.detail + (f" — {step.fix}" if step.fix and step.state !=
                                                                    troubleshoot.PROBLEM else ""), self)
            self.add_card(row)
            self.rows.append(row)
        self.set_expanded(True)


def _place_title(key: str) -> str:
    from ... import catalog
    info = catalog.BY_KEY.get(key)
    return info.label if info is not None else key


class TroubleshootSection(Section):
    page = "troubleshoot"
    title = "Troubleshooters"
    order = 10
    keywords = ("troubleshoot", "internet", "no internet", "sound", "no sound", "bluetooth", "display", "monitor",
                "fix", "diagnose", "problem")

    def __init__(self, parent: QWidget | None = None, *, runner=None, details_reader=None, bridge=None,
                 compositor=None):
        super().__init__(parent)
        self.runner = runner or context.runner()
        self.details_reader = details_reader
        self._bridge = bridge
        self.compositor = compositor
        self.checks: dict[str, Troubleshooter] = {}
        for key, title, description, icon in (
                ("internet", "Internet connection", troubleshoot.CONTACTED, "network-wireless-symbolic"),
                ("sound", "Sound", "Speakers or headphones that stay silent", "audio-volume-high-symbolic"),
                ("bluetooth", "Bluetooth", "A Bluetooth device that does not connect", "bluetooth-active-symbolic"),
                ("display", "Display", "A monitor that stays dark or is not found", "video-display-symbolic")):
            check = self.add(Troubleshooter(title, description, icon, self))
            check.run_button.clicked.connect(lambda _c=False, k=key: self.run(k))
            self.checks[key] = check
        self._generation = 0

    @property
    def bridge(self):
        if self._bridge is None:
            self._bridge = bridgeclient.for_widget(self)
        return self._bridge

    def activate(self) -> None:
        super().activate()
        self.bridge.start()

    def deactivate(self) -> None:
        super().deactivate()
        self._generation += 1
        for check in self.checks.values():
            check.set_running(False)

    def run(self, key: str) -> None:
        check = self.checks[key]
        check.set_running(True)
        generation = self._generation
        if key in ("sound", "bluetooth"):
            self.bridge.refresh(sb.AUDIO if key == "sound" else sb.BLUETOOTH)

        def done(report) -> None:
            if isValid(self) and generation == self._generation:
                check.set_running(False)
                check.show_report(report, lambda target: go(self, target))

        def failed(exc) -> None:
            if isValid(self) and generation == self._generation:
                check.set_running(False)
                self.show_error(f"The {check.header.title().lower()} check could not run. {user_message(exc)}")

        fluent.run_in_thread(lambda: self.check(key), on_done=done, on_error=failed)

    # --- the checks, on a worker thread ---
    def check(self, key: str) -> troubleshoot.Report:
        if key == "internet":
            from ... import netinfo
            details = (self.details_reader or (lambda: netinfo.read(self.runner, commands.argv, context.root)))()
            return troubleshoot.internet(details, context.resolve, context.fetch)
        if key == "sound":
            return troubleshoot.sound(self.bridge.status(), self.bridge.state(sb.AUDIO))
        if key == "bluetooth":
            service = None
            try:
                result = self.runner.run(commands.argv("systemctl", "is-active", "bluetooth.service"), READ_TIMEOUT)
                service = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "inactive"
            except Exception:                     # noqa: BLE001 - not asked: the bridge's answer still counts
                service = None
            return troubleshoot.bluetooth(service, self.bridge.status(), self.bridge.state(sb.BLUETOOTH))
        settings = self.compositor or context.compositor()
        try:
            outputs = settings.control.outputs()
        except Exception as exc:                  # noqa: BLE001 - no desktop: said in the report
            return troubleshoot.display(None, str(exc))
        return troubleshoot.display(outputs)


# --- advanced start-up ---------------------------------------------------------------------------------------

def logind_answer(text: str) -> str:
    """"yes", "no", "na" or "challenge" from busctl's JSON reply to a Can* question; "" when it is not one."""
    try:
        doc = json.loads(text)
    except ValueError:
        return ""
    data = doc.get("data") if isinstance(doc, dict) else None
    value = data[0] if isinstance(data, list) and data else None
    return value if value in ("yes", "no", "na", "challenge") else ""


class RecoverySection(ToolSection):
    page = "recovery"
    title = "Advanced start-up"
    order = 10
    keywords = ("uefi", "bios", "firmware settings", "boot menu", "advanced startup", "restart", "recovery")

    def __init__(self, parent: QWidget | None = None, *, runner=None):
        super().__init__(parent)
        self.runner = runner or context.runner()
        self.firmware_button = fluent.Button("Restart now", self)
        self.firmware_button.setAccessibleName("Restart now into the firmware settings")
        self.firmware_button.clicked.connect(lambda: self.restart("firmware"))
        self.firmware = self.add(fluent.SettingsCard(
            "Firmware settings (UEFI)", "Restart into the settings of this device's firmware: boot order, Secure "
            "Boot, virtualisation", fluent.icon("system-reboot-symbolic"), self.firmware_button, self))
        self.menu_button = fluent.Button("Restart now", self)
        self.menu_button.setAccessibleName("Restart now to the boot menu")
        self.menu_button.clicked.connect(lambda: self.restart("menu"))
        self.menu = self.add(fluent.SettingsCard(
            "Boot menu", "Restart and stop at the boot loader's menu, to start another system or an older kernel",
            fluent.icon("system-reboot-symbolic"), self.menu_button, self))
        self.none = self.add(fluent.SettingsCard(
            "No advanced start-up on this device", "The firmware of this device cannot be asked to open its settings "
            "or the boot menu from here. Press the key the device shows when it starts (often F2, F10, F12 or Del).",
            fluent.icon("dialog-information-symbolic"), None, self))
        self.reset_note = self.add(fluent.Label(
            "Boswas OS has no “reset this device”: to start again from scratch, reinstall Boswas OS from its boot "
            "medium.", "caption", self, tone="secondary", wrap=True))
        self.reset_note.setContentsMargins(4, 4, 0, 0)
        for widget in (self.firmware, self.menu, self.none):
            widget.hide()
        self.answers: dict[str, str] = {}

    def activate(self) -> None:
        super().activate()
        self._poll.stop()                      # what the firmware supports does not change while the page is open

    def read(self) -> dict[str, str]:
        answers = {}
        for key, question in (("firmware", "CanRebootToFirmwareSetup"), ("menu", "CanRebootToBootLoaderMenu")):
            try:
                result = self.runner.run(commands.logind_argv(question), LOGIND_TIMEOUT)
                answers[key] = logind_answer(result.stdout) if result.code == 0 else ""
            except Exception:                     # noqa: BLE001 - logind not there: not supported
                answers[key] = ""
        return answers

    def show_state(self, answers: dict[str, str]) -> None:
        self.answers = answers
        firmware = answers.get("firmware") in ("yes", "challenge")
        menu = answers.get("menu") in ("yes", "challenge")
        self.firmware.setVisible(firmware)
        self.menu.setVisible(menu)
        self.none.setVisible(not firmware and not menu)
        self.set_enabled(True)

    def load_failed(self, exc: Exception) -> None:
        self.show_state({})

    def set_enabled(self, enabled: bool) -> None:
        self.firmware_button.setEnabled(enabled)
        self.menu_button.setEnabled(enabled)

    def restart(self, kind: str) -> None:
        if self.busy or self.answers.get(kind) not in ("yes", "challenge"):
            return
        where = "its firmware settings" if kind == "firmware" else "the boot menu"
        if not ask(self, "Restart now?", f"This device restarts now and opens {where}. Save your work first: open "
                   "applications are closed. You may be asked for an administrator password.", "Restart Now"):
            return
        words = ("reboot", "--firmware-setup") if kind == "firmware" else ("reboot", "--boot-loader-menu=0")

        def apply() -> None:
            result = self.runner.run(commands.argv("systemctl", *words), REBOOT_TIMEOUT)
            if result.code != 0:
                raise tool_failure("Restarting", result)

        self.change(apply, "Restarting…")


SECTIONS = (DeviceNameSection, TroubleshootSection, RecoverySection)
