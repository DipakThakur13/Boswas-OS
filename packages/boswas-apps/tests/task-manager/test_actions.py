"""Signals, priorities, properties and starting programs; text helpers, settings and the graph history."""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tm_fixtures as fx  # noqa: E402
from test_grouping import proc, table  # noqa: E402

from boswas_apps.task_manager import actions, history, settings  # noqa: E402
from boswas_apps.task_manager import textutil as fmt  # noqa: E402
from boswas_apps.task_manager.sampler import Sampler  # noqa: E402


class FakeTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tm-actions-")
        self.root = Path(self._tmp.name)
        self.system = fx.FakeSystem(self.root)
        self.proc = str(self.root / "proc")
        self.sent: list[tuple[int, int]] = []

    def tearDown(self):
        self._tmp.cleanup()

    def kill(self, pid: int, signum: int) -> None:
        self.sent.append((pid, signum))


class SignalTest(FakeTree):
    def test_signal_goes_to_the_process_that_was_seen(self):
        self.system.add_process(100, "victim", start=777)
        actions.send_signal(100, signal.SIGTERM, 777, "victim", proc=self.proc, kill=self.kill)
        self.assertEqual(self.sent, [(100, signal.SIGTERM)])

    def test_reused_pid_is_not_signalled(self):
        self.system.add_process(100, "someone-else", start=999)
        with self.assertRaises(actions.ActionError) as caught:
            actions.send_signal(100, signal.SIGKILL, 777, "victim", proc=self.proc, kill=self.kill)
        self.assertEqual(str(caught.exception), "victim (PID 100) has already ended.")
        self.assertEqual(self.sent, [])

    def test_process_that_is_gone(self):
        with self.assertRaises(actions.ActionError):
            actions.send_signal(4242, signal.SIGTERM, 1, proc=self.proc, kill=self.kill)
        self.assertEqual(self.sent, [])

    def test_numbers_that_mean_process_groups_are_refused(self):
        for pid in (0, -1, -100, True, 1.5, "7", None):
            with self.subTest(pid=pid):
                with self.assertRaises(actions.ActionError):
                    actions.send_signal(pid, signal.SIGTERM, proc=self.proc, kill=self.kill)
        self.assertEqual(self.sent, [])

    def test_refusal_of_the_kernel_in_plain_words(self):
        self.system.add_process(100, "theirs", start=5)

        def refuse(_pid, _signum):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        with self.assertRaises(actions.ActionError) as caught:
            actions.send_signal(100, signal.SIGTERM, 5, "theirs", proc=self.proc, kill=refuse)
        text = str(caught.exception)
        self.assertIn("refused to signal theirs (PID 100)", text)
        self.assertIn("belongs to another user or to the system", text)
        self.assertNotIn("Errno", text)

        def vanished(_pid, _signum):
            raise ProcessLookupError(errno.ESRCH, "No such process")

        with self.assertRaises(actions.ActionError) as caught:
            actions.send_signal(100, signal.SIGTERM, 5, "theirs", proc=self.proc, kill=vanished)
        self.assertIn("already ended", str(caught.exception))

    def test_real_signal_to_a_real_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            found = actions.start_ticks_of(child.pid)
            self.assertIsNotNone(found)
            self.assertEqual(actions.condition(child.pid, found[0]), "running")
            with self.assertRaises(actions.ActionError):
                actions.send_signal(child.pid, signal.SIGTERM, found[0] + 12345, "child")
            self.assertIsNone(child.poll())
            actions.send_signal(child.pid, signal.SIGTERM, found[0], "child")
            deadline = time.monotonic() + 10
            while actions.condition(child.pid, found[0]) == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(actions.condition(child.pid, found[0]), "zombie")       # ended, not yet collected
            self.assertEqual(child.wait(10), -signal.SIGTERM)
            self.assertEqual(actions.condition(child.pid, found[0]), "gone")
        finally:
            child.kill()
            child.wait()

    def test_condition(self):
        self.system.add_process(100, "z", state="Z", start=5)
        self.system.add_process(101, "r", state="R", start=6)
        self.assertEqual(actions.condition(100, 5, self.proc), "zombie")
        self.assertEqual(actions.condition(101, 6, self.proc), "running")
        self.assertEqual(actions.condition(101, None, self.proc), "running")
        self.assertEqual(actions.condition(101, 7, self.proc), "gone")
        self.assertEqual(actions.condition(999, 1, self.proc), "gone")

    def test_tree(self):
        procs = table(proc(1, "init", 0), proc(10, "a", 1), proc(11, "b", 10), proc(12, "c", 10), proc(13, "d", 11),
                      proc(20, "x", 1), proc(30, "loop", 31), proc(31, "loop", 30))
        self.assertEqual(actions.descendants(procs, 10), [10, 11, 12, 13])
        self.assertEqual(actions.descendants(procs, 13), [13])
        self.assertEqual(actions.descendants(procs, 999), [999])
        self.assertEqual(sorted(actions.descendants(procs, 30)), [30, 31])
        self.assertEqual(actions.ancestors(procs, 13), [11, 10, 1])
        self.assertEqual(actions.ancestors(procs, 1), [])
        self.assertEqual(actions.ancestors(procs, 30), [31, 30])


