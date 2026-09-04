"""Disposable project + dashboard for browser tests; never touches user data."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix="bgate-e2e-"))
os.environ["BGATE_ROOT"] = str(root)

from bgate_core.store import project, scaffold  # noqa: E402
from bgate_core.board import seats  # noqa: E402

scaffold.new_project(root, "Browser smoke", kind="2d")
project.init(root, "Browser smoke", engine="godot", dimension="2d")
seats.apply_layout(root)

from bgate_ui.app import serve  # noqa: E402

serve(port=7791)
