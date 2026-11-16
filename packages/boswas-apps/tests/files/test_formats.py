"""Clipboard formats, desktop entries and Exec, mimeapps.list, thumbnail names."""

import hashlib
import os
import struct
import unittest
import zlib

from support import TempCase

from boswas_apps.files import clipformats as clip
from boswas_apps.files import desktopapps as apps
from boswas_apps.files import thumbs


class ClipboardTest(unittest.TestCase):
    PATHS = ["/home/me/plain.txt", "/home/me/with space & #hash?.txt", "/tmp/new\nline", "/tmp/ü/名前",
             "/tmp/100%/a'b\"c", "/tmp/-rf"]

    def test_uri_escaping_matches_glib(self):
        self.assertEqual(clip.path_to_uri("/a b/c#d?e%f"), "file:///a%20b/c%23d%3Fe%25f")
        self.assertEqual(clip.path_to_uri("/x/!$&'()*+,-.:=@_~"), "file:///x/!$&'()*+,-.:=@_~")
        self.assertEqual(clip.path_to_uri("/x/;[]{}|\\^`<>\""), "file:///x/%3B%5B%5D%7B%7D%7C%5C%5E%60%3C%3E%22")
        self.assertEqual(clip.path_to_uri("/ü"), "file:///%C3%BC")
        self.assertEqual(clip.path_to_uri("/bad\udcff"), "file:///bad%FF")

    def test_round_trip_of_every_format(self):
        for cut in (False, True):
            formats = clip.encode(self.PATHS, cut)
            self.assertEqual(clip.decode(formats), (self.PATHS, cut))
            self.assertEqual(clip.decode({clip.URI_LIST: formats[clip.URI_LIST]}), (self.PATHS, False))
            self.assertEqual(clip.decode({clip.GNOME_COPIED: formats[clip.GNOME_COPIED]}), (self.PATHS, cut))
            self.assertEqual(clip.decode({clip.URI_LIST: formats[clip.URI_LIST], clip.KDE_CUT: formats[clip.KDE_CUT]}),
                             (self.PATHS, cut))
        self.assertEqual(formats[clip.KDE_CUT], b"1")
        self.assertTrue(formats[clip.URI_LIST].endswith(b"\r\n"))
        self.assertTrue(formats[clip.GNOME_COPIED].startswith(b"cut\nfile:///home/me/plain.txt\n"))
        self.assertFalse(formats[clip.GNOME_COPIED].endswith(b"\n"))
        self.assertEqual(clip.encode(["/a"])[clip.KDE_CUT], b"0")

    def test_what_other_file_managers_send(self):
        dolphin = {clip.URI_LIST: b"file:///home/me/a%20b\r\nfile:///home/me/c\r\n", clip.KDE_CUT: b"1"}
        self.assertEqual(clip.decode(dolphin), (["/home/me/a b", "/home/me/c"], True))
        nautilus = {clip.GNOME_COPIED: b"copy\nfile:///home/me/a%20b\nfile:///home/me/c",
                    clip.PLAIN: b"/home/me/a b\n/home/me/c"}
        self.assertEqual(clip.decode(nautilus), (["/home/me/a b", "/home/me/c"], False))
        old_nautilus = {clip.PLAIN: b"x-special/nautilus-clipboard\ncut\nfile:///home/me/a\n"}
        self.assertEqual(clip.decode(old_nautilus), (["/home/me/a"], True))
        self.assertEqual(clip.decode({clip.PLAIN: b"/just/some/text"}), ([], False))
        self.assertEqual(clip.decode({}), ([], False))

    def test_uri_list_details(self):
        data = (b"# a comment\r\n\r\nfile:///one\r\nFILE://localhost/two\nhttp://example.org/x\n"
                b"file://server/share/three\nsmb://host/x\nfile:relative\nfile:///a/../b\nfile:///nul%00byte\n"
                b"  file:///padded  \nnot a uri\n")
        self.assertEqual(clip.decode_uri_list(data), ["/one", "/two", "/b", "/padded"])
        self.assertEqual(clip.decode_uri_list(b"\xff\xfe\0\0garbage"), [])
        self.assertIsNone(clip.uri_to_path("file://[bad"))

    def test_gnome_details(self):
        self.assertIsNone(clip.decode_gnome(b"file:///x"))
        self.assertIsNone(clip.decode_gnome(b""))
        self.assertEqual(clip.decode_gnome(b"cut"), (True, []))
        self.assertEqual(clip.decode_gnome(b"copy\r\nfile:///x\r\n"), (False, ["/x"]))

    def test_copy_as_path(self):
        self.assertEqual(clip.copy_as_path(["/a b", "/c"]), "/a b\n/c")


