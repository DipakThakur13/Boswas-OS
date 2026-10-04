"""Unit tests of the Compatibility Manager's presentation rules and backend client (no Qt).

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import ast
import contextlib
import copy
import dataclasses
import io
import os
import socket
import sys
import tempfile
import unittest
from datetime import timezone
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))

import manager_fixtures as fx  # noqa: E402

from boswas_agent.localapi import ApiError, ApiServer, Operation, Param  # noqa: E402
from boswas_manager import __version__, app  # noqa: E402
from boswas_manager import backend as be  # noqa: E402
from boswas_manager import viewmodel as vm  # noqa: E402

VIEWMODEL_SOURCE = (HERE.parents[1] / "boswas_manager" / "viewmodel.py").read_text(encoding="utf-8")
FORBIDDEN_WORDS = ("wine32", "i386", "wow64", "multiarch")
UTC = timezone.utc


def rows():
    return vm.app_rows(fx.APPS, fx.MANIFESTS, UTC)


def row(app_id: str) -> vm.AppRow:
    return next(r for r in rows() if r.id == app_id)


def ids(items) -> set[str]:
    return {r.id for r in items}


class SanitizeTest(unittest.TestCase):
    def test_ansi_c0_c1_and_del_are_removed(self):
        text = vm.sanitize("\x1b[31mred\x1b[0m \x1b]0;title\x07ok \x9b2Jx\x85y \x00z\x7f\x1bc\tq\nr\r")
        self.assertEqual(text, "red ok xy z\tq\nr")
        for ch in text:
            code = ord(ch)
            self.assertFalse(code < 32 and ch not in "\n\t", repr(ch))
            self.assertFalse(0x7F <= code <= 0x9F, repr(ch))

    def test_osc_with_string_terminator_and_dcs(self):
        self.assertEqual(vm.sanitize("a\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\b"), "alinkb")
        self.assertEqual(vm.sanitize("a\x1bPq#0;2;0;0;0\x1b\\b"), "ab")

    def test_bidi_overrides_are_removed(self):
        self.assertEqual(vm.sanitize("Invoice\u202eexe.pdf"), "Invoiceexe.pdf")

    def test_one_line_collapses_and_truncates(self):
        self.assertEqual(vm.one_line("a\n\tb   c"), "a b c")
        self.assertEqual(vm.one_line("x" * 50, 10), "x" * 9 + "\u2026")
        self.assertEqual(vm.one_line(None), "")

    def test_messages_hide_the_home_directory(self):
        self.assertEqual(vm.message_text("see /home/alice/.local/x.log"), "see ~/.local/x.log")

    def test_rows_are_sanitised(self):
        evil = fx.app("com.example.evil", "\x1b[2JEvil\x9b31m\u202e App", "INSTALLED", publisher="P\x07ub")
        r = vm.app_row(evil)
        self.assertEqual(r.name, "Evil App")
        self.assertEqual(r.publisher, "Pub")


class FilterTest(unittest.TestCase):
    def test_each_tab(self):
        all_rows = rows()
        expected = {
            "all": ids(all_rows),
            "installed": {"com.example.notepad", "com.example.paint", "com.example.stopped", "com.example.busy"},
            "running": {"com.example.paint"},
            "updates": {"com.example.notepad"},
            "blocked": {"com.example.blocked"},
            "repair": {"com.example.broken", "com.example.failed", "local.damaged"},
            "unsupported": {"com.example.old"},
        }
        self.assertEqual([key for key, _label in vm.FILTERS], list(expected))
        self.assertEqual([label for _key, label in vm.FILTERS],
                         ["All Applications", "Installed", "Running", "Updates", "Blocked", "Repair Required",
                          "Unsupported"])
        for key, wanted in expected.items():
            self.assertEqual(ids(vm.filter_rows(all_rows, key)), wanted, key)
        self.assertEqual(len(expected["all"]), len(fx.APPS))
        self.assertEqual(vm.filter_counts(all_rows)["repair"], 3)

    def test_search_matches_name_id_publisher_and_version(self):
        all_rows = rows()
        self.assertEqual(ids(vm.filter_rows(all_rows, "all", "paint")), {"com.example.paint"})
        self.assertEqual(ids(vm.filter_rows(all_rows, "all", "COM.EXAMPLE.OLD")), {"com.example.old"})
        self.assertEqual(ids(vm.filter_rows(all_rows, "all", "2.1")), {"com.example.notepad"})
        self.assertIn("com.example.notepad", ids(vm.filter_rows(all_rows, "installed", "example ltd")))
        self.assertEqual(vm.filter_rows(all_rows, "running", "notepad"), [])
        self.assertEqual(len(vm.filter_rows(all_rows, "all", "   ")), len(all_rows))

    def test_unknown_filter_is_an_error(self):
        with self.assertRaises(ValueError):
            vm.filter_rows(rows(), "everything")

    def test_unknown_state_is_shown_as_error(self):
        r = vm.app_row({"id": "com.example.x", "app_state": "EXPLODED"})
        self.assertEqual(r.state, "ERROR")
        self.assertTrue(r.state_reason)


class ActionTest(unittest.TestCase):
    EXPECTED = {
        "INSTALLED": {"launch", "repair", "update", "remove"},
        "STOPPED": {"launch", "repair", "update", "remove"},
        "RUNNING": {"stop"},
        "INSTALLING": set(),
        "ERROR": {"repair", "remove"},
        "REPAIR_REQUIRED": {"repair", "remove"},
        "BLOCKED": {"remove"},
        "UNSUPPORTED": {"remove"},
    }

    def test_actions_per_state(self):
        self.assertEqual(set(self.EXPECTED), set(vm.APP_STATES))
        for state, enabled in self.EXPECTED.items():
            actions = vm.available_actions(vm.app_row(fx.app("com.example.x", "X", state)))
            self.assertEqual({k for k, v in actions.items() if v} - {"logs", "details"}, enabled, state)
            self.assertTrue(actions["logs"] and actions["details"], state)

    def test_nothing_changes_while_busy(self):
        for state in vm.APP_STATES:
            actions = vm.available_actions(vm.app_row(fx.app("com.example.x", "X", state, busy="repair")))
            self.assertEqual({k for k, v in actions.items() if v}, {"logs", "details"}, state)
        self.assertEqual(row("com.example.busy").display_state, "Repairing\u2026")

    def test_damaged_entry_can_only_be_removed(self):
        actions = vm.available_actions(row("local.damaged"))
        self.assertFalse(actions["repair"])
        self.assertTrue(actions["remove"])

    def test_no_selection(self):
        self.assertFalse(any(vm.available_actions(None).values()))

    def test_running_column(self):
        self.assertEqual(row("com.example.paint").running_label, "Running")
        self.assertEqual(row("com.example.notepad").running_label, "Not running")


class InstallFlowTest(unittest.TestCase):
    def test_exact_32bit_texts(self):
        self.assertEqual(vm.UNSUPPORTED_32BIT_MESSAGE, "This application requires 32-bit Windows compatibility, "
                                                       "which is not supported by Boswas OS.")
        self.assertEqual(vm.UNSUPPORTED_32BIT_DETAIL,
                         "32-bit Windows applications are not supported by this version of Boswas OS.")
        self.assertEqual(vm.SUPPORTED_ARCHITECTURE, "x86_64 / 64-bit Windows applications")

    def test_32bit_installer_is_refused_with_the_fixed_texts(self):
        flow = vm.InstallFlow()
        self.assertEqual(flow.select("/home/u/Downloads/setup32.exe"), flow.CHECKING)
        self.assertEqual(flow.inspected(fx.INSPECT_32), flow.REFUSED_32BIT)
        self.assertEqual(flow.refusal_title, vm.UNSUPPORTED_32BIT_MESSAGE)
        self.assertEqual(flow.refusal_detail, vm.UNSUPPORTED_32BIT_DETAIL)
        self.assertEqual(flow.refusal_hint, "")
        with self.assertRaises(vm.FlowError):
            flow.install_params()
        with self.assertRaises(vm.FlowError):
            flow.started("0123456789abcdef")

    def test_32bit_detected_from_the_message_alone(self):
        doc = copy.deepcopy(fx.INSPECT_32)
        doc["architecture"] = {}
        doc["installer"]["machine"] = None
        flow = vm.InstallFlow()
        flow.select("/x/setup.msi")
        self.assertEqual(flow.inspected(doc), flow.REFUSED_32BIT)

    def test_other_architectures_are_not_called_32bit(self):
        doc = copy.deepcopy(fx.INSPECT_32)
        doc["architecture"].update(detected="arm64", message="setup.exe is a arm64 Windows program")
        doc["decision"]["message"] = "setup.exe is a arm64 Windows program; Boswas OS runs x86_64 only."
        flow = vm.InstallFlow()
        flow.select("/x/setup.exe")
        self.assertEqual(flow.inspected(doc), flow.REFUSED)
        self.assertEqual(flow.refusal_title, "This installer is not for 64-bit Windows")
        self.assertNotIn("32-bit", flow.refusal_title + flow.refusal_hint)

    def test_no_workaround_words_anywhere_in_the_viewmodel(self):
        lowered = VIEWMODEL_SOURCE.casefold()
        for word in FORBIDDEN_WORDS:
            self.assertNotIn(word, lowered)
        strings = [node.value for node in ast.walk(ast.parse(VIEWMODEL_SOURCE))
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        flow = vm.InstallFlow()
        flow.select("/x/setup32.exe")
        flow.inspected(fx.INSPECT_32)
        strings += [flow.refusal_title, flow.refusal_detail, flow.refusal_hint]
        strings += list(vm.REFUSAL_TITLES.values()) + list(vm.REFUSAL_HINTS.values())
        for text in strings:
            for word in FORBIDDEN_WORDS:
                self.assertNotIn(word, text.casefold())

    def test_valid_installer_installs_and_finishes(self):
        flow = vm.InstallFlow()
        flow.select("/home/u/viewer-setup.exe")
        self.assertEqual(flow.inspect_params(), {"path": "/home/u/viewer-setup.exe", "app_id": None, "name": None})
        self.assertEqual(flow.inspected(fx.INSPECT_64), flow.CONFIRM)
        self.assertEqual(flow.confirm_title, "Install Example Viewer")
        rows_ = {r.label: r.value for r in flow.decision_rows()}
        self.assertEqual(rows_["Architecture"], "x86_64 (64-bit)")
        self.assertEqual(rows_["Type"], "Windows program (.exe)")
        self.assertEqual(rows_["Catalog status"], "Tested")
        self.assertIn("Boswas compatibility catalog", rows_["Source"])
        self.assertEqual({r.key: r.value for r in flow.sandbox_rows()}["removable"], vm.REMOVABLE_DEVICES)
        self.assertEqual(flow.install_params(), {"path": "/home/u/viewer-setup.exe", "app_id": None, "name": None})
        self.assertEqual(flow.started("0123456789abcdef"), flow.INSTALLING)
        self.assertEqual(flow.job_update(fx.JOB_RUNNING), flow.INSTALLING)
        self.assertEqual(len(flow.progress), 2)
        self.assertEqual(flow.job_update(fx.JOB_DONE), flow.DONE)
        self.assertTrue(flow.can_launch)
        self.assertEqual(flow.application_id, "com.example.viewer")
        self.assertEqual(flow.done_title, "Example Viewer is installed.")

    def test_failed_job(self):
        flow = vm.InstallFlow()
        flow.select("/x/viewer-setup.exe")
        flow.inspected(fx.INSPECT_64)
        flow.started("0123456789abcdef")
        self.assertEqual(flow.job_update(fx.JOB_FAILED), flow.FAILED)
        self.assertEqual(flow.failed_title, "The installation failed")
        self.assertIn("exited with code 2", flow.error_message)
        self.assertNotIn("/home/", flow.error_message)
        self.assertEqual(flow.application_id, "com.example.viewer")

    def test_refusals(self):
        cases = ((fx.INSPECT_BLOCKED, "This installer is blocked"),
                 (fx.INSPECT_UNLISTED_DENIED, "Only applications from the compatibility catalog can be installed"),
                 (fx.INSPECT_INSTALLED, "This application is already installed"))
        for inspection, title in cases:
            flow = vm.InstallFlow()
            flow.select("/x/setup.exe")
            self.assertEqual(flow.inspected(inspection), flow.REFUSED)
            self.assertEqual(flow.refusal_title, title)
            self.assertEqual(flow.refusal_detail, inspection["decision"]["message"])
            self.assertTrue(flow.refusal_hint)

    def test_install_refused_at_start(self):
        flow = vm.InstallFlow()
        flow.select("/x/viewer-setup.exe")
        flow.inspected(fx.INSPECT_64)
        self.assertEqual(flow.start_refused("ALREADY_INSTALLED", "com.example.viewer is already installed"),
                         flow.REFUSED)
        self.assertEqual(flow.refusal_title, "This application is already installed")
        flow.select("/x/setup32.exe")
        flow.inspected(fx.INSPECT_64)
        self.assertEqual(flow.start_refused("ARCHITECTURE", fx.INSPECT_32["decision"]["message"]), flow.REFUSED_32BIT)

    def test_check_and_start_failures(self):
        flow = vm.InstallFlow()
        flow.select("/x/missing.exe")
        self.assertEqual(flow.check_failed("NOT_FOUND", "installer not found: /x/missing.exe"), flow.FAILED)
        self.assertEqual(flow.failed_title, "The installer could not be checked")
        flow.select("/x/viewer-setup.exe")
        flow.inspected(fx.INSPECT_64)
        self.assertEqual(flow.start_failed("UNAVAILABLE", vm.SESSION_UNAVAILABLE), flow.FAILED)

    def test_invalid_transitions(self):
        flow = vm.InstallFlow()
        with self.assertRaises(vm.FlowError):
            flow.inspected(fx.INSPECT_64)
        with self.assertRaises(vm.FlowError):
            flow.select("relative/setup.exe")
        flow.select("/x/setup.exe")
        with self.assertRaises(vm.FlowError):
            flow.select("/x/other.exe")             # still checking
        with self.assertRaises(vm.FlowError):
            flow.job_update(fx.JOB_DONE)

    def test_unlisted_options_are_passed_on(self):
        flow = vm.InstallFlow()
        flow.select("/x/tool.exe", " local.my-tool ", " My Tool ")
        self.assertEqual(flow.inspect_params(), {"path": "/x/tool.exe", "app_id": "local.my-tool", "name": "My Tool"})

    def test_upgrade(self):
        flow = vm.InstallFlow("com.example.notepad", "Example Notepad")
        flow.select("/x/notepad-2.2.exe", "local.ignored", "ignored")
        self.assertEqual(flow.inspect_params()["app_id"], "com.example.notepad")
        self.assertEqual(flow.inspected(fx.INSPECT_INSTALLED), flow.CONFIRM)
        self.assertEqual(flow.confirm_label, "Update")
        self.assertEqual(flow.install_params(), {"app_id": "com.example.notepad", "path": "/x/notepad-2.2.exe"})
        flow.started("0123456789abcdef")
        self.assertEqual(flow.installing_title, "Updating Example Notepad\u2026")
        upgrade_32 = vm.InstallFlow("com.example.notepad", "Example Notepad")
        upgrade_32.select("/x/setup32.exe")
        self.assertEqual(upgrade_32.inspected(fx.INSPECT_32), upgrade_32.REFUSED_32BIT)

    def test_progress_is_sanitised(self):
        flow = vm.InstallFlow()
        flow.select("/x/viewer-setup.exe")
        flow.inspected(fx.INSPECT_64)
        flow.started("0123456789abcdef")
        flow.job_update({**fx.JOB_RUNNING, "progress": ["\x1b[1mbold\x1b[0m\x9b5n line"]})
        self.assertEqual(flow.progress, ["bold line"])


class RemovalPlanTest(unittest.TestCase):
    def test_plan(self):
        plan = vm.removal_plan("Example Notepad", "com.example.notepad")
        self.assertEqual(plan.title, "Remove Example Notepad?")
        self.assertEqual(plan.confirm_label, "Remove")
        self.assertEqual(plan.items, ("The application Example Notepad (com.example.notepad)",
                                      "Its Wine prefix (C: drive) and everything stored inside it",
                                      "Its launcher in the application menu"))
        self.assertEqual(plan.warning, "Files the application saved inside its own C: drive are deleted. "
                                       "Files in folders it was granted (for example Documents) are kept.")

    def test_stop_warning(self):
        title, text = vm.stop_question("Example Paint")
        self.assertEqual(title, "Stop Example Paint?")
        self.assertIn("Unsaved work", text)


class PermissionRowsTest(unittest.TestCase):
    def test_rows(self):
        rows_ = vm.permission_rows(fx.STATUS["sandbox"], "enforce", "boswas-winapp", "boswas-winapp (enforce)")
        self.assertEqual([r.key for r in rows_], ["files", "network", "display", "audio", "gpu", "removable",
                                                  "apparmor"])
        values = {r.key: r.value for r in rows_}
        self.assertEqual(values["files"], "Its own C: drive, and your Documents and Downloads folders")
        self.assertEqual(values["network"], "Blocked")
        self.assertEqual(values["display"], "Allowed")
        self.assertEqual(values["gpu"], "Not allowed")
        self.assertTrue(values["apparmor"].startswith("Enforced"))
        self.assertIn("last run: boswas-winapp (enforce)", values["apparmor"])
        self.assertEqual(vm.PERMISSIONS_NOTICE, "Permissions come from the Boswas compatibility catalog and device "
                                                "policy. They cannot be changed here.")

    def test_removable_devices_are_never_available(self):
        rows_ = vm.permission_rows({"removable": True, "devices": True, "network": True}, "complain")
        removable = next(r for r in rows_ if r.key == "removable")
        self.assertEqual(removable.value, "Never available to Windows applications")
        self.assertFalse(removable.granted)
        self.assertEqual(vm.files_text([]), "Only its own C: drive")

    def test_rows_are_read_only(self):
        rows_ = vm.permission_rows(fx.SANDBOX)
        self.assertIsInstance(rows_[0], vm.PermissionRow)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            rows_[0].value = "Everything"      # type: ignore[misc]

    def test_confinement_without_status(self):
        self.assertEqual(vm.confinement_text(None), "Always applied (the Boswas AppArmor profile)")
        self.assertIn("Complain mode", vm.confinement_text("complain", "boswas-winapp"))


class DetailRowsTest(unittest.TestCase):
    def test_overview(self):
        rows_ = {r.label: r.value for r in vm.detail_rows(fx.STATUS, UTC)}
        self.assertEqual(rows_["Wine prefix"], "~/.local/share/boswas/wine/com.example.notepad")
        self.assertEqual(rows_["Name"], "Example Notepad")
        self.assertEqual(rows_["Executable"], "C:\\Program Files\\Example\\example.exe")
        self.assertEqual(rows_["Architecture"], "x86_64")
        self.assertEqual(rows_["Installed"], "2026-10-01 10:00")
        self.assertEqual(rows_["Last run"], "2026-10-03 08:30 (exited normally)")
        self.assertEqual(rows_["State"], "Installed")
        self.assertEqual(rows_["Catalog status"], "Approved")
        self.assertEqual(rows_["Policy decision"], "Allowed")
        self.assertEqual(rows_["Manifest source"], "Boswas compatibility catalog (Boswas OS)")
        for value in rows_.values():
            self.assertNotIn("/home/", value)
            self.assertNotIn("/var/lib/", value)

    def test_blocked_and_unlisted(self):
        status = copy.deepcopy(fx.STATUS)
        status["application"].update(app_state="BLOCKED", state_reason="blocked by policy", manifest_source="unlisted",
                                     launch=None, launch_candidates=["C:\\a.exe", "C:\\b.exe"])
        status["policy"] = {"allowed": False, "reason": "com.example.notepad is blocked by the device policy"}
        rows_ = {r.label: r.value for r in vm.detail_rows(status, UTC)}
        self.assertEqual(rows_["State"], "Blocked \u2014 blocked by policy")
        self.assertEqual(rows_["Policy decision"], "Not allowed: com.example.notepad is blocked by the device policy")
        self.assertIn("Not in the Boswas compatibility catalog", rows_["Manifest source"])
        self.assertEqual(rows_["Executable"], "Not chosen (candidates: C:\\a.exe, C:\\b.exe)")

    def test_prefix_display_never_uses_untrusted_ids(self):
        self.assertEqual(vm.prefix_display("../../etc"), "~/.local/share/boswas/wine/\u2026")
        self.assertEqual(vm.log_title(fx.LOGS), "launch-20261003.log")


class SystemRowsTest(unittest.TestCase):
    def values(self, system=fx.SYSTEM, agent=fx.AGENT) -> dict[str, str]:
        return {r.key: r.value for r in vm.system_rows(system, agent)}

    def test_order_and_values(self):
        rows_ = vm.system_rows(fx.SYSTEM, fx.AGENT)
        self.assertEqual([(r.key, r.label) for r in rows_], list(vm.SYSTEM_ROW_LABELS))
        values = self.values()
        self.assertEqual(values, {
            "wine": "Healthy (wine-10.0)", "apparmor": "Enforced", "bubblewrap": "Available (0.11.0)",
            "agent": "Running (Ready)", "control_plane": "Connected", "disk": "118.4 GiB free of 237.9 GiB",
            "architecture": "x86_64", "windows": "x86_64 / 64-bit only", "os_version": "1.0~alpha3",
            "policy": "Managed: 2026.10.1"})

    def test_apparmor_modes(self):
        for mode, label in (("enforce", "Enforced"), ("complain", "Complain mode"), ("not-loaded", "Not loaded"),
                            ("unavailable", "Unavailable"), ("unknown", "Unknown"), ("weird", "Unknown")):
            system = copy.deepcopy(fx.SYSTEM)
            system["runtime"]["apparmor"]["mode"] = mode
            self.assertEqual(self.values(system)["apparmor"], label, mode)

    def test_missing_components(self):
        system = copy.deepcopy(fx.SYSTEM)
        system["runtime"]["wine"] = {"available": False, "version": None}
        system["runtime"]["bubblewrap"] = {"available": False, "version": None}
        values = self.values(system)
        self.assertEqual((values["wine"], values["bubblewrap"]), ("Missing", "Missing"))

    def test_connections(self):
        for connection, label in (("CONNECTED", "Connected"), ("OFFLINE", "Offline"), ("STANDALONE", "Standalone"),
                                  ("UNENROLLED", "Not enrolled"), ("REVOKED", "Revoked")):
            self.assertEqual(self.values(agent={**fx.AGENT, "connection": connection})["control_plane"], label)

    def test_agent_not_running(self):
        for agent in (None, {"available": False}):
            values = self.values(agent=agent)
            self.assertEqual(values["agent"], "Not running")
            self.assertEqual(values["control_plane"], "Unknown")
            self.assertEqual(values["policy"], "Local")          # from the session agent's runtime policy
        header = vm.header_status(None)
        self.assertEqual(header.device, "Device agent: not running")
        self.assertEqual(header.control_plane, "Control Plane: unknown")

    def test_session_unavailable(self):
        values = self.values(system=None, agent=None)
        self.assertEqual(values["wine"], "Unknown")
        self.assertEqual(values["disk"], "Unknown")
        self.assertEqual(values["windows"], "x86_64 / 64-bit only")
        self.assertEqual(values["policy"], "Unknown")

    def test_local_policy_and_header(self):
        agent = {**fx.AGENT, "policy": {"managed": False, "version": None}, "state": "DEGRADED"}
        self.assertEqual(self.values(agent=agent)["policy"], "Local")
        header = vm.header_status(agent)
        self.assertEqual((header.device, header.device_level), ("Device: Degraded", "warning"))
        self.assertEqual(vm.header_status(None, checked=False).device, "Device: checking\u2026")


class UpdateDetectionTest(unittest.TestCase):
    def test_versions(self):
        notepad = fx.APPS[0]
        manifest = fx.MANIFESTS[0]
        self.assertEqual(vm.available_update(notepad, manifest), "2.2")
        self.assertIsNone(vm.available_update({**notepad, "version": "2.2"}, manifest))
        self.assertIsNone(vm.available_update({**notepad, "manifest_source": "unlisted"}, manifest))
        self.assertIsNone(vm.available_update(notepad, {**manifest, "status": "blocked"}))
        self.assertIsNone(vm.available_update(notepad, None))
        self.assertIsNone(vm.available_update(fx.APPS[-1], manifest))
        self.assertEqual(row("com.example.notepad").update_version, "2.2")
        self.assertIsNone(row("com.example.paint").update_version)


class DashboardTest(unittest.TestCase):
    def test_summary(self):
        summary = vm.dashboard_summary(rows())
        self.assertEqual(summary, vm.DashboardSummary(installed=9, running=1, attention=5, updates=1))

    def test_activity(self):
        items = vm.activity_items(fx.EVENTS, fx.COMMANDS, tz=UTC)
        self.assertEqual([i.sort_key for i in items], sorted((i.sort_key for i in items), reverse=True))
        self.assertEqual(items[0].text, "Started: com.example.paint started \u2014 requested by your administrator")
        command = next(i for i in items if i.text.startswith("Install com.example.paint"))
        self.assertTrue(command.text.endswith("requested by your administrator"))
        self.assertTrue(command.remote)
        local = next(i for i in items if i.text.startswith("Installed"))
        self.assertFalse(local.remote)
        self.assertEqual(local.when, "2026-10-04 11:00")

    def test_activity_is_sanitised(self):
        items = vm.activity_items([{"type": "launch.failed", "occurred_at": "x", "detail": "\x1b[31mbad\x9b2J"}], [])
        self.assertEqual(items[0].text, "Could not start: bad")


class FormattingTest(unittest.TestCase):
    def test_times(self):
        self.assertEqual(vm.format_time("2026-10-04T12:34:56Z", UTC), "2026-10-04 12:34")
        self.assertEqual(vm.format_time(None), "")
        self.assertEqual(vm.last_launch_text(None), "Never")
        self.assertEqual(vm.last_launch_text({"at": "2026-10-04T12:00:00Z", "stopped": True}, UTC),
                         "2026-10-04 12:00 (stopped)")
        self.assertEqual(vm.last_launch_text({"at": "2026-10-04T12:00:00Z", "exit_code": 3}, UTC),
                         "2026-10-04 12:00 (exit code 3)")

    def test_sizes(self):
        self.assertEqual(vm.format_size(512), "512 bytes")
        self.assertEqual(vm.format_size(5 * 1024 * 1024), "5.0 MiB")
        self.assertEqual(vm.format_size(None), "Unknown")

    def test_display_params(self):
        env = {"DISPLAY": ":0", "XAUTHORITY": "/run/user/1000/xauth_ABC", "WAYLAND_DISPLAY": "wayland-0", "HOME": "/x"}
        self.assertEqual(vm.display_params(env), {"display": ":0", "xauthority": "/run/user/1000/xauth_ABC",
                                                  "wayland_display": "wayland-0"})
        self.assertEqual(vm.display_params({"DISPLAY": "remote:10.0", "XAUTHORITY": "relative",
                                            "WAYLAND_DISPLAY": "/run/user/1000/wayland-0"}), {})
        self.assertEqual(vm.display_params({}), {})

    def test_launch_and_job_messages(self):
        self.assertEqual(vm.launch_message({"already_running": True}, "Paint"), "Paint is already running.")
        self.assertEqual(vm.launch_message({"started": True, "finished": True, "exit_code": 0}, "Paint"),
                         "Paint started and has already exited (exit code 0).")
        self.assertEqual(vm.launch_message({"started": True, "finished": False}, "Paint"), "Paint is starting.")
        self.assertEqual(vm.job_finished_text({"op": "remove", "state": "succeeded"}, "Paint"),
                         ("ok", "Paint was removed."))
        level, text = vm.job_finished_text({"op": "repair", "state": "failed",
                                            "error": {"code": "POLICY", "message": "not allowed"}}, "Paint")
        self.assertEqual((level, text), ("error", "Repair of Paint failed: not allowed"))
        level, _text = vm.job_finished_text({"op": "repair", "state": "succeeded",
                                             "result": {"healthy": False, "error": "program missing"}}, "Paint")
        self.assertEqual(level, "error")

    def test_logs(self):
        text = vm.log_text(fx.LOGS)
        self.assertEqual(text.splitlines(), ["fixme:ntdll:NtQuerySystemInformation", "err:module cleared line",
                                             "titledone", "plain line"])
        self.assertEqual([key for key, _label in vm.LOG_KINDS], ["latest", "launch", "install", "repair"])


class StubClient:
    """Stands in for localapi.ApiClient."""

    instances: list["StubClient"] = []

    def __init__(self, path, timeout=30):
        self.path, self.timeout = path, timeout
        self.calls: list[tuple] = []
        self.result: object = {}
        self.error: Exception | None = None
        StubClient.instances.append(self)

    def call(self, op, timeout=None, **params):
        self.calls.append((op, timeout, params))
        if self.error is not None:
            raise self.error
        return self.result


class BackendTest(unittest.TestCase):
    def setUp(self):
        StubClient.instances = []
        self.backend = be.Backend("/run/user/1000/boswas/session.sock", "/run/agent.sock", client_factory=StubClient)
        self.session, self.agent = StubClient.instances

    def test_socket_paths(self):
        self.assertEqual(be.session_socket_path({"XDG_RUNTIME_DIR": "/run/user/1000"}),
                         "/run/user/1000/boswas/session.sock")
        self.assertEqual(be.AGENT_SOCKET, "/run/boswas-agent/agent.sock")
        self.assertEqual((self.session.path, self.agent.path), ("/run/user/1000/boswas/session.sock",
                                                                "/run/agent.sock"))

    def test_results_and_parameters(self):
        self.session.result = {"applications": [{"id": "a.b"}, "junk"]}
        self.assertEqual(self.backend.list_apps(), [{"id": "a.b"}])
        self.session.result = {"application": {}}
        self.backend.inspect("/x/setup.exe")
        self.assertEqual(self.session.calls[-1], ("apps.inspect", be.INSPECT_TIMEOUT, {"path": "/x/setup.exe"}))
        self.backend.launch("a.b", display=":0", wayland_display="wayland-0")
        self.assertEqual(self.session.calls[-1], ("apps.launch", be.SLOW_TIMEOUT,
                                                  {"id": "a.b", "display": ":0", "wayland_display": "wayland-0"}))
        self.backend.logs("a.b", "install", 99999)
        self.assertEqual(self.session.calls[-1][2], {"id": "a.b", "kind": "install", "lines": be.MAX_LOG_LINES})

    def test_jobs(self):
        self.session.result = {"job_id": "0123456789abcdef"}
        self.assertEqual(self.backend.install("/x/setup.exe", "local.tool", "Tool"), "0123456789abcdef")
        self.assertEqual(self.session.calls[-1][2], {"path": "/x/setup.exe", "id": "local.tool", "name": "Tool"})
        self.session.result = {}
        with self.assertRaises(be.BackendRefused) as caught:
            self.backend.remove("a.b")
        self.assertEqual(caught.exception.code, "BAD_RESPONSE")

    def test_errors(self):
        self.session.error = ApiError("ARCHITECTURE", vm.UNSUPPORTED_32BIT_MESSAGE)
        with self.assertRaises(be.BackendRefused) as caught:
            self.backend.install("/x/setup32.exe")
        self.assertEqual((caught.exception.code, caught.exception.message),
                         ("ARCHITECTURE", vm.UNSUPPORTED_32BIT_MESSAGE))
        self.agent.error = ConnectionError("the service socket /run/agent.sock is not available")
        with self.assertRaises(be.BackendUnavailable) as unavailable:
            self.backend.agent_status()
        self.assertEqual(unavailable.exception.service, be.AGENT)

    def test_only_read_only_agent_operations(self):
        for op in ("enroll", "unenroll", "maintenance.set", "sync.now", "identity.reset", "session.register"):
            with self.assertRaises(ValueError):
                self.backend._agent_call(op)
        with self.assertRaises(ValueError):
            self.backend._session_call("apps.set_permissions")
        self.assertEqual(self.agent.calls, [])

    @unittest.skipUnless(hasattr(socket, "SO_PEERCRED") and hasattr(socket, "AF_UNIX"), "needs Linux Unix sockets")
    def test_real_socket_round_trip(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "session.sock"

        def missing(_req):
            raise ApiError("NOT_FOUND", "com.example.x is not installed")

        server = ApiServer(path, {
            "apps.list": Operation("apps.list", lambda _r: {"applications": copy.deepcopy(fx.APPS[:2])}, "owner"),
            "apps.status": Operation("apps.status", missing, "owner", {"id": Param(str, required=True)}),
        }, mode=0o600)
        server.start()
        self.addCleanup(server.stop)
        backend = be.Backend(str(path), os.path.join(directory.name, "no-agent.sock"), timeout=5)
        self.assertEqual([a["id"] for a in backend.list_apps()], ["com.example.notepad", "com.example.paint"])
        with self.assertRaises(be.BackendRefused) as caught:
            backend.app_status("com.example.x")
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        with self.assertRaises(be.BackendRefused) as unknown:
            backend.catalog()
        self.assertEqual(unknown.exception.code, "UNKNOWN_OPERATION")
        with self.assertRaises(be.BackendUnavailable):
            backend.agent_status()


class AppEntryTest(unittest.TestCase):
    def test_version_needs_no_display(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            app.main(["boswas-compat-manager", "--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"boswas-compat-manager {__version__}")
        self.assertEqual(__version__, "1.0~alpha3")


if __name__ == "__main__":
    unittest.main()
