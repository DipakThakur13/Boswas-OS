"""The parsers of /proc files: the formats the kernel writes, old and new, and input that is not one."""

from __future__ import annotations

import unittest

from boswas_apps.task_manager import procfs, textutil

STAT = (b"1234 (bash) S 1000 1234 1234 34816 5678 4194304 2500 31000 1 50 12 7 40 30 20 0 1 0 987654 "
        b"12345344 1321 18446744073709551615 1 1 0 0 0 0 65536 3670020 1266777851 0 0 0 17 3 0 0 0 0 0 "
        b"0 0 0 0 0 0 0 0\n")


class StatTest(unittest.TestCase):
    def test_ordinary_line(self):
        stat = procfs.parse_stat(STAT)
        self.assertEqual((stat.pid, stat.comm, stat.state, stat.ppid, stat.session), (1234, b"bash", "S", 1000, 1234))
        self.assertEqual((stat.utime, stat.stime, stat.nice, stat.threads), (12, 7, 0, 1))
        self.assertEqual((stat.starttime, stat.vsize, stat.rss, stat.tty_nr), (987654, 12345344, 1321, 34816))
        self.assertFalse(stat.kernel_thread)

    def test_name_with_spaces_and_parentheses(self):
        for name in (b"(sd-pam)", b"a) b (c", b"Web Content", b") R 1 1", b"x)\n(y", b"tmux: server (/tmp)"):
            line = STAT.replace(b"(bash)", b"(" + name + b")")
            stat = procfs.parse_stat(line)
            self.assertIsNotNone(stat, name)
            self.assertEqual(stat.comm, name)
            self.assertEqual((stat.state, stat.ppid, stat.utime), ("S", 1000, 12))

    def test_kernel_thread_flag_and_negative_numbers(self):
        line = b"2 (kthreadd) S 0 0 0 0 -1 2129984 0 0 0 0 0 5 0 0 20 -20 1 0 3 0 0 18446744073709551615 0 0\n"
        stat = procfs.parse_stat(line)
        self.assertTrue(stat.kernel_thread)
        self.assertEqual((stat.nice, stat.stime, stat.rss), (-20, 5, 0))

    def test_not_a_stat_line(self):
        for data in (b"", b"\n", b"1234", b"1234 (bash", b"bash) S 1", b"x (y) S 1", b"12 (a) S 1 2 3",
                     b"12 (a) S one two three" + b" 0" * 30, b"\xff\xfe" * 40, b"(x) " * 10):
            self.assertIsNone(procfs.parse_stat(data), data)

    def test_non_utf8_name_is_kept_as_bytes(self):
        stat = procfs.parse_stat(STAT.replace(b"(bash)", b"(caf\xe9 \xff)"))
        self.assertEqual(stat.comm, b"caf\xe9 \xff")
        self.assertEqual(textutil.decode(stat.comm), "caf� �")