class PriorityTest(FakeTree):
    def test_set(self):
        self.system.add_process(100, "p", start=5)
        calls = []
        actions.set_priority(100, 10, 5, "p", proc=self.proc, setpriority=lambda *args: calls.append(args))
        actions.set_priority(100, 99, 5, "p", proc=self.proc, setpriority=lambda *args: calls.append(args))
        self.assertEqual(calls, [(os.PRIO_PROCESS, 100, 10), (os.PRIO_PROCESS, 100, 19)])

    def test_refusal_to_raise_a_priority_is_reported_plainly(self):
        self.system.add_process(100, "p", start=5)

        def refuse(*_args):
            raise PermissionError(errno.EACCES, "Permission denied")

        with self.assertRaises(actions.ActionError) as caught:
            actions.set_priority(100, -10, 5, "p", proc=self.proc, setpriority=refuse)
        text = str(caught.exception)
        self.assertIn("refused to give p (PID 100) the priority “High” (nice -10): permission denied", text)
        self.assertIn("administrator rights", text)

        def foreign(*_args):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        with self.assertRaises(actions.ActionError) as caught:
            actions.set_priority(100, 5, 5, "p", proc=self.proc, setpriority=foreign)
        self.assertIn("belongs to another user", str(caught.exception))

    def test_gone_and_invalid(self):
        with self.assertRaises(actions.ActionError):
            actions.set_priority(100, 5, 5, proc=self.proc, setpriority=lambda *a: None)
        with self.assertRaises(actions.ActionError):
            actions.set_priority(0, 5, proc=self.proc, setpriority=lambda *a: None)

    def test_names(self):
        self.assertEqual([actions.priority_name(n) for n in (-20, -11, -10, -5, -1, 0, 1, 3, 5, 10, 15, 19)],
                         ["Very high", "High", "High", "Above normal", "Normal", "Normal", "Normal", "Below normal",
                          "Below normal", "Low", "Very low", "Very low"])

    def test_real_renice_of_a_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            start = actions.start_ticks_of(child.pid)[0]
            actions.set_priority(child.pid, 7, start, "child")
            self.assertEqual(os.getpriority(os.PRIO_PROCESS, child.pid), 7)
            if os.geteuid() != 0:
                with self.assertRaises(actions.ActionError) as caught:
                    actions.set_priority(child.pid, 0, start, "child")
                self.assertIn("permission denied", str(caught.exception))
        finally:
            child.kill()
            child.wait()


