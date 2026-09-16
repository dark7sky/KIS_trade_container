"""Reject environment files and runtime data in staged content / pushed history."""

import argparse
import subprocess
import sys
from pathlib import PurePosixPath


def forbidden(name):
    path = PurePosixPath(name)
    if path.name == ".env.example":
        return False
    return (
        path.name == ".env"
        or path.name.startswith(".env.")
        or any(p in {"data", "runtime", ".venv", "__pycache__"} for p in path.parts)
        or any(
            path.name.endswith(ext)
            for ext in (".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".db", ".pem", ".key")
        )
    )


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true")
    group.add_argument("--history")
    args = parser.parse_args()
    command = (
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
        if args.staged
        else ["git", "log", "--format=", "--name-only", "-z", args.history, "--"]
    )
    result = subprocess.run(command, check=True, capture_output=True)
    names = result.stdout.decode("utf-8", errors="replace").split("\0")
    if any(forbidden(name.strip("\r\n")) for name in names if name.strip("\r\n")):
        print(
            "Blocked: environment/secret/runtime files are present. Remove them from Git before committing or pushing.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