class SmallFilesTest(unittest.TestCase):
    def test_statm(self):
        self.assertEqual(procfs.parse_statm(b"5000 1321 400 10 0 300 0\n"), (5000, 1321, 400))
        self.assertIsNone(procfs.parse_statm(b""))
        self.assertIsNone(procfs.parse_statm(b"a b c"))

    def test_io(self):
        data = (b"rchar: 100\nwchar: 200\nsyscr: 1\nsyscw: 2\nread_bytes: 4096\nwrite_bytes: 8192\n"
                b"cancelled_write_bytes: 0\n")
        self.assertEqual(procfs.parse_io(data), (4096, 8192))
        self.assertIsNone(procfs.parse_io(b"rchar: 100\n"))
        self.assertIsNone(procfs.parse_io(b"read_bytes: x\nwrite_bytes: 1\n"))

    def test_status_uid(self):
        data = b"Name:\tx\nUid:\t1000\t1001\t1002\t1003\nGid:\t5\t5\t5\t5\n"
        self.assertEqual(procfs.status_uid(data), 1001)
        self.assertEqual(procfs.status_uid(b"Uid:\t0\t0\t0\t0\n"), 0)
        self.assertEqual(procfs.status_uid(b"Uid:\t7\n"), 7)
        self.assertIsNone(procfs.status_uid(b"Name:\tx\n"))
        self.assertIsNone(procfs.status_uid(b"Name:\tx\nUid:\tabc def\n"))
        self.assertEqual(procfs.parse_status(data)["Gid"], "5\t5\t5\t5")

    def test_cmdline(self):
        self.assertEqual(procfs.parse_cmdline(b"/usr/bin/python3\0-m\0http.server\0"),
                         ["/usr/bin/python3", "-m", "http.server"])
        self.assertEqual(procfs.parse_cmdline(b""), [])
        self.assertEqual(procfs.parse_cmdline(b"\0\0\0"), [])
        self.assertEqual(procfs.parse_cmdline(b"sshd: alice@pts/0\0\0\0\0"), ["sshd: alice@pts/0"])
        self.assertEqual(procfs.parse_cmdline(b"a\0\0b\0"), ["a", "", "b"])

    def test_cmdline_that_is_not_utf8_or_has_control_characters(self):
        arguments = procfs.parse_cmdline(b"prog\0caf\xe9\0line\nbreak\ttab\0\xe2\x80\xaeevil\0")
        self.assertEqual(arguments, ["prog", "caf�", "line break tab", "�evil"])
        self.assertEqual(procfs.command_line(arguments), "prog caf� line break tab �evil")

    def test_long_cmdline_is_cut(self):
        arguments = procfs.parse_cmdline(b"x" * 10000, limit=100)
        self.assertEqual(len(arguments), 1)
        self.assertTrue(arguments[0].endswith("…"))
        self.assertLessEqual(len(arguments[0]), 101)

    def test_smaps_rollup(self):
        data = (b"00400000-7ffd0000 ---p 00000000 00:00 0   [rollup]\nRss:  1708 kB\nPss:   257 kB\n"
                b"Pss_Dirty: 108 kB\nSwap: 0 kB\nGarbage\nVmFlags: rd wr\n")
        rollup = procfs.parse_smaps_rollup(data)
        self.assertEqual((rollup["Pss"], rollup["Rss"], rollup["Swap"]), (257 * 1024, 1708 * 1024, 0))
        self.assertEqual(procfs.parse_smaps_rollup(b""), {})

    def test_limits(self):
        data = (b"Limit                     Soft Limit           Hard Limit           Units     \n"
                b"Max cpu time              unlimited            unlimited            seconds   \n"
                b"Max open files            1024                 524288               files     \n"
                b"Max nice priority         0                    0                    \n"
                b"nonsense\n")
        limits = procfs.parse_limits(data)
        self.assertEqual([limit.name for limit in limits], ["Max cpu time", "Max open files", "Max nice priority"])
        self.assertEqual(limits[1], ("Max open files", "1024", "524288", "files"))
        self.assertEqual(limits[2].units, "")

    def test_cgroup(self):
        self.assertEqual(procfs.parse_cgroup(b"0::/user.slice/user-1000.slice/session-3.scope\n"),
                         "/user.slice/user-1000.slice/session-3.scope")
        legacy = b"12:pids:/x\n1:name=systemd:/system.slice/cron.service\n"
        self.assertEqual(procfs.parse_cgroup(legacy), "/system.slice/cron.service")
        self.assertEqual(procfs.parse_cgroup(legacy + b"0::/unified\n"), "/unified")
        self.assertEqual(procfs.parse_cgroup(b"0::/a:b\n"), "/a:b")
        self.assertEqual(procfs.parse_cgroup(b"garbage"), "")


