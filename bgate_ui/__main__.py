"""python -m bgate_ui [--port 7788] — run the dashboard."""
import sys

from .app import serve

port = 7788
if "--port" in sys.argv:
    port = int(sys.argv[sys.argv.index("--port") + 1])
serve(port=port)
