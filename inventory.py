"""Read filesystem or Immich inventories without accessing the Immich write API."""

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".avif"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}


@dataclass(frozen=True)
class Asset:
    id: str
    owner_id: str
    type: str
    path: Path
    name: str
    linked_video_id: str | None = None
    captured_at: str | None = None
    cid: str | None = None
    visibility: str = "timeline"
    source: str = "immich"
    camera_make: str | None = None
    camera_model: str | None = None

    @property
    def stem(self):
        # Pixel exports may add .MP to the photo but not the standalone clip.
        return re.sub(r"\.mp$", "", Path(self.name).stem.casefold())


def read_inventory(lines):
    assets = []
    seen = set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("expected an object")
            for field in ("id", "owner_id", "type", "path", "name"):
                if not isinstance(record.get(field), str) or not record[field]:
                    raise ValueError(f"missing or invalid {field}")
            for field in (
                "linked_video_id",
                "captured_at",
                "cid",
                "visibility",
                "camera_make",
                "camera_model",
            ):
                if record.get(field) is not None and not isinstance(record[field], str):
                    raise ValueError(f"invalid {field}")
            if record["id"] in seen:
                raise ValueError("duplicate asset id")
            if record.get("source", "immich") not in {"immich", "filesystem"}:
                raise ValueError("invalid inventory source")
            if record["type"] not in {"IMAGE", "VIDEO", "AUDIO", "OTHER"}:
                raise ValueError("unrecognized asset type")
            path = Path(record["path"])
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("path must be absolute without parent traversal")
            if Path(record["name"]).name != record["name"]:
                raise ValueError("name must be a filename")
            assets.append(
                Asset(
                    record["id"],
                    record["owner_id"],
                    record["type"],
                    path,
                    record["name"],
                    record.get("linked_video_id"),
                    record.get("captured_at"),
                    record.get("cid") or None,
                    record.get("visibility") or "timeline",
                    record.get("source", "immich"),
                    record.get("camera_make"),
                    record.get("camera_model"),
                )
            )
            seen.add(record["id"])
        except (ValueError, TypeError) as error:
            raise ValueError(f"inventory line {number}: {error}") from error
    return assets


def serialize_inventory(assets):
    return "".join(
        json.dumps({**asdict(asset), "path": str(asset.path)}, sort_keys=True) + "\n"
        for asset in sorted(assets, key=lambda asset: asset.id)
    )


def asset_id(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("expected a canonical asset UUID")
    return value


def database_query(container, filename, selected_ids=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
        raise ValueError("invalid database container name")
    sql = Path(__file__).with_name(filename).read_text()
    if filename == "trash_state.sql":
        # Only validated UUIDs enter the psql variable. Keep large selections on
        # stdin, not argv, both for privacy and OS argument-length limits.
        selection = ",".join(sorted(asset_id(value) for value in (selected_ids or [])))
        sql = "\\set selected_ids '" + selection + "'\n" + sql
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "sh",
            "-c",
            'exec psql --no-psqlrc --username="${POSTGRES_USER:-postgres}" '
            '--dbname="${POSTGRES_DB:-immich}" --tuples-only --no-align --quiet '
            "--set=ON_ERROR_STOP=1",
        ],
        input=sql,
        text=True,
        capture_output=True,
        timeout=120,
        check=True,
    )
    return result.stdout.splitlines()


def database_inventory(container):
    return read_inventory(database_query(container, "inventory.sql"))


def filesystem_inventory(root):
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("library root must be a directory")
    assets = []

    def onerror(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            suffix = path.suffix.casefold()
            if suffix not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS or path.is_symlink():
                continue
            assets.append(
                Asset(
                    str(path.relative_to(root)),
                    "filesystem",
                    "IMAGE" if suffix in IMAGE_EXTENSIONS else "VIDEO",
                    path,
                    name,
                    source="filesystem",
                )
            )
    return assets


class LibraryPaths:
    def __init__(self, roots, mappings):
        self.roots = [Path(root).resolve(strict=True) for root in roots]
        self.lexical_roots = [Path(root).absolute() for root in roots]
        self.mappings = []
        for value in mappings:
            source, separator, target = value.partition("=")
            if (
                not separator
                or not Path(source).is_absolute()
                or ".." in Path(source).parts
            ):
                raise ValueError("path maps must be /container/path=/host/path")
            destination = Path(target).resolve(strict=True)
            if not destination.is_dir():
                raise ValueError("path map target must be a directory")
            self.mappings.append((Path(source), destination))
            self.roots.append(destination)
        self.mappings.sort(key=lambda pair: len(pair[0].parts), reverse=True)
        if not self.roots:
            raise ValueError(
                "supply --library-root or --path-map to constrain file reads"
            )
        if any(not root.is_dir() for root in self.roots):
            raise ValueError("library roots must be directories")

    def mapped(self, path):
        """Constrain a lexical path for metadata-only stat calls, without opening it."""
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("asset path must be absolute without parent traversal")
        for source, target in self.mappings:
            if path.is_relative_to(source):
                path = target / path.relative_to(source)
                break
        if not any(
            path.is_relative_to(root) for root in self.roots + self.lexical_roots
        ):
            raise ValueError("asset path escapes the allowed library roots")
        return path

    def resolve(self, path, directory=False):
        path = self.mapped(path)
        path = path.resolve(strict=True)
        if not any(path.is_relative_to(root) for root in self.roots):
            raise ValueError("asset path escapes the allowed library roots")
        if directory:
            if not path.is_dir():
                raise ValueError("asset parent must be a directory")
        elif not path.is_file():
            raise ValueError("asset path must be a regular file")
        return path

    def check_output(self, path):
        resolved = path.resolve()
        if any(resolved.is_relative_to(root) for root in self.roots):
            raise ValueError(
                "output and temporary directories must be outside the library"
            )
