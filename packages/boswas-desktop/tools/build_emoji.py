#!/usr/bin/env python3
"""The data of the shell's emoji and symbols panel (Super+. and Super+;).

Generated from Unicode's own data as Debian packages it, so the same
packages give the same file byte for byte; nothing is downloaded:

  unicode-data       /usr/share/unicode/emoji/emoji-test.txt   the emoji, their
                     order, groups and names, and their skin tones
                     /usr/share/unicode/UnicodeData.txt        names of symbols
                     /usr/share/unicode/DerivedAge.txt          the version
  unicode-cldr-core  /usr/share/unicode/cldr/common/annotations/en.xml and
                     annotationsDerived/en.xml                  search keywords
                     /usr/share/unicode/cldr/common/dtd/ldml.dtd  the version

The kaomoji and the choice of symbols are this file's own lists.

Writes crates/boswas-shell/data/emoji.tsv (committed; the shell embeds it):

  build_emoji.py                 write the file
  build_emoji.py --check         exit 1 if the committed file differs from
                                 what the sources give, 2 if they are missing
  build_emoji.py --unicode DIR   the sources' directory (default
                                 /usr/share/unicode)

The file: UTF-8, one record per line, fields separated by tabs.

  # ...                                        comments (the first lines)
  G <key> <label> <tab>                        a group; <tab> is emoji,
                                               kaomoji or symbols
  <text> <name> <keywords> <tones>             an item of the group above:
                                               keywords separated by "|",
                                               tones (five emoji, light to
                                               dark skin, separated by ",")
                                               or empty

Licence of the data: the Unicode License v3 (crates/boswas-shell/data/README).
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT = HERE.parent / "crates" / "boswas-shell" / "data" / "emoji.tsv"
DEFAULT_SOURCES = Path("/usr/share/unicode")

# emoji-test.txt's groups as the panel shows them; "Component" (skin tone
# and hair swatches) is not a group of the panel.
GROUPS = {
    "Smileys & Emotion": ("smileys", "Smileys & emotion"),
    "People & Body": ("people", "People"),
    "Animals & Nature": ("animals", "Animals & nature"),
    "Food & Drink": ("food", "Food & drink"),
    "Travel & Places": ("travel", "Travel & places"),
    "Activities": ("activities", "Activities"),
    "Objects": ("objects", "Objects"),
    "Symbols": ("symbols", "Symbols"),
    "Flags": ("flags", "Flags"),
}

TONES = ("light skin tone", "medium-light skin tone", "medium skin tone", "medium-dark skin tone", "dark skin tone")

# Kaomoji: faces made of letters, by mood. (Text, what a search finds.)
KAOMOJI = (
    ("kaomoji-classic", "Classic", (
        (":-)", "smile"), (":-D", "laugh grin"), (";-)", "wink"), (":-(", "sad frown"), (":-P", "tongue"),
        (":-O", "surprised"), (":-|", "neutral"), (":-/", "unsure"), (":'-(", "crying"), ("<3", "heart love"),
        ("^_^", "happy"), ("^^;", "nervous sweat"), ("-_-", "annoyed"), ("o_O", "confused"), ("T_T", "crying"),
        (">_<", "frustrated"), ("x_x", "dead tired"), ("*_*", "starstruck"), ("=)", "smile"), ("XD", "laugh"),
    )),
    ("kaomoji-happy", "Happy", (
        ("(◕‿◕)", "happy smile"), ("(｡◕‿◕｡)", "happy smile"), ("(＾▽＾)", "happy"), ("(≧▽≦)", "excited happy"),
        ("ヽ(・∀・)ﾉ", "cheer happy"), ("(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧", "excited sparkle"), ("\\(^o^)/", "cheer yay"),
        ("٩(◕‿◕｡)۶", "happy cheer"), ("(✿◠‿◠)", "happy flower"), ("(´▽`)", "content"), ("(^‿^)", "smile"),
        ("o(≧▽≦)o", "excited"), ("(•‿•)", "smile"), ("(☆▽☆)", "starstruck"), ("(⌒‿⌒)", "content smile"),
    )),
    ("kaomoji-greeting", "Greeting", (
        ("(・∀・)ノ", "hello wave"), ("(^_^)/", "hi wave"), ("ヾ(＾∇＾)", "bye wave"), ("(￣▽￣)ノ", "hello wave"),
        ("( ´ ▽ ` )ﾉ", "wave"), ("(^o^)/", "hello"), ("ヾ(・ω・)ノ", "wave bye"), ("(*・ω・)ﾉ", "hi"),
        ("m(_ _)m", "bow sorry thanks"), ("(_ _)", "bow"),
    )),
    ("kaomoji-love", "Love", (
        ("(♥‿♥)", "love heart eyes"), ("(´∀｀)♡", "love"), ("(づ｡◕‿‿◕｡)づ", "hug"), ("(っ´▽`)っ", "hug"),
        ("(*˘︶˘*).｡.:*♡", "love dreamy"), ("(≧◡≦) ♡", "love"), ("(ღ˘⌣˘ღ)", "love"), ("♡＾▽＾♡", "love"),
        ("(˘³˘)♥", "kiss love"), ("( ˘ ³˘)♥", "kiss"),
    )),
    ("kaomoji-sad", "Sad", (
        ("(╥﹏╥)", "crying sad"), ("(ಥ﹏ಥ)", "crying"), ("(T⌓T)", "crying"), ("(｡•́︿•̀｡)", "sad"),
        ("(っ˘̩╭╮˘̩)っ", "sad"), ("(；︵；)", "sad crying"), ("(´；ω；`)", "crying"), ("(-_-;)", "nervous"),
        ("(._.)", "sad small"), ("(︶︹︺)", "sad pout"),
    )),
    ("kaomoji-angry", "Angry", (
        ("(╬ Ò﹏Ó)", "angry"), ("(ಠ_ಠ)", "disapproval look"), ("(￣^￣)", "annoyed"), ("(`皿´)", "angry"),
        ("(ノಠ益ಠ)ノ", "rage angry"), ("(＃`Д´)", "angry"), ("ヽ(`Д´)ﾉ", "angry"), ("(¬_¬)", "suspicious"),
        ("(╯°□°)╯︵ ┻━┻", "table flip"), ("┬─┬ノ( º _ ºノ)", "table put back calm"),
    )),
    ("kaomoji-surprised", "Surprised", (
        ("(⊙_⊙)", "surprised stare"), ("(°o°)", "surprised"), ("Σ(°△°)", "shocked"), ("(O_O)", "shocked"),
        ("(;ﾟДﾟ)", "shocked"), ("w(°ｏ°)w", "wow"), ("(ﾟoﾟ)", "surprised"), ("(◎_◎;)", "dizzy"),
    )),
    ("kaomoji-other", "Shrug and more", (
        ("¯\\_(ツ)_/¯", "shrug whatever"), ("( ͡° ͜ʖ ͡°)", "lenny face"), ("ʕ•ᴥ•ʔ", "bear"), ("(=^･ω･^=)", "cat"),
        ("(・ω・)", "cute"), ("(￣ω￣)", "smug"), ("(￣ー￣)", "smug"), ("(－_－) zzZ", "sleep sleepy"),
        ("(-.-)Zzz", "sleep"), ("(⌐■_■)", "cool sunglasses"), ("ᕦ(ò_óˇ)ᕤ", "strong flex"), ("(•_•)", "neutral"),
        ("( •_•)>⌐■-■", "putting on sunglasses cool"), ("¬‿¬", "smirk"), ("(ง'̀-'́)ง", "fight"),
    )),
)

# Symbols by kind: code point ranges (inclusive) and single characters. Only
# assigned, printable characters of these are taken.
SYMBOLS = (
    ("symbols-punctuation", "Punctuation", ((0x00A1, 0x00A1), (0x00A7, 0x00A7), (0x00AB, 0x00AB), (0x00B6, 0x00B7),
                                             (0x00BB, 0x00BB), (0x00BF, 0x00BF), (0x2010, 0x2027), (0x2030, 0x205E),
                                             (0x2E18, 0x2E18), (0x2E2E, 0x2E2E))),
    ("symbols-currency", "Currency", ((0x0024, 0x0024), (0x00A2, 0x00A5), (0x0E3F, 0x0E3F), (0x20A0, 0x20C0))),
    ("symbols-latin", "Latin", ((0x00C0, 0x00D6), (0x00D8, 0x00F6), (0x00F8, 0x00FF), (0x0152, 0x0153),
                                (0x0160, 0x0161), (0x0178, 0x017E), (0x0192, 0x0192), (0x01CD, 0x01DC))),
    ("symbols-greek", "Greek", ((0x0391, 0x03A1), (0x03A3, 0x03A9), (0x03B1, 0x03C9))),
    ("symbols-math", "Math", ((0x00AC, 0x00AC), (0x00B0, 0x00B1), (0x00B5, 0x00B5), (0x00D7, 0x00D7),
                              (0x00F7, 0x00F7), (0x00BC, 0x00BE), (0x2150, 0x215E), (0x2070, 0x2070),
                              (0x00B9, 0x00B9), (0x00B2, 0x00B3), (0x2074, 0x208E), (0x2200, 0x2262),
                              (0x2264, 0x226B), (0x2282, 0x228B), (0x22A2, 0x22A5), (0x22C0, 0x22C3),
                              (0x2300, 0x2300), (0x2308, 0x230B))),
    ("symbols-arrows", "Arrows", ((0x2190, 0x21D9), (0x21E0, 0x21EA), (0x2794, 0x2794), (0x27F5, 0x27FA))),
    ("symbols-geometric", "Geometric", ((0x25A0, 0x25FF),)),
    ("symbols-other", "Other", ((0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x2122, 0x2122), (0x2103, 0x2103),
                                (0x2109, 0x2109), (0x2116, 0x2117), (0x2120, 0x2120), (0x2126, 0x2126),
                                (0x212E, 0x212E), (0x2605, 0x2606), (0x260E, 0x260E), (0x2610, 0x2612),
                                (0x2615, 0x2615), (0x261A, 0x261F), (0x2620, 0x2620), (0x2639, 0x263C),
                                (0x2640, 0x2642), (0x2660, 0x2667), (0x2669, 0x266F), (0x2690, 0x2691),
                                (0x2713, 0x2718), (0x2720, 0x2727), (0x2729, 0x274B), (0x2756, 0x2756),
                                (0x2766, 0x2767), (0x2E3A, 0x2E3B))),
)


@dataclass
class Item:
    text: str
    name: str
    keywords: list[str] = field(default_factory=list)
    tones: list[str] = field(default_factory=list)


@dataclass
class Group:
    key: str
    label: str
    tab: str
    items: list[Item] = field(default_factory=list)


class SourcesMissing(Exception):
    pass


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as err:
        raise SourcesMissing(f"{path}: {err.strerror or err}") from err


def chars(hex_points: str) -> str:
    return "".join(chr(int(h, 16)) for h in hex_points.split())


def parse_emoji_test(text: str) -> tuple[str, list[tuple[str, str, str, str]]]:
    """The version and the fully-qualified emoji: (group, subgroup, text, name)."""
    version = ""
    group = subgroup = ""
    out = []
    for line in text.splitlines():
        if line.startswith("# Version:"):
            version = line.split(":", 1)[1].strip()
        elif line.startswith("# group:"):
            group = line.split(":", 1)[1].strip()
        elif line.startswith("# subgroup:"):
            subgroup = line.split(":", 1)[1].strip()
        elif line and not line.startswith("#"):
            points, rest = line.split(";", 1)
            status, comment = rest.split("#", 1)
            if status.strip() != "fully-qualified":
                continue
            # "# 😀 E1.0 grinning face": the text, the emoji version, the name.
            match = re.match(r"\s*\S+\s+E\d+\.\d+\s+(.*)$", comment)
            if not match:
                raise ValueError(f"unexpected line in emoji-test.txt: {line}")
            out.append((group, subgroup, chars(points), match.group(1).strip()))
    return version, out


def parse_annotations(*texts: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """CLDR keywords and spoken names by text (FE0F removed, as CLDR has it)."""
    keywords: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for text in texts:
        root = ElementTree.fromstring(text.encode("utf-8"))
        for node in root.iter("annotation"):
            cp = node.get("cp", "")
            value = (node.text or "").strip()
            if not cp or not value:
                continue
            if node.get("type") == "tts":
                names.setdefault(cp, value)
            else:
                words = [w.strip() for w in value.split("|") if w.strip()]
                keywords.setdefault(cp, words)
    return keywords, names


def parse_unicode_data(text: str) -> dict[int, str]:
    names = {}
    for line in text.splitlines():
        fields = line.split(";")
        if len(fields) < 3 or fields[1].startswith("<"):
            continue
        category = fields[2]
        if category.startswith("C") or category in ("Zs", "Zl", "Zp", "Mn", "Me"):
            continue
        names[int(fields[0], 16)] = fields[1].lower()
    return names


def clean_keywords(name: str, words: list[str]) -> list[str]:
    seen = {name.lower()}
    out = []
    for word in words:
        word = word.lower().replace("\t", " ").replace("|", " ")
        if word not in seen:
            seen.add(word)
            out.append(word)
    return out


def emoji_groups(entries, keywords) -> list[Group]:
    groups: dict[str, Group] = {}
    by_name: dict[str, Item] = {}
    toned: list[tuple[str, int, str]] = []
    for group_name, _subgroup, text, name in entries:
        if group_name not in GROUPS:
            continue
        key, label = GROUPS[group_name]
        group = groups.setdefault(key, Group(key, label, "emoji"))
        head, _, attrs = name.partition(": ")
        parts = [p.strip() for p in attrs.split(",")] if attrs else []
        tones = [p for p in parts if p in TONES]
        if tones:
            # A skin-tone variant: kept with its base when it has one tone.
            if len(tones) == 1:
                rest = [p for p in parts if p not in TONES]
                base = head + (": " + ", ".join(rest) if rest else "")
                toned.append((base, TONES.index(tones[0]), text))
            continue
        item = Item(text, name, clean_keywords(name, keywords.get(text.replace("\ufe0f", ""), [])))
        group.items.append(item)
        by_name[name] = item
    variants: dict[str, list[str]] = {}
    for base, index, text in toned:
        if base in by_name:
            variants.setdefault(base, [""] * len(TONES))[index] = text
    for base, texts in variants.items():
        if all(texts):
            by_name[base].tones = texts
    return [groups[key] for key, _ in GROUPS.values() if key in groups]


def kaomoji_groups() -> list[Group]:
    groups = []
    for key, label, faces in KAOMOJI:
        groups.append(Group(key, label, "kaomoji", [Item(text, words.split()[0], words.split()[1:]) for text, words in faces]))
    return groups


def symbol_groups(unicode_names: dict[int, str], keywords, names) -> list[Group]:
    groups = []
    seen: set[int] = set()
    for key, label, ranges in SYMBOLS:
        group = Group(key, label, "symbols")
        for first, last in ranges:
            for point in range(first, last + 1):
                if point in seen or point not in unicode_names:
                    continue
                seen.add(point)
                text = chr(point)
                name = names.get(text, unicode_names[point]).lower()
                group.items.append(Item(text, name, clean_keywords(name, keywords.get(text, []))))
        groups.append(group)
    return groups


def cldr_version(dtd: str) -> str:
    match = re.search(r'cldrVersion CDATA #FIXED "([^"]+)"', dtd)
    return match.group(1) if match else "unknown"


def unicode_version(derived_age: str) -> str:
    match = re.match(r"# DerivedAge-([\d.]+)\.txt", derived_age)
    return match.group(1) if match else "unknown"


def build(sources: Path) -> str:
    emoji_version, entries = parse_emoji_test(read(sources / "emoji" / "emoji-test.txt"))
    keywords, names = parse_annotations(read(sources / "cldr" / "common" / "annotations" / "en.xml"),
                                        read(sources / "cldr" / "common" / "annotationsDerived" / "en.xml"))
    unicode_names = parse_unicode_data(read(sources / "UnicodeData.txt"))
    versions = (f"emoji {emoji_version}, Unicode {unicode_version(read(sources / 'DerivedAge.txt'))}, "
                f"CLDR {cldr_version(read(sources / 'cldr' / 'common' / 'dtd' / 'ldml.dtd'))}")
    groups = emoji_groups(entries, keywords) + kaomoji_groups() + symbol_groups(unicode_names, keywords, names)
    return render(groups, versions)


def render(groups: list[Group], versions: str) -> str:
    lines = [
        "# The emoji and symbols of the shell's emoji panel. Generated by tools/build_emoji.py; do not edit.",
        f"# From Unicode's emoji-test.txt, UnicodeData.txt and CLDR's English annotations ({versions}).",
        "# Copyright 1991-2023 Unicode, Inc.; Unicode License v3 (data/README). Changed: selected and reformatted.",
        "# Fields: text, name, keywords (|), skin tones (,); a line \"G key label tab\" starts a group.",
    ]
    for group in groups:
        lines.append(f"G\t{group.key}\t{group.label}\t{group.tab}")
        for item in group.items:
            for part in (item.text, item.name, *item.keywords, *item.tones):
                if "\t" in part or "\n" in part:
                    raise ValueError(f"a tab or line break in {part!r}")
            lines.append(f"{item.text}\t{item.name}\t{'|'.join(item.keywords)}\t{','.join(item.tones)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="compare with the committed file instead of writing it")
    parser.add_argument("--unicode", type=Path, default=DEFAULT_SOURCES, help="directory of the Unicode data")
    parser.add_argument("--output", type=Path, default=OUTPUT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        text = build(args.unicode)
    except SourcesMissing as err:
        print(f"build_emoji.py: the Unicode data is missing ({err}); install unicode-data and "
              "unicode-cldr-core, or name their directory with --unicode", file=sys.stderr)
        return 2
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != text:
            print(f"build_emoji.py: {args.output} is not what the sources give ({text.splitlines()[1][2:]}); "
                  "run tools/build_emoji.py", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
