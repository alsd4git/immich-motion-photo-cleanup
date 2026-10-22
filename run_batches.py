#!/usr/bin/env python3
"""Audit a frozen inventory in resumable batches. Writes private reports only."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from inventory import (
    LibraryPaths,
    database_inventory,
    filesystem_inventory,
    read_inventory,
    serialize_inventory,
)
from motion_audit import READ_ERRORS, arguments, candidates, error_text
from motion_audit import main as audit_main


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        try:
            json.dump(value, temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


def tool_digest():
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parent
    for name in (
        "inventory.py",
        "inventory.sql",
        "media.py",
        "motion_audit.py",
        "run_batches.py",
    ):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def aggregate(root, completed):
    counts = Counter()
    cameras = Counter()
    extraction = Counter()
    reasons = Counter()
    proposals = {}
    errors = []
    analyzed = 0
    for batch in completed:
        report_path = (root / batch["report"]).resolve()
        if not report_path.is_relative_to(root.resolve()):
            raise ValueError("batch report path escapes the run directory")
        report = json.loads(report_path.read_text())
        analyzed += report["analyzed_photos"]
        counts.update(report["rows_by_action"])
        errors.extend(
            {"report": batch["report"], **error} for error in report["indexing_errors"]
        )
        photo_ids = set()
        for row in report["rows"]:
            if row["photo_id"] not in photo_ids:
                photo_ids.add(row["photo_id"])
                cameras[str(row.get("camera_model") or "unknown")] += 1
                extraction[str(row.get("extraction") or "none")] += 1
            reasons[row["reason"]] += 1
            if row["action"] == "error":
                errors.append(
                    {
                        "report": batch["report"],
                        "photo_id": row["photo_id"],
                        "video_id": row.get("video_id"),
                        "reason": row["reason"],
                        "error": row.get("error"),
                    }
                )
        for proposal in report["proposals"]:
            video_id = proposal["video_id"]
            item = proposals.setdefault(
                video_id,
                {
                    "video_id": video_id,
                    "owner_id": proposal["owner_id"],
                    "actions": set(),
                    "photo_ids": set(),
                    "reports": [],
                    "requires_review": True,
                    "requires_metadata_review": False,
                },
            )
            item["actions"].add(proposal["action"])
            item["photo_ids"].update(proposal["evidence_photo_ids"])
            item["reports"].append(batch["report"])
            item["requires_metadata_review"] |= proposal["requires_metadata_review"]
    merged = []
    for item in proposals.values():
        actions = item.pop("actions")
        photos = sorted(item.pop("photo_ids"))
        item["evidence_photo_ids"] = photos
        item["action"] = next(iter(actions)) if len(actions) == 1 else "review_conflict"
        if item["action"] == "propose_link" and len(photos) != 1:
            item["action"] = "review_conflict"
        merged.append(item)
    return {
        "read_only": True,
        "completed_batches": len(completed),
        "analyzed_photos": analyzed,
        "rows_by_action": dict(counts),
        "proposals_by_action": dict(Counter(p["action"] for p in merged)),
        "photos_by_camera_model": dict(cameras),
        "photos_by_extraction": dict(extraction),
        "rows_by_reason": dict(reasons),
        "file_errors": errors,
        "proposals": merged,
    }


def batch_arguments(args, root, output, offset):
    values = [
        "--inventory",
        str(root / "inventory.jsonl"),
        "--output",
        str(output),
        "--offset",
        str(offset),
        "--limit",
        str(args.limit),
        "--timeout",
        str(args.timeout),
        "--max-clip-mib",
        str(args.max_clip_mib),
        "--temp-dir",
        str(args.temp_dir),
    ]
    for library_root in args.library_root + ([args.root] if args.root else []):
        values += ["--library-root", str(library_root)]
    for mapping in args.path_map:
        values += ["--path-map", mapping]
    if args.owner:
        values += ["--owner", args.owner]
    for name in ("match_content", "compare_streams"):
        if getattr(args, name):
            values.append("--" + name.replace("_", "-"))
    return values


def run(args, run_batch=audit_main):
    root = args.output.resolve()
    paths = LibraryPaths(
        args.library_root + ([args.root] if args.root else []), args.path_map
    )
    paths.check_output(root)
    config = {
        "roots": [
            str(path.resolve())
            for path in args.library_root + ([args.root] if args.root else [])
        ],
        "path_maps": args.path_map,
        "owner": args.owner,
        "match_content": args.match_content,
        "compare_streams": args.compare_streams,
        "limit": args.limit,
        "offset": args.offset,
        "timeout": args.timeout,
        "max_clip_mib": args.max_clip_mib,
        "tool_sha256": tool_digest(),
    }
    manifest_path = root / "progress.json"
    if args.resume:
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("config") != config:
            raise ValueError(
                "resume configuration or tool source differs from the original run"
            )
    else:
        if root.exists():
            raise ValueError("run directory exists; use --resume for a known run")
        if args.immich_db_container:
            assets = database_inventory(args.immich_db_container)
        elif args.inventory:
            with args.inventory.open() as source:
                assets = read_inventory(source)
        else:
            assets = filesystem_inventory(args.root)
        if args.owner and not any(asset.owner_id == args.owner for asset in assets):
            raise ValueError("owner is absent from inventory")
        eligible = len(candidates(assets, args.owner, args.match_content)[0])
        snapshot = serialize_inventory(assets)
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
        with (root / "inventory.jsonl").open("x") as destination:
            destination.write(snapshot)
        manifest = {
            "read_only": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": config,
            "inventory_sha256": hashlib.sha256(snapshot.encode()).hexdigest(),
            "inventory_assets": len(assets),
            "eligible_photos": eligible,
            "next_offset": args.offset if args.offset < eligible else None,
            "completed": [],
            "status": "running",
        }
        atomic_json(manifest_path, manifest)
    if (
        hashlib.sha256((root / "inventory.jsonl").read_bytes()).hexdigest()
        != manifest["inventory_sha256"]
    ):
        raise ValueError("frozen inventory changed; refusing to resume")
    with (root / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another process is already auditing this run") from error
        manifest = json.loads(manifest_path.read_text())
        processed = 0
        while manifest["next_offset"] is not None:
            offset = manifest["next_offset"]
            batch_root = root / f"batch-{offset:06d}"
            attempt = 0
            while (
                batch_root.exists()
                or (root / f"batch-{offset:06d}-attempt-{attempt:03d}.log").exists()
            ):
                attempt += 1
                batch_root = root / f"batch-{offset:06d}-attempt-{attempt:03d}"
            manifest["status"] = "running"
            atomic_json(manifest_path, manifest)
            with (root / f"batch-{offset:06d}-attempt-{attempt:03d}.log").open(
                "x"
            ) as log:
                with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    status = run_batch(batch_arguments(args, root, batch_root, offset))
            if status not in (0, 1):
                manifest.update(status="failed", failed_offset=offset)
                atomic_json(manifest_path, manifest)
                raise ValueError(f"batch {offset} failed; inspect its log, then resume")
            report_path = batch_root / "report.json"
            report = json.loads(report_path.read_text())
            if (
                not report.get("read_only")
                or report["inventory_sha256"] != manifest["inventory_sha256"]
                or report["offset"] != offset
                or report["analyzed_photos"] <= 0
                or report["next_offset"] is not None
                and report["next_offset"] <= offset
            ):
                raise ValueError("batch report identity or pagination is invalid")
            manifest["completed"].append(
                {"offset": offset, "report": str(report_path.relative_to(root))}
            )
            manifest["next_offset"] = report["next_offset"]
            manifest["status"] = (
                "complete" if report["next_offset"] is None else "running"
            )
            summary = aggregate(root, manifest["completed"])
            summary.update(
                eligible_photos=manifest["eligible_photos"],
                next_offset=manifest["next_offset"],
                status=manifest["status"],
                inventory_sha256=manifest["inventory_sha256"],
            )
            atomic_json(root / "summary.json", summary)
            atomic_json(manifest_path, manifest)
            with (root / "journal.jsonl").open("a") as journal:
                journal.write(
                    json.dumps(
                        {
                            "at": datetime.now(timezone.utc).isoformat(),
                            "offset": offset,
                            "status": summary["status"],
                            "analyzed_photos": summary["analyzed_photos"],
                            "proposals_by_action": summary["proposals_by_action"],
                            "errors": len(summary["file_errors"]),
                        }
                    )
                    + "\n"
                )
            print(
                json.dumps(
                    {
                        key: summary[key]
                        for key in (
                            "status",
                            "completed_batches",
                            "analyzed_photos",
                            "eligible_photos",
                            "next_offset",
                            "proposals_by_action",
                        )
                    }
                ),
                flush=True,
            )
            processed += 1
            if (
                args.max_batches
                and processed >= args.max_batches
                and manifest["next_offset"] is not None
            ):
                manifest["status"] = "stopped_at_batch_limit"
                summary["status"] = manifest["status"]
                atomic_json(root / "summary.json", summary)
                atomic_json(manifest_path, manifest)
                break
        if manifest["next_offset"] is None:
            manifest["status"] = "complete"
            atomic_json(manifest_path, manifest)
            if not (root / "summary.json").exists():
                summary = aggregate(root, manifest["completed"])
                summary.update(
                    eligible_photos=manifest["eligible_photos"],
                    next_offset=None,
                    status="complete",
                    inventory_sha256=manifest["inventory_sha256"],
                )
                atomic_json(root / "summary.json", summary)
        summary = json.loads((root / "summary.json").read_text())
        return 1 if summary["file_errors"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume the same frozen inventory and configuration",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        help="stop after this many batches in this invocation",
    )
    args = arguments(argv, parser=parser)
    if args.max_batches is not None and args.max_batches <= 0:
        parser.error("--max-batches must be positive")
    try:
        return run(args)
    except READ_ERRORS as error:
        print(f"Batch run failed: {error_text(error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
