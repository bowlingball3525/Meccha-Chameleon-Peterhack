#!/usr/bin/env python3
"""Write VERSION from the current git HEAD (run before committing releases)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = ROOT / "VERSION"


def main() -> int:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"bump_version: git failed: {exc}", file=sys.stderr)
        return 1
    sha = out.stdout.strip()
    if not sha:
        print("bump_version: empty SHA", file=sys.stderr)
        return 1
    VERSION.write_text(sha + "\n", encoding="utf-8")
    print(f"VERSION -> {sha[:7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
