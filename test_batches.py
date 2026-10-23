import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from inventory import Asset, serialize_inventory
from motion_audit import arguments
from run_batches import aggregate, run


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.inventory = self.root / "input.jsonl"
        assets = []
        for index in range(5):
            for type, extension in (("IMAGE", "jpg"), ("VIDEO", "mp4")):
                name = f"sample-{index}.{extension}"
                assets.append(
                    Asset(name, "test-owner", type, self.library / name, name)
                )
        self.inventory.write_text(serialize_inventory(assets))
        self.output = self.root / "reports"
        self.calls = []

    def args(self, resume=False):
        parser = argparse.ArgumentParser()
        parser.add_argument("--resume", action="store_true")
        parser.add_argument("--max-batches", type=int)
        values = [
            "--inventory",
            str(self.inventory),
            "--library-root",
            str(self.library),
            "--output",
            str(self.output),
            "--limit",
            "2",
            "--max-batches",
            "1",
        ]
        if resume:
            values.append("--resume")
        return arguments(values, parser)

    def fake_batch(self, values):
        args = arguments(values)
        self.calls.append(args.offset)
        args.output.mkdir()
        count = min(args.limit, 5 - args.offset)
        report = {
            "read_only": True,
            "inventory_sha256": hashlib.sha256(args.inventory.read_bytes()).hexdigest(),
            "offset": args.offset,
            "analyzed_photos": count,
            "next_offset": args.offset + count if args.offset + count < 5 else None,
            "rows_by_action": {"keep": count},
            "proposals": [],
            "indexing_errors": [],
            "rows": [
                {"photo_id": str(i), "action": "keep", "reason": "in_use"}
                for i in range(args.offset, args.offset + count)
            ],
        }
        (args.output / "report.json").write_text(json.dumps(report))
        return 0

    def test_resume_never_repeats_completed_batches(self):
        for resume in (False, True, True, True):
            self.assertEqual(run(self.args(resume=resume), self.fake_batch), 0)
        self.assertEqual(self.calls, [0, 2, 4])
        summary = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(summary["analyzed_photos"], 5)
        self.assertEqual(summary["status"], "complete")

    def test_resume_rejects_changed_inventory_and_configuration(self):
        run(self.args(), self.fake_batch)
        args = self.args(resume=True)
        args.compare_streams = True
        with self.assertRaisesRegex(ValueError, "configuration"):
            run(args, self.fake_batch)
        with (self.output / "inventory.jsonl").open("a") as snapshot:
            snapshot.write("\n")
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            run(self.args(resume=True), self.fake_batch)

    def test_failed_batch_can_retry_without_overwriting_log(self):
        with self.assertRaisesRegex(ValueError, "batch 0 failed"):
            run(self.args(), lambda values: 2)
        old_log = self.output / "batch-000000-attempt-000.log"
        old_contents = old_log.read_bytes()
        self.assertEqual(run(self.args(resume=True), self.fake_batch), 0)
        self.assertEqual(old_log.read_bytes(), old_contents)
        self.assertTrue((self.output / "batch-000000-attempt-001").is_dir())

    def test_cross_batch_link_conflict_is_never_an_actionable_proposal(self):
        self.output.mkdir()
        completed = []
        for index in range(2):
            name = f"batch-{index}.json"
            report = {
                "analyzed_photos": 1,
                "rows_by_action": {"propose_link": 1},
                "rows": [
                    {
                        "photo_id": str(index),
                        "action": "propose_link",
                        "reason": "matching",
                    }
                ],
                "indexing_errors": [],
                "proposals": [
                    {
                        "video_id": "same-video",
                        "owner_id": "owner",
                        "action": "propose_link",
                        "evidence_photo_ids": [str(index)],
                        "requires_metadata_review": False,
                    }
                ],
            }
            (self.output / name).write_text(json.dumps(report))
            completed.append({"report": name})
        result = aggregate(self.output, completed)
        self.assertEqual(result["proposals_by_action"], {"review_conflict": 1})

    def test_new_run_refuses_an_existing_directory(self):
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, "directory exists"):
            run(self.args(), self.fake_batch)

    def test_new_and_resumed_runs_reject_checkout_before_writing(self):
        self.output = Path(__file__).resolve().parent / "private-batch-regression"
        for resume in (False, True):
            with self.subTest(resume=resume), self.assertRaisesRegex(
                ValueError, "outside the Git checkout"
            ):
                run(self.args(resume=resume), self.fake_batch)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
