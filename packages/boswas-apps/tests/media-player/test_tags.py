"""The tag readers: every format with bytes made by hand, and hostile input."""

from __future__ import annotations

import io
import random
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes  # noqa: E402

from boswas_apps.media_player import tags  # noqa: E402


class TagCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mp-tags-")
        self.tmp = Path(self._tmp.name)
        self.jpeg = fakes.jpeg_bytes()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(data)
        return path


class Id3Test(TagCase):
    def frames(self, version: int = 3) -> bytes:
        text = fakes.id3_text
        return (text("TIT2", "Blue in Green", version) + text("TPE1", "Miles Davis", version)
                + text("TALB", "Kind of Blue", version) + text("TPE2", "Miles Davis Sextet", version)
                + text("TRCK", "3/5", version) + text("TPOS", "1/2", version)
                + text("TDRC" if version == 4 else "TYER", "1959-08-17" if version == 4 else "1959", version)
                + text("TCON", "(8)", version))

    def check(self, found: tags.Tags) -> None:
        self.assertEqual((found.title, found.artist, found.album), ("Blue in Green", "Miles Davis", "Kind of Blue"))
        self.assertEqual(found.album_artist, "Miles Davis Sextet")
        self.assertEqual((found.track, found.disc, found.year, found.genre), (3, 1, 1959, "Jazz"))
        self.assertEqual(found.kind, "mp3")

    def test_v23_and_v24(self) -> None:
        for version in (3, 4):
            path = fakes.make_mp3(self.tmp / f"v{version}.mp3", fakes.id3_tag(self.frames(version), version))
            self.check(tags.read_tags(path))

    def test_v22(self) -> None:
        text = lambda frame, value: fakes.id3_frame(frame, b"\0" + value.encode("latin-1"), 2)  # noqa: E731
        frames = (text("TT2", "Blue in Green") + text("TP1", "Miles Davis") + text("TAL", "Kind of Blue")
                  + text("TP2", "Miles Davis Sextet") + text("TRK", "3") + text("TPA", "1") + text("TYE", "1959")
                  + text("TCO", "Jazz") + fakes.id3_frame("PIC", b"\0JPG\x03\0" + self.jpeg, 2))
        path = fakes.make_mp3(self.tmp / "v22.mp3", fakes.id3_tag(frames, 2))
        found = tags.read_tags(path)
        self.check(found)
        self.assertTrue(found.has_picture)
        self.assertEqual(tags.read_picture(path).data, self.jpeg)

    def test_text_encodings(self) -> None:
        for encoding in (0, 1, 2, 3):
            title = "Café" if encoding == 0 else "Καφές ☕"
            path = fakes.make_mp3(self.tmp / f"e{encoding}.mp3",
                                  fakes.id3_tag(fakes.id3_text("TIT2", title, 3, encoding)))
            self.assertEqual(tags.read_tags(path).title, title)

    def test_utf16_without_byte_order_mark_and_several_values(self) -> None:
        body = b"\x01" + "One".encode("utf-16-le")
        path = fakes.make_mp3(self.tmp / "a.mp3", fakes.id3_tag(fakes.id3_frame("TIT2", body)))
        self.assertEqual(tags.read_tags(path).title, "One")
        body = b"\x03" + "Ann\0Bob\0".encode("utf-8")
        path = fakes.make_mp3(self.tmp / "b.mp3", fakes.id3_tag(fakes.id3_frame("TPE1", body, 4), 4))
        self.assertEqual(tags.read_tags(path).artist, "Ann, Bob")

    def test_picture_and_mime(self) -> None:
        frames = fakes.id3_text("TIT2", "With art") + fakes.id3_frame("APIC", fakes.apic(self.jpeg))
        path = fakes.make_mp3(self.tmp / "art.mp3", fakes.id3_tag(frames))
        self.assertTrue(tags.read_tags(path).has_picture)
        picture = tags.read_picture(path)
        self.assertEqual((picture.mime, picture.data), ("image/jpeg", self.jpeg))

    def test_picture_that_is_a_link_or_not_an_image_is_ignored(self) -> None:
        link = fakes.id3_frame("APIC", fakes.apic(b"http://example.org/x.jpg", "-->"))
        path = fakes.make_mp3(self.tmp / "link.mp3", fakes.id3_tag(link))
        self.assertFalse(tags.read_tags(path).has_picture)
        self.assertIsNone(tags.read_picture(path))
        script = fakes.id3_frame("APIC", fakes.apic(b"#!/bin/sh\nrm -rf ~\n"))
        path = fakes.make_mp3(self.tmp / "script.mp3", fakes.id3_tag(script))
        self.assertIsNone(tags.read_picture(path))

    def test_whole_tag_unsynchronisation(self) -> None:
        frames = fakes.id3_text("TIT2", "ÿ Title", 3, 1)              # UTF-16 with FF FE: needs unsynchronising
        self.assertIn(b"\xff", frames)
        raw = frames.replace(b"\xff", b"\xff\x00")
        path = fakes.make_mp3(self.tmp / "u.mp3", fakes.id3_tag(raw, 3, 0x80))
        self.assertEqual(tags.read_tags(path).title, "ÿ Title")

    def test_frame_unsynchronisation_and_data_length_in_v24(self) -> None:
        body = b"\x01" + "Sync".encode("utf-16")
        stored = struct.pack(">I", len(body)) + body.replace(b"\xff", b"\xff\x00")
        frame = fakes.id3_frame("TIT2", stored, 4, flags=b"\x00\x03")
        path = fakes.make_mp3(self.tmp / "f.mp3", fakes.id3_tag(frame, 4))
        self.assertEqual(tags.read_tags(path).title, "Sync")

    def test_extended_header_and_footer(self) -> None:
        extended = struct.pack(">IHI", 6, 0, 0)
        frames = extended + fakes.id3_text("TIT2", "Extended")
        path = fakes.make_mp3(self.tmp / "x.mp3", fakes.id3_tag(frames, 3, 0x40))
        self.assertEqual(tags.read_tags(path).title, "Extended")

    def test_compressed_frames_are_skipped(self) -> None:
        frames = (fakes.id3_frame("TIT2", b"\x03garbage", 3, flags=b"\x00\x80")
                  + fakes.id3_text("TALB", "Kept"))
        path = fakes.make_mp3(self.tmp / "c.mp3", fakes.id3_tag(frames))
        found = tags.read_tags(path)
        self.assertEqual((found.title, found.album), ("", "Kept"))

    def test_v1_fills_what_v2_lacks(self) -> None:
        tail = fakes.id3v1("Old title", "Old artist", "Old album", "1994", 7, 17)
        path = fakes.make_mp3(self.tmp / "v1.mp3", fakes.id3_tag(fakes.id3_text("TIT2", "New title")), id3v1=tail)
        found = tags.read_tags(path)
        self.assertEqual((found.title, found.artist, found.album), ("New title", "Old artist", "Old album"))
        self.assertEqual((found.year, found.track, found.genre), (1994, 7, "Rock"))
        only = fakes.make_mp3(self.tmp / "only.mp3", id3v1=tail)
        self.assertEqual(tags.read_tags(only).title, "Old title")

    def test_duration_from_bitrate_xing_and_tlen(self) -> None:
        path = fakes.make_mp3(self.tmp / "cbr.mp3", audio_frames=100)       # 100 * 1152 / 44100 s
        self.assertAlmostEqual(tags.read_tags(path).duration_ms, 2612, delta=30)
        xing = bytearray(b"\xff\xfb\x90\x00" + b"\0" * 413)
        xing[36:40] = b"Xing"
        xing[40:44] = struct.pack(">I", 1)
        xing[44:48] = struct.pack(">I", 10000)
        path = self.write("vbr.mp3", bytes(xing) + fakes.mpeg_frames(5))
        self.assertEqual(tags.read_tags(path).duration_ms, 10000 * 1152 * 1000 // 44100)
        path = fakes.make_mp3(self.tmp / "tlen.mp3", fakes.id3_tag(fakes.id3_text("TLEN", "61000")))
        self.assertEqual(tags.read_tags(path).duration_ms, 61000)

    def test_genre_forms(self) -> None:
        for text, expected in (("17", "Rock"), ("(17)", "Rock"), ("(17)Hard Rock", "Hard Rock"), ("Ambient", "Ambient"),
                               ("(999)", ""), ("999", "")):
            path = fakes.make_mp3(self.tmp / "g.mp3", fakes.id3_tag(fakes.id3_text("TCON", text)))
            self.assertEqual(tags.read_tags(path).genre, expected, text)

    def test_lying_sizes(self) -> None:
        # A frame that claims more than the tag holds, a tag that claims more than the file holds.
        frame = b"TIT2" + struct.pack(">I", 0x7FFFFFFF) + b"\0\0" + b"\x03short"
        path = fakes.make_mp3(self.tmp / "lie1.mp3", fakes.id3_tag(frame))
        self.assertEqual(tags.read_tags(path).title, "")
        huge = b"ID3\x03\x00\x00" + b"\x7f\x7f\x7f\x7f" + fakes.id3_text("TIT2", "Inside")
        path = self.write("lie2.mp3", huge)
        self.assertEqual(tags.read_tags(path).title, "Inside")
        zero = b"TIT2" + struct.pack(">I", 0) + b"\0\0"
        path = fakes.make_mp3(self.tmp / "zero.mp3", fakes.id3_tag(zero * 5000))
        self.assertEqual(tags.read_tags(path).title, "")

    def test_oversized_text_and_picture_are_not_read(self) -> None:
        big = fakes.id3_frame("TIT2", b"\x03" + b"A" * (tags.MAX_TEXT_BYTES + 10))
        path = fakes.make_mp3(self.tmp / "big.mp3", fakes.id3_tag(big + fakes.id3_text("TALB", "After")))
        found = tags.read_tags(path)
        self.assertEqual((found.title, found.album), ("", "After"))

    def test_control_characters_and_overrides_are_removed(self) -> None:
        title = "A\x07B\u202eC\nD\x1b[31mE"
        path = fakes.make_mp3(self.tmp / "ctl.mp3", fakes.id3_tag(fakes.id3_text("TIT2", title)))
        self.assertEqual(tags.read_tags(path).title, "ABC D[31mE")
        self.assertEqual(tags.clean("x" * 5000), "x" * tags.MAX_TEXT_CHARS)


class FlacOggTest(TagCase):
    COMMENTS = ["TITLE=So What", "ARTIST=Miles Davis", "artist=Bill Evans", "ALBUM=Kind of Blue",
                "ALBUMARTIST=Miles Davis", "TRACKNUMBER=1/5", "DISCNUMBER=1", "DATE=1959-08-17", "GENRE=Jazz"]

    def check(self, found: tags.Tags) -> None:
        self.assertEqual((found.title, found.album, found.album_artist), ("So What", "Kind of Blue", "Miles Davis"))
        self.assertEqual(found.artist, "Miles Davis, Bill Evans")
        self.assertEqual((found.track, found.disc, found.year, found.genre), (1, 1, 1959, "Jazz"))

    def test_flac(self) -> None:
        path = fakes.make_flac(self.tmp / "a.flac", self.COMMENTS, cover=self.jpeg, samples=44100 * 7)
        found = tags.read_tags(path)
        self.check(found)
        self.assertEqual((found.kind, found.duration_ms, found.has_picture), ("flac", 7000, True))
        self.assertEqual(tags.read_picture(path).data, self.jpeg)

    def test_flac_after_an_id3_tag(self) -> None:
        flac = fakes.make_flac(self.tmp / "a.flac", self.COMMENTS).read_bytes()
        path = self.write("b.flac", fakes.id3_tag(fakes.id3_text("TIT2", "From ID3")) + flac)
        found = tags.read_tags(path)
        self.assertEqual((found.kind, found.title, found.album), ("flac", "From ID3", "Kind of Blue"))

    def test_ogg_vorbis_and_opus(self) -> None:
        path = fakes.make_ogg_vorbis(self.tmp / "a.ogg", self.COMMENTS, samples=44100 * 4)
        found = tags.read_tags(path)
        self.check(found)
        self.assertEqual((found.kind, found.duration_ms), ("ogg", 4000))
        path = fakes.make_opus(self.tmp / "a.opus", self.COMMENTS, samples=48000 * 5)
        found = tags.read_tags(path)
        self.check(found)
        self.assertEqual((found.kind, found.duration_ms), ("opus", 5000))

    def test_ogg_cover_in_a_comment(self) -> None:
        block = fakes.flac_picture(self.jpeg)
        path = fakes.make_ogg_vorbis(self.tmp / "art.ogg", ["TITLE=Art", "METADATA_BLOCK_PICTURE=" + fakes.b64(block)])
        self.assertTrue(tags.read_tags(path).has_picture)
        self.assertEqual(tags.read_picture(path).data, self.jpeg)
        bad = fakes.make_ogg_vorbis(self.tmp / "bad.ogg", ["METADATA_BLOCK_PICTURE=!!!not base64!!!"])
        self.assertIsNone(tags.read_picture(bad))

    def test_ogg_packet_over_several_pages_and_other_streams(self) -> None:
        identification = b"\x01vorbis" + struct.pack("<IBIiiiB", 0, 2, 44100, 0, 128000, 0, 0xB8) + b"\x01"
        comment = b"\x03vorbis" + fakes.vorbis_comment(["TITLE=" + "T" * 300, "ALBUM=Second"]) + b"\x01"
        first, second = comment[:255], comment[255:]
        page_one = fakes.ogg_page([identification], flags=2)
        other = fakes.ogg_page([b"\x80theora" + b"\0" * 20], serial=99, flags=2)
        # The first 255 bytes end a page without ending the packet: a lacing value of 255 and nothing after it.
        header = (b"OggS" + bytes([0, 0]) + struct.pack("<qIII", 0, 7, 1, 0) + bytes([1, 255]))
        page_two = header + first
        page_three = fakes.ogg_page([second], sequence=2, flags=1)
        end = fakes.ogg_page([b"\0" * 8], sequence=3, granule=44100 * 2, flags=4)
        path = self.write("split.ogg", page_one + other + page_two + page_three + end)
        found = tags.read_tags(path)
        self.assertEqual((found.title, found.album, found.duration_ms), ("T" * 300, "Second", 2000))

    def test_comment_lengths_that_lie(self) -> None:
        data = struct.pack("<I", 4) + b"test" + struct.pack("<I", 0xFFFFFFFF)
        data += struct.pack("<I", 9) + b"TITLE=One" + struct.pack("<I", 0x7FFFFFFF) + b"ALBUM=Two"
        path = self.write("lie.flac", b"fLaC" + fakes.flac_block(0, fakes.flac_streaminfo())
                          + fakes.flac_block(4, data, last=True))
        found = tags.read_tags(path)
        self.assertEqual((found.title, found.album), ("One", ""))
        vendor = struct.pack("<I", 0xFFFFFFF0) + b"x"
        path = self.write("vendor.flac", b"fLaC" + fakes.flac_block(4, vendor, last=True))
        self.assertEqual(tags.read_tags(path).title, "")

    def test_flac_block_beyond_the_file_and_endless_blocks(self) -> None:
        path = self.write("cut.flac", b"fLaC" + bytes([4]) + (0xFFFFFF).to_bytes(3, "big") + b"abc")
        self.assertEqual(tags.read_tags(path).kind, "flac")
        path = self.write("many.flac", b"fLaC" + fakes.flac_block(1, b"") * 100000)
        started = time.monotonic()
        tags.read_tags(path)
        self.assertLess(time.monotonic() - started, 2.0)


class Mp4Test(TagCase):
    def items(self) -> dict[bytes, bytes]:
        data = fakes.mp4_data
        return {b"\xa9nam": data("Flamenco Sketches".encode()), b"\xa9ART": data("Miles Davis".encode()),
                b"\xa9alb": data("Kind of Blue".encode()), b"aART": data("Miles Davis Sextet".encode()),
                b"\xa9day": data(b"1959-08-17"), b"\xa9gen": data(b"Jazz"),
                b"trkn": data(struct.pack(">HHHH", 0, 5, 5, 0), 0), b"disk": data(struct.pack(">HHH", 0, 1, 2), 0),
                b"covr": data(self.jpeg, 13)}

    def test_ilst(self) -> None:
        path = fakes.make_mp4(self.tmp / "a.m4a", self.items(), timescale=600, duration=600 * 9)
        found = tags.read_tags(path)
        self.assertEqual((found.title, found.artist, found.album), ("Flamenco Sketches", "Miles Davis", "Kind of Blue"))
        self.assertEqual((found.album_artist, found.year, found.genre), ("Miles Davis Sextet", 1959, "Jazz"))
        self.assertEqual((found.track, found.disc, found.duration_ms, found.kind), (5, 1, 9000, "mp4"))
        self.assertTrue(found.has_picture)
        self.assertEqual(tags.read_picture(path).data, self.jpeg)

    def test_numeric_genre_and_utf16(self) -> None:
        items = {b"gnre": fakes.mp4_data(struct.pack(">H", 18), 0),
                 b"\xa9nam": fakes.mp4_data("Ünïcode".encode("utf-16-be"), 2)}
        found = tags.read_tags(fakes.make_mp4(self.tmp / "g.m4a", items))
        self.assertEqual((found.genre, found.title), ("Rock", "Ünïcode"))

    def test_64_bit_atom_size_and_version_1_header(self) -> None:
        mvhd = struct.pack(">I", 0x01000000) + b"\0" * 16 + struct.pack(">IQ", 1000, 123456) + b"\0" * 80
        moov_body = fakes.atom(b"mvhd", mvhd)
        moov = struct.pack(">I4sQ", 1, b"moov", 16 + len(moov_body)) + moov_body
        path = self.write("big.mp4", fakes.atom(b"ftyp", b"isom\0\0\0\0") + moov)
        self.assertEqual(tags.read_tags(path).duration_ms, 123456)

    def test_atoms_that_lie(self) -> None:
        good = fakes.make_mp4(self.tmp / "good.m4a", self.items()).read_bytes()
        # An atom that claims to be larger than its parent ends the walk of that parent; no exception, no hang.
        for size in (0, 1, 7, 0x7FFFFFFF, 0xFFFFFFFF):
            index = good.index(b"ilst") - 4
            data = good[:index] + struct.pack(">I", size) + good[index + 4:]
            tags.read_tags(self.write("lie.m4a", data))
        nested = b""
        for _depth in range(200):
            nested = fakes.atom(b"moov", nested)
        tags.read_tags(self.write("deep.mp4", fakes.atom(b"ftyp", b"isom") + nested))
        many = fakes.atom(b"ftyp", b"isom") + fakes.atom(b"moov", fakes.atom(b"free", b"") * 200000)
        started = time.monotonic()
        tags.read_tags(self.write("many.mp4", many))
        self.assertLess(time.monotonic() - started, 2.0)


class WavTest(TagCase):
    def test_info_and_duration(self) -> None:
        info = {"INAM": "Blue Monk", "IART": "Thelonious Monk", "IPRD": "Monk's Dream", "ICRD": "1963", "IGNR": "Jazz",
                "ITRK": "4"}
        found = tags.read_tags(fakes.make_wav(self.tmp / "a.wav", 2.5, info=info))
        self.assertEqual((found.title, found.artist, found.album), ("Blue Monk", "Thelonious Monk", "Monk's Dream"))
        self.assertEqual((found.year, found.genre, found.track, found.duration_ms, found.kind),
                         (1963, "Jazz", 4, 2500, "wav"))

    def test_chunk_sizes_that_lie(self) -> None:
        data = fakes.make_wav(self.tmp / "a.wav", 1.0).read_bytes()
        index = data.index(b"data") + 4
        cut = data[:index] + struct.pack("<I", 0xFFFFFFFF) + data[index + 4:]
        self.assertEqual(tags.read_tags(self.write("cut.wav", cut)).duration_ms, 1000)
        listing = b"RIFF" + struct.pack("<I", 100) + b"WAVE" + b"LIST" + struct.pack("<I", 0xFFFFFF00) + b"INFO"
        self.assertEqual(tags.read_tags(self.write("list.wav", listing)).title, "")


class HostileTest(TagCase):
    """No input makes a reader raise, read far beyond its caps or run for long."""

    def samples(self) -> dict[str, bytes]:
        jpeg = self.jpeg
        text = fakes.id3_text
        mp3 = fakes.make_mp3(self.tmp / "s.mp3", fakes.id3_tag(
            text("TIT2", "T") + text("TPE1", "A") + fakes.id3_frame("APIC", fakes.apic(jpeg))),
            id3v1=fakes.id3v1("t", "a", "b", "2000", 1, 1), audio_frames=4).read_bytes()
        flac = fakes.make_flac(self.tmp / "s.flac", ["TITLE=T", "ARTIST=A"], cover=jpeg).read_bytes()
        ogg = fakes.make_ogg_vorbis(self.tmp / "s.ogg", ["TITLE=T", "METADATA_BLOCK_PICTURE="
                                                         + fakes.b64(fakes.flac_picture(jpeg))]).read_bytes()
        opus = fakes.make_opus(self.tmp / "s.opus", ["TITLE=T"]).read_bytes()
        mp4 = fakes.make_mp4(self.tmp / "s.m4a", {b"\xa9nam": fakes.mp4_data(b"T"),
                                                  b"covr": fakes.mp4_data(jpeg, 13)}).read_bytes()
        wav = fakes.make_wav(self.tmp / "s.wav", 0.01, info={"INAM": "T"}).read_bytes()
        return {"s.mp3": mp3, "s.flac": flac, "s.ogg": ogg, "s.opus": opus, "s.m4a": mp4, "s.wav": wav}

    def test_every_truncation(self) -> None:
        for name, data in self.samples().items():
            path = self.tmp / ("cut-" + name)
            step = max(1, len(data) // 400)
            for length in list(range(0, min(len(data), 600))) + list(range(600, len(data), step)):
                path.write_bytes(data[:length])
                tags.read_tags(path)
                tags.read_picture(path)

    def test_random_damage(self) -> None:
        rng = random.Random(20240229)
        started = time.monotonic()
        for name, data in self.samples().items():
            path = self.tmp / ("fuzz-" + name)
            for _round in range(300):
                damaged = bytearray(data)
                for _flip in range(rng.randint(1, 8)):
                    position = rng.randrange(len(damaged))
                    choice = rng.random()
                    if choice < 0.5:
                        damaged[position] = rng.randrange(256)
                    elif choice < 0.8:
                        damaged[position:position + 4] = rng.choice(
                            [b"\xff\xff\xff\xff", b"\x7f\xff\xff\xff", b"\0\0\0\0", b"\0\0\0\x01", b"\x80\0\0\0"])
                    else:
                        del damaged[position:position + rng.randint(1, 16)]
                path.write_bytes(bytes(damaged))
                found = tags.read_tags(path)
                self.assertLessEqual(len(found.title), tags.MAX_TEXT_CHARS)
                picture = tags.read_picture(path)
                if picture is not None:
                    self.assertTrue(tags.sniff_image(picture.data))
                    self.assertLessEqual(len(picture.data), tags.MAX_PICTURE_BYTES)
        self.assertLess(time.monotonic() - started, 60)

    def test_random_bytes_and_empty_files(self) -> None:
        rng = random.Random(7)
        for extension in (".mp3", ".flac", ".ogg", ".m4a", ".wav", ".bin"):
            for size in (0, 1, 3, 10, 127, 128, 129, 4096):
                path = self.write("r" + extension, rng.randbytes(size))
                self.assertIsInstance(tags.read_tags(path), tags.Tags)
        for magic in (b"ID3", b"fLaC", b"OggS", b"RIFF\0\0\0\0WAVE", b"\0\0\0\x08ftyp", b"\xff\xfb"):
            path = self.write("m.bin", magic)
            tags.read_tags(path)
            path = self.write("m2.bin", magic + rng.randbytes(64))
            tags.read_tags(path)

    def test_a_large_file_is_not_read_whole(self) -> None:
        # 64 MiB of audio after a small tag: the reader looks at the tag, the first frames and the last bytes.
        path = self.tmp / "large.mp3"
        with open(path, "wb") as stream:
            stream.write(fakes.id3_tag(fakes.id3_text("TIT2", "Large")) + fakes.mpeg_frames(4))
            stream.seek(64 * 1024 * 1024)
            stream.write(b"\0")
        reads = []
        real_open = open

        class Counting(io.FileIO):
            def read(self, size=-1):
                data = super().read(size)
                reads.append(len(data))
                return data

        original = tags.open if hasattr(tags, "open") else None
        tags.open = lambda name, mode="rb": Counting(name, "r")            # type: ignore[attr-defined]
        try:
            self.assertEqual(tags.read_tags(path).title, "Large")
        finally:
            if original is None:
                del tags.open                                              # type: ignore[attr-defined]
            else:
                tags.open = original                                       # type: ignore[attr-defined]
        _ = real_open
        self.assertLess(sum(reads), 2 * 1024 * 1024)

    def test_missing_file_raises_oserror(self) -> None:
        with self.assertRaises(OSError):
            tags.read_tags(self.tmp / "nope.mp3")
        with self.assertRaises(OSError):
            tags.read_picture(self.tmp)


if __name__ == "__main__":
    unittest.main()