class ExecTest(unittest.TestCase):
    def app(self, exec_line, **kwargs):
        return apps.DesktopApp("x.desktop", "/usr/share/applications/x.desktop", "X Viewer", exec_line, **kwargs)

    def test_split(self):
        split = lambda line: [token for token, _quoted in apps.split_exec(line)]       # noqa: E731
        self.assertEqual(split("prog  --flag\targ"), ["prog", "--flag", "arg"])
        self.assertEqual(split('prog "two words" plain'), ["prog", "two words", "plain"])
        self.assertEqual(split(r'prog "a \"quoted\" \$HOME \`id\` back\\slash"'),
                         ["prog", 'a "quoted" $HOME `id` back\\slash'])
        self.assertEqual(split(r'prog "keeps \n and \x"'), ["prog", r"keeps \n and \x"])
        self.assertEqual(split("sh -c 'echo \"hi\" $1'"), ["sh", "-c", 'echo "hi" $1'])
        self.assertEqual(split('pre"mid dle"post'), ["premid dlepost"])
        self.assertEqual(split('prog ""'), ["prog", ""])
        self.assertEqual(split(r"prog a\ b"), ["prog", "a b"])
        self.assertEqual(split("   "), [])
        self.assertEqual(apps.split_exec('prog "%f" %f'), [("prog", False), ("%f", True), ("%f", False)])
        for bad in ('prog "open', "prog 'open"):
            with self.assertRaises(apps.ExecError):
                apps.split_exec(bad)

    def test_field_codes(self):
        files = ["/a b/one.txt", "/two;rm -rf ~"]
        expand = lambda line, paths=files, **kw: apps.expand_exec(self.app(line, **kw), paths)      # noqa: E731
        self.assertEqual(expand("view %F"), [["view", *files]])
        self.assertEqual(expand("view %U"), [["view", "file:///a%20b/one.txt", "file:///two%3Brm%20-rf%20~"]])
        self.assertEqual(expand("view %f"), [["view", files[0]], ["view", files[1]]])
        self.assertEqual(expand("view %u"), [["view", "file:///a%20b/one.txt"], ["view", "file:///two%3Brm%20-rf%20~"]])
        self.assertEqual(expand("view --file=%f"), [["view", "--file=" + files[0]], ["view", "--file=" + files[1]]])
        self.assertEqual(expand("view"), [["view", files[0]], ["view", files[1]]])
        self.assertEqual(expand("view %F", []), [["view"]])
        self.assertEqual(expand("view %f --after", []), [["view", "--after"]])
        self.assertEqual(expand("view", []), [["view"]])
        self.assertEqual(expand("view %i %c %k %F", icon="x-icon"),
                         [["view", "--icon", "x-icon", "X Viewer", "/usr/share/applications/x.desktop", *files]])
        self.assertEqual(expand("view %i %F"), [["view", *files]])
        self.assertEqual(expand("view 100%% %d %D %n %N %v %m %z %F"), [["view", "100%", *files]])
        self.assertEqual(expand('view "%f" %F'), [["view", "%f", *files]])
        self.assertEqual(expand("env VAR=1 view %F"), [["env", "VAR=1", "view", *files]])

    def test_file_names_cannot_become_arguments_or_code(self):
        hostile = ["/tmp/--delete", "/tmp/a b", "/tmp/$(reboot)", "/tmp/`id`", "/tmp/x;y", "/tmp/new\nline", "/tmp/%f"]
        command = apps.expand_exec(self.app("view %F"), hostile)
        self.assertEqual(command, [["view", *hostile]])
        single = apps.expand_exec(self.app("view -o %f"), ["/tmp/%F %U"])
        self.assertEqual(single, [["view", "-o", "/tmp/%F %U"]])

    def test_errors(self):
        for bad in ("", "   ", '"" %f', "%f"):
            with self.assertRaises(apps.ExecError, msg=bad):
                apps.expand_exec(self.app(bad), [])
        with self.assertRaises(apps.ExecError):
            apps.expand_exec(self.app('view "unterminated'), ["/x"])

    def test_unescape_value(self):
        self.assertEqual(apps.unescape_value(r"a\sb\nc\td\re\\f\x"), "a b\nc\td\re\\f\\x")
        self.assertEqual(apps.unescape_value("plain"), "plain")
        self.assertEqual(apps.unescape_value("trailing\\"), "trailing\\")


