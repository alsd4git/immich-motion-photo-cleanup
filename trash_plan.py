"""Revalidate exact containment and Immich associations before proposing trash."""

import json
from collections import Counter, defaultdict
from pathlib import Path
from uuid import UUID

from inventory import Asset, database_query
from media import MediaReader
from sidecars import compare, manifests


def asset_id(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("expected a canonical asset UUID")
    return value


def current_state(container):
    result = {}
    for line in database_query(container, "trash_state.sql"):
        if not line.strip():
            continue
        row = json.loads(line)
        identifier = asset_id(row["id"])
        asset_id(row["owner_id"])
        if identifier in result:
            raise ValueError("duplicate asset in current state")
        result[identifier] = row
    return result


def evidence_from_run(root):
    root = root.resolve(strict=True)
    summary = json.loads((root / "summary.json").read_text())
    rows = defaultdict(list)
    reports = {}
    for proposal in summary["proposals"]:
        if proposal["action"] != "propose_trash":
            continue
        identifier = asset_id(proposal["video_id"])
        for relative in proposal["reports"]:
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError("report path escapes run directory")
            if path not in reports:
                reports[path] = json.loads(path.read_text())
            for row in reports[path]["rows"]:
                if (
                    row.get("video_id") == identifier
                    and row["action"] == "propose_trash"
                ):
                    if (
                        row.get("full_file_embedded") is True
                        and row.get("byte_preservation_verified") is True
                        and row.get("retained_companion_matches") is True
                        and row.get("requires_metadata_review") is False
                    ):
                        rows[identifier].append(row)
        # Missing or stream-only evidence never becomes executable.
        if identifier not in rows:
            rows[identifier] = []
    return dict(rows)


def to_asset(row):
    return Asset(
        row["id"],
        row["owner_id"],
        row["type"],
        Path(row["path"]),
        row["name"],
        row["linked_video_id"],
        visibility=row["visibility"],
    )


def active(row):
    return (
        row["deleted_at"] is None and row["status"] == "active" and not row["offline"]
    )


class Validator:
    def __init__(self, state, paths):
        self.state = state
        self.paths = paths
        self.references = {
            r["linked_video_id"] for r in state.values() if r["linked_video_id"]
        }
        self.path_ids = defaultdict(set)
        self.inode_ids = defaultdict(set)
        for row in state.values():
            self.path_ids[row["path"]].add(row["id"])
            try:
                # Index metadata only. Actual media/sidecar opens still resolve
                # every symlink and enforce the canonical library boundary.
                stat = paths.mapped(Path(row["path"])).stat()
                self.inode_ids[(stat.st_dev, stat.st_ino)].add(row["id"])
            except (OSError, ValueError):
                pass  # Missing/out-of-scope originals cannot alias a readable file.

    def verify(self, row, preserve_sidecars=False):
        video = self.state.get(asset_id(row["video_id"]))
        photo = self.state.get(asset_id(row["photo_id"]))
        retained = self.state.get(asset_id(row["linked_video_id"]))
        if not all((video, photo, retained)):
            raise ValueError("asset_missing")
        if len({video["id"], photo["id"], retained["id"]}) != 3:
            raise ValueError("asset_ids_not_distinct")
        if {r["owner_id"] for r in (video, photo, retained)} != {row["owner_id"]}:
            raise ValueError("owner_changed")
        if any(
            not active(r) or r["visibility"] == "locked"
            for r in (video, photo, retained)
        ):
            raise ValueError("inactive_offline_or_locked")
        if (
            video["type"] != "VIDEO"
            or photo["type"] != "IMAGE"
            or retained["type"] != "VIDEO"
        ):
            raise ValueError("asset_type_changed")
        if photo["linked_video_id"] != retained["id"] or video["id"] in self.references:
            raise ValueError("live_photo_reference_changed")
        for field, expected in (
            ("video_path", video),
            ("photo_path", photo),
            ("retained_companion_path", retained),
        ):
            if str(expected["path"]) != row[field]:
                raise ValueError("asset_path_changed")
        if (
            video["external"]
            or video["visibility"] != "timeline"
            or video["linked_video_id"]
        ):
            raise ValueError("not_an_independent_uploaded_timeline_video")
        for field in (
            "stack",
            "edited",
            "description",
            "rating",
            "locked_metadata",
            "shared",
            "custom_metadata",
            "file_reference",
        ):
            if video[field]:
                raise ValueError("protected_" + field)
        if video["favorite"] and not photo["favorite"]:
            raise ValueError("unique_favorite")
        for field in ("albums", "tags", "memories"):
            if not set(video.get(field, [])).issubset(photo.get(field, [])):
                raise ValueError("unique_" + field)
        if video["memory"] and not video.get("memories"):
            raise ValueError("protected_memory")
        if video["sidecar"] and not (preserve_sidecars or "sidecars" in row):
            raise ValueError("protected_sidecar")
        sidecars = manifests(video.get("sidecar_paths", []), self.paths)
        photo_sidecars = (
            manifests(photo.get("sidecar_paths", []), self.paths) if sidecars else []
        )
        if video["sidecar"] and not sidecars:
            raise ValueError("sidecar_paths_missing")
        if "sidecars" in row and row["sidecars"] != sidecars:
            raise ValueError("sidecar_changed_since_plan")
        if self.path_ids[video["path"]] != {video["id"]}:
            raise ValueError("shared_original_path")
        # A new reader for each proof also catches changes between batches.
        media = MediaReader(self.paths, None)
        va, pa, ra = map(to_asset, (video, photo, retained))
        resolved = [self.paths.resolve(a.path) for a in (va, pa, ra)]
        if len({(p.stat().st_dev, p.stat().st_ino) for p in resolved}) != 3:
            raise ValueError("source_files_alias")
        stat = resolved[0].stat()
        if self.inode_ids[(stat.st_dev, stat.st_ino)] != {video["id"]}:
            raise ValueError("shared_original_inode")
        fingerprint = media.fingerprint(va)
        if (
            fingerprint.sha256 != row["video_sha256"]
            or fingerprint.size != row["video_bytes"]
        ):
            raise ValueError("video_bytes_changed")
        offset = media.full_video_suffix(pa, va)
        if offset is None or offset != row["full_file_offset"]:
            raise ValueError("full_file_containment_changed")
        info = media.photo_from_contained_video(pa, [va], ra)
        if info is None:
            raise ValueError("retained_companion_not_contained")
        media.verify_unchanged()
        result = {
            key: row[key]
            for key in (
                "video_id",
                "photo_id",
                "linked_video_id",
                "owner_id",
                "video_path",
                "photo_path",
                "retained_companion_path",
                "video_sha256",
                "video_bytes",
                "full_file_offset",
            )
        }
        if preserve_sidecars or "sidecars" in row:
            result["sidecars"] = sidecars
            result["photo_sidecars"] = photo_sidecars
            result["sidecar_comparison"] = compare(sidecars, photo_sidecars)
        return result


def make_plan(evidence, state, paths, owner=None, preserve_sidecars=False):
    validator = Validator(state, paths)
    eligible, excluded = [], []
    for video_id, options in sorted(evidence.items()):
        reasons = []
        if owner and options and options[0]["owner_id"] != owner:
            continue
        for row in options:
            try:
                eligible.append(validator.verify(row, preserve_sidecars))
                break
            except (OSError, ValueError) as error:
                reasons.append(str(error))
        else:
            excluded.append(
                {
                    "video_id": video_id,
                    "reasons": sorted(set(reasons)) or ["no_full_file_proof"],
                }
            )
    return {
        "schema": 1,
        "read_only": True,
        "eligible": eligible,
        "excluded": excluded,
        "eligible_bytes": sum(r["video_bytes"] for r in eligible),
        "owners": dict(Counter(r["owner_id"] for r in eligible)),
        "exclusion_reasons": dict(
            Counter(reason for r in excluded for reason in r["reasons"])
        ),
        "sidecar_comparisons": dict(
            Counter(
                row["sidecar_comparison"]
                for row in eligible
                if "sidecar_comparison" in row
            )
        ),
    }
