"""The command line of boswas-archive-manager, the launcher, the desktop entry and the manual page."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

import support
from boswas_apps.archive_manager import __main__ as cli
from boswas_apps.archive_manager import formats
from support import raw_zip, zip_member

PACKAGE = support.HERE.parents[1]


def planned(*arguments):
    return cli.plan(list(arguments))[0]


class PlanTest(unittest.TestCase):
    def test_browse(self):
        plan = planned()
        self.assertEqual((plan.mode, plan.archives, plan.tasks), ("browse", [], []))
        plan = planned("a.zip", "/x/b.tar")
        self.assertEqual(plan.archives, [os.path.abspath("a.zip"), "/x/b.tar"])

    def test_extract_goes_to_a_folder_named_after_the_archive(self):
        plan = planned("--extract", "/data/photos.tar.gz", "/data/My Files.zip")
        self.assertEqual(plan.mode, "progress")
        self.assertEqual(plan.tasks, [("extract", "/data/photos.tar.gz", "/data/photos", None),
                                      ("extract", "/data/My Files.zip", "/data/My Files", None)])

    def test_extract_to(self):
        plan = planned("--extract", "/data/a.zip", "--to", "/target/dir")
        self.assertEqual(plan.tasks, [("extract", "/data/a.zip", "/target/dir", None)])
        plan = planned("--to=rel", "--extract", "/data/a.zip")
        self.assertEqual(plan.tasks[0][2], os.path.abspath("rel"))

    def test_extract_here(self):
        plan = planned("--extract-here", "/data/sub/a.zip")
        self.assertEqual(plan.tasks, [("extract", "/data/sub/a.zip", "/data/sub", None)])

    def test_create(self):
        plan = planned("--create", "/out/pack.tar.xz", "/data/a", "b.txt")
        self.assertEqual(plan.tasks, [("create", "/out/pack.tar.xz", None, ["/data/a", os.path.abspath("b.txt")])])

    def test_names_that_look_like_options_after_a_separator(self):
        plan = planned("--extract", "--", "-odd-name.zip")
        self.assertEqual(plan.tasks[0][1], os.path.abspath("-odd-name.zip"))

    def test_usage_errors(self):
        for arguments in (["--extract"], ["--extract-here"], ["--create", "/out/a.zip"], ["--create", "/out/a.7z", "x"],
                          ["--extract", "a.zip", "--extract-here"], ["--to", "/x", "a.zip"],
                          ["--extract-here", "a.zip", "--to", "/x"], ["--no-such-option"]):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as caught, \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                cli.plan(arguments)
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("boswas-archive-manager", errors.getvalue())

    def test_every_task_has_absolute_paths(self):
        """What the progress window is given never depends on the working directory later on."""
        for arguments in (["--extract", "rel.zip"], ["--extract", "rel.zip", "--to", "out"], ["--extract-here", "rel.zip"],
                          ["--extract-here", "sub/rel.tar.gz"], ["--create", "new.zip", "a", "b/c"]):
            for _kind, path, target, sources in planned(*arguments).tasks:
                self.assertTrue(os.path.isabs(path), arguments)
                self.assertTrue(target is None or os.path.isabs(target), arguments)
                self.assertTrue(all(os.path.isabs(source) for source in sources or []), arguments)
        # "Extract here" means the folder of the archive, not the folder the program was started in.
        self.assertEqual(planned("--extract-here", "/data/sub/a.zip").tasks[0][2], "/data/sub")

    def test_qt_options_are_left_for_qt(self):
        plan, rest = cli.plan(["-platform", "offscreen", "a.zip"])
        self.assertEqual(rest, ["-platform"])                  # its value looks like a path; Qt takes both itself
        self.assertEqual(plan.mode, "browse")

    def test_version_and_help_need_no_display(self):
        for option, expected in (("--version", "boswas-archive-manager 1."), ("--help", "--extract-here")):
            output = io.StringIO()
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(output):
                cli.main(["boswas-archive-manager", option])
            self.assertEqual(caught.exception.code, 0)
            self.assertIn(expected, output.getvalue())

    @unittest.skipUnless(os.geteuid() == 0 and support.PYSIDE, "only root is refused")
    def test_root_is_refused(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(cli.main(["boswas-archive-manager"]), 4)
        self.assertIn("never as root", errors.getvalue())


class PackagingTest(unittest.TestCase):
    def test_launcher(self):
        text = (PACKAGE / "bin" / "boswas-archive-manager").read_text()
        self.assertEqual(text.splitlines()[0], "#!/usr/bin/python3 -IB")
        self.assertIn('sys.path.insert(0, "/usr/lib/boswas/python")', text)
        self.assertIn("from boswas_apps.archive_manager.__main__ import main", text)
        self.assertTrue(text.rstrip().endswith("sys.exit(main())"))

    def test_desktop_entry(self):
        lines = (PACKAGE / "data" / "archive-manager" / "com.boswas.ArchiveManager.desktop").read_text().splitlines()
        entry = dict(line.split("=", 1) for line in lines[1:lines.index("")] if "=" in line)
        self.assertEqual(entry["Name"], "Archive Manager")
        self.assertEqual(entry["Exec"], "boswas-archive-manager %F")
        self.assertEqual(entry["Icon"], "utilities-file-archiver")
        self.assertEqual(entry["StartupWMClass"], "com.boswas.ArchiveManager")
        mime = entry["MimeType"].rstrip(";").split(";")
        for wanted in ("application/zip", "application/x-tar", "application/gzip", "application/x-compressed-tar",
                       "application/x-bzip-compressed-tar", "application/x-xz-compressed-tar", "application/x-bzip2",
                       "application/x-xz", "application/zstd", "application/x-zstd-compressed-tar",
                       "application/java-archive", "application/vnd.android.package-archive", "application/epub+zip"):
            self.assertIn(wanted, mime)
        self.assertTrue(entry["MimeType"].endswith(";"))
        icons = PACKAGE.parents[1] / "desktop" / "icons" / "Boswas-Prism" / "scalable" / "apps"
        self.assertTrue((icons / (entry["Icon"] + ".svg")).is_file())
        self.assertIn("[Desktop Action NewWindow]", lines)

    def test_manual_page(self):
        text = (PACKAGE / "man" / "boswas-archive-manager.1").read_text()
        for wanted in (".TH BOSWAS-ARCHIVE-MANAGER 1", ".SH NAME", ".SH SYNOPSIS", ".SH DESCRIPTION", ".SH OPTIONS",
                       ".SH FILES", ".SH SEE ALSO", "\\-\\-extract\\-here", "\\-\\-create", "\\-\\-to"):
            self.assertIn(wanted, text)

    def test_every_creatable_format_has_an_extension_the_command_line_knows(self):
        for key in formats.CREATABLE:
            self.assertEqual(formats.format_for_new("x" + formats.FORMATS[key].extension).key, key)


@unittest.skipUnless(support.PYSIDE and os.geteuid() != 0, "needs PySide6 and a user who is not root")
class ProgramTest(support.EngineCase):
    """The real program, started as a process, with only its progress window (offscreen)."""

    def run_program(self, *arguments) -> subprocess.CompletedProcess:
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", HOME=str(self.tmp / "home"),
                           XDG_RUNTIME_DIR=str(self.tmp / "run"), PYTHONPATH=os.pathsep.join(sys.path[:2]))
        environment.pop("DBUS_SESSION_BUS_ADDRESS", None)
        for name in ("home", "run"):
            (self.tmp / name).mkdir(mode=0o700, exist_ok=True)
        return subprocess.run([sys.executable, "-B", "-m", "boswas_apps.archive_manager", *arguments],
                              env=environment, capture_output=True, text=True, timeout=60, cwd=str(self.tmp))

    def test_extract_extract_here_and_create(self):
        work = self.tmp / "work"
        work.mkdir()
        archive = raw_zip(work / "pack.zip", [zip_member("a.txt", b"alpha"), zip_member("d/b.txt", b"beta")])
        done = self.run_program("--extract", str(archive))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((work / "pack" / "d" / "b.txt").read_bytes(), b"beta")
        done = self.run_program("--extract", str(archive), "--to", str(self.tmp / "elsewhere"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.tmp / "elsewhere" / "a.txt").read_bytes(), b"alpha")
        done = self.run_program("--extract-here", str(archive))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((work / "a.txt").read_bytes(), b"alpha")
        done = self.run_program("--create", str(self.tmp / "new.tar.gz"), str(work / "d"), str(work / "a.txt"))
        self.assertEqual(done.returncode, 0, done.stderr)
        import tarfile
        with tarfile.open(self.tmp / "new.tar.gz") as packed:
            self.assertEqual(sorted(packed.getnames()), ["a.txt", "d", "d/b.txt"])


if __name__ == "__main__":
    unittest.main()
