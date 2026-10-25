import contextlib
import io
import json
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from immich_motion_photo_cleanup.cli import doctor, main
from immich_motion_photo_cleanup.inventory import check_private_output, database_query
from immich_motion_photo_cleanup.resources import RUNTIME_FILES, resource_bytes, runtime_digest
from immich_motion_photo_cleanup.run_batches import tool_digest
from immich_motion_photo_cleanup.trash import code_digest


class CLITests(unittest.TestCase):
    def test_audit_dispatch_preserves_argument_order(self):
        argv = ["--root", "/library", "--max-batches", "1", "--output", "/private/run"]
        with patch("immich_motion_photo_cleanup.run_batches.main", return_value=1) as run:
            self.assertEqual(main(["audit", *argv]), 1)
        run.assert_called_once_with(argv, prog="immich-motion-photo-cleanup audit")

    def test_trash_commands_require_their_original_validation(self):
        for operation in ("plan", "trash", "restore"):
            with (
                self.subTest(operation=operation),
                patch("immich_motion_photo_cleanup.trash.main", return_value=2) as run,
            ):
                self.assertEqual(main([operation, "--json"]), 2)
                run.assert_called_once_with(
                    [operation, "--json"], prog="immich-motion-photo-cleanup"
                )

    def test_doctor_reports_missing_tools_without_contacting_server(self):
        output = io.StringIO()
        with (
            patch("immich_motion_photo_cleanup.cli.shutil.which", return_value=None),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(doctor(["--json"]), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ready"])
        self.assertFalse(result["api_key_only_supported"])

    def test_doctor_docker_requirement_is_explicit(self):
        with (
            patch(
                "immich_motion_photo_cleanup.cli.shutil.which",
                side_effect=lambda tool: None if tool == "docker" else "/synthetic/tool",
            ),
            patch("immich_motion_photo_cleanup.cli.os.name", "posix"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(doctor([]), 0)
            self.assertEqual(doctor(["--database"]), 1)

    def test_installed_guard_rejects_other_checkouts_and_source_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / ".git").write_text("gitdir: /synthetic/worktree")
            archive = root / "archive"
            (archive / "src/immich_motion_photo_cleanup").mkdir(parents=True)
            (archive / "pyproject.toml").touch()
            legacy = root / "legacy-source"
            legacy.mkdir()
            for name in ("pyproject.toml", "motion_audit.py", "inventory.sql"):
                (legacy / name).touch()
            for candidate in (checkout, archive, legacy):
                with (
                    self.subTest(candidate=candidate),
                    self.assertRaisesRegex(ValueError, "outside the Git checkout"),
                ):
                    check_private_output(candidate / "private-reports")
                self.assertFalse((candidate / "private-reports").exists())

    def test_queries_and_runtime_resources_fail_closed(self):
        for name in ("../pyproject.toml", "missing.sql"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                resource_bytes(name)
            with self.subTest(query=name), self.assertRaises(ValueError):
                database_query("synthetic", name)

    def test_fingerprint_covers_every_runtime_module_and_query(self):
        resources = files("immich_motion_photo_cleanup")
        names = {item.name for item in resources.iterdir() if item.name.endswith((".py", ".sql"))}
        self.assertEqual(names, set(RUNTIME_FILES))
        digest = runtime_digest()
        self.assertEqual(len(digest), 64)
        self.assertEqual(code_digest(), digest)
        self.assertEqual(tool_digest(), digest)
        with patch("immich_motion_photo_cleanup.resources.resource_bytes", return_value=b"changed"):
            self.assertNotEqual(runtime_digest(), digest)
