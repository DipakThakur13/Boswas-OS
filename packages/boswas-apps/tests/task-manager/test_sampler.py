"""The sampler and the /sys readers against a fake /proc and /sys tree."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tm_fixtures as fx  # noqa: E402

from boswas_apps.task_manager import sampler as sampler_module  # noqa: E402
from boswas_apps.task_manager import sysfs  # noqa: E402
from boswas_apps.task_manager.sampler import Sampler, display_name, memory_from_meminfo, program_names  # noqa: E402

USERS = {0: "root", 1000: "alice", 1001: "bob"}


class SamplerCase(unittest.TestCase):
    cpus = 4

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tm-test-")
        self.root = Path(self._tmp.name)
        self.system = fx.FakeSystem(self.root, cpus=self.cpus)
        self.clock = fx.FakeClock()
        self.sampler = self.make()

    def make(self, **options) -> Sampler:
        options.setdefault("fd_limit", 1024)
        return Sampler(str(self.root), clock=self.clock, wall_clock=lambda: 1_700_001_000.0, hz=fx.HZ,
                       page_size=fx.PAGE, user_name=lambda uid: USERS.get(uid, str(uid)),
                       ipv4=lambda name: ("192.0.2.7/24",) if name == "eth0" else (), **options)

    def tearDown(self):
        self.sampler.close()
        self._tmp.cleanup()

    def step(self, busy: int = 0, idle: int = 400, seconds: float = 1.0, parts=frozenset({"procs", "io"})):
        self.system.burn(busy, idle)
        self.clock.advance(seconds)
        return self.sampler.tick(parts)


class ProcessCpuTest(SamplerCase):
    def test_cpu_percent_is_process_ticks_over_all_ticks(self):
        self.system.add_process(100, "worker", utime=50, stime=10)
        self.system.add_process(101, "idle")
        first = self.sampler.tick({"procs"})
        self.assertEqual(first.procs[100].cpu, 0.0)          # nothing to compare with yet
        self.system.use_cpu(100, 100)                        # one core for one second
        sample = self.step(busy=100, idle=300)
        self.assertAlmostEqual(sample.procs[100].cpu, 25.0)  # 100 of 400 ticks: a quarter of four cores
        self.assertEqual(sample.procs[101].cpu, 0.0)
        self.assertAlmostEqual(sample.total_cpu, 25.0)
        self.assertEqual(sample.ncpu, 4)

    def test_all_cores_busy_is_100_percent(self):
        self.system.add_process(100, "burner")
        self.sampler.tick({"procs"})
        self.system.use_cpu(100, 400)
        self.assertAlmostEqual(self.step(busy=400, idle=0).procs[100].cpu, 100.0)

    def test_system_time_counts_and_values_never_exceed_100(self):
        self.system.add_process(100, "sys")
        self.sampler.tick({"procs"})
        self.system.update(100, stime=30, utime=10)
        self.assertAlmostEqual(self.step(busy=40, idle=360).procs[100].cpu, 10.0)
        self.system.update(100, stime=30, utime=5000)        # more than the machine has: clamped
        self.assertEqual(self.step(busy=10, idle=390).procs[100].cpu, 100.0)

    def test_counter_going_backwards_is_zero(self):
        self.system.add_process(100, "odd", utime=500)
        self.sampler.tick({"procs"})
        self.system.update(100, utime=400)
        self.assertEqual(self.step(busy=10, idle=390).procs[100].cpu, 0.0)

    def test_process_born_between_two_scans_counts_from_its_start(self):
        self.system.add_process(100, "old", utime=1000)
        self.sampler.tick({"procs"})
        self.system.add_process(200, "new", utime=40, start=5000)
        sample = self.step(busy=40, idle=360)
        self.assertAlmostEqual(sample.procs[200].cpu, 10.0)
        self.assertEqual(sample.procs[100].cpu, 0.0)

    def test_without_total_ticks_the_clock_is_used(self):
        self.system.add_process(100, "w")
        self.sampler.tick({"procs"})
        self.system.use_cpu(100, 200)
        self.clock.advance(2.0)                               # /proc/stat did not move
        sample = self.sampler.tick({"procs"})
        self.assertAlmostEqual(sample.procs[100].cpu, 25.0)  # 200 ticks / (2 s * 100 Hz * 4 CPUs)

    def test_rate_after_a_pause_is_the_average_over_the_pause(self):
        self.system.add_process(100, "w")
        self.sampler.tick({"procs"})
        self.system.use_cpu(100, 300)
        self.step(busy=300, idle=100, parts=frozenset({"memory"}))          # processes not asked for
        sample = self.step(busy=0, idle=400)
        self.assertIsNone(self.sampler.tick({"memory"}).procs)
        self.assertAlmostEqual(sample.procs[100].cpu, 37.5)  # 300 of 800 ticks since the last process scan


class ProcessFieldsTest(SamplerCase):
    def test_fields(self):
        self.system.add_process(321, "editor", ppid=7, uid=1000, nice=5, threads=4, start=250, resident=3000,
                                shared=1000, cmdline=["/usr/bin/editor", "--new", "a b.txt"], tty_nr=(136 << 8) | 2,
                                io=(4096, 0))
        proc = self.sampler.tick({"procs", "io"}).procs[321]
        self.assertEqual((proc.pid, proc.ppid, proc.name, proc.state), (321, 7, "editor", "S"))
        self.assertEqual((proc.uid, proc.user, proc.nice, proc.threads), (1000, "alice", 5, 4))
        self.assertEqual(proc.rss, 3000 * fx.PAGE)
        self.assertEqual(proc.mem, 2000 * fx.PAGE)
        self.assertEqual(proc.start, fx.BOOT + 2.5)
        self.assertEqual(proc.args, ("/usr/bin/editor", "--new", "a b.txt"))
        self.assertEqual(proc.cmdline, "/usr/bin/editor --new a b.txt")
        self.assertEqual(proc.exe, "/usr/bin/editor")
        self.assertEqual(proc.cgroup, "/user.slice/user-1000.slice/session-1.scope")
        self.assertEqual(proc.tty, (136 << 8) | 2)
        self.assertFalse(proc.kthread)

    def test_names_with_spaces_and_parentheses(self):
        self.system.add_process(10, "(sd-pam)")
        self.system.add_process(11, "Web Content", cmdline=["/usr/lib/firefox/firefox", "-contentproc"])
        self.system.add_process(12, "a) S 0 (b")
        procs = self.sampler.tick({"procs"}).procs
        self.assertEqual([procs[pid].name for pid in (10, 11, 12)], ["(sd-pam)", "Web Content", "a) S 0 (b"])
        self.assertEqual(procs[12].ppid, 1)

    def test_long_names_are_completed_from_the_command_line(self):
        self.system.add_process(10, "boswas-task-man", cmdline=["/usr/bin/python3", "-IB", "/usr/bin/boswas-task-manager"],
                                exe="/usr/bin/python3.13")
        self.system.add_process(11, "gnome-terminal-", cmdline=["/usr/libexec/gnome-terminal-server"])
        self.system.add_process(12, "exactly15chars_", cmdline=["something", "else"])
        procs = self.sampler.tick({"procs"}).procs
        self.assertEqual(procs[10].name, "boswas-task-manager")
        self.assertEqual(procs[11].name, "gnome-terminal-server")
        self.assertEqual(procs[12].name, "exactly15chars_")
        self.assertEqual(program_names(procs[10]), ["boswas-task-manager", "python3.13"])

    def test_kernel_thread(self):
        self.system.add_process(2, "kthreadd", ppid=0, uid=0, kthread=True)
        self.system.add_process(33, "kworker/0:1H-events_highpri", ppid=2, uid=0, kthread=True, state="I")
        procs = self.sampler.tick({"procs", "io", "pss"}).procs
        worker = procs[33]
        self.assertTrue(worker.kthread)
        self.assertEqual((worker.name, worker.state, worker.user), ("kworker/0:1H-events_highpri", "I", "root"))
        self.assertEqual((worker.args, worker.exe, worker.mem, worker.disk, worker.pss), ((), None, 0, None, None))
        self.assertEqual(worker.cmdline, "[kworker/0:1H-events_highpri]")

    def test_zombie(self):
        self.system.add_process(50, "defunct", state="Z", cmdline=[], exe=None, resident=0, shared=0, io=None,
                                pss_kb=None)
        proc = self.sampler.tick({"procs", "io"}).procs[50]
        self.assertEqual((proc.state, proc.args, proc.exe, proc.mem, proc.disk), ("Z", (), None, 0, None))
        self.assertEqual(proc.cmdline, "defunct")

    def test_command_line_that_is_not_utf8(self):
        self.system.add_process(60, b"caf\xe9", cmdline=[b"/bin/caf\xe9", b"--name=\xff\xfe", "ok"])
        proc = self.sampler.tick({"procs"}).procs[60]
        self.assertEqual(proc.name, "caf�")
        self.assertEqual(proc.args, ("/bin/caf�", "--name=��", "ok"))
        proc.cmdline.encode("utf-8")                              # shows without an error

    def test_user_of_another_account_and_unknown_user(self):
        self.system.add_process(70, "a", uid=1001)
        self.system.add_process(71, "b", uid=4242)
        procs = self.sampler.tick({"procs"}).procs
        self.assertEqual((procs[70].user, procs[71].user), ("bob", "4242"))

    def test_exec_is_noticed_by_the_changed_name(self):
        self.system.add_process(80, "sh", cmdline=["sh", "-c", "exec real"], exe="/bin/sh")
        self.assertEqual(self.sampler.tick({"procs"}).procs[80].exe, "/bin/sh")
        self.system.update(80, comm=b"real", cmdline=["real", "--serve"], exe="/usr/bin/real", uid=1001)
        proc = self.step().procs[80]
        self.assertEqual((proc.name, proc.exe, proc.args, proc.user), ("real", "/usr/bin/real", ("real", "--serve"), "bob"))

    def test_slow_attributes_are_read_again_after_a_while(self):
        self.system.add_process(81, "daemon", uid=0)
        self.assertEqual(self.sampler.tick({"procs"}).procs[81].user, "root")
        self.system.update(81, uid=1001)                          # dropped its privileges without exec
        users = set()
        for _ in range(2 * sampler_module.SLOW_PERIOD + 2):
            users.add(self.step().procs[81].user)
        self.assertEqual(self.step().procs[81].user, "bob")
        self.assertEqual(users, {"root", "bob"})


class VanishingTest(SamplerCase):
    def test_process_that_ends_between_two_scans(self):
        self.system.add_process(100, "short")
        self.system.add_process(101, "stays")
        self.assertEqual(sorted(self.sampler.tick({"procs"}).procs), [100, 101])
        before = self.sampler.open_descriptors()
        self.system.remove_process(100)
        self.assertEqual(sorted(self.step().procs), [101])
        self.assertLess(self.sampler.open_descriptors(), before)

    def test_process_that_vanishes_in_the_middle_of_a_scan(self):
        self.system.add_process(100, "ghost")
        self.system.add_process(101, "stays")
        self.system.remove("proc/100/stat")                       # listed, but gone when it is read
        self.assertEqual(sorted(self.sampler.tick({"procs", "io"}).procs), [101])
        (self.root / "proc/102").mkdir()                          # a directory without anything in it
        self.assertEqual(sorted(self.step().procs), [101])

    def test_files_that_vanish_after_stat_was_read(self):
        self.system.add_process(100, "half")
        for name in ("statm", "io", "cmdline", "status", "cgroup", "exe", "smaps_rollup"):
            self.system.remove(f"proc/100/{name}")
        proc = self.sampler.tick({"procs", "io", "pss"}).procs[100]
        self.assertEqual((proc.name, proc.args, proc.exe, proc.uid, proc.disk, proc.pss), ("half", (), None, None, None, None))
        self.assertEqual(proc.rss, 1000 * fx.PAGE)               # from stat itself

    def test_truncated_or_foreign_stat_file(self):
        self.system.add_process(100, "ok")
        self.system.write("proc/200/stat", b"200 (cut")
        self.system.write("proc/201/stat", self.system.stat_line(100))      # says it is PID 100
        self.system.write("proc/202/stat", b"")
        self.assertEqual(sorted(self.sampler.tick({"procs"}).procs), [100])

    def test_reused_pid_starts_from_nought(self):
        for limit in (1024, 0):                                   # with and without kept descriptors
            with self.subTest(fd_limit=limit):
                self.sampler.close()
                self.sampler = self.make(fd_limit=limit)
                self.system.add_process(100, "first", utime=5000, start=100)
                self.sampler.tick({"procs"})
                self.system.remove_process(100)
                self.system.add_process(100, "second", utime=20, start=9000, uid=1001)
                with fx.dead_descriptors():
                    proc = self.step(busy=20, idle=380).procs[100]
                self.assertEqual((proc.name, proc.user, proc.start_ticks), ("second", "bob", 9000))
                self.assertAlmostEqual(proc.cpu, 5.0)             # not -4980 ticks, not the old process
                self.system.remove_process(100)
                self.step()

    def test_entries_that_are_not_processes_are_ignored(self):
        self.system.add_process(100, "ok")
        (self.root / "proc/self").mkdir(exist_ok=True)
        (self.root / "proc/sys").mkdir()
        self.system.write("proc/12abc/stat", b"12 (x) S 1")
        self.assertEqual(sorted(self.sampler.tick({"procs"}).procs), [100])


class DescriptorTest(SamplerCase):
    def test_descriptors_are_kept_and_released(self):
        for pid in range(100, 110):
            self.system.add_process(pid, f"p{pid}")
        self.sampler.tick({"procs", "io"})
        self.assertEqual(self.sampler.open_descriptors(), 30)     # stat, statm and io of ten processes
        self.step()
        self.assertEqual(self.sampler.open_descriptors(), 30)
        self.sampler.close()
        self.assertEqual(self.sampler.open_descriptors(), 0)

    def test_same_numbers_with_few_or_no_kept_descriptors(self):
        results = []
        for limit in (1024, sampler_module._FD_RESERVE + 7, 0):
            with tempfile.TemporaryDirectory() as directory:
                system = fx.FakeSystem(Path(directory))
                clock = fx.FakeClock()
                sampler = Sampler(directory, clock=clock, hz=fx.HZ, page_size=fx.PAGE, fd_limit=limit,
                                  user_name=str, ipv4=lambda name: ())
                for pid in range(100, 110):
                    system.add_process(pid, f"p{pid}", io=(0, 0))
                sampler.tick({"procs", "io"})
                for pid in range(100, 110):
                    system.update(pid, utime=pid - 99, io=(4096 * (pid - 99), 0), resident=2000 + pid)
                system.burn(100, 300)
                clock.advance(1.0)
                procs = sampler.tick({"procs", "io"}).procs
                results.append(sorted((p.pid, round(p.cpu, 6), p.mem, p.disk) for p in procs.values()))
                self.assertLessEqual(sampler.open_descriptors(), max(0, limit - sampler_module._FD_RESERVE))
                sampler.close()
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])
        self.assertEqual(results[0][0], (100, 0.25, (2100 - 200) * fx.PAGE, 4096.0))


class DiskIoTest(SamplerCase):
    def test_bytes_per_second(self):
        self.system.add_process(100, "copy", io=(1000, 2000))
        self.assertEqual(self.sampler.tick({"procs", "io"}).procs[100].disk, 0.0)
        self.system.update(100, io=(1000 + 3_000_000, 2000 + 1_000_000), utime=3)
        self.assertAlmostEqual(self.step(seconds=2.0).procs[100].disk, 2_000_000.0)
        self.assertEqual(self.step().procs[100].disk, 0.0)        # and nothing since

    def test_unreadable_io_file(self):
        self.system.add_process(100, "theirs", uid=1001)
        self.system.add_process(101, "mine")
        with fx.deny("/100/io"):
            procs = self.sampler.tick({"procs", "io"}).procs
            self.assertIsNone(procs[100].disk)
            self.assertEqual(procs[101].disk, 0.0)
            self.assertIsNone(self.step().procs[100].disk)
        # It is not tried again at once, so a refusal costs one failed open per IO_RETRY ticks.
        self.assertIsNone(self.step().procs[100].disk)

    def test_io_is_not_read_when_not_asked_for(self):
        self.system.add_process(100, "p")
        with fx.deny("/io"):
            self.assertIsNone(self.sampler.tick({"procs"}).procs[100].disk)

    def test_rate_spans_the_time_since_the_counter_was_last_read(self):
        self.system.add_process(100, "p", io=(0, 0))
        self.sampler.tick({"procs", "io"})
        self.system.update(100, io=(4000, 0), utime=1)
        self.step(parts=frozenset({"procs"}))                     # Details page: no io
        self.system.use_cpu(100, 1)
        self.step(parts=frozenset({"procs"}))
        self.system.use_cpu(100, 1)
        self.assertAlmostEqual(self.step().procs[100].disk, 4000 / 3)


class IdleProcessTest(SamplerCase):
    def test_a_process_that_did_not_run_is_the_same_object_again(self):
        self.system.add_process(101, "idle", io=(5, 5))           # 101: not due for a full read in these ticks
        self.system.add_process(102, "busy")
        first = self.sampler.tick({"procs", "io"}).procs
        self.system.use_cpu(102, 40)
        second = self.step(busy=40, idle=360).procs
        self.assertIs(second[101], first[101])
        self.assertIsNot(second[102], first[102])
        self.assertAlmostEqual(second[102].cpu, 10.0)
        third = self.step().procs                                 # 102 stopped running: once more a new object
        self.assertEqual((third[102].cpu, third[102].disk), (0.0, 0.0))
        self.assertIsNot(third[102], second[102])
        self.assertIs(third[101], first[101])
        fourth = self.step().procs
        self.assertIs(fourth[102], third[102])

    def test_only_the_stat_file_of_an_idle_process_is_read(self):
        for pid in range(101, 108):
            self.system.add_process(pid, "idle")
        self.sampler.tick({"procs", "io"})
        self.step()
        calls = []
        real = os.pread

        def counting(fd, size, offset):
            calls.append(fd)
            return real(fd, size, offset)

        with unittest.mock.patch("os.pread", counting):
            self.step()
        full = sum(1 for pid in range(101, 108) if (self.sampler._tick + pid) % sampler_module.FULL_PERIOD == 0)
        self.assertEqual(len(calls), 7 + 2 * full)               # stat for each; statm and io only where due

    def test_changes_without_cpu_time_show_after_a_few_ticks(self):
        self.system.add_process(100, "quiet", io=(0, 0), resident=1000, shared=200)
        self.sampler.tick({"procs", "io"})
        self.system.update(100, io=(8000, 0), shared=600)         # the stat line stays as it is
        seen = [self.step().procs[100] for _ in range(sampler_module.FULL_PERIOD)]
        self.assertEqual(seen[-1].mem, 400 * fx.PAGE)
        self.assertEqual(max(proc.disk for proc in seen), 8000.0)  # all of it in the tick it was noticed
        self.assertEqual(sum(1 for proc in seen if proc.disk), 1)

    def test_idle_time_is_not_counted_into_the_next_rate(self):
        self.system.add_process(101, "sleeper", io=(0, 0))
        self.sampler.tick({"procs", "io"})
        for _ in range(5):
            self.step()
        self.system.update(101, io=(6000, 0), utime=1)
        self.assertEqual(self.step().procs[101].disk, 6000.0)     # in one second, not spread over six

    def test_stopped_and_reniced_processes_are_noticed_at_once(self):
        self.system.add_process(101, "p")
        self.sampler.tick({"procs"})
        self.system.update(101, state="T")
        self.assertEqual(self.step().procs[101].state, "T")
        self.system.update(101, nice=7)
        self.assertEqual(self.step().procs[101].nice, 7)


class PssTest(SamplerCase):
    def test_pss_is_read_in_turns(self):
        count = sampler_module.PSS_BATCH * 2 + 3
        for pid in range(100, 100 + count):
            self.system.add_process(pid, f"p{pid}", pss_kb=pid)
        first = self.sampler.tick({"procs", "pss"}).procs
        self.assertEqual(sum(1 for proc in first.values() if proc.pss is not None), sampler_module.PSS_BATCH)
        self.step(parts=frozenset({"procs", "pss"}))
        third = self.step(parts=frozenset({"procs", "pss"})).procs
        self.assertTrue(all(proc.pss == proc.pid * 1024 for proc in third.values()))

    def test_unreadable_rollup_stays_none_and_is_not_asked_again(self):
        self.system.add_process(100, "theirs")
        self.system.add_process(101, "mine", pss_kb=77)
        with fx.deny("/100/smaps_rollup"):
            procs = self.sampler.tick({"procs", "pss"}).procs
        self.assertIsNone(procs[100].pss)
        self.assertEqual(procs[101].pss, 77 * 1024)
        self.assertIsNone(self.step(parts=frozenset({"procs", "pss"})).procs[100].pss)

    def test_pss_is_not_read_unless_asked(self):
        self.system.add_process(100, "p")
        self.assertIsNone(self.sampler.tick({"procs"}).procs[100].pss)


class CpuPartTest(SamplerCase):
    def test_total_and_per_core(self):
        self.sampler.tick({"cpu"})
        self.system.burn(100, 0, cpu=0)
        self.system.burn(50, 50, cpu=1)
        self.system.burn(0, 100, cpu=2)
        self.system.burn(0, 100, cpu=3)
        self.clock.advance(1.0)
        cpu = self.sampler.tick({"cpu"}).cpu
        self.assertAlmostEqual(cpu.percent, 37.5)
        self.assertEqual(cpu.cores, ((0, 100.0), (1, 50.0), (2, 0.0), (3, 0.0)))

    def test_iowait_is_not_busy(self):
        self.sampler.tick({"cpu"})
        self.system.cpu_times[0][4] += 100                        # iowait
        self.system.burn(0, 300)
        self.clock.advance(1.0)
        self.assertEqual(self.sampler.tick({"cpu"}).cpu.percent, 0.0)

    def test_static_information(self):
        for number in range(4):
            self.system.write(f"sys/devices/system/cpu/cpu{number}/cache/index0/level", "1\n")
            self.system.write(f"sys/devices/system/cpu/cpu{number}/cache/index0/type", "Data\n")
            self.system.write(f"sys/devices/system/cpu/cpu{number}/cache/index0/size", "32K\n")
            self.system.write(f"sys/devices/system/cpu/cpu{number}/cache/index0/shared_cpu_list", f"{number}\n")
            self.system.write(f"sys/devices/system/cpu/cpu{number}/cache/index3/level", "3\n")
            self.system.write(f"sys/devices/system/cpu/cpu{number}/cache/index3/type", "Unified\n")
            self.system.write(f"sys/devices/system/cpu/cpu{number}/cache/index3/size", "8192K\n")
            self.system.write(f"sys/devices/system/cpu/cpu{number}/cache/index3/shared_cpu_list", "0-3\n")
        cpu = self.sampler.tick({"cpu"}).cpu
        static = cpu.static
        self.assertEqual(static.model, "Example(R) Core(TM) i7-1234X CPU @ 2.80GHz")
        self.assertEqual((static.sockets, static.cores, static.logical), (1, 2, 4))
        self.assertEqual(static.base_mhz, 2800.0)                 # from the model name: no cpufreq here
        self.assertEqual(static.virtualization, "VT-x")
        self.assertEqual(static.caches, {"L1d": 4 * 32 * 1024, "L3": 8192 * 1024})
        self.assertEqual(cpu.mhz, 1950.0)                         # the mean of "cpu MHz"
        self.assertEqual((cpu.uptime, cpu.threads, cpu.load.one), (12345.67, 345, 0.52))

    def test_frequency_from_cpufreq(self):
        for number, khz in enumerate((1200000, 2400000)):
            base = f"sys/devices/system/cpu/cpufreq/policy{number}"
            self.system.write(f"{base}/scaling_cur_freq", f"{khz}\n")
            self.system.write(f"{base}/cpuinfo_max_freq", "4600000\n")
            self.system.write(f"{base}/cpuinfo_min_freq", "400000\n")
            self.system.write(f"{base}/base_frequency", "2600000\n")
        cpu = self.sampler.tick({"cpu"}).cpu
        self.assertEqual(cpu.mhz, 1800.0)
        self.assertEqual((cpu.static.base_mhz, cpu.static.max_mhz), (2600.0, 4600.0))

    def test_process_count_without_a_process_scan(self):
        for pid in (1, 5, 9):
            self.system.add_process(pid, f"p{pid}")
        self.assertEqual(self.sampler.tick({"cpu"}).cpu.processes, 3)
        self.assertEqual(self.sampler.tick({"cpu", "procs"}).cpu.processes, 3)

    def test_total_is_in_every_sample(self):
        self.sampler.tick(set())
        self.system.burn(200, 200)
        self.clock.advance(1.0)
        sample = self.sampler.tick(set())
        self.assertAlmostEqual(sample.total_cpu, 50.0)
        self.assertEqual(sample.interval, 1.0)
        self.assertIsNone(sample.cpu)
        self.assertIsNone(sample.procs)

    def test_missing_proc_stat_is_reported_not_raised(self):
        self.system.remove("proc/stat")
        sample = self.sampler.tick({"cpu", "procs", "memory"})
        self.assertIsNone(sample.cpu)
        self.assertIsNone(sample.procs)
        self.assertIsNotNone(sample.memory)
        self.assertTrue(sample.errors)


class ManyCpusTest(SamplerCase):
    cpus = 4096

    def test_4096_cpus(self):
        self.sampler.tick({"cpu"})
        self.system.burn(4096 * 30, 4096 * 70)
        self.clock.advance(1.0)
        sample = self.sampler.tick({"cpu"})
        self.assertEqual(sample.ncpu, 4096)
        self.assertEqual(len(sample.cpu.cores), 4096)
        self.assertAlmostEqual(sample.cpu.percent, 30.0)
        self.assertAlmostEqual(sample.cpu.cores[4095][1], 30.0)
        self.assertEqual(sample.cpu.static.logical, 4096)

    def test_only_some_frequency_files_are_read(self):
        for number in range(0, 4096, 16):
            self.system.write(f"sys/devices/system/cpu/cpufreq/policy{number}/scaling_cur_freq", "2000000\n")
        opened = []
        real = os.open

        def counting(path, flags, *args, **kwargs):
            if "scaling_cur_freq" in os.fspath(path):
                opened.append(path)
            return real(path, flags, *args, **kwargs)

        with unittest.mock.patch("os.open", counting):
            self.assertEqual(sysfs.cpu_current_mhz(str(self.root / "sys")), 2000.0)
        self.assertLessEqual(len(opened), sysfs.MAX_FREQUENCY_FILES)


class MemoryTest(SamplerCase):
    def test_numbers(self):
        memory = self.sampler.tick({"memory"}).memory
        kb = 1024
        self.assertEqual(memory.total, 16000000 * kb)
        self.assertEqual(memory.in_use, 6000000 * kb)
        self.assertEqual(memory.cached, (500000 + 5000000 + 600000 - 400000) * kb)
        self.assertEqual((memory.modified, memory.swap_used), (2000 * kb, 1000000 * kb))
        self.assertEqual(memory.used + memory.modified + memory.standby + memory.free, memory.total)
        self.assertAlmostEqual(memory.percent, 37.5)

    def test_odd_meminfo(self):
        empty = memory_from_meminfo({})
        self.assertEqual((empty.total, empty.in_use, empty.percent), (0, 0, 0.0))
        old = memory_from_meminfo({"MemTotal": 1000, "MemFree": 400, "Cached": 300})      # no MemAvailable
        self.assertEqual((old.available, old.in_use), (700, 300))
        wild = memory_from_meminfo({"MemTotal": 1000, "MemFree": 5000, "Cached": 9000, "Dirty": 99999})
        self.assertEqual(wild.used + wild.modified + wild.standby + wild.free, wild.total)
        self.assertGreaterEqual(min(wild.used, wild.modified, wild.standby, wild.free), 0)

    def test_unreadable_meminfo(self):
        self.system.remove("proc/meminfo")
        self.assertIsNone(self.sampler.tick({"memory"}).memory)


class DiskPartTest(SamplerCase):
    def setUp(self):
        super().setUp()
        self.system.add_disk("sda", sectors=1953525168, rotational=1, model="WDC WD10EZEX", vendor="ATA",
                             partitions=("sda1", "sda2"), dev="8:0")
        self.system.add_disk("nvme0n1", sectors=1000215216, model="Fast NVMe 512", partitions=("nvme0n1p1",), dev="259:0")
        self.system.add_disk("loop0", dev="7:0")
        self.system.add_disk("sr0", sectors=0, dev="11:0")
        self.system.add_disk("sdb", sectors=31250000, removable=1, model="Cruzer", vendor="SanDisk", dev="8:16")
        self.system.write("sys/block/dm-0/dev", "253:0\n")
        self.system.write("sys/block/dm-0/dm/name", "vg-home\n")
        self.system.write("sys/block/dm-0/slaves/sda2/dev", "8:2\n")
        self.system.write("proc/self/mountinfo",
                          "20 1 259:1 / / rw - ext4 /dev/nvme0n1p1 rw\n"
                          "21 20 253:0 / /home rw - ext4 /dev/mapper/vg-home rw\n"
                          "22 20 8:1 / /boot rw - vfat /dev/sda1 rw\n"
                          "23 20 0:30 / /run rw - tmpfs tmpfs rw\n")
        self.system.write("proc/swaps", "Filename Type Size Used Priority\n/dev/sda2 partition 1 0 -2\n")
        self.system.set_diskstats({"sda": (10, 1000, 100, 20, 2000, 200, 500), "nvme0n1": (0, 0, 0, 0, 0, 0, 0),
                                   "sdb": (0, 0, 0, 0, 0, 0, 0)})

    def test_which_disks_and_what_they_are(self):
        disks = {disk.info.name: disk for disk in self.sampler.tick({"disks"}).disks}
        self.assertEqual(sorted(disks), ["nvme0n1", "sda", "sdb"])          # no loop device, no empty drive
        self.assertEqual((disks["sda"].info.kind, disks["sda"].info.model), ("HDD", "WDC WD10EZEX"))
        self.assertEqual(disks["sda"].info.capacity, 1953525168 * 512)
        self.assertEqual(disks["nvme0n1"].info.kind, "SSD (NVMe)")
        self.assertEqual((disks["sdb"].info.kind, disks["sdb"].info.model), ("Removable", "SanDisk Cruzer"))

    def test_mounts_through_device_mapper_and_swap(self):
        disks = {disk.info.name: disk for disk in self.sampler.tick({"disks"}).disks}
        self.assertEqual(disks["nvme0n1"].mounts, ("/",))
        self.assertTrue(disks["nvme0n1"].system)
        self.assertEqual(disks["sda"].mounts, ("/boot", "/home"))
        self.assertFalse(disks["sda"].system)
        self.assertTrue(disks["sda"].swap)
        self.assertEqual(disks["sdb"].mounts, ())

    def test_rates(self):
        self.sampler.tick({"disks"})
        self.system.set_diskstats({"sda": (10 + 5, 1000 + 2048, 100 + 30, 20 + 5, 2000 + 4096, 200 + 70, 500 + 250),
                                   "nvme0n1": (0, 0, 0, 0, 0, 0, 0), "sdb": (0, 0, 0, 0, 0, 0, 0)})
        self.clock.advance(1.0)
        disks = {disk.info.name: disk for disk in self.sampler.tick({"disks"}).disks}
        sda = disks["sda"]
        self.assertAlmostEqual(sda.active, 25.0)
        self.assertAlmostEqual(sda.read_bps, 2048 * 512)
        self.assertAlmostEqual(sda.write_bps, 4096 * 512)
        self.assertAlmostEqual(sda.response_ms, 10.0)
        self.assertEqual((disks["nvme0n1"].active, disks["nvme0n1"].response_ms), (0.0, None))

    def test_busy_counter_wraps_at_32_bits(self):
        near = 2 ** 32 - 100
        self.system.set_diskstats({"sda": (0, 0, 0, 0, 0, 0, near)})
        self.sampler.tick({"disks"})
        self.system.set_diskstats({"sda": (0, 0, 0, 0, 0, 0, 400)})          # 500 ms later, past the wrap
        self.clock.advance(1.0)
        sda = next(d for d in self.sampler.tick({"disks"}).disks if d.info.name == "sda")
        self.assertAlmostEqual(sda.active, 50.0)

    def test_counters_that_were_reset(self):
        self.sampler.tick({"disks"})
        self.system.set_diskstats({"sda": (0, 0, 0, 0, 0, 0, 0)})
        self.clock.advance(1.0)
        sda = next(d for d in self.sampler.tick({"disks"}).disks if d.info.name == "sda")
        self.assertEqual((sda.active, sda.read_bps, sda.write_bps), (0.0, 0.0, 0.0))


class NetPartTest(SamplerCase):
    def setUp(self):
        super().setUp()
        self.system.add_interface("eth0")
        self.system.add_interface("wlan0", wireless=True, speed=None, state="down")
        self.system.add_interface("lo", physical=False, arp_type=772, state="unknown", speed=None)
        self.system.add_interface("docker0", physical=False, state="down", speed=None)
        self.system.add_interface("tun0", physical=False, state="unknown", speed=None, arp_type=65534)
        self.system.write("proc/net/if_inet6",
                          "fe800000000000000000000000000001 02 40 20 80     eth0\n"
                          "20010db8000000000000000000000001 02 40 00 00     eth0\n")
        self.system.set_net({"lo": (5, 5), "eth0": (1000, 2000), "wlan0": (0, 0), "docker0": (0, 0), "tun0": (10, 10)})

    def test_which_interfaces(self):
        nets = self.sampler.tick({"net"}).nets
        self.assertEqual([net.info.name for net in nets], ["eth0", "wlan0", "tun0"])   # hardware first
        kinds = {net.info.name: net.info.kind for net in nets}
        self.assertEqual(kinds, {"eth0": "Ethernet", "wlan0": "Wi-Fi", "tun0": "Virtual"})
        eth0 = nets[0]
        self.assertEqual((eth0.info.speed_mbps, eth0.info.state), (1000, "up"))
        self.assertEqual(eth0.ipv4, ("192.0.2.7/24",))
        self.assertEqual(eth0.ipv6, ("2001:db8::1/64", "fe80::1/64"))      # the global address first

    def test_rates(self):
        self.sampler.tick({"net"})
        self.system.set_net({"lo": (5, 5), "eth0": (1000 + 125000, 2000 + 2500), "wlan0": (0, 0), "docker0": (0, 0),
                             "tun0": (10, 10)})
        self.clock.advance(0.5)
        eth0 = self.sampler.tick({"net"}).nets[0]
        self.assertAlmostEqual(eth0.rx_bps, 250000.0)
        self.assertAlmostEqual(eth0.tx_bps, 5000.0)
        self.assertEqual((eth0.rx_total, eth0.tx_total), (126000, 4500))

    def test_32_bit_counter_wrap_and_reset(self):
        self.system.set_net({"eth0": (2 ** 32 - 1000, 5000)})
        self.sampler.tick({"net"})
        self.system.set_net({"eth0": (3000, 100)})                # received wrapped; sent was reset
        self.clock.advance(1.0)
        eth0 = self.sampler.tick({"net"}).nets[0]
        self.assertAlmostEqual(eth0.rx_bps, 4000.0)
        self.assertEqual(eth0.tx_bps, 0.0)

    def test_interface_that_appears_later(self):
        self.sampler.tick({"net"})
        self.system.add_interface("usb0")
        self.system.set_net({"eth0": (1000, 2000), "usb0": (0, 0)})
        self.clock.advance(1.0)
        names = [net.info.name for net in self.sampler.tick({"net"}).nets]
        self.assertEqual(names, ["eth0", "usb0"])


class GpuAndBatteryTest(SamplerCase):
    def test_gpu_with_utilisation(self):
        self.system.add_gpu()
        self.system.write("usr/share/misc/pci.ids",
                          "# comment\n1002  Advanced Micro Devices, Inc. [AMD/ATI]\n\t73bf  Navi 21 [Radeon RX 6800]\n"
                          "\t\t1002 0e3a  Reference\n8086  Intel Corporation\n\t9a49  TigerLake-LP GT2\n")
        gpu = self.sampler.tick({"gpu"}).gpus[0]
        self.assertEqual((gpu.card, gpu.vendor, gpu.model, gpu.driver), ("card0", "AMD", "Navi 21 [Radeon RX 6800]", "amdgpu"))
        self.assertEqual((gpu.bus_id, gpu.busy_percent, gpu.memory_used, gpu.memory_total),
                         ("1002:73bf", 37, 2 << 30, 8 << 30))
        self.system.write("sys/class/drm/card0/device/gpu_busy_percent", "250\n")
        self.assertEqual(self.sampler.tick({"gpu"}).gpus[0].busy_percent, 100)

    def test_gpu_identification_only(self):
        self.system.add_gpu("card1", vendor=0x8086, device=0x9A49, driver="i915", busy=None, vram=None)
        gpu = self.sampler.tick({"gpu"}).gpus[0]
        self.assertEqual((gpu.vendor, gpu.model, gpu.busy_percent, gpu.memory_total), ("Intel", "Device 9a49", None, None))

    def test_unknown_vendor_and_platform_device(self):
        self.system.add_gpu("card0", vendor=0xABCD, device=0x0001, driver="x", busy=None, vram=None)
        self.system.add_gpu("card1", vendor=None, driver="vc4-drm", busy=None, vram=None, compatible="brcm,bcm2711-vc5")
        gpus = self.sampler.tick({"gpu"}).gpus
        self.assertEqual((gpus[0].vendor, gpus[0].model), ("Vendor abcd", "Device 0001"))
        self.assertEqual((gpus[1].vendor, gpus[1].model, gpus[1].bus_id), ("", "bcm2711-vc5", "brcm,bcm2711-vc5"))

    def test_no_drm(self):
        sample = self.sampler.tick({"gpu", "battery"})
        self.assertEqual((sample.gpus, sample.batteries), ([], []))

    def test_battery(self):
        self.system.add_battery()
        self.system.add_battery("AC", TYPE="Mains")
        self.system.add_battery("hidpp_battery_0", SCOPE="Device", CAPACITY="50")
        batteries = self.sampler.tick({"battery"}).batteries
        self.assertEqual(len(batteries), 1)
        battery = batteries[0]
        self.assertEqual((battery.name, battery.percent, battery.status), ("BAT0", 76.0, "Discharging"))
        self.assertAlmostEqual(battery.power_w, 9.5)
        self.assertAlmostEqual(battery.energy_wh, 38.0)
        self.assertAlmostEqual(battery.seconds_left, 4 * 3600)
        self.assertEqual((battery.cycles, battery.technology, battery.model), (123, "Li-ion", "ACME X1"))

    def test_battery_that_reports_charge_instead_of_energy(self):
        self.system.add_battery(ENERGY_NOW="", ENERGY_FULL="", ENERGY_FULL_DESIGN="", POWER_NOW="", CAPACITY="",
                                CHARGE_NOW="2000000", CHARGE_FULL="4000000", CURRENT_NOW="-1000000",
                                VOLTAGE_NOW="10000000", STATUS="Charging")
        battery = self.sampler.tick({"battery"}).batteries[0]
        self.assertAlmostEqual(battery.percent, 50.0)
        self.assertAlmostEqual(battery.power_w, 10.0)
        self.assertAlmostEqual(battery.seconds_left, 2 * 3600)   # 20 Wh to go at 10 W

    def test_hostile_battery_file(self):
        self.system.write("sys/class/power_supply/BAT9/uevent",
                          "POWER_SUPPLY_TYPE=Battery\nPOWER_SUPPLY_CAPACITY=900\nPOWER_SUPPLY_ENERGY_NOW=abc\n"
                          "POWER_SUPPLY_STATUS=Dis‮charging\nnonsense\n")
        battery = self.sampler.tick({"battery"}).batteries[0]
        self.assertEqual((battery.percent, battery.energy_wh, battery.status), (100.0, None, "Dis�charging"))


class SysfsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.system = fx.FakeSystem(Path(self._tmp.name), cpus=8)
        self.sys = str(self.system.sys)

    def tearDown(self):
        self._tmp.cleanup()

    def test_topology_with_two_sockets_and_an_offline_cpu(self):
        for number in range(8):
            base = f"sys/devices/system/cpu/cpu{number}/topology"
            self.system.write(f"{base}/physical_package_id", f"{number // 4}\n")
            self.system.write(f"{base}/core_id", f"{number % 4 // 2}\n")
        self.system.remove("sys/devices/system/cpu/cpu7/topology")
        self.system.write("sys/devices/system/cpu/cpufreq/x", "")
        self.system.write("sys/devices/system/cpu/cpuidle/x", "")
        self.assertEqual(sysfs.cpu_numbers(self.sys), list(range(8)))
        self.assertEqual(sysfs.cpu_topology(self.sys), (7, 2, 4))

    def test_nothing_there(self):
        empty = str(Path(self._tmp.name) / "nowhere")
        self.assertEqual(sysfs.cpu_topology(empty), (0, 0, 0))
        self.assertEqual(sysfs.cpu_caches(empty), {})
        self.assertIsNone(sysfs.cpu_current_mhz(empty))
        self.assertEqual(sysfs.cpu_frequency_limits(empty), (None, None, None))
        self.assertEqual((sysfs.disk_names(empty), sysfs.net_names(empty), sysfs.gpu_cards(empty)), ([], [], []))
        self.assertIsNone(sysfs.battery(empty, "BAT0"))

    def test_pci_names(self):
        self.system.write("usr/share/hwdata/pci.ids", "10de  NVIDIA Corporation\n\t1c82  GP107 [GeForce GTX 1050 Ti]\n"
                                                      "8086  Intel Corporation\n\t0001  Something\n")
        root = str(self.system.root)
        self.assertEqual(sysfs.pci_names(root, 0x10DE, 0x1C82), ("NVIDIA Corporation", "GP107 [GeForce GTX 1050 Ti]"))
        self.assertEqual(sysfs.pci_names(root, 0x10DE, 0x0001), ("NVIDIA Corporation", ""))
        self.assertEqual(sysfs.pci_names(root, 0x1234, 0x0001), ("", ""))


class NameTest(unittest.TestCase):
    def test_display_name(self):
        self.assertEqual(display_name("bash", ("bash",)), "bash")
        self.assertEqual(display_name("0123456789abcde", ("/opt/0123456789abcdefgh", "x")), "0123456789abcdefgh")
        self.assertEqual(display_name("0123456789abcde", ("/opt/0123456789abcde fgh",)), "0123456789abcde")
        self.assertEqual(display_name("kworker/u8:3-ev", (), True), "kworker/u8:3-ev")

    def test_ipv4_of_an_interface_that_does_not_exist(self):
        self.assertEqual(sampler_module.ipv4_addresses("no-such-interface-xyz"), ())
        self.assertEqual(sampler_module.ipv4_addresses("x" * 300), ())


if __name__ == "__main__":
    unittest.main()