class DesktopEntryTest(TempCase):
    def entry(self, relative, body, base="share/applications"):
        return self.write(os.path.join(base, relative), "[Desktop Entry]\n" + body)

    def index(self):
        return apps.AppIndex().load()

    def test_load_app(self):
        tool = self.write("bin/tool", "#!/bin/sh\n", mode=0o755)
        path = self.entry("org.example.Viewer.desktop", (
            "Type=Application\nName=Viewer\nName[de]=Betrachter\nComment=Shows things\nIcon=viewer\n"
            f"Exec={tool} --name \"A\\\\sB\" %F\nTryExec={tool}\nMimeType=image/png;image/jpeg;;\nTerminal=true\n"
            "NoDisplay=true\nPath=/work\\sdir\nActions=New;Broken;\n\n[Desktop Action New]\nName=New Window\n"
            "Exec=tool --new\n[Desktop Action Broken]\nName=No exec\n"))
        app = apps.load_app(path, "org.example.Viewer.desktop", [])
        self.assertEqual((app.name, app.icon, app.comment), ("Viewer", "viewer", "Shows things"))
        self.assertEqual(app.mime_types, ("image/png", "image/jpeg"))
        self.assertTrue(app.terminal and app.no_display)
        self.assertEqual(app.working_dir, "/work dir")
        self.assertEqual(app.actions, {"New": ("New Window", "tool --new")})
        self.assertEqual(apps.expand_exec(app, ["/x"]), [[tool, "--name", "A\\sB", "/x"]])
        self.assertEqual(apps.load_app(path, "v.desktop", ["de_DE", "de"]).name, "Betrachter")

    def test_entries_that_are_not_applications(self):
        self.assertIsNone(apps.load_app(self.entry("link.desktop", "Type=Link\nName=L\nURL=http://x\n"), "l"))
        self.assertIsNone(apps.load_app(self.entry("noexec.desktop", "Type=Application\nName=N\n"), "n"))
        self.assertIsNone(apps.load_app(self.entry("try.desktop", "Name=T\nExec=t\nTryExec=/no/such/program\n"), "t"))
        self.assertIsNone(apps.load_app(self.write("share/applications/empty.desktop", ""), "e"))
        self.assertIsNone(apps.load_app(self.write("share/applications/bin.desktop", os.urandom(4000)), "b"))
        self.assertIsNone(apps.load_app(self.path("missing.desktop"), "m"))
        hidden = apps.load_app(self.entry("hidden.desktop", "Name=H\nExec=h\nHidden=true\n"), "h")
        self.assertEqual(hidden.exec_line, "")

    def make_apps(self):
        self.entry("kate.desktop", "Name=Kate\nExec=kate %U\nMimeType=text/plain;text/x-python;\n")
        self.entry("gedit.desktop", "Name=Gedit\nExec=gedit %F\nMimeType=text/plain;\n")
        self.entry("viewer.desktop", "Name=Viewer\nExec=viewer %f\nMimeType=image/png;\nNoDisplay=true\n")
        self.entry("kde/okular.desktop", "Name=Okular\nExec=okular %U\nMimeType=application/pdf;image/png;\n")
        self.entry("plain.desktop", "Name=Plain Tool\nExec=plain\n")
        self.entry("gone.desktop", "Name=Gone\nExec=gone\nMimeType=text/plain;\nTryExec=/no/such\n")

    def test_index_and_ids(self):
        self.make_apps()
        index = self.index()
        self.assertEqual(sorted(index.apps), ["gedit.desktop", "kate.desktop", "kde-okular.desktop", "plain.desktop",
                                              "viewer.desktop"])
        self.assertEqual([app.name for app in index.visible_apps()], ["Gedit", "Kate", "Okular", "Plain Tool"])
        self.assertEqual([app.name for app in index.apps_for(["text/plain"])], ["Gedit", "Kate"])
        self.assertEqual([app.name for app in index.apps_for(["text/x-python", "text/plain"])], ["Kate", "Gedit"])
        self.assertEqual([app.name for app in index.apps_for(["image/png"])], ["Okular", "Viewer"])
        self.assertIsNone(index.default_for(["video/mp4"]))
        self.assertEqual(index.apps_for(["video/mp4"]), [])

    def test_user_entry_overrides_and_hides(self):
        self.make_apps()
        self.entry("kate.desktop", "Name=My Kate\nExec=mykate %F\nMimeType=text/plain;\n",
                   base="home/.local/share/applications")
        self.entry("gedit.desktop", "Name=Gedit\nExec=gedit\nHidden=true\n", base="home/.local/share/applications")
        index = self.index()
        self.assertEqual(index.apps["kate.desktop"].name, "My Kate")
        self.assertNotIn("gedit.desktop", index.apps)
        self.assertEqual([app.name for app in index.apps_for(["text/plain"])], ["My Kate"])

    def test_mimeapps_precedence(self):
        self.make_apps()
        self.write("share/applications/mimeapps.list",
                   "[Default Applications]\ntext/plain=gedit.desktop\nimage/png=viewer.desktop\n")
        self.write("xdg/mimeapps.list", "[Default Applications]\ntext/plain=missing.desktop;kate.desktop;\n"
                                        "[Added Associations]\nvideo/mp4=plain.desktop;\n"
                                        "[Removed Associations]\nimage/png=kde-okular.desktop;\n")
        index = self.index()
        self.assertEqual(index.default_for(["text/plain"]).name, "Kate")       # /etc/xdg before the data dirs
        self.assertEqual(index.default_for(["image/png"]).name, "Viewer")
        self.assertEqual([app.name for app in index.apps_for(["image/png"])], ["Viewer"])
        self.assertEqual(index.default_for(["video/mp4"]).name, "Plain Tool")
        self.write("home/.config/mimeapps.list", "[Default Applications]\ntext/plain=gedit.desktop\n")
        self.assertEqual(self.index().default_for(["text/plain"]).name, "Gedit")
        os.environ["XDG_CURRENT_DESKTOP"] = "Boswas:KDE"
        self.write("home/.config/boswas-mimeapps.list", "[Default Applications]\ntext/plain=kate.desktop\n")
        self.assertEqual(self.index().default_for(["text/plain"]).name, "Kate")
        self.assertEqual(apps.mimeapps_files()[:3], [self.path("home/.config/boswas-mimeapps.list"),
                                                     self.path("home/.config/kde-mimeapps.list"),
                                                     self.path("home/.config/mimeapps.list")])

    def test_default_falls_back_to_parent_types(self):
        self.make_apps()
        index = self.index()
        self.assertEqual(index.default_for(["text/x-csrc", "text/plain", "application/octet-stream"]).name, "Gedit")

    def test_set_default_writes_the_user_list(self):
        self.make_apps()
        index = self.index()
        index.set_default("text/plain", "kate.desktop")
        self.assertEqual(index.default_for(["text/plain"]).name, "Kate")
        self.assertEqual(self.read(self.path("home/.config/mimeapps.list")).decode(),
                         "[Default Applications]\ntext/plain=kate.desktop\n\n[Added Associations]\n"
                         "text/plain=kate.desktop;\n")
        index.set_default("video/mp4", "plain.desktop")
        self.assertEqual(self.index().default_for(["video/mp4"]).name, "Plain Tool")
        self.assertEqual(self.index().default_for(["text/plain"]).name, "Kate")

    def test_set_default_keeps_the_rest_of_the_file(self):
        path = self.write("home/.config/mimeapps.list", (
            "# my comment\n[Added Associations]\ntext/plain=gedit.desktop;other.desktop;\nimage/png=viewer.desktop;\n\n"
            "[Removed Associations]\ntext/plain=kate.desktop;bad.desktop;\nimage/gif=kate.desktop;\n\n"
            "[Custom Group]\nKey=Value\n\n[Default Applications]\ntext/plain=gedit.desktop\napplication/pdf=x.desktop\n"))
        apps.set_default_in_file(path, "text/plain", "kate.desktop")
        self.assertEqual(self.read(path).decode(), (
            "# my comment\n[Added Associations]\ntext/plain=kate.desktop;gedit.desktop;other.desktop;\n"
            "image/png=viewer.desktop;\n\n"
            "[Removed Associations]\ntext/plain=bad.desktop;\nimage/gif=kate.desktop;\n\n"
            "[Custom Group]\nKey=Value\n\n[Default Applications]\ntext/plain=kate.desktop\napplication/pdf=x.desktop\n"))
        apps.set_default_in_file(path, "image/gif", "kate.desktop")
        text = self.read(path).decode()
        self.assertNotIn("image/gif=kate.desktop;\n\n[Custom", text)
        self.assertIn("image/gif=kate.desktop\n", text)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["mimeapps.list"])

    def test_set_default_rejects_injection(self):
        index = self.index()
        for mime, app_id in (("text/plain\n[Default Applications]", "a.desktop"), ("text/plain", "a.desktop\nx=y"),
                             ("noslash", "a.desktop"), ("text/plain", "../a.desktop"), ("text/plain", "a.exe"),
                             ("a=b/c", "a.desktop"), ("", "a.desktop")):
            with self.assertRaises(ValueError, msg=(mime, app_id)):
                index.set_default(mime, app_id)
        self.assertFalse(os.path.exists(self.path("home/.config/mimeapps.list")))

    def test_find_program(self):
        tool = self.write("bin/tool", "#!/bin/sh\n", mode=0o755)
        plain = self.write("bin/notexec", "data", mode=0o644)
        self.assertEqual(apps.find_program(tool), tool)
        self.assertIsNone(apps.find_program(plain))
        self.assertEqual(apps.find_program("tool", self.path("bin")), tool)
        self.assertIsNone(apps.find_program("tool", self.path("nowhere")))
        self.assertIsNone(apps.find_program("bin/tool"))
        self.assertIsNone(apps.find_program(""))


