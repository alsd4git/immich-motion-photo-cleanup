#!/usr/bin/env python3
"""Plan or execute reversible, full-file-proven Immich trash operations."""

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from immich_api import ImmichAPI, read_key
from inventory import LibraryPaths, asset_id
from run_batches import atomic_json
from sidecars import read_sidecar
from trash_plan import (
    Validator,
    current_state,
    evidence_from_run,
    make_plan,
    validate_clip_limit,
)


def now():
    return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, path, header):
        self.path = Path(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, "r+")
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.getuid()
            ):
                raise ValueError(
                    "journal must be a private regular file owned by this user"
                )
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.events = [json.loads(line) for line in self.file if line.strip()]
            if self.events:
                if self.events[0] != header:
                    raise ValueError(
                        "journal belongs to another plan, server, owner or tool revision"
                    )
            else:
                self.append(header)
        except BaseException:
            self.file.close()
            raise

    def append(self, event):
        self.file.seek(0, os.SEEK_END)
        self.file.write(json.dumps(event, sort_keys=True) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())
        self.events.append(event)

    def record(self, status, ids, **details):
        self.append({"time": now(), "status": status, "ids": ids, **details})

    def statuses(self):
        statuses = {}
        for event in self.events[1:]:
            for identifier in event["ids"]:
                statuses[identifier] = event["status"]
        return statuses

    def close(self):
        self.file.close()


def check_api_asset(api, row, state, trashed=False):
    response = api.get_asset(row["id"])
    if any(
        response.get(key) != expected
        for key, expected in (
            ("id", row["id"]),
            ("ownerId", row["owner_id"]),
            ("type", row["type"]),
            ("originalPath", row["path"]),
            ("isTrashed", trashed),
            ("visibility", row["visibility"]),
            ("isOffline", False),
        )
    ):
        raise ValueError("API and database state differ")
    if response.get("livePhotoVideoId") != row["linked_video_id"]:
        raise ValueError("live photo relationship changed")
    if not trashed and state[row["id"]]["deleted_at"] is not None:
        raise ValueError("asset is no longer active")


