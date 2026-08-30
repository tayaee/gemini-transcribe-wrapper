"""Print the latest version of a PyPI package from its JSON metadata.

Usage:
    python get-pypi-version.py <package-name>

Reads https://pypi.org/pypi/<package-name>/json and prints info.version.
PyPI returns the JSON as a single very long line (>8KB) which trips
findstr's "Line is too long" limit, so we parse it with Python instead.
"""

from __future__ import annotations

import json
import sys
import urllib.request


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: get-pypi-version.py <package-name>", file=sys.stderr)
        return 2
    pkg = sys.argv[1]
    url = f"https://pypi.org/pypi/{pkg}/json"
    with urllib.request.urlopen(url) as r:
        data = json.load(r)
    print(data["info"]["version"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
