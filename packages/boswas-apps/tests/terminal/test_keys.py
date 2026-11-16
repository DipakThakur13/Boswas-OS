"""Keys, mouse events and pasted text as bytes for the program."""

import unittest

from boswas_apps.terminal import keys
from boswas_apps.terminal.keys import ALT, CTRL, SHIFT, encode_key, encode_mouse, encode_paste


class Keys(unittest.TestCase):
    def test_cursor_keys(self):
        self.assertEqual(encode_key("Up"), b"\x1b[A")
        self.assertEqual(encode_key("Down"), b"\x1b[B")
        self.assertEqual(encode_key("Right"), b"\x1b[C")
        self.assertEqual(encode_key("Left"), b"\x1b[D")
        self.assertEqual(encode_key("Home"), b"\x1b[H")
        self.assertEqual(encode_key("End"), b"\x1b[F")

    def test_cursor_keys_in_application_mode(self):
        self.assertEqual(encode_key("Up", app_cursor=True), b"\x1bOA")
        self.assertEqual(encode_key("Home", app_cursor=True), b"\x1bOH")
        self.assertEqual(encode_key("Up", CTRL, app_cursor=True), b"\x1b[1;5A")

    def test_cursor_keys_with_modifiers(self):
        self.assertEqual(encode_key("Up", SHIFT), b"\x1b[1;2A")
        self.assertEqual(encode_key("Left", ALT), b"\x1b[1;3D")
        self.assertEqual(encode_key("Right", CTRL), b"\x1b[1;5C")
        self.assertEqual(encode_key("Down", CTRL | SHIFT), b"\x1b[1;6B")
        self.assertEqual(encode_key("End", CTRL | ALT | SHIFT), b"\x1b[1;8F")

    def test_editing_keys(self):
        self.assertEqual(encode_key("Insert"), b"\x1b[2~")
        self.assertEqual(encode_key("Delete"), b"\x1b[3~")
        self.assertEqual(encode_key("PageUp"), b"\x1b[5~")
        self.assertEqual(encode_key("PageDown"), b"\x1b[6~")
        self.assertEqual(encode_key("Delete", CTRL), b"\x1b[3;5~")
        self.assertEqual(encode_key("PageUp", ALT), b"\x1b[5;3~")

    def test_function_keys(self):
        expected = {1: b"\x1bOP", 2: b"\x1bOQ", 3: b"\x1bOR", 4: b"\x1bOS", 5: b"\x1b[15~", 6: b"\x1b[17~",
                    7: b"\x1b[18~", 8: b"\x1b[19~", 9: b"\x1b[20~", 10: b"\x1b[21~", 11: b"\x1b[23~", 12: b"\x1b[24~"}
        for number, data in expected.items():
            self.assertEqual(encode_key(f"F{number}"), data, number)
        self.assertEqual(encode_key("F1", SHIFT), b"\x1b[1;2P")
        self.assertEqual(encode_key("F4", CTRL), b"\x1b[1;5S")
        self.assertEqual(encode_key("F5", SHIFT), b"\x1b[15;2~")
        self.assertEqual(encode_key("F12", CTRL | ALT), b"\x1b[24;7~")

    def test_enter_tab_backspace_escape(self):
        self.assertEqual(encode_key("Enter"), b"\r")
        self.assertEqual(encode_key("Enter", newline_mode=True), b"\r\n")
        self.assertEqual(encode_key("Enter", ALT), b"\x1b\r")
        self.assertEqual(encode_key("Tab"), b"\t")
        self.assertEqual(encode_key("Tab", SHIFT), b"\x1b[Z")
        self.assertEqual(encode_key("Backtab", SHIFT), b"\x1b[Z")
        self.assertEqual(encode_key("Backspace"), b"\x7f")
        self.assertEqual(encode_key("Backspace", CTRL), b"\x08")
        self.assertEqual(encode_key("Backspace", ALT), b"\x1b\x7f")
        self.assertEqual(encode_key("Escape"), b"\x1b")

    def test_text(self):
        self.assertEqual(encode_key("a", 0, "a"), b"a")
        self.assertEqual(encode_key("a", SHIFT, "A"), b"A")
        self.assertEqual(encode_key("e", 0, "é"), "é".encode())
        self.assertEqual(encode_key("", 0, "日本"), "日本".encode())
        self.assertEqual(encode_key("", 0, ""), b"")

    def test_alt_is_an_escape_prefix(self):
        self.assertEqual(encode_key("a", ALT, "a"), b"\x1ba")
        self.assertEqual(encode_key("b", ALT | SHIFT, "B"), b"\x1bB")
        self.assertEqual(encode_key(".", ALT, "."), b"\x1b.")
        self.assertEqual(encode_key("x", ALT, ""), b"\x1bx")

    def test_control_combinations(self):
        self.assertEqual(encode_key("a", CTRL, "\x01"), b"\x01")
        self.assertEqual(encode_key("c", CTRL, ""), b"\x03")
        self.assertEqual(encode_key("z", CTRL | SHIFT, "Z"), b"\x1a")
        self.assertEqual(encode_key(" ", CTRL, " "), b"\x00")
        self.assertEqual(encode_key("@", CTRL, "@"), b"\x00")
        self.assertEqual(encode_key("[", CTRL, "["), b"\x1b")
        self.assertEqual(encode_key("\\", CTRL, "\\"), b"\x1c")
        self.assertEqual(encode_key("]", CTRL, "]"), b"\x1d")
        self.assertEqual(encode_key("^", CTRL, "^"), b"\x1e")
        self.assertEqual(encode_key("_", CTRL, "_"), b"\x1f")
        self.assertEqual(encode_key("/", CTRL, "/"), b"\x1f")
        self.assertEqual(encode_key("?", CTRL, "?"), b"\x7f")
        self.assertEqual(encode_key("x", CTRL | ALT, ""), b"\x1b\x18")
        self.assertEqual(encode_key("1", CTRL, "1"), b"1")

    def test_keypad(self):
        self.assertEqual(encode_key("KP_5", 0, "5"), b"5")
        self.assertEqual(encode_key("KP_5", app_keypad=True), b"\x1bOu")
        self.assertEqual(encode_key("KP_0", app_keypad=True), b"\x1bOp")
        self.assertEqual(encode_key("KP_Add", app_keypad=True), b"\x1bOk")
        self.assertEqual(encode_key("KP_Subtract"), b"-")
        self.assertEqual(encode_key("KP_Decimal", app_keypad=True), b"\x1bOn")
        self.assertEqual(encode_key("KP_Enter"), b"\r")
        self.assertEqual(encode_key("KP_Enter", app_keypad=True), b"\x1bOM")
        self.assertEqual(encode_key("KP_Divide", ALT), b"\x1b/")