def backup_sidecars(rows, paths, directory):
    if not any(row.get("sidecars") for row in rows):
        return
    paths.check_output(directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("sidecar backup directory must be private and not a symlink")
    for row in rows:
        for sidecar in row.get("sidecars", []):
            content = read_sidecar(paths.resolve(Path(sidecar["path"])))
            if (
                len(content) != sidecar["bytes"]
                or hashlib.sha256(content).hexdigest() != sidecar["sha256"]
            ):
                raise ValueError("sidecar changed before backup")
            target = directory / (sidecar["sha256"] + ".bin")
            if not target.exists():
                fd = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
            if (
                target.is_symlink()
                or target.stat().st_mode & 0o077
                or target.read_bytes() != content
            ):
                raise ValueError("sidecar backup verification failed")
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def process(
    plan,
    api,
    load_state,
    paths,
    journal,
    owner,
    batch_size=25,
    max_batches=1,
    restore=False,
):
    limit = validate_clip_limit(plan.get("max_clip_mib"))
    items = [r for r in plan["eligible"] if r["owner_id"] == owner]
    if len({r["video_id"] for r in items}) != len(items):
        raise ValueError("plan contains duplicate video IDs")
    for row in items:
        for key in ("owner_id", "video_id", "photo_id", "linked_video_id"):
            asset_id(row[key])
    statuses = journal.statuses()
    # Never replay an ambiguous write. Reconcile against both DB and API first.
    pending_status = "restore_pending" if restore else "trash_pending"
    pending = [
        r
        for r in items
        if statuses.get(r["video_id"]) in {"trash_pending", "restore_pending"}
    ]
    if pending:
        api.preflight(owner, for_restore=restore)
        state = load_state({r["video_id"] for r in pending})
        for row in pending:
            current = state.get(row["video_id"])
            was_restore = statuses[row["video_id"]] == "restore_pending"
            expected = "active" if was_restore else "trashed"
            if (
                not current
                or current["status"] != expected
                or current["owner_id"] != owner
            ):
                raise ValueError(
                    "unresolved pending request; inspect journal and server before retrying"
                )
            check_api_asset(api, current, state, trashed=not was_restore)
            journal.record(
                "restored" if was_restore else "trashed",
                [row["video_id"]],
                reconciled=True,
            )
        statuses = journal.statuses()
    wanted = {"trashed"} if restore else {None}
    remaining = [r for r in items if statuses.get(r["video_id"]) in wanted]
    batches = 0
    for start in range(0, len(remaining), batch_size):
        if max_batches and batches >= max_batches:
            break
        batches += 1
        retention = api.preflight(owner, for_restore=restore)
        batch = remaining[start : start + batch_size]
        selected_ids = {
            row[key]
            for row in batch
            for key in ("video_id", "photo_id", "linked_video_id")
        }
        state = load_state(selected_ids)
        validator = None if restore else Validator(state, paths, limit)
        accepted = []
        for row in batch:
            if restore:
                current = state.get(row["video_id"])
                if (
                    not current
                    or current["status"] != "trashed"
                    or current["owner_id"] != owner
                ):
                    raise ValueError(
                        "restore target is missing, no longer trashed or belongs to another owner"
                    )
                check_api_asset(api, current, state, trashed=True)
            else:
                try:
                    validator.verify(row, verify_media=False)
                except (ValueError, OSError) as error:
                    journal.record("skipped", [row["video_id"]], reason=str(error))
                    continue
                for identifier in (
                    row["video_id"],
                    row["photo_id"],
                    row["linked_video_id"],
                ):
                    check_api_asset(api, state[identifier], state)
            accepted.append(row)
        if not accepted:
            continue
        # Recheck associations and bytes after the API reads, as close to the
        # write as possible. Immich's API has no atomic conditional-delete API.
        latest = load_state(selected_ids)
        for row in accepted:
            for identifier in (
                row["video_id"],
                row["photo_id"],
                row["linked_video_id"],
            ):
                if latest.get(identifier) != state.get(identifier):
                    raise ValueError(
                        "asset changed during preflight; no batch write attempted"
                    )
        if not restore:
            fresh_validator = Validator(latest, paths, limit)
            for row in accepted:
                fresh_validator.verify(row)
            backup_sidecars(accepted, paths, journal.path.parent / "sidecar-backups")
        retention = api.preflight(owner, for_restore=restore)
        ids = [r["video_id"] for r in accepted]
        journal.record(pending_status, ids, retention_days=retention["days"])
        # These are the only write operations in this tool, with no automatic retry.
        if restore:
            api.restore(ids)
        else:
            api.trash(ids)
        after = load_state(selected_ids)
        for identifier in ids:
            row = after.get(identifier)
            expected = "active" if restore else "trashed"
            if not row or row["status"] != expected or row["owner_id"] != owner:
                raise ValueError(
                    "write outcome unconfirmed; resume to reconcile the journal"
                )
            check_api_asset(api, row, after, trashed=not restore)
        journal.record("restored" if restore else "trashed", ids)
        print(
            f"{'Restored' if restore else 'Trashed'} batch of {len(ids)} assets",
            flush=True,
        )
    return dict(Counter(journal.statuses().values()))


def code_digest():
    digest = hashlib.sha256()
    for name in (
        "inventory.py",
        "media.py",
        "run_batches.py",
        "sidecars.py",
        "trash_state.sql",
        "trash_plan.py",
        "immich_api.py",
        "trash.py",
    ):
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "trash", "restore"))
    parser.add_argument(
        "--run", type=Path, help="completed run_batches.py directory, for plan"
    )
    parser.add_argument("--plan", type=Path, help="plan.json produced by this command")
    parser.add_argument("--output", type=Path, help="new private plan directory")
    parser.add_argument("--immich-db-container", required=True)
    parser.add_argument("--library-root", type=Path, action="append", default=[])
    parser.add_argument("--path-map", action="append", default=[])
    parser.add_argument("--owner", type=asset_id)
    parser.add_argument(
        "--preserve-sidecars",
        action="store_true",
        help="allow sidecars after hashing them; execution backs them up privately",
    )
    parser.add_argument("--url", help="Immich API URL ending in /api")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument(
        "--plan-sha256", help="reviewed plan digest, required for writes"
    )
    parser.add_argument(
        "--execute", action="store_true", help="explicitly enable API writes"
    )
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument(
        "--max-batches", type=int, default=1, help="0 for all; default 1"
    )
    args = parser.parse_args(argv)
    journal = None
    try:
        paths = LibraryPaths(args.library_root, args.path_map)

        def load_state(selected_ids=None):
            return current_state(args.immich_db_container, selected_ids)

        if args.operation == "plan":
            if not args.run or not args.output or args.execute:
                raise ValueError("plan requires --run and --output, without --execute")
            paths.check_output(args.output)
            if args.output.resolve().is_relative_to(Path(__file__).resolve().parent):
                raise ValueError("private plans must stay outside the Git checkout")
            progress = json.loads((args.run / "progress.json").read_text())
            if not isinstance(progress, dict) or progress.get("status") != "complete":
                raise ValueError("trash planning requires a completed batch audit")
            config = progress.get("config")
            if not isinstance(config, dict):
                raise ValueError("completed run has no audited configuration")
            max_clip_mib = validate_clip_limit(config.get("max_clip_mib"))
            evidence = evidence_from_run(args.run)
            selected_ids = {
                row[key]
                for options in evidence.values()
                for row in options
                for key in ("video_id", "photo_id", "linked_video_id")
            }
            result = make_plan(
                evidence,
                load_state(selected_ids),
                paths,
                args.owner,
                args.preserve_sidecars,
                max_clip_mib,
            )
            result["created_at"] = now()
            args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
            atomic_json(args.output / "plan.json", result)
            print(
                json.dumps(
                    {
                        "eligible": len(result["eligible"]),
                        "excluded": len(result["excluded"]),
                        "bytes": result["eligible_bytes"],
                        "owners": result["owners"],
                        "exclusion_reasons": result["exclusion_reasons"],
                        "sidecar_comparisons": result["sidecar_comparisons"],
                    }
                )
            )
            print(
                "Plan SHA256: "
                + hashlib.sha256((args.output / "plan.json").read_bytes()).hexdigest()
            )
            return 0
        if not all((args.plan, args.owner, args.url, args.api_key_file, args.journal)):
            raise ValueError(
                "trash/restore requires --plan, --owner, --url, --api-key-file and --journal"
            )
        if not 1 <= args.batch_size <= 100 or args.max_batches < 0:
            raise ValueError(
                "batch-size must be 1..100; max-batches must be nonnegative"
            )
        paths.check_output(args.journal)
        if args.journal.resolve().is_relative_to(Path(__file__).resolve().parent):
            raise ValueError("private journals must stay outside the Git checkout")
        data = args.plan.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        plan = json.loads(data)
        if plan.get("schema") != 1:
            raise ValueError("unsupported plan schema")
        validate_clip_limit(plan.get("max_clip_mib"))
        api = ImmichAPI(args.url, read_key(args.api_key_file))
        retention = api.preflight(args.owner, for_restore=args.operation == "restore")
        if not args.execute:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "plan_sha256": digest,
                        "retention_days": retention["days"],
                        "owner_candidates": sum(
                            r["owner_id"] == args.owner for r in plan["eligible"]
                        ),
                    }
                )
            )
            return 0
        if digest != args.plan_sha256:
            raise ValueError("--plan-sha256 must match the reviewed plan")
        if args.operation == "restore" and not args.journal.is_file():
            raise ValueError("restore requires an existing execution journal")
        header = {
            "schema": 1,
            "plan_sha256": digest,
            "server": api.url,
            "owner": args.owner,
            "tool_sha256": code_digest(),
            "database_container": args.immich_db_container,
            "library_roots": [str(path.resolve()) for path in args.library_root],
            "path_maps": args.path_map,
        }
        journal = Journal(args.journal, header)
        print(
            json.dumps(
                process(
                    plan,
                    api,
                    load_state,
                    paths,
                    journal,
                    args.owner,
                    args.batch_size,
                    args.max_batches,
                    args.operation == "restore",
                )
            )
        )
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 2
    finally:
        if journal:
            journal.close()


if __name__ == "__main__":
    sys.exit(main())
