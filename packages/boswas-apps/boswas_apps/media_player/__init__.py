"""Boswas Media Player: plays the user's music and videos (boswas-media-player(1)).

Modules without Qt: ``tags`` (ID3, Vorbis comments, MP4 atoms), ``playlist_io``
(M3U, M3U8, PLS), ``model`` (tracks, play queue, settings, playlists),
``library`` (the index of the library folders), ``opening`` (files, folders
and addresses into tracks). With Qt: ``backend`` (the player interface and
its Qt Multimedia implementation), ``controller``, ``mpris``, and the
widgets (``window``, ``pages``, ``transport``, ``widgets``, ``covers``).
"""

from .. import __version__

APP_ID = "com.boswas.MediaPlayer"

__all__ = ["APP_ID", "__version__"]
