#!/usr/bin/env python3
"""Version helper for python-rako.

`python_rako/__version__.py` is the single source of truth for the package
version (a plain ``__version__ = "X.Y.Z"`` assignment, read by hatchling via
``[tool.hatch.version]`` and imported by ``python_rako.__init__``). This
script is the only supported way to change it, and the only supported way to
check it against a release tag.

Usage:
    scripts/bump_version.py bump major|minor|patch
        Bump the given part of the version in python_rako/__version__.py
        and print the new version. Does NOT commit or tag - see RELEASING.md.

    scripts/bump_version.py check TAG
        Verify that TAG (accepts an optional leading "v", e.g. "v0.5.0" or
        "0.5.0") matches the version recorded in python_rako/__version__.py.
        Exits non-zero with a loud error message on any mismatch. Used by
        the release workflow to refuse to publish a mismatched build instead
        of silently rewriting the version file.

    scripts/bump_version.py current
        Print the current version and exit.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "python_rako" / "__version__.py"
VERSION_RE = re.compile(r'^__version__\s*=\s*"(?P<version>\d+\.\d+\.\d+)"\s*$')


def read_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8").strip()
    match = VERSION_RE.match(text)
    if not match:
        raise SystemExit(
            f"ERROR: {VERSION_FILE} does not contain a valid "
            f"'__version__ = \"X.Y.Z\"' assignment. Found: {text!r}"
        )
    return match.group("version")


def write_version(new_version: str) -> None:
    VERSION_FILE.write_text(f'__version__ = "{new_version}"\n', encoding="utf-8")


def bump(part: str) -> str:
    current = read_version()
    major, minor, patch = (int(x) for x in current.split("."))
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    else:  # pragma: no cover - argparse restricts choices
        raise SystemExit(f"ERROR: unknown bump part {part!r}")
    new_version = f"{major}.{minor}.{patch}"
    write_version(new_version)
    return new_version


def normalise_tag(tag: str) -> str:
    return tag[1:] if tag[:1] in ("v", "V") else tag


def check_tag(tag: str) -> None:
    current = read_version()
    tag_version = normalise_tag(tag)
    if tag_version != current:
        print(
            "ERROR: release tag does not match python_rako/__version__.py - refusing to "
            "publish.\n"
            f"  git tag:             {tag!r} (normalised: {tag_version!r})\n"
            f"  __version__.py has:  {current!r}\n"
            "Fix by bumping the version (scripts/bump_version.py bump <major|minor|patch>), "
            "committing, and re-tagging/re-releasing so the tag matches __version__.py "
            "exactly (see RELEASING.md).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"OK: tag {tag!r} matches __version__.py ({current!r})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bump_parser = subparsers.add_parser("bump", help="Bump major, minor, or patch version")
    bump_parser.add_argument("part", choices=["major", "minor", "patch"])

    check_parser = subparsers.add_parser(
        "check", help="Verify a release tag matches __version__.py"
    )
    check_parser.add_argument("tag")

    subparsers.add_parser("current", help="Print the current version")

    args = parser.parse_args(argv)

    if args.command == "bump":
        new_version = bump(args.part)
        print(f"Bumped version to {new_version}")
    elif args.command == "check":
        check_tag(args.tag)
    elif args.command == "current":
        print(read_version())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
