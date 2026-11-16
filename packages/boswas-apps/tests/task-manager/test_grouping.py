"""Rows of the Processes page, sessions, and the plain-data side of systemd and logind."""

from __future__ import annotations

import unittest

from boswas_apps.task_manager import grouping, services
from boswas_apps.task_manager.desktopfile import AppIndex, Application
from boswas_apps.task_manager.sampler import Proc

ME = 1000
SESSION = "/user.slice/user-1000.slice/session-1.scope"
USER_SERVICE = "/user.slice/user-1000.slice/user@1000.service/app.slice/x.service"


def proc(pid, name, ppid=1, *, uid=ME, cpu=0.0, mem=1000, disk=0.0, args=None, exe="", kthread=False, state="S",
         cgroup=SESSION, start=100.0, tty=0):
    if kthread:
        args, exe, cgroup, uid, mem, disk = (), None, "/", 0, 0, None
    return Proc(pid, ppid, name, state, uid, {0: "root", ME: "alice", 1001: "bob"}.get(uid, str(uid)), cpu, 0, mem,
                mem * 2, None, 1, 0, start, int(start), tuple(args if args is not None else (name,)),
                (f"/usr/bin/{name}" if exe == "" else exe), disk, kthread, pid, tty, cgroup)


def table(*procs):
    return {p.pid: p for p in procs}


APPS = AppIndex([
    Application("com.boswas.Terminal.desktop", "Terminal", "utilities-terminal", "boswas-terminal", "", "/t"),
    Application("firefox-esr.desktop", "Firefox", "firefox-esr", "firefox-esr", "", "/f"),
    Application("com.boswas.Files.desktop", "Files", "boswas-files", "boswas-files", "com.boswas.Files", "/b"),
])


def rows(procs, uid=ME, index=APPS):
    groups = grouping.Grouper(index, uid).groups(procs)
    return {group.name: group for group in groups}