class SystemStatTest(unittest.TestCase):
    def test_current_kernel(self):
        data = (b"cpu  100 10 50 800 20 5 5 10 0 0\ncpu0 60 5 25 400 10 3 2 5 0 0\ncpu1 40 5 25 400 10 2 3 5 0 0\n"
                b"intr 5 1 2\nctxt 9\nbtime 1700000000\nprocesses 77\nprocs_running 3\nprocs_blocked 1\n")
        stat = procfs.parse_proc_stat(data)
        self.assertEqual(stat.total.total, 1000)
        self.assertEqual(stat.total.busy, 180)
        self.assertEqual(stat.cpus, {0: (510, 100), 1: (490, 80)})
        self.assertEqual((stat.count, stat.boot_time, stat.running, stat.blocked), (2, 1700000000, 3, 1))
        quick = procfs.parse_proc_stat(data, per_cpu=False)
        self.assertEqual((quick.total, quick.count, quick.boot_time, quick.running, quick.blocked),
                         (stat.total, 2, 1700000000, 3, 1))

    def test_old_kernels_with_fewer_columns(self):
        stat = procfs.parse_proc_stat(b"cpu  10 0 5 85\ncpu0 10 0 5 85\n")
        self.assertEqual((stat.total.total, stat.total.busy, stat.total.iowait), (100, 15, 0))
        stat = procfs.parse_proc_stat(b"cpu 1 2 3 4 5 6 7\n")
        self.assertEqual(stat.total.total, 28)
        self.assertIsNone(procfs.parse_proc_stat(b"cpu 1 2 3\n"))

    def test_guest_time_is_not_counted_twice(self):
        stat = procfs.parse_proc_stat(b"cpu 100 0 0 100 0 0 0 0 60 0\n")
        self.assertEqual(stat.total.total, 200)

    def test_4096_cpus(self):
        lines = ["cpu  " + " ".join(["4096000"] * 10)]
        lines += [f"cpu{n} 1000 1000 1000 1000 1000 1000 1000 1000 0 0" for n in range(4096)]
        lines.append("intr " + " ".join("7" for _ in range(20000)))
        lines.append("btime 5")
        stat = procfs.parse_proc_stat("\n".join(lines).encode())
        self.assertEqual(len(stat.cpus), 4096)
        self.assertEqual(stat.cpus[4095], (8000, 6000))
        self.assertEqual((stat.count, stat.boot_time), (4096, 5))
        quick = procfs.parse_proc_stat("\n".join(lines).encode(), per_cpu=False)
        self.assertEqual((quick.count, quick.boot_time, quick.cpus, quick.total), (4096, 5, {}, stat.total))

    def test_offline_cpus_and_missing_total(self):
        stat = procfs.parse_proc_stat(b"cpu0 1 1 1 1\ncpu2 2 2 2 2\ncpuX 9 9 9 9\ncpu-1 9 9 9 9\n")
        self.assertEqual(sorted(stat.cpus), [0, 2])
        self.assertEqual(stat.total.total, 12)

    def test_not_a_stat_file(self):
        for data in (b"", b"hello\n", b"cpu a b c d\n", b"\0" * 100):
            self.assertIsNone(procfs.parse_proc_stat(data))


class MemoryAndLoadTest(unittest.TestCase):
    def test_meminfo(self):
        info = procfs.parse_meminfo(b"MemTotal:  16000 kB\nHugePages_Total:   3\nBad line\nX: y kB\nEmpty:\n")
        self.assertEqual(info, {"MemTotal": 16000 * 1024, "HugePages_Total": 3})

    def test_loadavg(self):
        load = procfs.parse_loadavg(b"0.52 1.48 2.40 2/345 4242\n")
        self.assertEqual((load.one, load.five, load.fifteen, load.running, load.entities, load.last_pid),
                         (0.52, 1.48, 2.4, 2, 345, 4242))
        self.assertIsNone(procfs.parse_loadavg(b"0.5 0.4\n"))
        self.assertIsNone(procfs.parse_loadavg(b"a b c d/e f\n"))

    def test_uptime(self):
        self.assertEqual(procfs.parse_uptime(b"12345.67 40000.00\n"), 12345.67)
        self.assertIsNone(procfs.parse_uptime(b""))


class CpuInfoTest(unittest.TestCase):
    def test_x86(self):
        text = ""
        for number in range(8):
            text += (f"processor\t: {number}\nvendor_id\t: GenuineIntel\nmodel name\t: Intel(R) Core(TM) i7 @ 2.80GHz\n"
                     f"cpu MHz\t\t: {2000 + number}.5\nphysical id\t: {number // 4}\ncore id\t\t: {number % 2}\n"
                     "flags\t\t: fpu vmx hypervisor\n\n")
        info = procfs.parse_cpuinfo(text.encode())
        self.assertEqual((info.model, info.vendor, info.logical), ("Intel(R) Core(TM) i7 @ 2.80GHz", "GenuineIntel", 8))
        self.assertEqual((info.sockets, info.cores), (2, 4))
        self.assertEqual(len(info.mhz), 8)
        self.assertIn("vmx", info.flags)

    def test_arm_without_a_model_name(self):
        text = ("processor\t: 0\nBogoMIPS\t: 108.00\nFeatures\t: fp asimd\nCPU part\t: 0xd0b\n\n"
                "processor\t: 1\nBogoMIPS\t: 108.00\nFeatures\t: fp asimd\n\nHardware\t: BCM2835\nModel\t: Raspberry Pi 4\n")
        info = procfs.parse_cpuinfo(text.encode())
        self.assertEqual((info.logical, info.model, info.sockets, info.cores), (2, "BCM2835", 0, 0))
        self.assertEqual(info.mhz, ())
        self.assertIn("asimd", info.flags)

    def test_empty_and_hostile(self):
        info = procfs.parse_cpuinfo(b"")
        self.assertEqual((info.logical, info.model), (0, ""))
        info = procfs.parse_cpuinfo("processor: 0\nmodel name: evil‮name\nline\n".encode())
        self.assertEqual(info.model, "evil�name")


