"""Policy tests and an integration test using generated, non-private media."""

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from inventory import (
    Asset,
    LibraryPaths,
    check_private_output,
    filesystem_inventory,
    read_inventory,
)
from media import Fingerprint, MediaError, MediaReader, PhotoInfo
from motion_audit import audit, main

CLIP = Fingerprint("a" * 64, 100)
OTHER = Fingerprint("b" * 64, 100)


class FakeMedia:
    def __init__(self, photos=None, fingerprints=None, identities=None):
        self.photos = photos or {}
        self.fingerprints = fingerprints or {}
        self.identities = identities or {}

    def photo(self, asset):
        value = self.photos.get(asset.id, PhotoInfo(None, None, None))
        if isinstance(value, Exception):
            raise value
        return value

    def video(self, asset):
        value = self.fingerprints.get(asset.id, OTHER)
        if isinstance(value, Exception):
            raise value
        return value

    def identity(self, asset):
        return {}, self.identities.get(asset.id)

    def size(self, asset):
        return self.video(asset).size

    def full_video_suffix(self, photo, video):
        return None

    def prefetch(self, assets):
        pass

    def photo_from_contained_video(self, photo, videos, retained):
        return None


def photo(id="photo", **changes):
    return replace(
        Asset(id, "alice", "IMAGE", Path(f"/library/{id}.jpg"), "PXL_1.MP.jpg"),
        **changes,
    )


def video(id="video", **changes):
    return replace(
        Asset(id, "alice", "VIDEO", Path(f"/library/{id}.mp4"), "PXL_1.mp4"), **changes
    )