class GroupingTest(unittest.TestCase):
    def test_three_kinds(self):
        procs = table(
            proc(1, "systemd", 0, uid=0, cgroup="/init.scope"),
            proc(2, "kthreadd", 0, kthread=True), proc(3, "kworker/0:0", 2, kthread=True, cpu=1.5),
            proc(300, "sshd", 1, uid=0, cgroup="/system.slice/ssh.service"),
            proc(400, "boswas-compositor", 1), proc(410, "boswas-shell", 400),
            proc(500, "firefox-esr", 410, cpu=2.0, mem=5000, disk=10.0),
            proc(501, "Web Content", 500, exe="/usr/lib/firefox-esr/firefox-esr", cpu=1.0, mem=3000, disk=None),
            proc(600, "pipewire", 1, cgroup=USER_SERVICE),
            proc(700, "nobody-d", 1, uid=65534, cgroup="/system.slice/x.service"),
            proc(800, "bobs", 1, uid=1001, cgroup="/user.slice/user-1001.slice/session-4.scope"),
        )
        found = rows(procs)
        kinds = {name: group.kind for name, group in found.items()}
        self.assertEqual(kinds, {
            "systemd": "system", "Kernel threads": "system", "sshd": "system", "nobody-d": "system",
            "boswas-compositor": "background", "boswas-shell": "background", "pipewire": "background",
            "bobs": "background", "Firefox": "app"})
        firefox = found["Firefox"]
        self.assertEqual([p.pid for p in firefox.members], [500, 501])
        self.assertEqual((firefox.cpu, firefox.mem, firefox.disk, firefox.icon), (3.0, 8000, 10.0, "firefox-esr"))
        self.assertEqual(firefox.title, "Firefox (2)")
        self.assertEqual(found["Kernel threads"].title, "Kernel threads (2)")
        self.assertEqual(found["Kernel threads"].cpu, 1.5)
        self.assertIsNone(found["Kernel threads"].disk)

    def test_an_application_owns_everything_it_starts(self):
        procs = table(
            proc(10, "boswas-shell", 1),
            proc(20, "boswas-terminal", 10, exe="/usr/bin/python3.13",
                 args=("/usr/bin/python3", "-IB", "/usr/bin/boswas-terminal")),
            proc(21, "bash", 20), proc(22, "vim", 21), proc(23, "make", 21), proc(24, "cc1", 23),
        )
        found = rows(procs)
        self.assertEqual(sorted(found), ["Terminal", "boswas-shell"])
        self.assertEqual([p.pid for p in found["Terminal"].members], [20, 21, 22, 23, 24])

    def test_an_application_started_from_another_gets_its_own_row(self):
        procs = table(
            proc(20, "boswas-terminal", 1), proc(21, "bash", 20),
            proc(30, "firefox-esr", 21), proc(31, "firefox-esr", 30), proc(32, "boswas-terminal", 30),
        )
        groups = grouping.Grouper(APPS, ME).groups(procs)
        self.assertEqual({g.root.pid: (g.name, [p.pid for p in g.members]) for g in groups},
                         {20: ("Terminal", [20, 21]), 30: ("Firefox", [30, 31]),
                          32: ("Terminal", [32])})                 # a terminal below Firefox is its own row

    def test_workers_of_the_same_program_share_a_row_but_other_children_do_not(self):
        procs = table(
            proc(100, "nginx", 1, uid=0, cgroup="/system.slice/nginx.service"),
            proc(101, "nginx", 100, uid=0, cgroup="/system.slice/nginx.service"),
            proc(102, "nginx", 100, uid=33, cgroup="/system.slice/nginx.service"),     # another user: its own row
            proc(200, "sshd", 1, uid=0), proc(201, "sshd", 200, uid=0), proc(202, "bash", 201, uid=0),
            proc(300, "python3", 1, args=("python3", "/opt/a.py"), exe="/usr/bin/python3"),
            proc(301, "python3", 300, args=("python3", "/opt/b.py"), exe="/usr/bin/python3"),
        )
        groups = grouping.Grouper(AppIndex([]), ME).groups(procs)
        self.assertEqual(sorted([p.pid for p in g.members] for g in groups),
                         [[100, 101], [102], [200, 201], [202], [300], [301]])

    def test_what_makes_an_application(self):
        system_slice = proc(10, "firefox-esr", 1, cgroup="/system.slice/odd.service")
        other_user = proc(11, "firefox-esr", 1, uid=1001)
        no_cgroup = proc(12, "firefox-esr", 1, cgroup="")
        by_class = proc(13, "files-bin", 1, exe="/opt/files/com.boswas.files")
        kinds = {g.root.pid: g.kind for g in grouping.Grouper(APPS, ME).groups(table(system_slice, other_user, no_cgroup, by_class))}
        self.assertEqual(kinds, {10: "background", 11: "background", 12: "app", 13: "app"})

    def test_task_manager_run_by_root_sees_its_own_processes_as_user_rows(self):
        procs = table(proc(10, "boswas-files", 1, uid=0, cgroup=""), proc(11, "cron", 1, uid=0, cgroup=""),
                      proc(12, "daemon", 1, uid=5, cgroup=""))
        kinds = {g.root.pid: g.kind for g in grouping.Grouper(APPS, 0).groups(procs)}
        self.assertEqual(kinds, {10: "app", 11: "background", 12: "system"})

    def test_odd_trees(self):
        procs = table(proc(10, "a", 11), proc(11, "a", 10),                # a cycle
                      proc(20, "orphan", 9999),                           # parent not in the table
                      proc(30, "self", 30),
                      proc(40, "kchild", 2), proc(2, "kthreadd", 0, kthread=True))
        groups = grouping.Grouper(AppIndex([]), ME).groups(procs)
        self.assertEqual(sum(len(g.members) for g in groups), 6)
        self.assertEqual(grouping.Grouper(AppIndex([]), ME).groups({}), [])

    def test_kernel_threads_without_kthreadd_in_the_table(self):
        procs = table(proc(7, "kworker/1:1", 2, kthread=True), proc(8, "ksoftirqd/0", 2, kthread=True))
        groups = grouping.Grouper(AppIndex([]), ME).groups(procs)
        self.assertEqual(sorted(g.name for g in groups), ["ksoftirqd/0", "kworker/1:1"])
        self.assertTrue(all(g.kind == "system" for g in groups))

    def test_status(self):
        self.assertEqual([grouping.status_text(s) for s in "RSDTtZI"], ["", "", "", "Suspended", "Being traced", "Zombie", ""])
        group = grouping.Grouper(AppIndex([]), ME).groups(table(proc(5, "stopped", 1, state="T")))[0]
        self.assertEqual((group.status, group.title, group.key), ("Suspended", "stopped", "5.100"))

    def test_remembered_matches_follow_an_exec(self):
        grouper = grouping.Grouper(APPS, ME)
        first = table(proc(10, "sh", 1, args=("sh", "-c", "exec firefox-esr"), exe="/bin/sh"))
        self.assertEqual(grouper.groups(first)[0].kind, "background")
        second = table(proc(10, "firefox-esr", 1))
        self.assertEqual(grouper.groups(second)[0].kind, "app")
        grouper.set_index(AppIndex([]))
        self.assertEqual(grouper.groups(second)[0].kind, "background")

    def test_unchanged_rows_are_the_same_objects(self):
        grouper = grouping.Grouper(APPS, ME)
        procs = table(proc(20, "boswas-terminal", 1), proc(21, "bash", 20), proc(30, "pipewire", 1))
        first = {g.root.pid: g for g in grouper.groups(procs)}
        again = dict(procs)
        again[21] = procs[21]._replace(cpu=5.0)                   # one process of the terminal did something
        second = {g.root.pid: g for g in grouper.groups(again)}
        self.assertIs(second[30], first[30])
        self.assertIsNot(second[20], first[20])
        self.assertEqual(second[20].cpu, 5.0)
        third = {g.root.pid: g for g in grouper.groups(dict(again))}
        self.assertIs(third[20], second[20])
        again[22] = proc(22, "vim", 21)
        self.assertEqual(len({g.root.pid: g for g in grouper.groups(again)}[20].members), 3)

    def test_many_processes(self):
        procs = table(*[proc(pid, f"p{pid % 50}", 1 if pid % 7 == 0 else pid - 1) for pid in range(10, 3010)])
        groups = grouping.Grouper(APPS, ME).groups(procs)
        self.assertEqual(sum(len(g.members) for g in groups), 3000)