class PropertiesTest(FakeTree):
    def test_read(self):
        self.system.add_process(100, "editor", exe="/usr/bin/editor")
        found = actions.read_properties(100, self.proc)
        self.assertEqual((found.exe, found.exe_deleted, found.cwd, found.open_files, found.denied),
                         ("/usr/bin/editor", False, "/home/alice", 3, False))
        self.assertEqual(found.limits[4], ("Max open files", "1024", "524288", "files"))
        self.assertEqual(found.status["Threads"], "1")
        self.assertFalse(hasattr(found, "environ"))

    def test_replaced_executable(self):
        self.system.add_process(100, "old", exe="/usr/bin/old (deleted)")
        found = actions.read_properties(100, self.proc)
        self.assertEqual((found.exe, found.exe_deleted), ("/usr/bin/old", True))

    def test_process_that_is_gone_or_closed_to_us(self):
        gone = actions.read_properties(4242, self.proc)
        self.assertEqual((gone.exe, gone.cwd, gone.open_files, gone.limits, gone.status), (None, None, None, [], {}))
        self.system.add_process(100, "kthread-like", exe=None)
        self.system.remove("proc/100/cwd")
        self.system.remove("proc/100/fd")
        found = actions.read_properties(100, self.proc)
        self.assertEqual((found.exe, found.cwd, found.open_files), (None, None, None))

    def test_file_location(self):
        self.assertEqual(actions.file_location(sys.executable), sys.executable)
        for exe, words in ((None, "not known"), ("", "not known"), ("/no/such/program-xyz", "no longer exists"),
                           ("/no/such/program (deleted)", "no longer exists"), ("relative/path", "no longer exists")):
            with self.subTest(exe=exe):
                with self.assertRaises(actions.ActionError) as caught:
                    actions.file_location(exe)
                self.assertIn(words, str(caught.exception))


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tm-launch-")
        self.dir = Path(self._tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.tool = self.bin / "tool"
        self.tool.write_text("#!/bin/sh\nexit 0\n")
        self.tool.chmod(0o755)
        (self.bin / "data.txt").write_text("not a program")
        self.calls: list[tuple] = []

    def tearDown(self):
        self._tmp.cleanup()

    def popen(self, argv, **options):
        self.calls.append((argv, options))
        return "process"

    def test_split_without_a_shell(self):
        split = actions.split_command
        self.assertEqual(split("firefox --new-window https://example.org"), ["firefox", "--new-window", "https://example.org"])
        self.assertEqual(split('  editor "my file.txt" \'it is\' a\\ b  '), ["editor", "my file.txt", "it is", "a b"])
        self.assertEqual(split("echo $HOME `id` *.txt ; rm -rf / | x > y && z"),
                         ["echo", "$HOME", "`id`", "*.txt", ";", "rm", "-rf", "/", "|", "x", ">", "y", "&&", "z"])
        self.assertEqual(split("a # not a comment"), ["a", "#", "not", "a", "comment"])
        for text, words in (("", "Type the name"), ("   ", "Type the name"), ('say "unclosed', "quotation mark"),
                            ("it's", "quotation mark"), ('""', "Type the name")):
            with self.subTest(text=text):
                with self.assertRaises(actions.ActionError) as caught:
                    split(text)
                self.assertIn(words, str(caught.exception))

    def test_resolve_program(self):
        path = str(self.bin)
        self.assertEqual(actions.resolve_program("tool", "/", path), str(self.tool))
        self.assertEqual(actions.resolve_program(str(self.tool), "/", ""), str(self.tool))
        self.assertEqual(actions.resolve_program("./tool", str(self.bin), ""), str(self.tool))
        self.assertEqual(actions.resolve_program("~/bin/tool", "/", "", home=str(self.dir)), str(self.tool))
        for name, words in (("no-such-tool-xyz", "No program named"), ("./missing", "does not exist"),
                            (str(self.bin), "is a folder"), (str(self.bin / "data.txt"), "not marked executable")):
            with self.subTest(name=name):
                with self.assertRaises(actions.ActionError) as caught:
                    actions.resolve_program(name, str(self.bin), path)
                self.assertIn(words, str(caught.exception))

    def test_resolve_directory(self):
        self.assertEqual(actions.resolve_directory("", home=str(self.dir)), str(self.dir))
        self.assertEqual(actions.resolve_directory("~/bin", home=str(self.dir)), str(self.bin))
        self.assertEqual(actions.resolve_directory(f" {self.bin} "), str(self.bin))
        with self.assertRaises(actions.ActionError) as caught:
            actions.resolve_directory(str(self.dir / "nowhere"))
        self.assertIn("does not exist", str(caught.exception))
        with self.assertRaises(actions.ActionError):
            actions.resolve_directory(str(self.tool))

    def test_start_passes_an_argument_list_and_no_shell(self):
        result = actions.start_program(["tool", "--flag", "a b; rm -rf /"], str(self.dir), search_path=str(self.bin),
                                       popen=self.popen)
        self.assertEqual(result, "process")
        argv, options = self.calls[0]
        self.assertEqual(argv, ["tool", "--flag", "a b; rm -rf /"])
        self.assertEqual((options["executable"], options["cwd"]), (str(self.tool), str(self.dir)))
        self.assertTrue(options["start_new_session"])
        self.assertNotIn("shell", options)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)

    def test_start_errors_in_plain_words(self):
        with self.assertRaises(actions.ActionError):
            actions.start_program([], str(self.dir), popen=self.popen)
        with self.assertRaises(actions.ActionError) as caught:
            actions.start_program(["nope-xyz"], str(self.dir), search_path=str(self.bin), popen=self.popen)
        self.assertIn("No program named “nope-xyz”", str(caught.exception))
        broken = self.bin / "broken"
        broken.write_bytes(b"\x00\x01garbage")
        broken.chmod(0o755)
        with self.assertRaises(actions.ActionError) as caught:
            actions.start_program([str(broken)], str(self.dir))
        self.assertIn("could not be started", str(caught.exception))
        self.assertNotIn("Errno", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_real_start_runs_the_program_with_its_arguments(self):
        marker = self.dir / "ran.txt"
        script = self.bin / "writer"
        script.write_text('#!/bin/sh\nprintf "%s|%s|%s" "$1" "$2" "$PWD" > ran.txt\n')
        script.chmod(0o755)
        process = actions.start_program(actions.split_command('writer "a b" $(id)'), str(self.dir), search_path=str(self.bin))
        self.assertEqual(process.wait(10), 0)
        self.assertEqual(marker.read_text(), f"a b|$(id)|{os.path.realpath(self.dir)}")

    def test_show_in_files(self):
        files = self.bin / actions.FILES_PROGRAM
        files.write_text("#!/bin/sh\n")
        files.chmod(0o755)
        actions.show_in_files("/usr/bin/x y", search_path=str(self.bin), popen=self.popen)
        self.assertEqual(self.calls[0][0], ["boswas-files", "--select", "/usr/bin/x y"])
        with self.assertRaises(actions.ActionError) as caught:
            actions.show_in_files("/usr/bin/x", search_path=str(self.dir), popen=self.popen)
        self.assertIn("not installed", str(caught.exception))


class TextTest(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(fmt.clean("a\tb\nc\x00d\x7f"), "a b c d ")
        self.assertEqual(fmt.clean("safe‮evil⁦x​﻿"), "safe�evil�x��")
        self.assertEqual(fmt.clean("x" * 100, 10), "x" * 9 + "…")
        self.assertEqual(fmt.clean(None), "")
        self.assertEqual(fmt.clean("lone \udcff surrogate"), "lone ? surrogate")
        self.assertEqual(fmt.clean("Größe 日本語 😀"), "Größe 日本語 😀")

    def test_sizes_and_rates(self):
        self.assertEqual([fmt.size(v) for v in (None, 0, 1023, 1024, 1536, 10 * 1024 ** 2, 200 * 1024 ** 3, 5 * 1024 ** 5, -5)],
                         ["—", "0 B", "1023 B", "1.0 KB", "1.5 KB", "10.0 MB", "200 GB", "5.0 PB", "0 B"])
        self.assertEqual([fmt.megabytes(v) for v in (None, 0, 1048576 * 1234.56)], ["—", "0.0 MB", "1,234.6 MB"])
        self.assertEqual([fmt.rate(v) for v in (None, 0, 1000, 104858, 5 * 1048576)], ["—", "0 MB/s", "0 MB/s", "0.1 MB/s", "5.0 MB/s"])
        self.assertEqual([fmt.speed(v) for v in (None, 0, 512, 2048, 3 * 1048576)], ["—", "0 B/s", "512 B/s", "2.0 KB/s", "3.0 MB/s"])
        self.assertEqual([fmt.bitrate(v) for v in (None, 0, 5, 100, 1250, 125000, 12.5e6, 1.25e9, 1.25e12)],
                         ["—", "0 Kbps", "0 Kbps", "0.8 Kbps", "10.0 Kbps", "1.0 Mbps", "100 Mbps", "10.0 Gbps", "10000 Gbps"])

    def test_percent_frequency_and_times(self):
        self.assertEqual([fmt.percent(v) for v in (None, 0, 0.04, 0.4, 12.5, 100, -3)], ["—", "0%", "0%", "0.4%", "12.5%", "100%", "0%"])
        self.assertEqual(fmt.percent(12.6, 0), "13%")
        self.assertEqual([fmt.frequency(v) for v in (None, 0, 800, 2400.4)], ["—", "—", "800 MHz", "2.40 GHz"])
        self.assertEqual([fmt.uptime(v) for v in (None, -1, 0, 59, 3661, 90061)], ["—", "—", "0:00:00:00", "0:00:00:59", "0:01:01:01", "1:01:01:01"])
        self.assertEqual([fmt.span(v) for v in (None, 45, 600, 3900, 180000)], ["—", "45 s", "10 min", "1 h 05 min", "2 d 2 h"])
        self.assertEqual([fmt.cpu_time(v) for v in (None, 0, 3725)], ["—", "0:00:00", "1:02:05"])
        self.assertEqual([fmt.count(v) for v in (None, 0, 1234567)], ["—", "0", "1,234,567"])

    def test_moment(self):
        now = time.mktime((2026, 5, 17, 15, 0, 0, 0, 0, -1))
        self.assertEqual(fmt.moment(now - 3600, now), time.strftime("%H:%M:%S", time.localtime(now - 3600)))
        self.assertEqual(fmt.moment(now - 3 * 86400, now), time.strftime("%Y-%m-%d %H:%M", time.localtime(now - 3 * 86400)))
        self.assertEqual([fmt.moment(v, now) for v in (None, 0, -5, 1e30)], ["—", "—", "—", "—"])


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "conf" / "settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_when_there_is_no_file(self):
        loaded = settings.load(self.path)
        self.assertEqual(loaded, settings.Settings())
        self.assertEqual((loaded.refresh, loaded.start_page, loaded.interval()), ("normal", "processes", 1.0))

    def test_round_trip(self):
        wanted = settings.Settings("low", "performance", True, "dark", False, "cores")
        settings.save(self.path, wanted)
        self.assertEqual(settings.load(self.path), wanted)
        self.assertEqual(wanted.interval(), 4.0)
        self.assertIsNone(settings.Settings("paused").interval())
        self.assertEqual(os.listdir(self.path.parent), ["settings.json"])

    def test_damaged_and_hostile_files(self):
        self.path.parent.mkdir(parents=True)
        for content in (b"", b"{", b"[1, 2]", b'"text"', b"\xff\xfe", b"null", b"{" + b" " * 70000 + b"}"):
            self.path.write_bytes(content)
            self.assertEqual(settings.load(self.path), settings.Settings(), content[:10])
        self.path.write_text(json.dumps({"refresh": "ludicrous", "start_page": "settings", "full_command_line": "yes",
                                         "theme": 7, "group_by_type": None, "cpu_view": "cores", "extra": [1]}))
        loaded = settings.load(self.path)
        self.assertEqual(loaded, settings.Settings(cpu_view="cores"))

    def test_unwritable_place_raises_oserror(self):
        self.path.parent.mkdir(parents=True)
        (self.path.parent / "blocked").write_text("file")
        with self.assertRaises(OSError):
            settings.save(self.path.parent / "blocked" / "settings.json", settings.Settings())


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tm-history-")
        self.system = fx.FakeSystem(Path(self._tmp.name))
        self.clock = fx.FakeClock()
        self.sampler = Sampler(self._tmp.name, clock=self.clock, hz=fx.HZ, page_size=fx.PAGE, fd_limit=0, user_name=str,
                               ipv4=lambda name: ("192.0.2.7/24",))
        self.system.add_disk("sda", partitions=("sda1",))
        self.system.set_diskstats({"sda": (0, 0, 0, 0, 0, 0, 0)})
        self.system.add_interface("eth0")
        self.system.set_net({"eth0": (0, 0)})
        self.history = history.History()

    def tearDown(self):
        self.sampler.close()
        self._tmp.cleanup()

    def tick(self, seconds=1.0, parts=frozenset({"cpu", "memory", "disks", "net", "gpu", "battery"}), busy=100):
        self.system.burn(busy, 400 - busy)
        self.clock.advance(seconds)
        sample = self.sampler.tick(parts)
        self.history.push(sample)
        return sample

    def test_items_and_their_text(self):
        self.tick()
        self.system.set_net({"eth0": (125000, 2500)})
        self.system.set_diskstats({"sda": (10, 2048, 50, 0, 0, 0, 300)})
        self.tick()
        items = self.history.items()
        self.assertEqual([item.key for item in items], ["cpu", "memory", "disk:sda", "net:eth0"])
        cpu, memory, disk, net = items
        self.assertEqual((cpu.title, cpu.heading, cpu.summary), ("CPU", "Example(R) Core(TM) i7-1234X CPU @ 2.80GHz", ["25%  1.95 GHz"]))
        self.assertEqual(dict(cpu.stats)["Utilisation"], "25%")
        self.assertEqual(dict(cpu.stats)["Up time"], "0:03:25:45")
        self.assertEqual(dict(cpu.info)["Logical processors:"], "4")
        self.assertEqual(dict(cpu.info)["Base speed:"], "2.80 GHz")
        self.assertEqual(cpu.current(), 25.0)
        self.assertEqual(len(cpu.cores), 4)
        self.assertEqual(memory.summary, ["5.7/15.3 GB (38%)"])
        self.assertEqual(sum(size for _label, size in memory.composition), 16000000 * 1024)
        self.assertEqual(memory.graphs[0].top(), 16000000 * 1024.0)
        self.assertEqual((disk.title, disk.summary), ("Disk 0 (sda)", ["SSD", "30%"]))
        self.assertEqual(dict(disk.stats)["Read speed"], "1.0 MB/s")
        self.assertEqual(dict(disk.stats)["Average response time"], "5.0 ms")
        self.assertEqual((net.title, net.summary), ("Ethernet (eth0)", ["S: 20.0 Kbps", "R: 1.0 Mbps"]))
        self.assertEqual(dict(net.info)["IPv4 address:"], "192.0.2.7/24")
        self.assertEqual(dict(net.info)["Link speed:"], "1 Gbps")
        self.assertEqual(net.graphs[0].top_label(), "1.0 Mbps")
        self.assertTrue(net.graphs[0].series[1].dashed)

    def test_series_have_a_fixed_length_and_gaps(self):
        for _ in range(70):
            self.tick()
        cpu = self.history.item("cpu")
        values = cpu.graphs[0].series[0].values
        self.assertEqual(len(values), history.LENGTH)
        self.assertNotIn(None, values)
        self.tick(parts=frozenset({"memory"}))                 # another page: the total keeps the graph going
        self.assertEqual(list(values)[-2:], [25.0, 25.0])
        self.assertEqual(list(cpu.cores[0][1])[-2:], [25.0, None])     # but the single processors were not read
        self.system.burn(300, 900)
        self.clock.advance(3.0)                                # nothing was sampled for a while (window hidden)
        self.tick(seconds=1.0)
        self.assertEqual(list(values)[-3:], [25.0, None, 25.0])
        self.assertEqual(len(values), history.LENGTH)
        self.assertEqual(list(self.history.item("memory").graphs[0].series[0].values)[-3:-1], [float(6000000 * 1024), None])

    def test_a_sample_right_after_another_replaces_it(self):
        for _ in range(3):
            self.tick()
        values = self.history.item("cpu").graphs[0].series[0].values
        before = list(values)
        self.system.burn(400, 0)
        self.clock.advance(0.05)                               # the page was switched: sampled again at once
        self.history.push(self.sampler.tick({"cpu", "memory"}))
        self.assertEqual(list(values), before)                 # same length, and no rate over 50 ms
        self.tick(busy=0, seconds=0.95)
        self.assertEqual(list(values)[-2:], [25.0, 50.0])      # 400 busy of 800 ticks since the last real sample

    def test_slower_refresh_is_not_a_gap(self):
        self.history.interval = 4.0
        for _ in range(3):
            self.tick(seconds=4.0)
        self.assertEqual(list(self.history.item("cpu").graphs[0].series[0].values)[-3:], [0.0, 25.0, 25.0])

    def test_devices_come_and_go(self):
        self.tick()
        self.system.add_gpu()
        self.system.add_battery()
        self.system.add_disk("sdb", dev="8:16")
        self.system.set_diskstats({"sda": (0, 0, 0, 0, 0, 0, 0), "sdb": (0, 0, 0, 0, 0, 0, 0)})
        fresh = Sampler(self._tmp.name, clock=self.clock, hz=fx.HZ, page_size=fx.PAGE, fd_limit=0)
        self.history.push(fresh.tick({"disks", "gpu", "battery"}))
        keys = [item.key for item in self.history.items()]
        self.assertEqual(keys, ["cpu", "memory", "disk:sda", "disk:sdb", "net:eth0", "gpu:card0", "battery:BAT0"])
        gpu, battery = self.history.item("gpu:card0"), self.history.item("battery:BAT0")
        self.assertEqual((gpu.title, gpu.summary, gpu.note), ("GPU 0", ["AMD Device 73bf", "37%"], ""))
        self.assertEqual(dict(gpu.stats)["Video memory"], "2.0 GB of 8.0 GB")
        self.assertEqual((battery.title, battery.summary), ("Battery", ["76%  Discharging"]))
        self.assertEqual(dict(battery.stats)["Time remaining"], "4 h 00 min")
        self.assertEqual(dict(battery.info)["Health:"], "88%")
        self.system.remove("sys/block/sdb")
        self.system.remove("sys/class/drm")
        fresh2 = Sampler(self._tmp.name, clock=self.clock, hz=fx.HZ, page_size=fx.PAGE, fd_limit=0)
        self.history.push(fresh2.tick({"disks", "gpu"}))
        self.assertEqual([item.key for item in self.history.items()], ["cpu", "memory", "disk:sda", "net:eth0", "battery:BAT0"])

    def test_gpu_without_utilisation_has_a_note_instead_of_a_graph(self):
        self.system.add_gpu(vendor=0x8086, device=0x9A49, driver="i915", busy=None, vram=None)
        self.tick()
        gpu = self.history.item("gpu:card0")
        self.assertEqual((gpu.graphs, gpu.stats, gpu.summary), ([], [], ["Intel Device 9a49"]))
        self.assertIn("does not report its utilisation", gpu.note)
        self.assertEqual(dict(gpu.info)["Driver:"], "i915")
        self.assertIsNone(gpu.current())

    def test_many_processors_have_values_but_no_series(self):
        from boswas_apps.task_manager.sampler import CpuSample, CpuStatic, Sample
        static = CpuStatic("Big", None, 3000.0, 2, 64, 128, {}, "")
        cores = tuple((n, float(n % 100)) for n in range(128))
        self.history.push(Sample(1.0, 1.0, None, cpu=CpuSample(50.0, cores, None, None, None, 10, None, static)))
        cpu = self.history.item("cpu")
        self.assertEqual((cpu.cores, len(cpu.core_values)), ([], 128))
        self.assertEqual(dict(cpu.info)["Maximum speed:"], "3.00 GHz")
        self.assertEqual(cpu.summary, ["50%"])

    def test_round_tops_of_the_scale(self):
        kb = 1024
        self.assertEqual([history.nice_ceiling(v) for v in (0, 1, 150 * kb, 1024 * kb, 3.2 * kb * kb, 900 * kb * kb)],
                         [1.0, 1.0, 200 * kb, 1024 * kb, 5 * kb * kb, 1024 * kb * kb])
        self.assertEqual([history.nice_ceiling(v * kb * kb) / (kb * kb) for v in (5, 5.1, 9, 10, 11, 99, 400, 501)],
                         [5, 10, 10, 10, 20, 100, 500, 1024])
        self.assertEqual([history.nice_ceiling(v, "bitrate") * 8 for v in (12500, 13000, 1.3e6 / 8, 7e8 / 8)],
                         [100_000, 200_000, 2_000_000, 1_000_000_000])
        graph = history.Graph("x", [history.Series("a", [None, 5.0, 300 * kb])], maximum=None, unit="speed", floor=100 * kb)
        self.assertEqual((graph.top(), graph.top_label()), (500 * kb, "500 KB/s"))
        empty = history.Graph("x", [history.Series("a", [None, None])], maximum=None, unit="bitrate", floor=12500)
        self.assertEqual(empty.top_label(), "100 Kbps")

    def test_natural_order_of_devices(self):
        from boswas_apps.task_manager.sampler import DiskSample, Sample
        from boswas_apps.task_manager.sysfs import DiskInfo
        disks = [DiskSample(DiskInfo(name, "", 1, "SSD", False, (), ()), 0, 0, 0, None, (), False, False)
                 for name in ("sda", "sdb", "nvme10n1", "nvme2n1")]
        self.history.push(Sample(1.0, 1.0, None, disks=disks))
        self.assertEqual([item.key for item in self.history.items()], ["disk:nvme2n1", "disk:nvme10n1", "disk:sda", "disk:sdb"])


if __name__ == "__main__":
    unittest.main()
