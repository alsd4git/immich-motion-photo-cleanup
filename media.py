"""Read media metadata and hash embedded clips. Only temporary clips are written."""

import hashlib
import json
import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class MediaError(ValueError):
    pass


@dataclass(frozen=True)
class Fingerprint:
    sha256: str
    size: int


@dataclass(frozen=True)
class PhotoInfo:
    embedded: Fingerprint | None
    cid: str | None
    captured_at: str | None
    extraction: str | None = None
    streams: dict | None = None
    make: str | None = None
    model: str | None = None


class MediaReader:
    def __init__(
        self,
        paths,
        temporary_directory,
        timeout=60,
        max_clip_bytes=256 * 1024**2,
        compare_streams=False,
    ):
        self.paths = paths
        self.temporary_directory = temporary_directory
        self.timeout = timeout
        self.max_clip_bytes = max_clip_bytes
        self.compare_streams = compare_streams
        self.cache = {}
        self.snapshots = {}

    def _path(self, asset):
        path = self.paths.resolve(asset.path)
        stat = path.stat()
        signature = (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
        if path in self.snapshots and self.snapshots[path] != signature:
            raise MediaError("source file changed during the audit; run a fresh batch")
        self.snapshots[path] = signature
        return path

    def _metadata_many(self, paths):
        args = [
            "exiftool",
            "-json",
            "-n",
            "-struct",
            "-MotionPhoto",
            "-MicroVideo",
            "-MicroVideoOffset",
            "-ContainerDirectory",
            "-EmbeddedVideoType",
            "-EmbeddedVideoFile",
            "-MotionPhotoVideo",
            "-ContentIdentifier",
            "-MediaGroupUUID",
            "-DateTimeOriginal",
            "-CreateDate",
            "-Make",
            "-Model",
            *map(str, paths),
        ]
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=self.timeout, check=False
        )
        metadata = json.loads(result.stdout)
        if not isinstance(metadata, list) or any(
            not isinstance(item, dict) for item in metadata
        ):
            raise MediaError("ExifTool could not read the media metadata")
        return {item.get("SourceFile"): item for item in metadata}

    def _metadata(self, path):
        metadata = self._metadata_many([path]).get(str(path))
        if not metadata or metadata.get("Error"):
            raise MediaError("ExifTool could not read the media metadata")
        return metadata

    def prefetch(self, assets):
        """Amortize ExifTool startup across bounded groups of candidate files."""
        pending = {}
        for asset in assets:
            try:
                path = self._path(asset)
            except (OSError, ValueError):
                continue  # The normal per-file read records its own error later.
            if ("metadata", path) not in self.cache:
                pending[path] = asset
        paths = list(pending)
        for start in range(0, len(paths), 64):
            group = paths[start : start + 64]
            try:
                metadata = self._metadata_many(group)
            except (OSError, ValueError, subprocess.SubprocessError):
                continue  # Fall back to per-file reads when a group cannot be read.
            for path in group:
                item = metadata.get(str(path))
                if item and not item.get("Error"):
                    self.cache[("metadata", path)] = item
                self._path(pending[path])

    def _probe(self, path):
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=True,
        )
        data = json.loads(result.stdout)
        if not any(
            stream.get("codec_type") == "video" for stream in data.get("streams", [])
        ):
            raise MediaError("clip has no readable video stream")
        try:
            duration = float(data["format"]["duration"])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise MediaError("clip has no positive duration") from error

    @staticmethod
    def _hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return Fingerprint(digest.hexdigest(), path.stat().st_size)

    def video(self, asset):
        path = self._path(asset)
        key = ("video", path)
        if key not in self.cache:
            if path.stat().st_size > self.max_clip_bytes:
                raise MediaError("video exceeds --max-clip-mib; left for review")
            self._probe(path)
            self.cache[key] = self._hash(path)
            self._path(asset)
        return self.cache[key]

    def streams(self, asset):
        path = self._path(asset)
        key = ("streams", path)
        if key not in self.cache:
            self.video(asset)
            self.cache[key] = self._streams(path)
            self._path(asset)
        return self.cache[key]

    def full_video_suffix(self, photo, video):
        """Prove the whole standalone file, including trailers, is in the photo."""
        photo_path = self._path(photo)
        fingerprint = self.video(video)
        photo_size = photo_path.stat().st_size
        if fingerprint.size >= photo_size:
            return None
        offset = photo_size - fingerprint.size
        key = ("suffix", photo_path, fingerprint.size)
        if key not in self.cache:
            digest = hashlib.sha256()
            with photo_path.open("rb") as source:
                source.seek(offset)
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            self.cache[key] = digest.hexdigest()
            self._path(photo)
        return offset if self.cache[key] == fingerprint.sha256 else None

    def photo_from_contained_video(self, photo, videos, retained):
        """Use direct byte proofs when a registered companion is already present."""
        if (
            retained is None
            or retained.type != "VIDEO"
            or retained.owner_id != photo.owner_id
        ):
            return None
        fingerprint = self.video(retained)
        for video in videos:
            if video.id == retained.id:
                continue
            offset = self.full_video_suffix(photo, video)
            if offset is None:
                continue
            path = self._path(photo)
            digest = hashlib.sha256()
            with path.open("rb") as source:
                source.seek(offset)
                remaining = fingerprint.size
                while remaining:
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        raise MediaError(
                            "truncated source while verifying the retained companion"
                        )
                    remaining -= len(block)
                    digest.update(block)
            self._path(photo)
            if digest.hexdigest() == fingerprint.sha256:
                return PhotoInfo(
                    fingerprint,
                    photo.cid,
                    photo.captured_at,
                    "verified_containment",
                    make=photo.camera_make,
                    model=photo.camera_model,
                )
        return None

    def _streams(self, path):
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_packets",
                "-show_data_hash",
                "sha256",
                "-show_entries",
                "packet=stream_index,pts,dts,duration,pts_time,dts_time,duration_time,size,flags,data_hash,side_data_list:stream",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=True,
        )
        data = json.loads(result.stdout)
        canonical = []
        packets = data.get("packets", [])
        for stream in data.get("streams", []):
            # Text metadata can differ between Google Takeout's standalone MP4
            # and the embedded clip. Include decoding/presentation parameters,
            # every timed packet and every track, including audio and data.
            fields = (
                "codec_type",
                "codec_name",
                "codec_tag_string",
                "profile",
                "level",
                "width",
                "height",
                "coded_width",
                "coded_height",
                "pix_fmt",
                "sample_rate",
                "channels",
                "channel_layout",
                "time_base",
                "avg_frame_rate",
                "sample_aspect_ratio",
                "color_space",
                "color_transfer",
                "color_primaries",
                "color_range",
                "chroma_location",
                "field_order",
                "bits_per_raw_sample",
                "extradata_hash",
                "side_data_list",
                "disposition",
            )
            track = {field: stream[field] for field in fields if field in stream}
            track["rotate"] = stream.get("tags", {}).get("rotate")
            track["packets"] = [
                {key: value for key, value in packet.items() if key != "stream_index"}
                for packet in packets
                if packet.get("stream_index") == stream["index"]
            ]
            if any("data_hash" not in packet for packet in track["packets"]):
                raise MediaError("ffprobe did not hash every packet")
            canonical.append(track)
        if not canonical or not packets:
            raise MediaError("no packets available for stream comparison")
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        return {
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "tracks": len(canonical),
            "packets": len(packets),
        }

    def size(self, asset):
        return self._path(asset).stat().st_size

    def identity(self, asset):
        path = self._path(asset)
        key = ("metadata", path)
        if key not in self.cache:
            self.cache[key] = self._metadata(path)
            self._path(asset)
        metadata = self.cache[key]
        return metadata, metadata.get("ContentIdentifier") or metadata.get(
            "MediaGroupUUID"
        )

    @staticmethod
    def _range(metadata, size):
        directory = metadata.get("ContainerDirectory", [])
        if isinstance(directory, list) and metadata.get("MotionPhoto"):
            for index, entry in enumerate(directory):
                if not isinstance(entry, dict) or not isinstance(
                    entry.get("Item"), dict
                ):
                    raise MediaError("invalid container directory structure")
                item = entry.get("Item", {})
                if item.get("Semantic") == "MotionPhoto":
                    # The standard requires the video to be the final item. Older
                    # Samsung files can add padding; let ExifTool handle those.
                    if index != len(directory) - 1:
                        raise MediaError("MotionPhoto is not the final container item")
                    length = item.get("Length")
                    if type(length) is not int or length <= 0:
                        raise MediaError("invalid MotionPhoto item length")
                    padding = item.get("Padding", 0)
                    if type(padding) is not int or padding != 0:
                        raise MediaError("unsupported container video padding")
                    return size - length, length, "ContainerDirectory"
        if metadata.get("MicroVideo"):
            length = metadata.get("MicroVideoOffset")
            if type(length) is not int or length <= 0:
                raise MediaError("invalid MicroVideoOffset")
            return size - length, length, "MicroVideoOffset"
        return None

    def photo(self, asset):
        path = self._path(asset)
        key = ("photo", path)
        if key in self.cache:
            return self.cache[key]
        metadata, cid = self.identity(asset)
        captured = metadata.get("DateTimeOriginal") or metadata.get("CreateDate")
        binary_tag = None
        if metadata.get("MotionPhotoVideo"):
            binary_tag = "MotionPhotoVideo"
        elif metadata.get("EmbeddedVideoType") == "MotionPhoto_Data" and metadata.get(
            "EmbeddedVideoFile"
        ):
            binary_tag = "EmbeddedVideoFile"
        region = None if binary_tag else self._range(metadata, path.stat().st_size)
        if binary_tag is None and region is None:
            if metadata.get("MotionPhoto"):
                raise MediaError("MotionPhoto flag without an extractable clip")
            info = PhotoInfo(
                None,
                cid,
                captured,
                make=metadata.get("Make"),
                model=metadata.get("Model"),
            )
        else:
            with tempfile.NamedTemporaryFile(
                suffix=".mp4",
                dir=self.temporary_directory,
            ) as temporary:
                destination = Path(temporary.name)
                if binary_tag:
                    if path.stat().st_size > self.max_clip_bytes:
                        raise MediaError(
                            "source too large for bounded binary-tag extraction"
                        )
                    # Redirect the requested binary tag to a temporary file, never
                    # pass ExifTool any metadata assignment or overwrite option.
                    subprocess.run(
                        ["exiftool", "-b", f"-{binary_tag}", str(path)],
                        stdout=temporary,
                        stderr=subprocess.PIPE,
                        timeout=self.timeout,
                        check=True,
                    )
                    temporary.flush()
                    extraction = binary_tag
                    length = destination.stat().st_size
                else:
                    offset, length, extraction = region
                    if offset <= 0 or length > self.max_clip_bytes:
                        raise MediaError(
                            "embedded clip offset or size outside supported limits"
                        )
                    with path.open("rb") as source:
                        source.seek(offset)
                        remaining = length
                        while remaining:
                            block = source.read(min(remaining, 1024 * 1024))
                            if not block:
                                raise MediaError("truncated embedded clip")
                            temporary.write(block)
                            remaining -= len(block)
                    temporary.flush()
                if not 0 < length <= self.max_clip_bytes:
                    raise MediaError("embedded clip size outside supported limits")
                self._probe(destination)
                streams = self._streams(destination) if self.compare_streams else None
                info = PhotoInfo(
                    self._hash(destination),
                    cid,
                    captured,
                    extraction,
                    streams,
                    metadata.get("Make"),
                    metadata.get("Model"),
                )
        self._path(asset)
        self.cache[key] = info
        return info

    def verify_unchanged(self):
        for path, signature in self.snapshots.items():
            stat = path.stat()
            current = (
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
            if current != signature:
                raise MediaError(
                    "a source file changed during the audit; discard this report"
                )