class SessionTest(unittest.TestCase):
    def sessions(self):
        return [services.Session("1", ME, "alice", "seat0", "tty2", "active", 1000.0, "wayland", "user", 400),
                services.Session("7", ME, "alice", "", "pts/3", "online", 2000.0, "tty", "user", 900),
                services.Session("4", 1001, "bob", "", "pts/4", "online", 3000.0, "tty", "user", 950)]

    def test_assignment(self):
        procs = table(
            proc(400, "compositor", 1), proc(600, "pipewire", 1, cgroup=USER_SERVICE),
            proc(900, "bash", 1, cgroup="/user.slice/user-1000.slice/session-7.scope"),
            proc(950, "bash", 1, uid=1001, cgroup="/user.slice/user-1001.slice/session-4.scope"),
            proc(960, "stale", 1, uid=1001, cgroup="/user.slice/user-1001.slice/session-99.scope"),
            proc(1, "systemd", 0, uid=0, cgroup="/init.scope"), proc(2, "kthreadd", 0, kthread=True),
            proc(970, "carol", 1, uid=1002, cgroup=""),
        )
        assigned = grouping.assign_sessions(procs, self.sessions())
        self.assertEqual({key: [p.pid for p in value] for key, value in assigned.items()},
                         {"1": [400, 600], "7": [900], "4": [950, 960]})

    def test_session_of_cgroup(self):
        self.assertEqual(grouping.session_of_cgroup("/user.slice/user-1000.slice/session-c2.scope"), "c2")
        self.assertEqual(grouping.session_of_cgroup("/user.slice/user-1000.slice/session-3.scope/sub"), "3")
        self.assertIsNone(grouping.session_of_cgroup("/system.slice/session-3.scopes"))
        self.assertIsNone(grouping.session_of_cgroup(""))

    def test_fallback_session_from_proc_alone(self):
        procs = table(proc(10, "bash", 1, start=500.0), proc(20, "boswas-task-manager", 10, start=900.0, tty=(136 << 8) | 5),
                      proc(30, "other", 1, uid=1001, start=1.0))
        session = grouping.fallback_session(procs, ME, "alice", 20)
        self.assertEqual((session.user, session.tty, session.state, session.since, session.id), ("alice", "pts/5", "active", 500.0, ""))
        empty = grouping.fallback_session({}, ME, "alice", 20)
        self.assertEqual((empty.tty, empty.since), ("", 0.0))


UNIT_ROW = ("cups.service", "CUPS Scheduler", "loaded", "active", "running", "", "/org/freedesktop/systemd1/unit/cups_2eservice",
            0, "", "/")