class Mouse(unittest.TestCase):
    def test_nothing_without_a_mode(self):
        self.assertEqual(encode_mouse("press", 0, 1, 1, mode=0, encoding=0), b"")

    def test_x10(self):
        self.assertEqual(encode_mouse("press", 0, 0, 0, mode=9, encoding=0), b"\x1b[M !!")
        self.assertEqual(encode_mouse("press", 2, 9, 4, CTRL, mode=9, encoding=0), b"\x1b[M\"*%")
        self.assertEqual(encode_mouse("release", 0, 0, 0, mode=9, encoding=0), b"")
        self.assertEqual(encode_mouse("press", 4, 0, 0, mode=9, encoding=0), b"")

    def test_normal_tracking_default_encoding(self):
        self.assertEqual(encode_mouse("press", 0, 9, 4, mode=1000, encoding=0), b"\x1b[M *%")
        self.assertEqual(encode_mouse("release", 0, 9, 4, mode=1000, encoding=0), b"\x1b[M#*%")
        self.assertEqual(encode_mouse("press", 1, 0, 0, SHIFT | ALT | CTRL, mode=1000, encoding=0),
                         b"\x1b[M" + bytes((32 + 1 + 4 + 8 + 16, 33, 33)))
        self.assertEqual(encode_mouse("move", 0, 1, 1, mode=1000, encoding=0), b"")
        self.assertEqual(encode_mouse("press", 0, 300, 4, mode=1000, encoding=0), b"")

    def test_sgr_encoding(self):
        self.assertEqual(encode_mouse("press", 0, 9, 4, mode=1000, encoding=1006), b"\x1b[<0;10;5M")
        self.assertEqual(encode_mouse("release", 2, 9, 4, mode=1000, encoding=1006), b"\x1b[<2;10;5m")
        self.assertEqual(encode_mouse("press", 0, 500, 300, mode=1000, encoding=1006), b"\x1b[<0;501;301M")
        self.assertEqual(encode_mouse("press", 4, 0, 0, mode=1000, encoding=1006), b"\x1b[<64;1;1M")
        self.assertEqual(encode_mouse("press", 5, 0, 0, CTRL, mode=1000, encoding=1006), b"\x1b[<81;1;1M")
        self.assertEqual(encode_mouse("release", 4, 0, 0, mode=1000, encoding=1006), b"")

    def test_motion(self):
        self.assertEqual(encode_mouse("move", 0, 2, 3, mode=1002, encoding=1006), b"\x1b[<32;3;4M")
        self.assertEqual(encode_mouse("move", 3, 2, 3, mode=1002, encoding=1006), b"")
        self.assertEqual(encode_mouse("move", 3, 2, 3, mode=1003, encoding=1006), b"\x1b[<35;3;4M")
        self.assertEqual(encode_mouse("move", 1, 2, 3, mode=1003, encoding=0), b"\x1b[M" + bytes((32 + 33, 35, 36)))

    def test_other_encodings(self):
        self.assertEqual(encode_mouse("press", 0, 9, 4, mode=1000, encoding=1015), b"\x1b[32;10;5M")
        self.assertEqual(encode_mouse("press", 0, 299, 4, mode=1000, encoding=1005),
                         b"\x1b[M " + chr(332).encode() + b"%")


class Paste(unittest.TestCase):
    def test_line_breaks_become_enter(self):
        self.assertEqual(encode_paste("a\nb\r\nc\rd", False), b"a\rb\rc\rd")

    def test_bracketed(self):
        self.assertEqual(encode_paste("ls\n", True), b"\x1b[200~ls\r\x1b[201~")

    def test_control_characters_cannot_escape_the_brackets(self):
        data = encode_paste("a\x1b[201~rm -rf /\n\x03\x00\x9b\x7f", True)
        self.assertEqual(data, b"\x1b[200~a[201~rm -rf /\r\x1b[201~")
        self.assertEqual(encode_paste("tab\there \x1b]0;x\x07", False), b"tab\there ]0;x")

    def test_several_lines(self):
        self.assertFalse(keys.has_several_lines("one line"))
        self.assertFalse(keys.has_several_lines("one line\n"))
        self.assertFalse(keys.has_several_lines("one line\r\n"))
        self.assertTrue(keys.has_several_lines("two\nlines"))
        self.assertTrue(keys.has_several_lines("two\rlines\n"))

    def test_unicode(self):
        self.assertEqual(encode_paste("naïve 日本", False), "naïve 日本".encode())


if __name__ == "__main__":
    unittest.main()