class DiskAndMountTest(unittest.TestCase):
    def test_diskstats_of_three_kernel_generations(self):
        data = (b"   8       0 sda 100 5 2000 300 50 2 1000 400 0 250 700\n"
                b"   8       1 sda1 10 0 200 30 5 0 100 40 0 25 70 0 0 0 0\n"
                b" 259       0 nvme0n1 7 0 56 3 9 0 72 4 0 5 7 1 0 8 0 2 1\n"
                b"   8      16 hdb1 100 200 300 400\n"
                b"garbage line\n"
                b"   8 0 bad a b c d e f g h i j k\n")
        stats = procfs.parse_diskstats(data)
        self.assertEqual(sorted(stats), ["nvme0n1", "sda", "sda1"])
        self.assertEqual(stats["sda"], (100, 2000, 300, 50, 1000, 400, 250))
        self.assertEqual(stats["nvme0n1"].busy_ms, 5)

    def test_mountinfo(self):
        data = (b"36 35 98:0 /mnt1 /mnt2 rw,noatime master:1 - ext3 /dev/root rw,errors=continue\n"
                b"40 35 8:2 / /home/My\\040Files\\134x rw shared:1 master:2 - ext4 /dev/sda2 rw\n"
                b"41 35 0:45 / /run rw - tmpfs tmpfs rw\n"
                b"broken line without separator\n"
                b"42 35 8:3 /\n")
        mounts = procfs.parse_mountinfo(data)
        self.assertEqual(len(mounts), 3)
        self.assertEqual(mounts[0], ("98:0", "/mnt1", "/mnt2", "ext3", "/dev/root"))
        self.assertEqual(mounts[1].mount_point, "/home/My Files\\x")
        self.assertEqual(mounts[2].fstype, "tmpfs")

    def test_swaps(self):
        data = b"Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n/dev/sda3 partition 100 0 -2\n/swap\\040file file 5 0 -3\n"
        self.assertEqual(procfs.parse_swaps(data), ["/dev/sda3", "/swap file"])
        self.assertEqual(procfs.parse_swaps(b""), [])


class NetworkTest(unittest.TestCase):
    def test_net_dev(self):
        data = (b"Inter-|   Receive  |  Transmit\n face |bytes packets|bytes packets\n"
                b"    lo: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
                b"eth0.100:5000 50 0 0 0 0 0 0 7000 70 0 0 0 0 0 0\n"
                b" wlp2s0: 1 2 3\n"
                b"  bad: a b c d e f g h i j k l m n o p\n")
        devices = procfs.parse_net_dev(data)
        self.assertEqual(sorted(devices), ["eth0.100", "lo"])
        self.assertEqual(devices["eth0.100"], (5000, 50, 7000, 70))

    def test_if_inet6(self):
        data = (b"fe800000000000000000000000000001 02 40 20 80     eth0\n"
                b"20010db8000000000000000000000001 02 40 00 00     eth0\n"
                b"00000000000000000000000000000001 01 80 10 80       lo\n"
                b"zz 02 40 20 80 eth1\nshort\n")
        addresses = procfs.parse_if_inet6(data)
        self.assertEqual(addresses["eth0"], [("fe80::1", 64, 0x20), ("2001:db8::1", 64, 0)])
        self.assertEqual(addresses["lo"], [("::1", 128, 0x10)])
        self.assertNotIn("eth1", addresses)


class CounterTest(unittest.TestCase):
    def test_delta(self):
        self.assertEqual(procfs.delta(150, 100), 50)
        self.assertEqual(procfs.delta(100, 100), 0)

    def test_32_bit_counter_wraps(self):
        self.assertEqual(procfs.delta(10, 2 ** 32 - 5, 32), 15)
        self.assertEqual(procfs.delta(10, 2 ** 32 - 5), 15)           # a 32-bit kernel behind a 64-bit reader

    def test_counter_reset_is_not_a_wrap(self):
        self.assertEqual(procfs.delta(10, 5000, 32), 0)                # the device went away and came back
        self.assertEqual(procfs.delta(10, 2 ** 40), 0)
        self.assertEqual(procfs.delta(2 ** 31, 2 ** 32 - 5, 32), 0)

    def test_tty_names(self):
        self.assertEqual(procfs.tty_name(0), "")
        self.assertEqual(procfs.tty_name((136 << 8) | 3), "pts/3")
        self.assertEqual(procfs.tty_name((4 << 8) | 2), "tty2")
        self.assertEqual(procfs.tty_name((4 << 8) | 64), "ttyS0")
        self.assertEqual(procfs.tty_name((137 << 8) | 1), "pts/257")


if __name__ == "__main__":
    unittest.main()
