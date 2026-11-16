"""The sound of a reminder: generated here, played by a helper process.

There is no sound file to install. ``wave_bytes(kind)`` computes a short
chime (16-bit mono PCM in a WAV container) for an alarm, a timer or a
reminder. ``main`` is the helper the service starts with ``--chime``: it
writes the chime into the runtime directory and plays it through
QtMultimedia. The helper is a process of its own so that the service
itself never loads the multimedia libraries, and so that an audio
back-end that hangs or crashes cannot stop reminders from being shown.
The helper ends when the sound is over, after ``--repeat`` times, or
when the service ends it because the alarm was answered.

Exit codes of the helper: 0 played, 3 QtMultimedia is not installed, 4 no
audio output, 5 the sound could not be played.
"""

from __future__ import annotations

import io
import math
import os
import stat
import struct
import sys
import tempfile
import wave
from pathlib import Path

from . import paths

RATE = 44100
KINDS = ("alarm", "timer", "reminder")
# (frequency in Hz, start in seconds, length in seconds) of each note
_NOTES = {
    "alarm": ((880.0, 0.00, 0.28), (1108.7, 0.16, 0.28), (1318.5, 0.32, 0.55), (880.0, 0.70, 0.28),
              (1108.7, 0.86, 0.28), (1318.5, 1.02, 0.70)),
    "timer": ((1046.5, 0.00, 0.35), (784.0, 0.22, 0.35), (1046.5, 0.44, 0.60)),
    "reminder": ((659.3, 0.00, 0.40), (987.8, 0.18, 0.75)),
}
_LENGTH = {"alarm": 2.0, "timer": 1.4, "reminder": 1.1}
MAX_REPEAT = 60
MAX_SECONDS = 120


def samples(kind: str) -> list[int]:
    """The chime as 16-bit samples: bell-like notes (a sine with two overtones that die away)."""
    total = int(_LENGTH[kind] * RATE)
    mix = [0.0] * total
    for frequency, start, length in _NOTES[kind]:
        first = int(start * RATE)
        count = min(int(length * RATE), total - first)
        for index in range(count):
            time = index / RATE
            attack = min(1.0, time / 0.006)
            decay = math.exp(-time * 5.5 / length)
            angle = 2 * math.pi * frequency * time
            tone = math.sin(angle) + 0.35 * math.sin(2 * angle) + 0.12 * math.sin(3 * angle)
            mix[first + index] += tone * attack * decay
    peak = max(1e-9, max(abs(value) for value in mix))
    scale = 0.6 * 32767 / peak
    return [int(value * scale) for value in mix]


def wave_bytes(kind: str) -> bytes:
    """A WAV file with the chime of ``kind`` (alarm, timer or reminder)."""
    if kind not in KINDS:
        raise ValueError(f"unknown chime {kind!r}")
    data = samples(kind)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(struct.pack(f"<{len(data)}h", *data))
    return buffer.getvalue()


def chime_file(kind: str) -> Path:
    """The chime as a file in a private directory (written when it is not there yet)."""
    runtime = paths.runtime_dir()
    if runtime is not None:
        directory = runtime / "boswas-reminders"
    else:
        directory = Path(tempfile.gettempdir()) / f"boswas-reminders-{os.getuid()}"
    paths.ensure_private_dir(directory)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError("the directory for the chime is not private")
    target = directory / f"{kind}.wav"
    data = wave_bytes(kind)
    try:
        if target.read_bytes() == data:
            return target
    except OSError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=f".{kind}.", suffix=".wav", dir=directory)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.replace(temporary, target)
    return target


def main(kind: str, repeat: int = 1) -> int:
    """Play the chime ``repeat`` times and return an exit code (see the module text)."""
    if kind not in KINDS:
        sys.stderr.write(f"boswas-reminders: unknown chime {kind!r}\n")
        return 2
    repeat = max(1, min(MAX_REPEAT, int(repeat)))
    from PySide6.QtCore import QCoreApplication, QTimer, QUrl
    try:
        from PySide6.QtMultimedia import QMediaDevices, QSoundEffect
    except ImportError:
        return 3
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    if not QMediaDevices.audioOutputs():
        return 4
    try:
        path = chime_file(kind)
    except OSError:
        return 5
    effect = QSoundEffect(app)
    effect.setSource(QUrl.fromLocalFile(str(path)))
    effect.setLoopCount(repeat)
    effect.setVolume(0.9)
    result = {"code": 0, "started": False}

    def playing_changed() -> None:
        if effect.isPlaying():
            result["started"] = True
        elif result["started"]:
            app.quit()

    def status_changed() -> None:
        if effect.status() == QSoundEffect.Status.Error:
            result["code"] = 5
            app.quit()
        elif effect.status() == QSoundEffect.Status.Ready and not result["started"]:
            effect.play()

    effect.playingChanged.connect(playing_changed)
    effect.statusChanged.connect(status_changed)
    status_changed()
    QTimer.singleShot(min(MAX_SECONDS, int(_LENGTH[kind] * repeat) + 5) * 1000, app.quit)
    app.exec()
    effect.stop()
    return int(result["code"])
