#!/usr/bin/env python3
"""Scan tracked/staged content without printing credential values. No dependencies."""
import argparse
from pathlib import PurePosixPath
import re
import subprocess
import sys

PATTERNS = (
    re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----"),
    re.compile(rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})"),
    re.compile(rb"\b(?:binyu|user|root)/[0-9]{4,}\b"),
)


def forbidden_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        (path.name.startswith(".env") and path.name != ".env.example")
        or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pem", ".key"}
        or any(part in {"artifacts", ".codex-backups", "backups"} for part in path.parts)
        or ".codex-backup-" in name
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Scan exact index blobs, including renamed files")
    args = parser.parse_args()
    command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"] if args.staged else ["git", "ls-files", "-z"]
    names = subprocess.check_output(command).decode().split("\0")
    failures = []
    for name in filter(None, names):
        if forbidden_path(name):
            failures.append(f"{name}: sensitive file must not be tracked")
            continue
        result = subprocess.run(["git", "show", f":{name}"], capture_output=True)
        if result.returncode:
            failures.append(f"{name}: cannot inspect index blob")
            continue
        for number, line in enumerate(result.stdout.splitlines(), 1):
            if any(pattern.search(line) for pattern in PATTERNS):
                failures.append(f"{name}:{number}: credential pattern detected (value hidden)")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("Credential check passed (index content; values never printed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