class ThumbsTest(TempCase):
    def png(self, text):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        body = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        for key, value in text.items():
            body += chunk(b"tEXt", key.encode("latin-1") + b"\0" + value.encode("latin-1"))
        return body + chunk(b"IDAT", zlib.compress(b"\0\0")) + chunk(b"IEND", b"")

    def test_names_follow_the_specification(self):
        # The example of the thumbnail specification.
        self.assertEqual(hashlib.md5(b"file:///home/jens/photos/me.png").hexdigest(),
                         "c6ee772d9e49320e97ec29a7eb5b1697")
        self.assertEqual(thumbs.thumb_name("/home/jens/photos/me.png"), "c6ee772d9e49320e97ec29a7eb5b1697.png")
        self.assertEqual(thumbs.cache_root(), self.path("home/.cache/thumbnails"))
        self.assertEqual(thumbs.thumb_path("/home/jens/photos/me.png", "large"),
                         self.path("home/.cache/thumbnails/large/c6ee772d9e49320e97ec29a7eb5b1697.png"))
        self.assertEqual(thumbs.fail_path("/a"), self.path("home/.cache/thumbnails/fail/boswas-files",
                                                           thumbs.thumb_name("/a")))
        self.assertEqual(thumbs.thumb_name("/with space/ü.png"),
                         hashlib.md5(b"file:///with%20space/%C3%BC.png").hexdigest() + ".png")

    def test_sizes(self):
        self.assertEqual(thumbs.size_for(1), ("normal", 128))
        self.assertEqual(thumbs.size_for(128), ("normal", 128))
        self.assertEqual(thumbs.size_for(129), ("large", 256))
        self.assertEqual(thumbs.size_for(512), ("x-large", 512))
        self.assertEqual(thumbs.size_for(5000), ("xx-large", 1024))

    def test_wants_thumbnail(self):
        self.assertTrue(thumbs.wants_thumbnail("/p/photo.JPG", 5000))
        self.assertFalse(thumbs.wants_thumbnail("/p/notes.txt", 5000))
        self.assertFalse(thumbs.wants_thumbnail("/p/photo.jpg", 0))
        self.assertFalse(thumbs.wants_thumbnail("/p/huge.png", thumbs.MAX_SOURCE_BYTES + 1))
        self.assertFalse(thumbs.wants_thumbnail(thumbs.thumb_path("/x.png", "normal"), 5000))

    def test_png_text_and_validity(self):
        path = "/pictures/a b.png"
        good = self.write("t/good.png", self.png({"Thumb::URI": "file:///pictures/a%20b.png", "Thumb::MTime": "1700"}))
        self.assertEqual(thumbs.read_png_text(good), {"Thumb::URI": "file:///pictures/a%20b.png",
                                                      "Thumb::MTime": "1700"})
        self.assertTrue(thumbs.is_valid(good, path, 1700.9))
        self.assertFalse(thumbs.is_valid(good, path, 1701))
        self.assertFalse(thumbs.is_valid(good, "/pictures/other.png", 1700))
        self.assertFalse(thumbs.is_valid(self.write("t/nomtime.png", self.png({"Thumb::URI": clip.path_to_uri(path)})),
                                         path, 1700))
        self.assertFalse(thumbs.is_valid(self.write("t/badmtime.png", self.png(
            {"Thumb::URI": clip.path_to_uri(path), "Thumb::MTime": "soon"})), path, 1700))

    def test_png_reader_survives_garbage(self):
        valid = self.png({"Thumb::URI": "file:///x", "Thumb::MTime": "1"})
        samples = [b"", b"\x89PNG\r\n\x1a\n", valid[:20], valid[:40], b"GIF89a" + valid, os.urandom(500),
                   b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 0xFFFFFFFF) + b"tEXt" + b"x" * 50,
                   valid.replace(b"Thumb::URI", b"Thumb::URJ", 1)]
        for index, sample in enumerate(samples):
            result = thumbs.read_png_text(self.write(f"t/{index}.png", sample))
            self.assertNotIn("Thumb::URI", result, index)
        self.assertEqual(thumbs.read_png_text(self.path("t/missing.png")), {})
        self.assertEqual(thumbs.read_png_text(self.tmp), {})

    def test_find_valid_and_failures(self):
        source = self.write("pics/a.png", b"not really an image")
        mtime = os.stat(source).st_mtime
        self.assertIsNone(thumbs.find_valid(source, mtime, 128))
        self.assertFalse(thumbs.has_failed(source, mtime))
        record = self.png({"Thumb::URI": clip.path_to_uri(source), "Thumb::MTime": str(int(mtime))})
        directory = thumbs.ensure_dirs("large")
        self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(thumbs.cache_root()).st_mode & 0o777, 0o700)
        with open(thumbs.thumb_path(source, "large"), "wb") as handle:
            handle.write(record)
        self.assertEqual(thumbs.find_valid(source, mtime, 128), thumbs.thumb_path(source, "large"))
        self.assertEqual(thumbs.find_valid(source, mtime, 256), thumbs.thumb_path(source, "large"))
        self.assertIsNone(thumbs.find_valid(source, mtime, 400))
        self.assertIsNone(thumbs.find_valid(source, mtime + 5, 128))
        thumbs.ensure_dirs(thumbs.FAIL_DIR)
        with open(thumbs.fail_path(source), "wb") as handle:
            handle.write(record)
        self.assertTrue(thumbs.has_failed(source, mtime))
        self.assertFalse(thumbs.has_failed(source, mtime + 5))


if __name__ == "__main__":
    unittest.main()
