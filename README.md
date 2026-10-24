# Immich Motion Photo Cleanup

**Inspect Google Takeout Motion Photo duplicates in Immich, verify actual media bytes, and optionally move proven redundant videos to Immich's reversible trash.**

Google Takeout and other exports may include both a JPEG/HEIC Motion Photo and a separate MP4/MOV. After importing, Immich can display a redundant standalone video next to the Motion Photo. **Matching filenames are not sufficient evidence for deletion.** This project audits existing imports against their embedded video and Immich's actual Live Photo relationships.

> [!IMPORTANT]
> **Audit is read-only by default.** No file deletion or database writes. Planning is separate from execution: only a full standalone-file byte-containment proof, verified again immediately before writing, can qualify a video for trash. Every write requires `--execute` and an explicitly reviewed plan SHA-256. Immich eventually empties trash according to its retention policy; maintain independent backups.

## Quick start: audit only

Requirements: Python 3.10+ on a POSIX system such as Linux or macOS, [ExifTool](https://exiftool.org/), `ffprobe` (FFmpeg). Run where you can read the Immich originals and have access to its PostgreSQL Docker container. The Python tool itself has **no third-party package dependencies**.

```sh
git clone https://github.com/alsd4git/motion-photo-audit.git
cd motion-photo-audit
python3 run_batches.py \
  --immich-db-container immich_postgres \
  --path-map /data=/srv/immich \
  --compare-streams \
  --limit 200 --max-batches 1 \
  --output /absolute/private/path/motion-audit-run
```

Adapt `immich_postgres` and `/data=/srv/immich` to **your actual container and host mount**. This first batch writes a private report directory **outside the Git repository and media library**, not changes to Immich. Filesystem-only scans are also available without database access; see the [operations guide](docs/operations.md).

Inspect `summary.json`, the batch `summary.txt`, and `report.json`. Typical outcomes:

| Outcome | Meaning |
| --- | --- |
| `keep` | Existing live-photo companion must remain |
| `propose_trash` | Potentially redundant standalone video; **not** automatically eligible for execution |
| `propose_link` | Candidate relationship; **never automatically linked** |
| `review` | Evidence is ambiguous or insufficient |
| `no_candidate` | No standalone candidate found |
| `error` | Asset could not be inspected |

To process further batches, repeat with `--resume` and the same arguments (omitting `--max-batches 1` when you want the full run). **A complete run, a fresh trash plan, ownership-scoped API credentials and explicit approval are required** before any trash operation.

## What it does (and does not do)

- Audits JPEG/HEIC/AVIF Motion Photos against standalone MP4/MOV files, using Immich's inventory and optional byte/packet comparisons.
- Protects companion relationships, asset owners, existing associations, sidecars and file aliases; ambiguous links stay under review.
- Supports resumable snapshots and reports; a separate opt-in workflow can move narrowly verified duplicates to **trash** and restore only this tool's confirmed actions.
- Does **not** permanently delete assets, empty trash, modify metadata, automatically link photos/videos, or repair an imported library. It does **not** guarantee that trashed assets remain recoverable after retention expires.
- Database access is read-only. Trash/restore use Immich's authenticated API.

See the [complete operations and safety reference](docs/operations.md) for setup, filesystem scans, matching rules, plan review, API-key security, execution, restoration, and known limitations.

## Similar projects

| Project | Main purpose | Difference |
| --- | --- | --- |
| [itsWavs/immich-motion-deduplicator](https://github.com/itsWavs/immich-motion-deduplicator) | Convenient installable scan/ID/delete CLI with Docker support for existing duplicates | Uses filename-oriented candidates and a simpler deletion workflow; this project requires evidence of the *entire* standalone video's byte preservation before executable trash proposals |
| [ChazyChaxxx/Immich-motion-deduplicator](https://github.com/ChazyChaxxx/Immich-motion-deduplicator) | Original three-step CSV scan, ID lookup and deletion | Useful straightforward workflow; this tool focuses on conservative media/relationship verification |
| [immich-go](https://github.com/simulot/immich-go) ([issue #1142](https://github.com/simulot/immich-go/issues/1142), [PR #1143](https://github.com/simulot/immich-go/pull/1143)) | Import and migration from Google Takeout, including motion-photo companion handling | Fixes at import time prevent duplicates; this tool audits **already imported** Immich libraries |
| [Google libmotionphoto](https://github.com/google/libmotionphoto) | Parse/validate Motion Photo formats | Library for media formats, not an Immich cleanup tool |

We can learn from the streamlined onboarding and packaging of other tools **without adopting filename-only deletion criteria**. This project is independent and **not officially affiliated with Immich**.

## Development

Use the locked development tools (synthetic-media tests additionally require `ffmpeg`):

```sh
uv sync --locked --dev
uv run --locked python -m unittest discover -s . -p 'test_*.py' -v
```

See [AGENTS.md](AGENTS.md) for contribution, integration and validation rules.
Direct `python3` execution remains supported without third-party Python packages.

MIT-licensed. Source code and test fixtures do not require access to your actual media library.
