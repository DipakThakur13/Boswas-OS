"""Boswas Camera: take photos and record videos with a camera of the computer (boswas-camera(1)).

``storage`` (no Qt) knows where captures go, how they are named, the
settings and the free-space rules; ``backend`` puts the camera behind a
small interface (Qt Multimedia in the program, a fake in the tests);
``window`` is the user interface.
"""

from .. import __version__

APP_ID = "com.boswas.Camera"

__all__ = ["APP_ID", "__version__"]
