"""The Builders Gate studio-floor assets: img/ and audio/, ~30MB of decoration.

Installed as `builders-gate-floor-assets` (the `floor` extra of the main
package). The dashboard imports this module and mounts `path()` under
/static/img/floor and /static/audio/floor; nothing else reads it.
"""
from pathlib import Path


def path() -> str:
    """The directory holding img/ and audio/ — what the dashboard mounts."""
    return str(Path(__file__).resolve().parent)
