"""The Qt Multimedia backend with real files, in a child process (skipped where the backend cannot start).

No audio device is needed: Qt plays into nothing when there is none. The
audio file is a WAV written by the test. The video part runs only where an
``ffmpeg`` program is installed to make a small clip; it is not needed for
anything else.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import PySide6.QtMultimedia  # noqa: F401
    MULTIMEDIA = True
except ImportError:
    MULTIMEDIA = False

FFMPEG = shutil.which("ffmpeg", path="/usr/bin:/bin")


def write_wav(path: Path, seconds: float) -> None:
    import math
    import struct
    import wave
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(n / 3))) for n in range(int(8000 * seconds))))


@unittest.skipUnless(MULTIMEDIA, "PySide6.QtMultimedia is not installed")
class RealBackendTest(unittest.TestCase):
    result: dict | None = None
    reason = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="mp-real-")
        tmp = Path(cls._tmp.name)
        audio, broken, video = tmp / "tone.wav", tmp / "broken.mp3", tmp / "clip.mp4"
        write_wav(audio, 3.0)
        broken.write_bytes(b"this is not an MP3 file at all" * 50)
        if FFMPEG:
            made = subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                                   "testsrc=duration=2:size=160x120:rate=10", "-c:v", "mpeg4", str(video)],
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            if made.returncode != 0 or not video.is_file():
                video = None
        else:
            video = None
        base = {key: value for key, value in os.environ.items() if not key.startswith("XDG_")}
        base.update(QT_QPA_PLATFORM="offscreen", HOME=str(tmp), XDG_RUNTIME_DIR=str(tmp))
        base.pop("DBUS_SESSION_BUS_ADDRESS", None)
        # The second attempt names Qt's PulseAudio backend: a machine with a broken PipeWire set-up (no
        # configuration files) crashes inside the PipeWire library with the default.
        for extra in ({}, {"QT_AUDIO_BACKEND": "pulseaudio"}):
            try:
                child = subprocess.run([sys.executable, "-B", str(HERE / "real_backend_child.py"), str(audio),
                                        str(video or ""), str(broken)], env={**base, **extra},
                                       stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
            except subprocess.TimeoutExpired:
                cls.reason = "the multimedia backend did not finish in time"
                continue
            line = next((line for line in child.stdout.decode("utf-8", "replace").splitlines()
                         if line.startswith("RESULT ")), None)
            if child.returncode == 0 and line is not None:
                cls.result = json.loads(line[7:])
                cls.has_video = video is not None
                return
            cls.reason = f"the multimedia backend could not start here (exit status {child.returncode})"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        if self.result is None:
            self.skipTest(self.reason)

    def test_audio_file_plays_seeks_pauses_and_ends(self) -> None:
        result = self.result
        self.assertTrue(result["available"])
        self.assertTrue(result["audio_playing"])
        self.assertAlmostEqual(result["audio_duration"], 3000, delta=100)
        self.assertTrue(result["audio_seekable"] and result["audio_seek"] and result["audio_paused"])
        self.assertTrue(result["audio_ended"])
        self.assertEqual(len(result["audio_tracks"]), 1)

    def test_a_file_that_is_not_media_reports_an_error_in_plain_words(self) -> None:
        self.assertTrue(self.result["broken_error"])
        message = self.result["broken_message"][0]
        self.assertTrue(message.endswith("."))
        self.assertNotIn("Traceback", message)

    def test_formats_are_reported(self) -> None:
        formats = self.result["formats"]
        self.assertTrue(formats["containers"] and formats["audio"])

    def test_video_frames_and_thumbnail(self) -> None:
        if not self.has_video:
            self.skipTest("no ffmpeg program to make a video clip with")
        result = self.result
        self.assertTrue(result["video_frames"] and result["video_flag"])
        self.assertEqual(result["video_frame_size"], [160, 120])
        self.assertAlmostEqual(result["video_duration"], 2000, delta=200)
        self.assertEqual(result["video_errors"], [])
        self.assertEqual(result["grab_count"], 2)
        self.assertEqual(result["grab_size"], [160, 120])
        self.assertAlmostEqual(result["grab_duration"], 2000, delta=200)
        self.assertTrue(result["grab_broken"])


if __name__ == "__main__":
    unittest.main()
