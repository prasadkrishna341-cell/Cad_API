#!/usr/bin/env python3
"""Update kitealgo to the latest code.

Downloads the current branch and copies the project files over your working
copy. Your own files are never in the download, so they survive untouched:

    .env                  your API key and secret
    .kitealgo/            access token, cached candles, database, logs
    .venv/                the installed libraries

Usage:
    python update.py              update in place
    python update.py --dry-run    show what would change, change nothing
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = "https://github.com/prasadkrishna341-cell/Cad_API"
BRANCH = "claude/kite-algo-trading-setup-oazmh0"
ARCHIVE = f"{REPO}/archive/refs/heads/{BRANCH}.zip"

#: Never overwritten, even if a future version ships one of these.
PROTECTED = {".env", ".venv", ".kitealgo"}


def _is_protected(relative: Path) -> bool:
    return bool(PROTECTED.intersection(relative.parts))


def download(url: str, into: Path) -> Path:
    archive = into / "update.zip"
    print(f"Downloading {BRANCH} ...")
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            archive.write_bytes(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SystemExit(f"Download failed: {exc}\nCheck your internet connection.")
    print(f"  {archive.stat().st_size:,} bytes")
    return archive


def extract(archive: Path, into: Path) -> Path:
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(into)
    roots = [p for p in into.iterdir() if p.is_dir() and p.name != "__MACOSX"]
    if len(roots) != 1:
        raise SystemExit(f"Unexpected archive layout: {[p.name for p in roots]}")
    return roots[0]


def sync(source: Path, target: Path, dry_run: bool) -> tuple[int, int]:
    added = changed = 0
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        if _is_protected(relative):
            continue    # belt and braces: these are never in the archive anyway

        destination = target / relative
        if not destination.exists():
            added += 1
            verb = "new"
        elif destination.read_bytes() != item.read_bytes():
            changed += 1
            verb = "updated"
        else:
            continue

        print(f"  {verb:8s} {relative.as_posix()}")
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
    return added, changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would change without changing it")
    args = parser.parse_args()

    project = Path(__file__).resolve().parent
    if not (project / "kitealgo").is_dir():
        raise SystemExit(f"{project} does not look like the project folder.")

    with tempfile.TemporaryDirectory() as workspace:
        work = Path(workspace)
        source = extract(download(ARCHIVE, work), work)
        added, changed = sync(source, project, args.dry_run)

    # Report what was protected by naming what actually exists, rather than
    # implying files were skipped when they were never in the download.
    yours = [name for name in sorted(PROTECTED) if (project / name).exists()]

    if not added and not changed:
        print("\nAlready up to date.")
    else:
        action = "would be updated" if args.dry_run else "updated"
        print(f"\n{added} new, {changed} {action}.")

    if yours:
        print(f"Untouched (not part of the download): {', '.join(yours)}")
    if (added or changed) and not args.dry_run:
        print("\nVerify with:\n    python -m pytest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
