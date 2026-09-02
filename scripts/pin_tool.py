"""Record the SHA-256 of a tool archive, so toolbin can verify it later.

    python scripts/pin_tool.py ffmpeg

WHY THIS IS A SCRIPT AND NOT SOMETHING THE APP DOES. A checksum computed at
install time from the file you just downloaded proves nothing — it is the
definition of trusting the download. The digest has to be recorded by a human,
once, from a release they chose, and committed. Then every later install is
checked against a value that came from somewhere other than that download.

Prints the digest and the size. Paste both into the Tool entry in
bgate_core/runtime/toolbin.py; this deliberately does not edit source, because a script
that rewrites a security constant is a script nobody reads the diff of.
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from bgate_core.runtime import toolbin      # noqa: E402


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in toolbin.TOOLS:
        print(f"usage: pin_tool.py <{'|'.join(toolbin.TOOLS)}>")
        return 2
    tool = toolbin.TOOLS[sys.argv[1]]
    print(f"fetching {tool.url}")
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(tool.url, timeout=300) as r:
        length = int(r.headers.get("content-length") or 0)
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if length:
                pct = 100 * total / length
                print(f"\r  {total / 1048576:6.1f} / {length / 1048576:.1f} MB "
                      f"({pct:4.1f}%)", end="", flush=True)
    print()
    print(f"  size    {total / 1048576:.1f} MB  ({total} bytes)")
    print(f"  sha256  {digest.hexdigest()}")
    print(f"\nPaste into TOOLS['{tool.name}'] in bgate_core/runtime/toolbin.py:")
    print(f'    sha256="{digest.hexdigest()}",')
    print(f"    size_mb={round(total / 1048576)},")
    return 0


if __name__ == "__main__":
    sys.exit(main())
