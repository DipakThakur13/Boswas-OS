"""A real pseudo-terminal: programs are started, read, written to, resized and reaped."""

import os
import select
import signal
import tempfile
import time
import unittest

from boswas_apps.terminal import ptyhost
from boswas_apps.terminal.emulator import Terminal
from boswas_apps.terminal.ptyhost import PtyProcess, SpawnError


def read_until(process, condition, timeout=10.0):
    """Read from the child until ``condition(bytes so far)`` holds or the terminal hangs up."""
    data = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition(data):
        if process.master < 0:
            break
        ready, _w, _x = select.select([process.master], [], [], 0.05)
        if ready:
            chunk = process.read()
            if chunk is None:
                break
            data += chunk
    return data


def wait_exit(process, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = process.poll()
        if status is not None:
            return status
        time.sleep(0.01)
    return None


def is_zombie(pid):
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return False


class RoundTrip(unittest.TestCase):
    def setUp(self):
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            process.hangup()
            if wait_exit(process, 2) is None:
                process.kill()
                wait_exit(process, 5)

    def spawn(self, argv, **options):
        process = PtyProcess(argv, **options)
        self.processes.append(process)
        return process

    def test_output_and_exit_status(self):
        process = self.spawn(["/bin/sh", "-c", "printf hello; exit 3"])
        data = read_until(process, lambda seen: False, 5)
        self.assertEqual(data, b"hello")
        self.assertEqual(wait_exit(process), 3)
        self.assertEqual(process.exit_signal, 0)
        self.assertFalse(os.path.exists(f"/proc/{process.pid}"), "the child was not reaped")

    def test_through_the_emulator(self):
        process = self.spawn(["/bin/sh", "-c", "printf '\\033[1;31mred\\033[0m\\r\\nplain'"], cols=20, rows=4)
        term = Terminal(20, 4)
        term.feed(read_until(process, lambda seen: False, 5))
        self.assertEqual(term.screen_text()[:2], ["red", "plain"])

    def test_cat_echoes(self):
        process = self.spawn(["/bin/cat"])
        process.write("héllo\n".encode())
        data = read_until(process, lambda seen: seen.count(b"h\xc3\xa9llo") >= 2)
        # once echoed by the terminal's line discipline, once written by cat
        self.assertEqual(data.count("héllo".encode()), 2, data)
        process.write(b"\x04")
        self.assertEqual(wait_exit(process), 0)

    def test_resize_reaches_the_program(self):
        process = self.spawn(["/bin/sh"], cols=80, rows=24, environ=ptyhost.child_environment({"PS1": "$ ", "PATH": os.defpath}))
        process.write(b"stty size\n")
        data = read_until(process, lambda seen: b"24 80" in seen)
        self.assertIn(b"24 80", data)
        process.resize(132, 43, 1188, 774)
        process.write(b"stty size\n")
        data = read_until(process, lambda seen: b"43 132" in seen)
        self.assertIn(b"43 132", data)

    def test_window_change_signal(self):
        script = "trap 'echo WINCH; exit 0' WINCH; echo ready; while :; do sleep 0.05; done"
        process = self.spawn(["/bin/sh", "-c", script])
        read_until(process, lambda seen: b"ready" in seen)
        process.resize(100, 30)
        data = read_until(process, lambda seen: b"WINCH" in seen)
        self.assertIn(b"WINCH", data)
        self.assertEqual(wait_exit(process), 0)

    def test_controlling_terminal_and_session(self):
        process = self.spawn(["/bin/sh", "-c", "tty; ps -o sid= -o pgid= -o pid= -p $$; echo done"])
        data = read_until(process, lambda seen: b"done" in seen).decode()
        self.assertIn("/dev/pts/", data)
        numbers = [int(word) for word in data.split("\n")[1].split()]
        self.assertEqual(numbers, [process.pid] * 3, data)

    def test_environment(self):
        os.environ["WAYLAND_SOCKET"] = "7"
        os.environ["BOSWAS_COMPOSITOR_FD"] = "8"
        try:
            process = self.spawn(["/usr/bin/env"])
        finally:
            del os.environ["WAYLAND_SOCKET"], os.environ["BOSWAS_COMPOSITOR_FD"]
        data = read_until(process, lambda seen: False, 5).decode()
        variables = dict(line.split("=", 1) for line in data.replace("\r", "").splitlines() if "=" in line)
        self.assertEqual(variables.get("TERM"), "xterm-256color")
        self.assertEqual(variables.get("COLORTERM"), "truecolor")
        self.assertEqual(variables.get("TERM_PROGRAM"), "boswas-terminal")
        self.assertNotIn("WAYLAND_SOCKET", variables)
        self.assertNotIn("BOSWAS_COMPOSITOR_FD", variables)
        self.assertNotIn("COLUMNS", variables)

    def test_no_descriptors_leak(self):
        extra = os.open("/dev/null", os.O_RDONLY)
        os.set_inheritable(extra, True)
        try:
            process = self.spawn(["/bin/sh", "-c", "ls /proc/$$/fd | tr '\\n' ' '; echo end"])
            data = read_until(process, lambda seen: b"end" in seen).decode()
        finally:
            os.close(extra)
        self.assertEqual(data.split("end")[0].split(), ["0", "1", "2"])

    def test_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            real = os.path.realpath(directory)
            process = self.spawn(["/bin/sh", "-c", "pwd; read line"], cwd=real)
            data = read_until(process, lambda seen: b"\n" in seen)
            self.assertEqual(data.decode().strip(), real)
            self.assertEqual(process.working_directory(), real)
            process.write(b"\n")
            wait_exit(process)

    def test_missing_directory_falls_back(self):
        process = self.spawn(["/bin/sh", "-c", "pwd"], cwd="/nonexistent/place")
        data = read_until(process, lambda seen: False, 5).decode().strip()
        self.assertEqual(data, os.path.realpath(ptyhost.home_directory()))

    def test_foreground_process(self):
        process = self.spawn(["/bin/sh"], environ=ptyhost.child_environment({"PS1": "$ ", "PATH": os.defpath}))
        read_until(process, lambda seen: b"$ " in seen)
        self.assertIsNone(process.foreground_process())
        process.write(b"sleep 30\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.foreground_process() is None:
            time.sleep(0.02)
        self.assertEqual(process.foreground_process(), "sleep")
        process.write(b"\x03")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.foreground_process() is not None:
            time.sleep(0.02)
        self.assertIsNone(process.foreground_process())

    def test_interrupt_from_the_keyboard(self):
        process = self.spawn(["/bin/sleep", "30"])
        time.sleep(0.1)
        process.write(b"\x03")
        self.assertEqual(wait_exit(process), 128 + signal.SIGINT)
        self.assertEqual(process.exit_signal, signal.SIGINT)

    def test_ignored_signals_are_not_inherited(self):
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            process = self.spawn(["/bin/sleep", "30"])
        finally:
            signal.signal(signal.SIGINT, previous)
        time.sleep(0.1)
        process.write(b"\x03")
        self.assertEqual(wait_exit(process), 128 + signal.SIGINT)

    def test_hangup_ends_the_child_and_leaves_no_zombie(self):
        process = self.spawn(["/bin/sh", "-c", "sleep 30"])
        time.sleep(0.1)
        pid = process.pid
        process.hangup()
        self.assertEqual(wait_exit(process), 128 + signal.SIGHUP)
        self.assertFalse(is_zombie(pid))
        self.assertFalse(os.path.exists(f"/proc/{pid}"))
        self.assertIsNone(process.read())
        self.assertEqual(process.write(b"x"), 0)

    def test_large_output_is_complete(self):
        process = self.spawn(["/bin/sh", "-c", "seq 1 20000; echo END"])
        data = read_until(process, lambda seen: b"END" in seen, 30)
        lines = data.replace(b"\r", b"").split(b"\n")
        self.assertEqual(lines[:3], [b"1", b"2", b"3"])
        self.assertEqual(lines[19999], b"20000")

    def test_write_never_blocks(self):
        process = self.spawn(["/bin/sleep", "30"])
        started = time.monotonic()
        sent = 0
        for _ in range(200):
            sent += process.write(b"x" * 4096)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertLess(sent, 200 * 4096)


class Starting(unittest.TestCase):
    def test_missing_program(self):
        with self.assertRaises(SpawnError) as caught:
            PtyProcess(["/nonexistent/program"])
        self.assertIn("not found", str(caught.exception))
        with self.assertRaises(SpawnError):
            PtyProcess(["boswas-no-such-program-anywhere"])
        with self.assertRaises(SpawnError):
            PtyProcess([])
        with self.assertRaises(SpawnError):
            PtyProcess(["/tmp"])

    def test_program_that_may_not_run(self):
        with tempfile.NamedTemporaryFile() as handle:
            with self.assertRaises(SpawnError):
                PtyProcess([handle.name])

    def test_failed_start_leaks_no_descriptor(self):
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            with self.assertRaises(SpawnError):
                PtyProcess(["/nonexistent/program"])
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_resolve_program(self):
        self.assertEqual(ptyhost.resolve_program("sh", "/", {"PATH": "/bin:/usr/bin"}), "/bin/sh")
        self.assertEqual(ptyhost.resolve_program("./sh", "/bin", {}), "/bin/sh")
        with self.assertRaises(SpawnError):
            ptyhost.resolve_program("sh", "/", {"PATH": "/nonexistent"})

    def test_shell_command(self):
        with tempfile.NamedTemporaryFile() as rcfile:
            self.assertEqual(ptyhost.shell_command("/usr/bin/bash", rcfile.name),
                             ("/usr/bin/bash", ["bash", "--rcfile", rcfile.name]))
        self.assertEqual(ptyhost.shell_command("/usr/bin/bash", "/nonexistent/bashrc"), ("/usr/bin/bash", ["-bash"]))
        self.assertEqual(ptyhost.shell_command("/usr/bin/zsh"), ("/usr/bin/zsh", ["-zsh"]))

    def test_default_shell(self):
        shell = ptyhost.default_shell({})
        self.assertTrue(shell.startswith("/") and os.access(shell, os.X_OK))

    def test_split_command(self):
        self.assertEqual(ptyhost.split_command("vim -u NONE 'my file.txt'"), ["vim", "-u", "NONE", "my file.txt"])
        self.assertEqual(ptyhost.split_command("echo $(rm -rf /); ls"), ["echo", "$(rm", "-rf", "/);", "ls"])
        with self.assertRaises(SpawnError):
            ptyhost.split_command("unbalanced 'quote")

    def test_child_environment(self):
        environ = ptyhost.child_environment({"WAYLAND_SOCKET": "5", "BOSWAS_COMPOSITOR_FD": "6", "HOME": "/h",
                                             "TERM": "dumb", "LINES": "3", "NOTIFY_SOCKET": "/x"}, "/bin/zsh")
        self.assertEqual(environ, {"HOME": "/h", "TERM": "xterm-256color", "COLORTERM": "truecolor",
                                   "TERM_PROGRAM": "boswas-terminal", "TERM_PROGRAM_VERSION": "1.0",
                                   "SHELL": "/bin/zsh"})


if __name__ == "__main__":
    unittest.main()
