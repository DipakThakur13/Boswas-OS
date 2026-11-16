"""Run by test_real_backend.py in a process of its own: the Qt Multimedia backend plays real files.

Prints one JSON object. A process of its own, because a multimedia stack
that is installed badly can take the whole process down with it.
"""

from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from boswas_apps.media_player.backend import QtFrameGrabber, QtPlayerBackend, describe_backend  # noqa: E402


def wait(condition, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return bool(condition())


def main() -> int:
    audio, video, broken = sys.argv[1], sys.argv[2], sys.argv[3]
    app = QApplication(sys.argv[:1])
    result: dict = {"formats": describe_backend()}
    backend = QtPlayerBackend()
    result["available"] = backend.available()
    result["devices"] = len(backend.audio_devices())
    seen = {"states": [], "statuses": [], "errors": [], "metadata": {}, "frames": 0, "video": []}
    backend.state_changed.connect(seen["states"].append)
    backend.status_changed.connect(seen["statuses"].append)
    backend.error.connect(seen["errors"].append)
    backend.metadata_changed.connect(lambda data: seen["metadata"].update(
        {key: value for key, value in data.items() if isinstance(value, str)}))
    backend.video_changed.connect(seen["video"].append)
    frames: list = []
    backend.frame_changed.connect(lambda frame: frames.append(frame) if frame is not None else None)

    backend.set_volume(0.5)
    backend.set_rate(1.0)
    backend.set_source(audio)
    backend.play()
    result["audio_playing"] = wait(lambda: backend.state() == "playing" and backend.position() > 100, 8)
    result["audio_duration"] = backend.duration()
    result["audio_seekable"] = backend.seekable()
    backend.seek(1000)
    result["audio_seek"] = wait(lambda: backend.position() >= 1000, 3)
    backend.pause()
    result["audio_paused"] = wait(lambda: backend.state() == "paused", 3)
    backend.set_rate(2.0)
    backend.play()
    result["audio_ended"] = wait(lambda: "ended" in seen["statuses"], 8)
    result["audio_metadata"] = dict(seen["metadata"])
    result["audio_tracks"] = backend.audio_tracks()

    seen["statuses"].clear()
    backend.set_source(broken)
    backend.play()
    result["broken_error"] = wait(lambda: bool(seen["errors"]), 8)
    result["broken_message"] = seen["errors"][:1]

    if video:
        seen["errors"].clear()
        backend.set_rate(1.0)
        backend.set_source(video)
        backend.play()
        result["video_frames"] = wait(lambda: len(frames) >= 3, 8)
        result["video_flag"] = backend.has_video()
        image = frames[-1].toImage() if frames else QImage()
        result["video_frame_size"] = [image.width(), image.height()]
        result["video_duration"] = backend.duration()
        result["video_errors"] = list(seen["errors"])
        backend.stop()
        grabbed: list = []
        grabber = QtFrameGrabber()
        grabber.grab(video, lambda picture, duration: grabbed.append((picture, duration)))
        grabber.grab(broken, lambda picture, duration: grabbed.append((picture, duration)))
        wait(lambda: len(grabbed) == 2, 20)
        result["grab_count"] = len(grabbed)
        if grabbed:
            picture, duration = grabbed[0]
            result["grab_size"] = [picture.width(), picture.height()] if picture is not None else None
            result["grab_duration"] = duration
        if len(grabbed) > 1:
            result["grab_broken"] = grabbed[1][0] is None
        grabber.shutdown()
    backend.shutdown()
    print("RESULT " + json.dumps(result))
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
