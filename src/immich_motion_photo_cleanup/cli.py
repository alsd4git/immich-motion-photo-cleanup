"""Command entry point; operation parsers retain their canonical validation."""

import argparse
import json
import os
import platform
import shutil

from . import __version__


def doctor(argv=None):
    parser = argparse.ArgumentParser(prog="immich-motion-photo-cleanup doctor")
    parser.add_argument("--database", action="store_true", help="also require Docker")
    parser.add_argument("--json", action="store_true", help="print structured diagnostics")
    args = parser.parse_args(argv)
    tools = {name: bool(shutil.which(name)) for name in ("exiftool", "ffprobe", "docker")}
    required = ["exiftool", "ffprobe"] + (["docker"] if args.database else [])
    result = {
        "version": __version__,
        "python": platform.python_version(),
        "posix": os.name == "posix",
        "tools": tools,
        "api_key_only_supported": False,
        "ready": os.name == "posix" and all(tools[name] for name in required),
    }
    if args.json:
        print(json.dumps(result))
    else:
        print(f"Immich Motion Photo Cleanup {__version__}")
        print(f"Python: {result['python']}; POSIX: {result['posix']}")
        for name, found in tools.items():
            print(f"{name}: {'found' if found else 'missing'}")
        print("Checks executable availability only, not database access or media health.")
        print("API-key-only analysis is not implemented. No server was contacted.")
    return 0 if result["ready"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="immich-motion-photo-cleanup",
        description="Audit Motion Photos and explicitly plan reversible cleanup.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("audit", "run a read-only, resumable batched audit"),
        ("plan", "create a private plan from a completed audit"),
        ("trash", "preview, or explicitly execute, reviewed trash operations"),
        ("restore", "preview, or explicitly restore, this tool's recorded actions"),
        ("doctor", "check local requirements without contacting Immich"),
    ):
        commands.add_parser(name, help=description, add_help=False)
    args, remainder = parser.parse_known_args(argv)
    if args.command == "doctor":
        return doctor(remainder)
    if args.command == "audit":
        from .run_batches import main as audit_main

        return audit_main(remainder, prog=f"{parser.prog} audit")
    from .trash import main as trash_main

    return trash_main([args.command, *remainder], prog=parser.prog)