class PolicyTests(unittest.TestCase):
    def analyze(self, assets, media=None, **kwargs):
        return audit(assets, media or FakeMedia(), **kwargs)

    def test_name_only_is_never_a_link_or_trash_proposal(self):
        report = self.analyze([photo(), video()])
        self.assertEqual(report["rows"][0]["action"], "review")
        self.assertEqual(report["proposals"], [])

    def test_exact_embedded_clip_without_companion_proposes_link(self):
        media = FakeMedia({"photo": PhotoInfo(CLIP, None, None)}, {"video": CLIP})
        row = self.analyze([photo(), video()], media)["rows"][0]
        self.assertEqual(row["action"], "propose_link")
        self.assertTrue(row["byte_identical"])

    def test_content_identifier_pair_proposes_link(self):
        row = self.analyze([photo(cid="cid"), video(cid="cid")])["rows"][0]
        self.assertEqual(row["action"], "propose_link")

    def test_stale_database_content_identifier_requires_review(self):
        media = FakeMedia(
            {"photo": PhotoInfo(None, "actual-photo-cid", None)},
            identities={"video": "actual-video-cid"},
        )
        row = self.analyze([photo(cid="stale-cid"), video(cid="stale-cid")], media)[
            "rows"
        ][0]
        self.assertEqual(row["action"], "review")
        self.assertTrue(row["content_identifier_conflict"])

    def test_conflicting_embedded_bytes_block_cid_link(self):
        media = FakeMedia({"photo": PhotoInfo(CLIP, "cid", None)})
        row = self.analyze([photo(cid="cid"), video(cid="cid")], media)["rows"][0]
        self.assertEqual(row["action"], "review")

    def test_companion_in_use_by_any_photo_is_kept(self):
        assets = [
            photo(),
            video(),
            photo("locked", linked_video_id="video", visibility="locked"),
        ]
        media = FakeMedia({"photo": PhotoInfo(CLIP, None, None)}, {"video": CLIP})
        row = self.analyze(assets, media)["rows"][0]
        self.assertEqual(row["action"], "keep")
        self.assertEqual(row["video_used_by"], ["locked"])

    def test_missing_or_different_retained_companion_blocks_trash(self):
        for retained in ([], [video("retained", name="different.mp4")]):
            with self.subTest(retained=retained):
                media = FakeMedia(
                    {"photo": PhotoInfo(CLIP, None, None)}, {"video": CLIP}
                )
                rows = self.analyze(
                    [photo(linked_video_id="retained"), video(), *retained], media
                )["rows"]
                self.assertTrue(all(row["action"] != "propose_trash" for row in rows))

    def test_multiple_link_candidates_across_batches_are_ambiguous(self):
        media = FakeMedia({"a": PhotoInfo(CLIP, None, None)}, {"video": CLIP})
        row = self.analyze([photo("a"), photo("b"), video()], media, limit=1)["rows"][0]
        self.assertEqual(row["reason"], "multiple_possible_live_photo_links")

    def test_cross_owner_same_name_and_identifier_do_not_match(self):
        report = self.analyze([photo(cid="same"), video(owner_id="bob", cid="same")])
        self.assertEqual(report["analyzed_photos"], 0)

    def test_locked_photos_and_videos_are_not_analyzed(self):
        report = self.analyze([photo(visibility="locked"), video()])
        self.assertEqual(report["analyzed_photos"], 0)
        report = self.analyze([photo(), video(visibility="locked")])
        self.assertEqual(report["analyzed_photos"], 0)

    def test_byte_hash_difference_including_audio_prevents_trash(self):
        media = FakeMedia({"photo": PhotoInfo(CLIP, None, None)})
        row = self.analyze([photo(), video()], media)["rows"][0]
        self.assertFalse(row["byte_identical"])
        self.assertEqual(row["action"], "review")

    def test_content_search_finds_exact_copies_with_different_names(self):
        media = FakeMedia({"photo": PhotoInfo(CLIP, None, None)}, {"video": CLIP})
        report = self.analyze(
            [photo(), video(name="renamed.mp4")], media, match_content=True
        )
        self.assertEqual(report["rows"][0]["action"], "propose_link")
        self.assertFalse(report["rows"][0]["same_normalized_name"])

    def test_same_size_unrelated_content_is_not_a_candidate(self):
        media = FakeMedia({"photo": PhotoInfo(CLIP, None, None)})
        report = self.analyze(
            [photo(), video(name="renamed.mp4")], media, match_content=True
        )
        self.assertEqual(report["rows"][0]["action"], "no_candidate")

    def test_filesystem_only_reports_cannot_propose_mutations(self):
        media = FakeMedia({"photo": PhotoInfo(CLIP, None, None)}, {"video": CLIP})
        report = self.analyze(
            [photo(source="filesystem"), video(source="filesystem")], media
        )
        self.assertEqual(report["rows"][0]["action"], "review")

    def test_media_error_is_reported_and_next_photo_is_processed(self):
        media = FakeMedia({"a": MediaError("broken photo")})
        report = self.analyze([photo("a"), photo("b"), video()], media)
        self.assertEqual(report["rows_by_action"], {"error": 1, "review": 1})

    def test_pagination_is_deterministic(self):
        report = self.analyze(
            [photo("c"), video(), photo("b"), photo("a")], offset=1, limit=1
        )
        self.assertEqual(report["rows"][0]["photo_id"], "b")
        self.assertEqual(report["next_offset"], 2)


