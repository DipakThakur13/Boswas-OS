"""Shared set-up of the boswas_fluent tests.

The tests add their own import path, so they run without PYTHONPATH:

    cd desktop/ui/qt && python3 -B -m unittest discover -s tests

The token, style sheet and contrast tests need only the standard library.
The widget tests need PySide6 and skip themselves without it (PySide6 is
not a build dependency of boswas-branding); they run on Qt's offscreen
platform and never touch the user's configuration.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

QT_DIR = Path(__file__).resolve().parents[1]
REPO = QT_DIR.parents[2]
TOKENS = QT_DIR.parent / "tokens.json"
if str(QT_DIR) not in sys.path:
    sys.path.insert(0, str(QT_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Never the configuration, cache or services of whoever runs the tests.
for name in ("BOSWAS_UI_TOKENS", "DBUS_SESSION_BUS_ADDRESS"):
    os.environ.pop(name, None)

try:
    import PySide6.QtWidgets  # noqa: F401
    PYSIDE = True
except ImportError:
    PYSIDE = False

SKIP_QT = "PySide6 is not installed"