class ServicesTest(unittest.TestCase):
    def test_parse_units(self):
        rows_in = [
            UNIT_ROW,
            list(UNIT_ROW[:7]),                                            # a list instead of a tuple, shorter
            ("dev-sda.device", "Disk", "loaded", "active", "plugged", "", "/x", 0, "", "/"),
            ("bad name.service", "x", "loaded", "active", "running", "", "/x", 0, "", "/"),
            ("../../etc.service", "x", "loaded", "active", "running", "", "/x", 0, "", "/"),
            ("short.service", "x"),
            ("n.service", 5, "loaded", "active", "running", "", "/x", 0, "", "/"),
            "not a row", None, 7,
            ("getty@tty1.service", "Getty on tty1‮\n", "loaded", "active", "running", "", "not-a-path", 0, "", "/"),
        ]
        units = services.parse_units(rows_in, "system")
        self.assertEqual([unit.name for unit in units], ["cups.service", "cups.service", "getty@tty1.service"])
        cups = units[0]
        self.assertEqual((cups.description, cups.status, cups.scope, cups.key), ("CUPS Scheduler", "Running", "system", "system:cups.service"))
        self.assertEqual((units[2].description, units[2].path), ("Getty on tty1� ", ""))
        for junk in (None, "text", 5, {"a": 1}):
            self.assertEqual(services.parse_units(junk, "user"), [])

    def test_unit_files_and_merge(self):
        files = services.parse_unit_files([
            ("/usr/lib/systemd/system/cups.service", "enabled"), ("/etc/systemd/system/mine.service", "disabled"),
            ("/usr/lib/systemd/system/getty@.service", "enabled"), ("/usr/lib/systemd/system/x.socket", "enabled"),
            ("/usr/lib/systemd/system/cups.service", "masked"), ("bad",), (1, 2), "x",
        ])
        self.assertEqual(files, {"cups.service": "enabled", "mine.service": "disabled"})
        merged = services.merge_units(services.parse_units([UNIT_ROW], "system"), files, "system")
        self.assertEqual([(u.name, u.enabled, u.status) for u in merged],
                         [("cups.service", "enabled", "Running"), ("mine.service", "disabled", "Inactive")])
        self.assertEqual(merged[1].scope_title, "System")

    def test_status_and_what_can_be_done(self):
        def unit(active, sub, enabled="enabled", load="loaded"):
            return services.Unit("x.service", "", load, active, sub, "", "user", enabled)
        self.assertEqual([services.status_text(a, s) for a, s in (
            ("active", "running"), ("active", "exited"), ("active", "listening"), ("failed", "failed"),
            ("inactive", "dead"), ("activating", "start"), ("deactivating", "stop"), ("", ""))],
            ["Running", "Exited", "Active (listening)", "Failed", "Inactive", "Starting", "Stopping", "Unknown"])
        self.assertTrue(services.can_start(unit("inactive", "dead")))
        self.assertTrue(services.can_start(unit("failed", "failed")))
        self.assertFalse(services.can_start(unit("active", "running")))
        self.assertFalse(services.can_start(unit("inactive", "dead", enabled="masked")))
        self.assertTrue(services.can_stop(unit("active", "running")))
        self.assertTrue(services.can_stop(unit("activating", "start")))
        self.assertFalse(services.can_stop(unit("inactive", "dead")))

    def test_main_pid(self):
        self.assertEqual([services.main_pid(v) for v in (1234, 0, -5, True, "7", None, 1 << 40)], [1234, 0, 0, 0, 0, 0, 0])

    def test_sessions(self):
        found = services.parse_sessions([
            ("3", 1000, "alice", "seat0", "/org/freedesktop/login1/session/_33"),
            ["c1", 110, "greeter", "seat0", "/org/freedesktop/login1/session/c1"],
            ("bad id!", 1, "x", "", "/x"), ("5", -1, "x", "", "/x"), ("6", "1000", "x", "", "/x"), ("7", 1, 2, "", "/x"),
            ("8", True, "x", "", "/x"), ("9",), None,
        ])
        self.assertEqual([(s.id, s.uid, s.user, s.seat) for s in found], [("3", 1000, "alice", "seat0"), ("c1", 110, "greeter", "seat0")])
        session = services.with_properties(found[0], {
            "TTY": "tty2", "State": "active", "Timestamp": 1_700_000_000_000_000, "Type": "wayland", "Class": "user",
            "Leader": 4321, "Remote": False, "RemoteHost": "ignored"})
        self.assertEqual((session.tty, session.state, session.since, session.kind, session.leader, session.remote),
                         ("tty2", "active", 1_700_000_000.0, "wayland", 4321, ""))
        self.assertEqual((session.place, session.state_title), ("seat0 · tty2", "Active"))
        remote = services.with_properties(found[0], {"Remote": True, "RemoteHost": "192.0.2.9", "TTY": "pts/1", "State": "online"})
        self.assertEqual(remote.place, "seat0 · pts/1 · from 192.0.2.9")
        hostile = services.with_properties(found[0], {"TTY": 5, "State": None, "Timestamp": "soon", "Leader": -3})
        self.assertEqual((hostile.tty, hostile.state, hostile.since, hostile.leader), ("", "online", 0.0, 0))
        self.assertIs(services.with_properties(found[0], "nonsense"), found[0])
        greeter = services.with_properties(found[1], {"Class": "greeter"})
        self.assertEqual(services.user_sessions([session, greeter]), [session])


if __name__ == "__main__":
    unittest.main()
