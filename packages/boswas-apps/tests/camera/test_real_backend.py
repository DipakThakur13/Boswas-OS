"""The Qt Multimedia camera backend in a child process: it must behave without a camera (none is ever started)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

try:
    import PySide6.QtMultimedia  # noqa: F401
    MULTIMEDIA = True
except ImportError:
    MULTIMEDIA = False


@unittest.skipUnless(MULTIMEDIA, "PySide6.QtMultimedia is not installed")
class RealCameraBackendTest(unittest.TestCase):
    def test_backend_without_a_started_camera(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cam-real-") as folder:
            base = {key: value for key, value in os.environ.items() if not key.startswith("XDG_")}
            base.update(QT_QPA_PLATFORM="offscreen", HOME=folder, XDG_RUNTIME_DIR=folder)
            base.pop("DBUS_SESSION_BUS_ADDRESS", None)
            captures = Path(folder) / "captures"
            captures.mkdir()
            result, reason = None, ""
            # See the Media Player test: the second attempt avoids a PipeWire library without configuration.
            for extra in ({}, {"QT_AUDIO_BACKEND": "pulseaudio"}):
                try:
                    child = subprocess.run([sys.executable, "-B", str(HERE / "real_backend_child.py"), str(captures)],
                                           env={**base, **extra}, stdin=subprocess.DEVNULL, capture_output=True,
                                           timeout=60)
                except subprocess.TimeoutExpired:
                    reason = "the multimedia backend did not finish in time"
                    continue
                line = next((line for line in child.stdout.decode("utf-8", "replace").splitlines()
                             if line.startswith("RESULT ")), None)
                if child.returncode == 0 and line is not None:
                    result = json.loads(line[7:])
                    break
                reason = f"the multimedia backend could not start here (exit status {child.returncode})"
            if result is None:
                self.skipTest(reason)
        self.assertEqual((result["current"], result["active"], result["state"]), ("", False, "stopped"))
        self.assertIn(result["extension"], (".mp4", ".mkv", ".webm", ".mov", ".avi"))
        self.assertEqual(result["features"], [None, [], False, False])
        self.assertEqual(result["start_error"], ["The camera is not connected."])
        self.assertEqual(result["failures"], ["The camera is not ready.", "The camera is not ready."])
        self.assertEqual(result["files"], [])                      # nothing was written without a camera


if __name__ == "__main__":
    unittest.main()
