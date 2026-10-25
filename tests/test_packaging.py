"""Build a clean source copy and exercise its wheel outside any checkout."""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from immich_motion_photo_cleanup import __version__


@unittest.skipUnless(shutil.which("uv"), "distribution test requires uv")
class DistributionTests(unittest.TestCase):
    def test_installed_wheel_resources_commands_and_private_output_guards(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(project / name, source / name)
            shutil.copytree(
                project / "src",
                source / "src",
                ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
            }

            def run(argv, expected=0):
                result = subprocess.run(
                    argv, cwd=root, env=environment, text=True, capture_output=True, timeout=180
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return result.stdout

            dist = root / "dist"
            run(["uv", "build", "--python", sys.executable, "--out-dir", str(dist), str(source)])
            (wheel,) = dist.glob("*.whl")
            (sdist,) = dist.glob("*.tar.gz")
            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
                for query in ("inventory.sql", "trash_state.sql"):
                    self.assertIn("immich_motion_photo_cleanup/" + query, names)
                self.assertFalse(any(".local-secrets" in name or "test_" in name for name in names))
            with tarfile.open(sdist) as archive:
                self.assertTrue(any(name.endswith("/inventory.sql") for name in archive.getnames()))
                self.assertFalse(any(".local-secrets" in name for name in archive.getnames()))
            self.assertEqual(
                run(
                    [
                        "uv",
                        "tool",
                        "run",
                        "--isolated",
                        "--python",
                        sys.executable,
                        "--from",
                        str(wheel),
                        "immich-motion-photo-cleanup",
                        "--version",
                    ]
                ).strip(),
                __version__,
            )
            venv = root / "venv"
            run(["uv", "venv", "--python", sys.executable, str(venv)])
            python = venv / "bin/python"
            command = str(venv / "bin/immich-motion-photo-cleanup")
            run(["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)])
            self.assertEqual(run([command, "--version"]).strip(), __version__)
            run([str(python), "-I", "-m", "immich_motion_photo_cleanup", "--help"])
            for operation in ("audit", "plan", "trash", "restore", "doctor"):
                self.assertIn("usage:", run([command, operation, "--help"]))
            run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "from immich_motion_photo_cleanup.resources import resource_bytes; "
                    "assert b'READ ONLY' in resource_bytes('inventory.sql'); "
                    "assert b'READ ONLY' in resource_bytes('trash_state.sql')",
                ]
            )
            library = root / "library"
            library.mkdir()
            output = root / "audit"
            run([command, "audit", "--root", str(library), "--output", str(output)])
            progress = json.loads((output / "progress.json").read_text())
            self.assertEqual(progress["status"], "complete")
            # Neither the installed location nor the current directory is this
            # source archive, so this catches regressions in checkout detection.
            for extra in (
                ["--output", str(source / "report")],
                ["--output", str(root / "rejected"), "--temp-dir", str(source)],
            ):
                run([command, "audit", "--root", str(library), *extra], expected=2)
            self.assertFalse((source / "report").exists())
            self.assertFalse((root / "rejected").exists())
