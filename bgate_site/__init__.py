"""The arcade — a static site that hosts the games this repo builds.

Builders Gate already exports playable Web builds (bgate_ui/webbuild.py) and
already knows every project on the machine (~/.bgate/projects.json). What was
missing was the last hop: turning "there is a build in export/web" into "there
is a URL a person can open". That is all this package does.

Deliberately static. No database, no accounts, no server — `bgate publish`
writes a directory of files that any static host serves, and Cloudflare Pages
in particular (see theme/_headers). The games are the dynamic part; the site
around them should never be the thing that breaks.
"""
from __future__ import annotations

from .builder import build, HOSTS, REBUILD_MODES
from .collect import discover, describe, site_config

# builder.py, not build.py: `from .build import build` would shadow the
# submodule with the function on the package, and every `from bgate_site import
# build as site_build` downstream would get the wrong object.
__all__ = ["discover", "describe", "site_config", "build", "HOSTS",
           "REBUILD_MODES"]