class InputTests(unittest.TestCase):
    def test_invalid_inventory_is_rejected(self):
        record = {
            "id": "id",
            "owner_id": "alice",
            "type": "IMAGE",
            "path": "/data/x.jpg",
            "name": "x.jpg",
        }
        for changes in (
            {"path": "/data/../outside.jpg"},
            {"name": "../x.jpg"},
            {"owner_id": None},
            {"cid": ["bad"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                read_inventory([json.dumps({**record, **changes})])
        with self.assertRaises(ValueError):
            read_inventory([json.dumps(record), json.dumps(record)])

    def test_path_map_rejects_symlink_escape_and_output_inside_library(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "library"
            root.mkdir()
            outside = Path(temporary) / "outside.jpg"
            outside.touch()
            (root / "escape.jpg").symlink_to(outside)
            paths = LibraryPaths([], [f"/data={root}"])
            with self.assertRaises(ValueError):
                paths.resolve(Path("/data/escape.jpg"))
            with self.assertRaises(ValueError):
                paths.check_output(root / "report")
            self.assertEqual(filesystem_inventory(root), [])

    def test_container_directory_validation(self):
        metadata = {
            "MotionPhoto": 1,
            "ContainerDirectory": [
                {"Item": {"Semantic": "Primary"}},
                {"Item": {"Semantic": "MotionPhoto", "Length": 50}},
            ],
        }
        self.assertEqual(
            MediaReader._range(metadata, 200), (150, 50, "ContainerDirectory")
        )
        metadata["ContainerDirectory"].append({"Item": {"Semantic": "Other"}})
        with self.assertRaises(MediaError):
            MediaReader._range(metadata, 200)


class PrivateOutputTests(unittest.TestCase):
    def test_repo_checkout_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the Git checkout"):
            check_private_output(Path(__file__).resolve().parent / "reports" / "user-data")

    def test_external_private_directory_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            check_private_output(Path(directory) / "audit-run")

    def test_direct_audit_rejects_checkout_before_reading_media(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(__file__).resolve().parent / "private-output-regression"
            with mock.patch(
                "motion_audit.shutil.which", return_value="synthetic-tool"
            ), mock.patch("motion_audit.audit") as analyze:
                result = main(
                    ["--root", directory, "--output", str(output)]
                )
            self.assertEqual(result, 2)
            analyze.assert_not_called()
            self.assertFalse(output.exists())

    def test_direct_audit_rejects_checkout_temp_directory_before_reading_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            output = root / "report"
            with mock.patch(
                "motion_audit.shutil.which", return_value="synthetic-tool"
            ), mock.patch("motion_audit.audit") as analyze:
                result = main([
                    "--root", str(library), "--output", str(output),
                    "--temp-dir", str(Path(__file__).resolve().parent),
                ])
            self.assertEqual(result, 2)
            analyze.assert_not_called()
            self.assertFalse(output.exists())


@unittest.skipUnless(
    all(shutil.which(command) for command in ("ffmpeg", "ffprobe", "exiftool")),
    "integration test requires ffmpeg, ffprobe and exiftool",
)
class RealMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        cls.root = cls.base / "library"
        cls.root.mkdir()
        cls.still = cls.root / "motion.jpg"
        cls.clip = cls.root / "motion.mp4"
        cls.retained = cls.root / "retained.mp4"
        for output, options in (
            (cls.still, ["-frames:v", "1"]),
            (cls.clip, ["-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p"]),
        ):
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=32x32:r=10",
                    *options,
                    str(output),
                ],
                check=True,
                capture_output=True,
            )
        cls.payload = cls.clip.read_bytes()
        subprocess.run(
            [
                "exiftool",
                "-overwrite_original",
                "-XMP-GCamera:MicroVideo=1",
                f"-XMP-GCamera:MicroVideoOffset={len(cls.payload)}",
                str(cls.still),
            ],
            check=True,
            capture_output=True,
        )
        with cls.still.open("ab") as destination:
            destination.write(cls.payload)
        shutil.copyfile(cls.clip, cls.retained)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def media(self):
        return MediaReader(LibraryPaths([self.root], []), self.base)

    def assets(self):
        return [
            photo(path=self.still, name=self.still.name, linked_video_id="retained"),
            video(path=self.clip, name=self.clip.name),
            video(
                "retained",
                path=self.retained,
                name="unrelated.mp4",
                visibility="hidden",
            ),
        ]

    def test_extraction_and_trash_proposal_preserve_all_sources(self):
        before = {path: path.read_bytes() for path in self.root.iterdir()}
        media = self.media()
        info = media.photo(self.assets()[0])
        self.assertEqual(info.embedded.sha256, hashlib.sha256(self.payload).hexdigest())
        row = audit(self.assets(), media)["rows"][0]
        self.assertEqual(row["action"], "propose_trash")
        self.assertTrue(row["retained_companion_matches"])
        media.verify_unchanged()
        self.assertEqual(
            before, {path: path.read_bytes() for path in self.root.iterdir()}
        )

    def test_samsung_style_trailer_is_preserved_in_the_photo(self):
        with tempfile.TemporaryDirectory(dir=self.root) as generated:
            root = Path(generated)
            trailer = b"synthetic metadata" + b"SEFH" + b"test directory" + b"SEFT"
            still = root / "motion.jpg"
            standalone = root / "motion.mp4"
            still.write_bytes(self.still.read_bytes() + trailer)
            standalone.write_bytes(self.payload + trailer)
            assets = self.assets()
            assets[0] = replace(assets[0], path=still)
            assets[1] = replace(assets[1], path=standalone)
            media = self.media()
            # Supply verified extraction metadata; the suffix check itself uses
            # the real files and includes the whole synthetic Samsung-style tail.
            media.cache[("photo", still.resolve())] = PhotoInfo(
                Fingerprint(
                    hashlib.sha256(self.payload).hexdigest(), len(self.payload)
                ),
                None,
                None,
            )
            row = audit(assets, media)["rows"][0]
            self.assertEqual(row["action"], "propose_trash")
            self.assertTrue(row["full_file_embedded"])
            self.assertFalse(row["requires_metadata_review"])
            standalone.write_bytes(self.payload + b"unique metadata")
            self.assertIsNone(self.media().full_video_suffix(assets[0], assets[1]))

    def test_shared_retained_file_is_not_a_trash_proposal(self):
        assets = self.assets()
        assets[2] = replace(assets[2], path=self.clip)
        row = audit(assets, self.media())["rows"][0]
        self.assertEqual(row["action"], "review")
        self.assertEqual(row["reason"], "duplicate_and_companion_share_a_file")

    def test_byte_proofs_avoid_reextracting_an_existing_companion(self):
        media = self.media()
        with (
            mock.patch.object(
                media,
                "_metadata_many",
                side_effect=AssertionError("unnecessary extraction"),
            ),
            mock.patch.object(
                media, "_probe", side_effect=AssertionError("unnecessary media probe")
            ),
        ):
            row = audit(self.assets(), media)["rows"][0]
        self.assertEqual(row["action"], "propose_trash")
        self.assertEqual(row["extraction"], "verified_containment")

        self.assertFalse(row["media_verified"])
        self.assertTrue(row["byte_preservation_verified"])

    def test_changed_source_invalidates_report(self):
        mutable = self.root / "mutable.mp4"
        shutil.copyfile(self.clip, mutable)
        media = self.media()
        media.video(video(path=mutable))
        mutable.touch()
        with self.assertRaises(MediaError):
            media.verify_unchanged()
        mutable.unlink()

    def test_remuxed_clip_proposal_requires_metadata_review(self):
        remuxed = self.root / "remuxed.mp4"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(self.clip),
                    "-c",
                    "copy",
                    "-metadata",
                    "comment=synthetic test metadata",
                    str(remuxed),
                ],
                check=True,
                capture_output=True,
            )
            assets = self.assets()
            assets[1] = replace(assets[1], path=remuxed)
            media = MediaReader(
                LibraryPaths([self.root], []), self.base, compare_streams=True
            )
            row = audit(assets, media)["rows"][0]
            self.assertFalse(row["byte_identical"])
            self.assertTrue(row["streams_identical"])
            self.assertEqual(row["action"], "propose_trash")
            self.assertTrue(row["requires_metadata_review"])
        finally:
            remuxed.unlink(missing_ok=True)

    def test_extra_audio_track_prevents_stream_match(self):
        with_audio = self.root / "with-audio.mp4"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(self.clip),
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=44100",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(with_audio),
                ],
                check=True,
                capture_output=True,
            )
            assets = self.assets()
            assets[1] = replace(assets[1], path=with_audio)
            media = MediaReader(
                LibraryPaths([self.root], []), self.base, compare_streams=True
            )
            row = audit(assets, media)["rows"][0]
            self.assertFalse(row["streams_identical"])
            self.assertEqual(row["action"], "review")
        finally:
            with_audio.unlink(missing_ok=True)

    def test_cli_writes_only_a_new_report_and_refuses_overwrite(self):
        output = self.base / "report"
        args = [
            "--root",
            str(self.root),
            "--output",
            str(output),
            "--temp-dir",
            str(self.base),
        ]
        self.assertEqual(main(args), 0)
        report = json.loads((output / "report.json").read_text())
        self.assertTrue(report["read_only"])
        self.assertFalse(report["proposals"])
        contents = (output / "report.json").read_bytes()
        self.assertEqual(main(args), 2)
        self.assertEqual((output / "report.json").read_bytes(), contents)


if __name__ == "__main__":
    unittest.main()
