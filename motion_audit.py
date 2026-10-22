#!/usr/bin/env python3
"""Produce a read-only motion-photo audit. There is deliberately no apply command."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from inventory import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    LibraryPaths,
    database_inventory,
    filesystem_inventory,
    read_inventory,
)

from media import MediaReader

VERSION = 1
READ_ERRORS = (OSError, ValueError, subprocess.SubprocessError)


def date(value):
    if not value:
        return None
    # ExifTool uses EXIF's colon-separated date, Immich uses ISO 8601.
    if len(value) >= 10 and value[4] == ":" and value[7] == ":":
        value = value[:10].replace(":", "-") + value[10:]
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def time_difference(left, right):
    if left is None or right is None or (left.tzinfo is None) != (right.tzinfo is None):
        return None
    return abs((left - right).total_seconds())


def error_text(error):
    # Subprocess exceptions include argv but never child stderr, which could
    # contain unrelated private metadata. Diagnostics identify only this asset.
    if isinstance(error, subprocess.TimeoutExpired):
        return "media command timed out"
    if isinstance(error, subprocess.CalledProcessError):
        return f"{Path(error.cmd[0]).name} exited with status {error.returncode}"
    return str(error)


def candidates(assets, owner, match_content):
    by_id = {asset.id: asset for asset in assets}
    references = defaultdict(set)
    names = defaultdict(set)
    identifiers = defaultdict(set)
    videos = []
    for asset in assets:
        if asset.linked_video_id:
            references[asset.linked_video_id].add(asset.id)
        if (
            asset.type == "VIDEO"
            and asset.path.suffix.casefold() in VIDEO_EXTENSIONS
            and asset.visibility != "locked"
            and (owner is None or asset.owner_id == owner)
        ):
            videos.append(asset)
            names[(asset.owner_id, asset.stem)].add(asset.id)
            if asset.cid:
                identifiers[(asset.owner_id, asset.cid)].add(asset.id)
    photos = []
    matches = {}
    for asset in assets:
        if (
            asset.type != "IMAGE"
            or asset.path.suffix.casefold() not in IMAGE_EXTENSIONS
            or asset.visibility in {"locked", "hidden"}
            or (owner is not None and asset.owner_id != owner)
        ):
            continue
        ids = names[(asset.owner_id, asset.stem)].copy()
        if asset.cid:
            ids.update(identifiers[(asset.owner_id, asset.cid)])
        if ids or match_content:
            photos.append(asset)
            matches[asset.id] = ids
    return (
        sorted(photos, key=lambda a: (a.owner_id, a.id)),
        matches,
        videos,
        by_id,
        references,
    )


def classify(
    photo,
    video,
    info,
    same_bytes,
    same_cid,
    references,
    retained_matches,
    same_streams=False,
):
    same_content = same_bytes or same_streams
    if video.id in references:
        return "keep", "video_is_a_live_photo_companion"
    if photo.linked_video_id:
        if same_content and retained_matches:
            return (
                "propose_trash",
                "embedded_clip_and_retained_companion_are_byte_identical"
                if same_bytes
                else "identical_media_packets_with_retained_companion_metadata_requires_review",
            )
        return "review", "existing_live_photo_link_requires_review"
    if photo.source == "filesystem":
        if same_content:
            return "review", "embedded_duplicate_confirmed_but_immich_links_are_unknown"
        return "review", "filesystem_candidates_need_immich_inventory"
    if same_content:
        return (
            "propose_link",
            "standalone_video_is_the_exact_embedded_clip"
            if same_bytes
            else "identical_media_packets_without_a_companion_metadata_requires_review",
        )
    if same_cid and info.embedded is None:
        return "propose_link", "matching_live_photo_content_identifier"
    if same_cid and info.embedded:
        return "review", "content_identifier_matches_but_embedded_video_bytes_differ"
    return "review", "name_or_date_only_is_not_proof"


def audit(assets, media, *, owner=None, offset=0, limit=200, match_content=False):
    photos, matches, videos, by_id, references = candidates(
        assets, owner, match_content
    )
    selected = photos[offset : offset + limit]
    global_link_candidates = defaultdict(set)
    for photo in photos:
        if not photo.linked_video_id:
            for video_id in matches[photo.id]:
                global_link_candidates[video_id].add(photo.id)
    sizes = defaultdict(list)
    indexing_errors = []
    if match_content:
        for video in videos:
            try:
                sizes[(video.owner_id, media.size(video))].append(video.id)
            except READ_ERRORS as error:
                indexing_errors.append(
                    {"video_id": video.id, "error": error_text(error)}
                )
    rows = []
    for position, photo in enumerate(selected, 1):
        print(
            f"Analyzing photo {position}/{len(selected)}", file=sys.stderr, flush=True
        )
        base = {
            "photo_id": photo.id,
            "owner_id": photo.owner_id,
            "photo_path": str(photo.path),
            "photo_name": photo.name,
            "linked_video_id": photo.linked_video_id,
        }
        try:
            info = media.photo(photo)
            retained_matches = False
            retained_error = None
            retained = by_id.get(photo.linked_video_id)
            if photo.linked_video_id and info.embedded:
                if (
                    retained is None
                    or retained.type != "VIDEO"
                    or retained.owner_id != photo.owner_id
                ):
                    retained_error = (
                        "linked companion is absent or belongs to a different owner"
                    )
                else:
                    try:
                        retained_matches = media.video(retained) == info.embedded
                        if not retained_matches:
                            retained_error = (
                                "linked companion differs from the embedded clip"
                            )
                    except READ_ERRORS as error:
                        retained_error = error_text(error)
            ids = matches[photo.id].copy()
            if match_content and info.embedded:
                ids.update(sizes[(photo.owner_id, info.embedded.size)])
            matched_rows = []
            for video_id in sorted(ids):
                video = by_id[video_id]
                row = {
                    **base,
                    "video_id": video.id,
                    "video_path": str(video.path),
                    "video_name": video.name,
                    "video_visibility": video.visibility,
                    "video_used_by": sorted(references.get(video.id, set())),
                    "embedded_sha256": info.embedded.sha256 if info.embedded else None,
                    "embedded_bytes": info.embedded.size if info.embedded else None,
                    "extraction": info.extraction,
                    "retained_companion_matches": retained_matches,
                    "retained_companion_error": retained_error,
                    "retained_companion_path": str(retained.path) if retained else None,
                }
                try:
                    fingerprint = media.video(video)
                    same_bytes = (
                        info.embedded is not None and fingerprint == info.embedded
                    )
                    if video_id not in matches[photo.id] and not same_bytes:
                        continue  # A same-sized file alone is not a candidate.
                    metadata, video_cid = media.identity(video)
                    photo_cid = photo.cid or info.cid
                    same_cid = bool(photo_cid and photo_cid == (video.cid or video_cid))
                    cid_conflict = bool(
                        (photo.cid and info.cid and photo.cid != info.cid)
                        or (video.cid and video_cid and video.cid != video_cid)
                    )
                    delta = time_difference(
                        date(photo.captured_at or info.captured_at),
                        date(video.captured_at or metadata.get("CreateDate")),
                    )
                    streams_match = False
                    if info.streams and not same_bytes:
                        stream_hash = media.streams(video)
                        streams_match = stream_hash == info.streams
                        row.update(
                            streams_identical=streams_match,
                            embedded_streams=info.streams,
                            video_streams=stream_hash,
                        )
                    action, reason = classify(
                        photo,
                        video,
                        info,
                        same_bytes,
                        same_cid,
                        references,
                        retained_matches,
                        same_streams=streams_match,
                    )
                    if cid_conflict and action == "propose_link":
                        action, reason = (
                            "review",
                            "database_and_file_content_identifiers_disagree",
                        )
                    row.update(
                        {
                            "action": action,
                            "reason": reason,
                            "byte_identical": same_bytes,
                            "content_identifier_matches": same_cid,
                            "content_identifier_conflict": cid_conflict,
                            "date_delta_seconds": delta,
                            "same_normalized_name": photo.stem == video.stem,
                            "video_sha256": fingerprint.sha256,
                            "video_bytes": fingerprint.size,
                            "photo_captured_at": photo.captured_at or info.captured_at,
                            "video_captured_at": video.captured_at
                            or metadata.get("CreateDate"),
                            "requires_metadata_review": streams_match
                            and not same_bytes,
                        }
                    )
                    # Distinct asset IDs can point to the same file, or to hardlinks.
                    # Removing one could remove bytes still required by the other.
                    if action == "propose_trash" and retained is not None:
                        if os.path.samefile(
                            media.paths.resolve(video.path),
                            media.paths.resolve(retained.path),
                        ):
                            row.update(
                                action="review",
                                reason="duplicate_and_companion_share_a_file",
                            )
                except READ_ERRORS as error:
                    row.update(
                        action="error",
                        reason="unable_to_validate_video",
                        error=error_text(error),
                    )
                matched_rows.append(row)
            if not matched_rows:
                rows.append(
                    {
                        **base,
                        "action": "no_candidate",
                        "reason": "no_matching_standalone_video",
                        "embedded_sha256": info.embedded.sha256
                        if info.embedded
                        else None,
                    }
                )
            else:
                rows.extend(matched_rows)
        except READ_ERRORS as error:
            rows.append(
                {
                    **base,
                    "action": "error",
                    "reason": "unable_to_validate_photo",
                    "error": error_text(error),
                }
            )

    # Never suggest assigning a video to multiple photos or choose arbitrarily
    # among multiple possible companions for a photo.
    links = [row for row in rows if row["action"] == "propose_link"]
    photos_per_video = Counter(row["video_id"] for row in links)
    videos_per_photo = Counter(row["photo_id"] for row in links)
    for row in links:
        if (
            photos_per_video[row["video_id"]] > 1
            or videos_per_photo[row["photo_id"]] > 1
            or len(global_link_candidates[row["video_id"]]) > 1
        ):
            row.update(action="review", reason="multiple_possible_live_photo_links")
    conflicting = {
        row["video_id"] for row in rows if row["action"] == "propose_link"
    } & {row["video_id"] for row in rows if row["action"] == "propose_trash"}
    for row in rows:
        if row.get("video_id") in conflicting and row["action"].startswith("propose_"):
            row.update(
                action="review", reason="conflicting_proposals_for_the_same_video"
            )
    # A single video can appear against several stills. Count each proposed action
    # once and keep every piece of supporting evidence in rows.
    proposals = {}
    for row in rows:
        if row["action"] in {"propose_link", "propose_trash"}:
            key = (row["action"], row["video_id"])
            proposal = proposals.setdefault(
                key,
                {
                    "action": row["action"],
                    "video_id": row["video_id"],
                    "owner_id": row["owner_id"],
                    "evidence_photo_ids": [],
                    "requires_review": True,
                    "requires_metadata_review": False,
                },
            )
            proposal["evidence_photo_ids"].append(row["photo_id"])
            proposal["requires_metadata_review"] |= row["requires_metadata_review"]
    return {
        "schema_version": VERSION,
        "read_only": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "inventory_assets": len(assets),
        "eligible_photos": len(photos),
        "analyzed_photos": len(selected),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + len(selected)
        if offset + len(selected) < len(photos)
        else None,
        "match_content": match_content,
        "rows_by_action": dict(Counter(row["action"] for row in rows)),
        "indexing_errors": indexing_errors,
        "proposals": list(proposals.values()),
        "rows": rows,
    }


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument(
        "--root", type=Path, help="scan media files under this filesystem directory"
    )
    sources.add_argument(
        "--inventory", type=Path, help="Immich JSONL exported using inventory.sql"
    )
    sources.add_argument(
        "--immich-db-container",
        help="read inventory using docker exec and a READ ONLY transaction",
    )
    parser.add_argument(
        "--library-root",
        type=Path,
        action="append",
        default=[],
        help="allowed directory for inventory paths; may be repeated",
    )
    parser.add_argument(
        "--path-map",
        action="append",
        default=[],
        metavar="CONTAINER=HOST",
        help="map a container directory to an allowed host directory; may be repeated",
    )
    parser.add_argument(
        "--owner", help="only analyze photos and candidates belonging to this owner ID"
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="skip this many eligible photos in the sorted inventory",
    )
    parser.add_argument(
        "--limit", type=int, default=200, help="maximum photos to analyze; default: 200"
    )
    parser.add_argument(
        "--match-content",
        action="store_true",
        help="also find exact embedded copies with different names; scans more images",
    )
    parser.add_argument(
        "--compare-streams",
        action="store_true",
        help="also propose based on identical media packets; marks metadata differences for review",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new report directory, outside the library",
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=Path(tempfile.gettempdir()),
        help="existing parent directory for temporary extracted clips",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="seconds per media subprocess; default: 60",
    )
    parser.add_argument(
        "--max-clip-mib",
        type=int,
        default=256,
        help="maximum clip size; default: 256 MiB",
    )
    args = parser.parse_args(argv)
    if (
        args.offset < 0
        or args.limit <= 0
        or args.timeout <= 0
        or args.max_clip_mib <= 0
    ):
        parser.error(
            "offset must be nonnegative; limit, timeout and max-clip-mib must be positive"
        )
    return args


def main(argv=None):
    args = arguments(argv)
    try:
        for command in ("exiftool", "ffprobe"):
            if not shutil.which(command):
                raise ValueError(f"required executable not found: {command}")
        roots = args.library_root + ([args.root] if args.root else [])
        paths = LibraryPaths(roots, args.path_map)
        paths.check_output(args.output)
        paths.check_output(args.temp_dir)
        if not args.temp_dir.is_dir():
            raise ValueError("--temp-dir must be an existing directory")
        if args.output.exists():
            raise ValueError("report directory already exists; choose a new --output")
        if args.immich_db_container:
            assets = database_inventory(args.immich_db_container)
        elif args.inventory:
            with args.inventory.open() as source:
                assets = read_inventory(source)
        else:
            assets = filesystem_inventory(args.root)
        if args.owner and not any(asset.owner_id == args.owner for asset in assets):
            raise ValueError("--owner is not present in this inventory")
        with tempfile.TemporaryDirectory(
            prefix="motion-audit-", dir=args.temp_dir
        ) as temporary:
            paths.check_output(Path(temporary))
            media = MediaReader(
                paths,
                temporary,
                args.timeout,
                args.max_clip_mib * 1024**2,
                compare_streams=args.compare_streams,
            )
            report = audit(
                assets,
                media,
                owner=args.owner,
                offset=args.offset,
                limit=args.limit,
                match_content=args.match_content,
            )
            media.verify_unchanged()
        report["compare_streams"] = args.compare_streams
        snapshot = "".join(
            json.dumps({**asdict(asset), "path": str(asset.path)}, sort_keys=True)
            + "\n"
            for asset in sorted(assets, key=lambda asset: asset.id)
        )
        report["inventory_sha256"] = hashlib.sha256(snapshot.encode()).hexdigest()
        # The only persistent writes are a newly created report directory.
        args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        if args.immich_db_container:
            with (args.output / "inventory.jsonl").open("x") as destination:
                destination.write(snapshot)
        with (args.output / "report.json").open("x") as destination:
            json.dump(report, destination, indent=2, ensure_ascii=False)
            destination.write("\n")
        with (args.output / "summary.txt").open("x") as destination:
            destination.write(f"Read-only audit, schema {VERSION}\n")
            destination.write(
                f"Photos analyzed: {report['analyzed_photos']} / {report['eligible_photos']}\n"
            )
            for action, count in sorted(report["rows_by_action"].items()):
                destination.write(f"{action}: {count} rows\n")
            destination.write(f"Unique proposals: {len(report['proposals'])}\n")
            destination.write(f"Indexing errors: {len(report['indexing_errors'])}\n")
            destination.write(f"Next offset: {report['next_offset']}\n")
            destination.write("No assets, links or source files were modified.\n")
        print((args.output / "summary.txt").read_text(), end="")
        return (
            1
            if report["indexing_errors"] or report["rows_by_action"].get("error")
            else 0
        )
    except READ_ERRORS as error:
        print(f"Audit failed: {error_text(error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
