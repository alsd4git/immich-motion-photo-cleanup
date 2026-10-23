"""Synthetic containment, metadata protection and journal/API safety tests."""

import contextlib
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

from immich_api import ApiError, ImmichAPI, NoRedirect, read_key
from inventory import LibraryPaths, database_query
from media import MediaReader
from sidecars import compare, xml_fingerprint
from trash import (
    Journal,
    backup_sidecars,
    human_size,
    main,
    plan_summary,
    preflight_summary,
    process,
)
from trash_plan import (
    Validator,
    evidence_from_run,
    make_plan,
    original_inode_index,
    plan_clip_limit,
)

OWNER, PHOTO, VIDEO, RETAINED, OTHER = [str(UUID(int=i)) for i in range(1, 6)]


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.paths = LibraryPaths([self.library], [])
        self.state = {}
        for identifier, name, kind, content in (
            (PHOTO, "sample.jpg", "IMAGE", b"JPEG" + b"video" + b"trailer"),
            (VIDEO, "sample.mp4", "VIDEO", b"video" + b"trailer"),
            (RETAINED, "companion.mp4", "VIDEO", b"video"),
        ):
            path = self.library / name
            path.write_bytes(content)
            self.state[identifier] = {
                "id": identifier,
                "owner_id": OWNER,
                "name": name,
                "type": kind,
                "path": str(path),
                "status": "active",
                "deleted_at": None,
                "updated_at": "now",
                "linked_video_id": None,
                "visibility": "timeline",
                "albums": [],
                "tags": [],
                **dict.fromkeys(
                    (
                        "offline",
                        "external",
                        "favorite",
                        "stack",
                        "edited",
                        "description",
                        "rating",
                        "locked_metadata",
                        "shared",
                        "memory",
                        "custom_metadata",
                        "sidecar",
                        "file_reference",
                    ),
                    False,
                ),
            }
        self.state[PHOTO]["linked_video_id"] = RETAINED
        self.state[RETAINED]["visibility"] = "hidden"
        self.row = {
            "owner_id": OWNER,
            "photo_id": PHOTO,
            "video_id": VIDEO,
            "linked_video_id": RETAINED,
            "photo_path": self.state[PHOTO]["path"],
            "video_path": self.state[VIDEO]["path"],
            "retained_companion_path": self.state[RETAINED]["path"],
            "video_sha256": hashlib.sha256(b"videotrailer").hexdigest(),
            "video_bytes": 12,
            "full_file_offset": 4,
        }
        self.plan = {"schema": 2, "eligible": [self.row], "max_clip_mib": 256}
        self.api = Mock()
        self.api.preflight.return_value = {"enabled": True, "days": 30}
        self.api.get_asset.side_effect = self.api_asset
        self.api.trash.side_effect = lambda ids: self.change_status(ids, "trashed")
        self.api.restore.side_effect = lambda ids: self.change_status(ids, "active")
        self.journal = Journal(self.root / "journal.jsonl", {"schema": 1})
        self.addCleanup(self.journal.close)

    def api_asset(self, identifier):
        row = self.state[identifier]
        return {
            "id": identifier,
            "ownerId": row["owner_id"],
            "type": row["type"],
            "originalPath": row["path"],
            "isTrashed": row["status"] == "trashed",
            "isOffline": row["offline"],
            "visibility": row["visibility"],
            "livePhotoVideoId": row["linked_video_id"],
        }

    def change_status(self, ids, status):
        for identifier in ids:
            self.state[identifier]["status"] = status
            self.state[identifier]["deleted_at"] = (
                "today" if status == "trashed" else None
            )

    def execute(self, **kwargs):
        return process(
            self.plan,
            self.api,
            lambda ids: copy.deepcopy(self.state),
            self.paths,
            self.journal,
            OWNER,
            **kwargs,
        )

    def test_full_file_and_companion_are_reproved_without_media_tools(self):
        self.assertEqual(Validator(self.state, self.paths).verify(self.row), self.row)

    def test_reviewed_limit_reaches_both_execution_validation_passes(self):
        self.plan["max_clip_mib"] = 512
        with patch("trash_plan.MediaReader", wraps=MediaReader) as reader:
            self.assertEqual(self.execute(), {"trashed": 1})
        self.assertEqual(reader.call_count, 2)
        for invocation in reader.call_args_list:
            self.assertEqual(invocation.kwargs["max_clip_bytes"], 512 * 1024**2)

    def test_legacy_plan_retains_its_historical_limit(self):
        self.plan["schema"] = 1
        self.plan.pop("max_clip_mib")
        with patch("trash_plan.MediaReader", wraps=MediaReader) as reader:
            self.assertEqual(self.execute(), {"trashed": 1})
        for invocation in reader.call_args_list:
            self.assertEqual(invocation.kwargs["max_clip_bytes"], 256 * 1024**2)

    def test_configured_limit_is_enforced_on_synthetic_containment(self):
        payload = b"v" * (1024**2 + 1)
        Path(self.state[VIDEO]["path"]).write_bytes(payload)
        Path(self.state[PHOTO]["path"]).write_bytes(b"JPEG" + payload)
        Path(self.state[RETAINED]["path"]).write_bytes(payload)
        row = {
            **self.row,
            "video_bytes": len(payload),
            "video_sha256": hashlib.sha256(payload).hexdigest(),
        }
        with self.assertRaisesRegex(ValueError, "exceeds --max-clip-mib"):
            Validator(self.state, self.paths, max_clip_mib=1).verify(row)
        self.assertEqual(
            Validator(self.state, self.paths, max_clip_mib=2).verify(row), row
        )

    def test_alias_index_is_deferred_until_the_final_byte_proof(self):
        with patch(
            "trash_plan.original_inode_index", wraps=original_inode_index
        ) as index:
            validator = Validator(self.state, self.paths)
            validator.verify(self.row, verify_media=False)
            index.assert_not_called()
            validator.verify(self.row)
            index.assert_called_once()
            validator.verify(self.row)
            index.assert_called_once()

    def test_alias_added_during_api_preflight_blocks_the_final_write(self):
        def read(identifier):
            if OTHER not in self.state:
                alias = self.library / "new-alias.mp4"
                os.link(self.state[VIDEO]["path"], alias)
                self.state[OTHER] = {
                    "id": OTHER,
                    "path": str(alias),
                    "linked_video_id": None,
                }
            return self.api_asset(identifier)

        self.api.get_asset.side_effect = read
        with self.assertRaisesRegex(ValueError, "shared_original_inode"):
            self.execute()
        self.api.trash.assert_not_called()

    def test_each_personal_property_blocks_trash(self):
        for field in (
            "favorite",
            "stack",
            "edited",
            "description",
            "rating",
            "locked_metadata",
            "shared",
            "memory",
            "custom_metadata",
            "sidecar",
            "file_reference",
            "external",
            "offline",
        ):
            with self.subTest(field=field):
                state = copy.deepcopy(self.state)
                state[VIDEO][field] = True
                with self.assertRaises(ValueError):
                    Validator(state, self.paths).verify(self.row)

    def test_albums_tags_and_favorite_already_on_photo_are_preserved(self):
        for row in (self.state[PHOTO], self.state[VIDEO]):
            row.update(albums=["album"], tags=["tag"], favorite=True)
        Validator(self.state, self.paths).verify(self.row)
        self.state[VIDEO]["albums"].append("unique")
        with self.assertRaisesRegex(ValueError, "unique_albums"):
            Validator(self.state, self.paths).verify(self.row)

    def test_trashed_photo_references_also_protect_video(self):
        other = {
            **self.state[PHOTO],
            "id": OTHER,
            "linked_video_id": VIDEO,
            "status": "trashed",
        }
        self.state[OTHER] = other
        with self.assertRaisesRegex(ValueError, "reference_changed"):
            Validator(self.state, self.paths).verify(self.row)

    def test_minimal_global_reference_still_protects_a_companion(self):
        self.state[OTHER] = {
            "id": OTHER,
            "path": str(self.library / "unrelated.jpg"),
            "linked_video_id": VIDEO,
        }
        with self.assertRaisesRegex(ValueError, "reference_changed"):
            Validator(self.state, self.paths).verify(self.row)

    def test_changed_video_photo_and_companion_bytes_are_rejected(self):
        for identifier in (VIDEO, PHOTO, RETAINED):
            with self.subTest(identifier=identifier):
                path = Path(self.state[identifier]["path"])
                original = path.read_bytes()
                path.write_bytes(b"changed" + original)
                with self.assertRaises(ValueError):
                    Validator(self.state, self.paths).verify(self.row)
                path.write_bytes(original)

    def test_other_owner_and_changed_link_are_rejected(self):
        for field, value in (
            ("owner_id", OTHER),
            ("visibility", "locked"),
            ("status", "trashed"),
        ):
            with self.subTest(field=field):
                state = copy.deepcopy(self.state)
                state[PHOTO][field] = value
                with self.assertRaises(ValueError):
                    Validator(state, self.paths).verify(self.row)
        self.state[PHOTO]["linked_video_id"] = None
        with self.assertRaises(ValueError):
            Validator(self.state, self.paths).verify(self.row)

    def test_alias_of_an_unrelated_original_is_protected(self):
        alias = self.library / "alias.mp4"
        os.link(self.state[VIDEO]["path"], alias)
        self.state[OTHER] = {**self.state[VIDEO], "id": OTHER, "path": str(alias)}
        with self.assertRaisesRegex(ValueError, "shared_original_inode"):
            Validator(self.state, self.paths).verify(self.row)

    def test_symlink_alias_of_another_original_is_protected(self):
        alias = self.library / "alias.mp4"
        alias.symlink_to(self.state[VIDEO]["path"])
        self.state[OTHER] = {**self.state[VIDEO], "id": OTHER, "path": str(alias)}
        with self.assertRaisesRegex(ValueError, "shared_original_inode"):
            Validator(self.state, self.paths).verify(self.row)

    def test_directory_symlink_alias_is_protected(self):
        alias = self.library / "alias"
        alias.symlink_to(self.library, target_is_directory=True)
        self.state[OTHER] = {
            **self.state[VIDEO],
            "id": OTHER,
            "path": str(alias / "sample.mp4"),
        }
        with self.assertRaisesRegex(ValueError, "shared_original_inode"):
            Validator(self.state, self.paths).verify(self.row)

    def test_plan_excludes_stream_only_or_missing_proofs(self):
        result = make_plan({VIDEO: []}, self.state, self.paths)
        self.assertEqual(result["eligible"], [])
        self.assertEqual(result["excluded"][0]["reasons"], ["no_full_file_proof"])

    def test_only_current_owner_is_executed_and_restore_is_idempotent(self):
        self.plan["eligible"].append({**self.row, "owner_id": OTHER, "video_id": OTHER})
        self.assertEqual(self.execute(), {"trashed": 1})
        self.execute()
        self.api.trash.assert_called_once_with([VIDEO])
        self.assertEqual(self.execute(restore=True), {"restored": 1})
        self.execute(restore=True)
        self.api.restore.assert_called_once_with([VIDEO])
        self.assertEqual(Path(self.state[VIDEO]["path"]).read_bytes(), b"videotrailer")

    def test_changed_property_is_skipped_without_write(self):
        self.state[VIDEO]["description"] = True
        self.assertEqual(self.execute(), {"skipped": 1})
        self.api.trash.assert_not_called()

    def test_api_ownership_mismatch_blocks_entire_batch(self):
        self.api.get_asset.side_effect = lambda _: {"ownerId": OTHER}
        with self.assertRaises(ValueError):
            self.execute()
        self.api.trash.assert_not_called()

    def test_new_album_during_api_reads_blocks_write(self):
        def read(identifier):
            self.state[VIDEO]["albums"] = ["new-album"]
            return self.api_asset(identifier)

        self.api.get_asset.side_effect = read
        with self.assertRaisesRegex(ValueError, "changed during preflight"):
            self.execute()
        self.api.trash.assert_not_called()

    def test_bytes_changed_during_api_reads_block_final_write(self):
        def read(identifier):
            Path(self.state[VIDEO]["path"]).write_bytes(b"changed content")
            return self.api_asset(identifier)

        self.api.get_asset.side_effect = read
        with self.assertRaisesRegex(ValueError, "video_bytes_changed"):
            self.execute()
        self.api.trash.assert_not_called()

    def test_pending_is_durable_and_timeout_is_not_blindly_replayed(self):
        def timeout(ids):
            self.assertEqual(self.journal.statuses()[VIDEO], "trash_pending")
            self.change_status(ids, "trashed")
            raise ApiError("timeout")

        self.api.trash.side_effect = timeout
        with self.assertRaises(ApiError):
            self.execute()
        self.assertEqual(self.execute(), {"trashed": 1})
        self.api.trash.assert_called_once()

    def test_unresolved_pending_never_retries_a_write(self):
        self.journal.record("trash_pending", [VIDEO])
        with self.assertRaisesRegex(ValueError, "unresolved pending"):
            self.execute()
        self.api.trash.assert_not_called()

    def test_failed_postcheck_leaves_pending_for_recovery(self):
        self.api.trash.side_effect = None
        with self.assertRaisesRegex(ValueError, "outcome unconfirmed"):
            self.execute()
        self.assertEqual(self.journal.statuses(), {VIDEO: "trash_pending"})

    def test_restore_never_uses_global_trash(self):
        self.execute()
        self.api.reset_mock()
        self.execute(restore=True)
        self.api.trash.assert_not_called()
        self.api.restore.assert_called_once_with([VIDEO])

    def test_journal_refuses_changed_header_and_concurrent_execution(self):
        with self.assertRaises(OSError):
            Journal(self.root / "journal.jsonl", {"schema": 1})
        other = Journal(self.root / "other.jsonl", {"schema": 1})
        other.close()
        with self.assertRaises(ValueError):
            Journal(self.root / "other.jsonl", {"schema": 2})

    def test_evidence_cannot_escape_the_run_directory(self):
        run = self.root / "run"
        run.mkdir()
        (run / "summary.json").write_text(
            json.dumps(
                {
                    "proposals": [
                        {
                            "video_id": VIDEO,
                            "action": "propose_trash",
                            "reports": ["../journal.jsonl"],
                        }
                    ]
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "escapes"):
            evidence_from_run(run)

    def add_sidecar(self):
        path = self.library / "sample.mp4.xmp"
        path.write_bytes(b"synthetic sidecar metadata")
        self.state[VIDEO].update(sidecar=True, sidecar_paths=[str(path)])
        return path

    def test_sidecars_need_opt_in_and_are_hashed_in_plan(self):
        self.add_sidecar()
        with self.assertRaisesRegex(ValueError, "protected_sidecar"):
            Validator(self.state, self.paths).verify(self.row)
        row = Validator(self.state, self.paths).verify(self.row, preserve_sidecars=True)
        self.assertEqual(len(row["sidecars"]), 1)
        self.plan["eligible"] = [row]
        self.execute()
        backup = self.root / "sidecar-backups" / (row["sidecars"][0]["sha256"] + ".bin")
        self.assertEqual(backup.read_bytes(), b"synthetic sidecar metadata")

    def test_changed_sidecar_blocks_trash(self):
        path = self.add_sidecar()
        row = Validator(self.state, self.paths).verify(self.row, preserve_sidecars=True)
        path.write_bytes(b"new information")
        self.plan["eligible"] = [row]
        self.execute()
        self.api.trash.assert_not_called()

    def test_corrupt_backup_is_never_overwritten(self):
        self.add_sidecar()
        row = Validator(self.state, self.paths).verify(self.row, preserve_sidecars=True)
        directory = self.root / "sidecar-backups"
        backup_sidecars([row], self.paths, directory)
        target = directory / (row["sidecars"][0]["sha256"] + ".bin")
        target.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "verification failed"):
            backup_sidecars([row], self.paths, directory)
        self.assertEqual(target.read_bytes(), b"corrupt")

    def test_memory_membership_must_already_exist_on_photo(self):
        self.state[VIDEO].update(memory=True, memories=["memory"])
        with self.assertRaisesRegex(ValueError, "unique_memories"):
            Validator(self.state, self.paths).verify(self.row)
        self.state[PHOTO]["memories"] = ["memory"]
        Validator(self.state, self.paths).verify(self.row)


class ClipSizePropagationTests(unittest.TestCase):
    def test_plan_schemas_distinguish_legacy_and_explicit_limits(self):
        self.assertEqual(plan_clip_limit({"schema": 1}), 256)
        for schema in (1, 2):
            self.assertEqual(plan_clip_limit({"schema": schema, "max_clip_mib": 512}), 512)
        for plan in (
            {}, {"schema": True}, {"schema": 3}, {"schema": 2},
            {"schema": 1, "max_clip_mib": None},
            {"schema": 2, "max_clip_mib": "512"},
        ):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                plan_clip_limit(plan)

    def test_validator_stores_configured_limit_in_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = LibraryPaths([Path(directory)], [])
            validator = Validator({}, paths, max_clip_mib=512)
            self.assertEqual(validator.max_clip_bytes, 512 * 1024**2)

    def test_invalid_limits_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = LibraryPaths([Path(directory)], [])
            for value in (0, -1, None, True, "512"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Validator({}, paths, max_clip_mib=value)

class PlanCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.run = self.root / "run"
        self.run.mkdir()
        self.output = self.root / "plan"
        self.progress = {"status": "complete", "config": {"max_clip_mib": 512}}
        (self.run / "summary.json").write_text(json.dumps({"proposals": []}))

    def arguments(self):
        return [
            "plan", "--run", str(self.run), "--output", str(self.output),
            "--library-root", str(self.library), "--immich-db-container", "synthetic",
        ]

    def test_plan_cli_persists_the_completed_audit_limit(self):
        (self.run / "progress.json").write_text(json.dumps(self.progress))
        with patch("trash.current_state", return_value={}), contextlib.redirect_stdout(
            io.StringIO()
        ):
            result = main(self.arguments())
        self.assertEqual(result, 0)
        plan = json.loads((self.output / "plan.json").read_text())
        self.assertEqual(plan["schema"], 2)
        self.assertEqual(plan["max_clip_mib"], 512)

    def test_plan_cli_rejects_missing_malformed_or_incomplete_audit_limits(self):
        invalid = [
            {}, {"status": "running", "config": {"max_clip_mib": 512}},
            {"status": "complete"}, {"status": "complete", "config": None},
        ] + [
            {"status": "complete", "config": {"max_clip_mib": value}}
            for value in (None, True, 0, -1, "512")
        ]
        for progress in invalid:
            with self.subTest(progress=progress):
                (self.run / "progress.json").write_text(json.dumps(progress))
                with patch("trash.current_state") as state, contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    result = main(self.arguments())
                self.assertEqual(result, 2)
                state.assert_not_called()
                self.assertFalse(self.output.exists())


class SummaryTests(unittest.TestCase):
    def test_plan_summary(self):
        plan = {
            "eligible": [{"video_id": "a"}],
            "excluded": [{"video_id": "b"}, {"video_id": "c"}],
            "eligible_bytes": 2 * 1024**3,
            "exclusion_reasons": {"protected_album": 2, "shared": 1},
        }
        output = plan_summary(plan, "abc123")
        self.assertIn("2.00 GiB", output)
        self.assertIn("Excluded candidates: 2", output)
        self.assertIn("protected_album: 2", output)
        self.assertIn("Plan SHA256: abc123", output)
        self.assertIn("\n", output)

    def test_empty_plan_and_preflight(self):
        plan = {"eligible": [], "excluded": [], "eligible_bytes": 0, "exclusion_reasons": {}}
        self.assertIn("0.00 MiB", plan_summary(plan))
        self.assertIn("DRY RUN", preflight_summary({
            "owner_candidates": 0, "retention_days": 30, "plan_sha256": "abc",
        }))
        with self.assertRaises(ValueError):
            human_size(-1)


class SidecarTests(unittest.TestCase):
    def test_xml_formatting_normalizes_but_values_do_not(self):
        first = xml_fingerprint(b'<x b="2" a="1"><date>2020</date></x>')
        self.assertEqual(
            first, xml_fingerprint(b'<x a="1" b="2">\n <date>2020</date>\n</x>')
        )
        self.assertNotEqual(
            first, xml_fingerprint(b'<x a="1" b="2"><date>2021</date></x>')
        )

    def test_entities_and_non_xml_are_not_interpreted(self):
        self.assertIsNone(
            xml_fingerprint(b'<!DOCTYPE x [<!ENTITY name "value">]><x>&name;</x>')
        )
        self.assertIsNone(xml_fingerprint("<!DOCTYPE x><x/>".encode("utf-16")))
        self.assertIsNone(xml_fingerprint(b"not xml"))

    def test_sidecar_comparison_reports_missing_and_different_peers(self):
        video = [{"sha256": "a", "xml_sha256": "xml-a"}]
        self.assertEqual(compare(video, []), "photo_sidecar_missing")
        self.assertEqual(compare(video, video), "byte_identical")
        self.assertEqual(
            compare(video, [{"sha256": "b", "xml_sha256": "xml-a"}]), "xml_identical"
        )
        self.assertEqual(
            compare(video, [{"sha256": "b", "xml_sha256": "xml-b"}]),
            "different_preserved_in_backup",
        )


class ApiTests(unittest.TestCase):
    def test_database_selection_rejects_injection_before_starting_a_process(self):
        with patch("inventory.subprocess.run") as process:
            with self.assertRaises(ValueError):
                database_query(
                    "database", "trash_state.sql", ["'); DELETE FROM asset; --"]
                )
            process.assert_not_called()

    def test_database_selection_stays_on_stdin_not_process_arguments(self):
        with patch("inventory.subprocess.run", return_value=Mock(stdout="")) as process:
            database_query("database", "trash_state.sql", [VIDEO])
            self.assertNotIn(VIDEO, " ".join(process.call_args.args[0]))
            self.assertIn(
                "\\set selected_ids '" + VIDEO + "'", process.call_args.kwargs["input"]
            )

    def test_https_only_except_loopback_and_no_url_credentials(self):
        for url in (
            "http://server/api",
            "https://user:pass@server/api",
            "https://server/api?q=x",
            "https://server/not-api",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ImmichAPI(url, "secret")
        ImmichAPI("http://127.0.0.1:2283/api", "secret")

    def test_trash_always_passes_force_false_and_restore_is_scoped(self):
        api = ImmichAPI("https://example.test/api", "secret")
        api.request = Mock()
        api.trash([VIDEO])
        api.request.assert_called_once_with(
            "DELETE", "/assets", {"ids": [VIDEO], "force": False}
        )
        api.restore([VIDEO])
        api.request.assert_called_with(
            "POST", "/trash/restore/assets", {"ids": [VIDEO]}
        )

    def test_disabled_or_short_trash_retention_blocks_writes(self):
        api = ImmichAPI("https://example.test/api", "secret")
        for trash in (
            {"enabled": False, "days": 30},
            {"enabled": True, "days": 0},
            {"enabled": True, "days": 6},
        ):
            api.request = Mock(
                side_effect=[{"id": OWNER}, {"permissions": ["all"]}, {"trash": trash}]
            )
            with self.assertRaisesRegex(ApiError, "at least 7 days"):
                api.preflight(OWNER)

    def test_restore_remains_available_when_retention_is_disabled(self):
        api = ImmichAPI("https://example.test/api", "secret")
        api.request = Mock(
            side_effect=[
                {"id": OWNER},
                {"permissions": ["all"]},
                {"trash": {"enabled": False, "days": 0}},
            ]
        )
        self.assertEqual(api.preflight(OWNER, for_restore=True)["days"], 0)

    def test_wrong_owner_and_insufficient_permissions_are_rejected(self):
        api = ImmichAPI("https://example.test/api", "secret")
        api.request = Mock(return_value={"id": OTHER})
        with self.assertRaisesRegex(ApiError, "different owner"):
            api.preflight(OWNER)
        api.request = Mock(side_effect=[{"id": OWNER}, {"permissions": ["asset.read"]}])
        with self.assertRaisesRegex(ApiError, "requires"):
            api.preflight(OWNER)

    def test_credentials_are_not_forwarded_by_redirects_or_error_logs(self):
        self.assertIsNone(
            NoRedirect().redirect_request(
                None, None, 302, None, None, "https://evil.test"
            )
        )
        api = ImmichAPI("https://example.test/api", "secret")
        api.opener.open = Mock(
            side_effect=urllib.error.HTTPError(api.url, 302, "secret", {}, None)
        )
        with self.assertRaises(ApiError) as error:
            api.get_asset(VIDEO)
        self.assertNotIn("secret", str(error.exception))

    def test_key_file_rejects_broad_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key"
            path.write_text("synthetic-key")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_key(path)
            path.chmod(0o600)
            self.assertEqual(read_key(path), "synthetic-key")
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_key(link)

    def test_untrusted_asset_ids_cannot_change_request_path(self):
        api = ImmichAPI("https://example.test/api", "secret")
        with self.assertRaises(ValueError):
            api.get_asset("../trash/empty")

    def test_http_timeout_is_never_automatically_retried(self):
        api = ImmichAPI("https://example.test/api", "secret")
        with patch.object(api.opener, "open", side_effect=TimeoutError) as request:
            with self.assertRaises(ApiError):
                api.trash([VIDEO])
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
