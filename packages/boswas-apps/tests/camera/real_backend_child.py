"""Run by test_real_backend.py in a process of its own: the Qt Multimedia camera backend on this machine.

Prints one JSON object. Whatever cameras the machine has are listed but
never started: a test must not turn a camera on.
"""

from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from boswas_apps.camera.backend import QtCameraBackend  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv[:1])
    backend = QtCameraBackend()
    errors: list[str] = []
    backend.error.connect(errors.append)
    failures: list[str] = []
    backend.photo_failed.connect(failures.append)
    backend.recording_failed.connect(failures.append)
    cameras = backend.cameras()
    result = {"cameras": len(cameras), "current": backend.current(), "active": backend.is_active(),
              "state": backend.recording_state(), "extension": backend.video_extension(),
              "microphone": backend.has_microphone()}
    features = backend.features()
    result["features"] = [features.zoom, list(features.flash_modes), features.torch, features.exposure]
    backend.start("a camera that does not exist")
    QCoreApplication.processEvents()
    result["start_error"] = errors[:1]
    backend.capture_photo(os.path.join(sys.argv[1], "photo.jpg"))
    backend.start_recording(os.path.join(sys.argv[1], "video" + backend.video_extension()), "high", True)
    QCoreApplication.processEvents()
    result["failures"] = failures
    result["files"] = sorted(os.listdir(sys.argv[1]))
    backend.set_photo_size((640, 480))
    backend.set_photo_size(None)
    backend.set_microphone_muted(True)
    backend.play_shutter_sound()
    backend.shutdown()
    print("RESULT " + json.dumps(result))
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
