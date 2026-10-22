# Motion photo audit

Read-only audit of standalone MP4/MOV files and JPEG/HEIC/AVIF motion photos.
It produces proposed actions and their evidence. It has no command to trash,
delete, link, rewrite metadata, refresh Immich jobs or change the database.

Requires Python 3.10+, ExifTool and ffprobe. Python uses only the standard library.
The integration tests also require ffmpeg. Run it on the machine that can read
the original files. The database mode additionally requires Docker access.

## Audit an Immich library

Run from this directory on the host. Use the server's actual volume mapping.
For example, if `/data` in the container maps to `/srv/immich` on the host:

```sh
uv run --no-project motion_audit.py \
  --immich-db-container immich_postgres \
  --path-map /data=/srv/immich \
  --compare-streams \
  --limit 200 \
  --output /tmp/motion-audit-batch-000
```

`python3 motion_audit.py` also works without uv. The script uses `docker exec`
to run the bundled `inventory.sql` inside a PostgreSQL `READ ONLY` transaction.
It reads the configured `POSTGRES_USER` and `POSTGRES_DB` inside the container.
It does not need an API key or print credentials. Database schema fields follow
Immich v3.2.2. A schema
change produces a query error rather than an incomplete inventory.

The first database run saves `inventory.jsonl` in the report directory. Reuse
that snapshot for subsequent batches, so uploads cannot shift the pagination:

```sh
uv run --no-project motion_audit.py \
  --inventory /tmp/motion-audit-batch-000/inventory.jsonl \
  --path-map /data=/srv/immich \
  --offset 200 --limit 200 \
  --output /tmp/motion-audit-batch-001
```

Use `--owner OWNER_UUID` to scope candidate matching and media reads to one
account. Existing companion references from the entire inventory remain
protected. Photos and videos in Immich's locked folder are excluded from
candidate analysis. Hidden videos are included because they can be companions.
Archived photos are included.

Use `--library-root /absolute/library` when inventory paths already match the
host. Repeat `--library-root` or `--path-map` for external libraries. Paths must
resolve inside an allowed root; symlinks escaping those roots are rejected.

## Audit a filesystem or Takeout directory

```sh
uv run --no-project motion_audit.py \
  --root /absolute/path/to/Takeout \
  --limit 200 \
  --output /tmp/takeout-motion-audit-000
```

The scan skips symlinks and sidecars. A filesystem-only audit can confirm that a
clip is embedded, but reports `review` because it cannot know which video Immich
needs for live playback. It never generates actionable trash/link proposals.

## Run all batches and resume

`run_batches.py` freezes the inventory once and processes every eligible photo
in batches of `--limit`, 200 by default. Run it on the library host:

```sh
python3 run_batches.py \
  --immich-db-container immich_postgres \
  --path-map /data=/srv/immich \
  --compare-streams --limit 200 \
  --output /absolute/private/path/motion-audit-run
```

Repeat the same command with `--resume` after an interruption. It verifies the
inventory hash, tool source and matching configuration before continuing.
`--max-batches 1` is useful for a first batch; it can be omitted when resuming.
A process lock prevents two invocations from auditing the same run concurrently.

The run directory contains `progress.json`, an append-only `journal.jsonl`,
per-batch reports and logs, and a cumulative `summary.json`. The summary merges
proposals by video ID and demotes conflicting links across batches to
`review_conflict`. It records camera-model coverage and file errors. Metadata
reads are grouped to avoid starting ExifTool for every file separately.

Source files remain read-only. The runner never executes the proposals. Use a
private run directory outside both the library and the Git checkout; filenames,
asset IDs and original paths are user data and must not be committed.

## Matching and evidence

Candidate matching uses the same owner and either the filename stem or the
stored Live Photo content identifier. Matching ignores case and an optional
trailing `.MP` in the stem. It does not discard numbered suffixes, dates or edit
markers, which could identify different originals.

With `--match-content`, the script also examines photos without name matches and
looks for byte-identical embedded clips among videos with the same owner and
size. This can find renamed files but reads more media. Without it, the report
covers name/identifier candidates only, not the entire library's embedded clips.
Pagination counts eligible photos, not photo/video pairs.

ExifTool reads the motion-photo metadata. The script extracts Google container
and legacy MicroVideo ranges into temporary files. For Samsung embedded JPEG
or HEIC video tags it asks ExifTool to extract the binary data. ffprobe checks
that each compared clip has a video stream and a positive duration. SHA-256
compares the complete clip, including audio and container metadata. Similar
frames or matching timestamps alone never justify a trash proposal.

It also checks whether the entire standalone file is an exact suffix of the
original photo. This includes Samsung SEF trailers and other appended data.
When that comparison matches, `full_file_embedded` records the proof and
`requires_metadata_review` is false because all standalone bytes are preserved
in the original photo. The existing Immich companion is still protected.

For an already linked photo, direct byte comparisons can prove both the full
standalone suffix and the retained companion inside it. That path records
`verified_containment` and avoids re-extracting the clip. Candidates that are
already registered companions and have no unused alternatives can be kept from
their database relationships alone. Their rows state `media_verified: false`;
this is duplicate analysis, not a file-integrity check of every kept asset.

Direct containment proofs also avoid media-probe subprocesses. These rows set
`byte_preservation_verified: true` and `media_verified: false`: the complete
standalone file and the retained companion were compared against bytes in the
original photo, without checking their playback health. Media probing and
packet comparisons remain available for cases without that stronger byte proof.

With `--compare-streams`, it also hashes all encoded packets and compares their
timestamps and stream decoding/presentation parameters, including audio, other
tracks and rotation. Matching packets can support a trash or link proposal, with
`requires_metadata_review: true` when containers or text metadata differ. Review
those differences before acting; container and text metadata may be worth preserving.

The script does not compare decoded or perceptual video fingerprints. Re-encoded
or trimmed copies stay in `review`. Unsupported or malformed extraction metadata
produces an error.
Large clips exceed the configurable `--max-clip-mib`, 256 MiB by default.

| Action | Meaning |
| --- | --- |
| `keep` | The video is already referenced by an active photo anywhere in the inventory. |
| `propose_trash` | The standalone file has identical bytes or media packets to the embedded clip, and a different retained Immich companion matches the embedded clip byte for byte. Neither file aliases the other. |
| `propose_link` | An unlinked photo and unused video have identical embedded bytes/media packets or matching Live Photo content identifiers. Competing candidate links require review. |
| `review` | Names/dates only, unknown Immich relationships, conflicting evidence, ambiguous links or an existing companion that cannot be verified. |
| `no_candidate` | No standalone copy was found under the selected matching rules. |
| `error` | A file could not be read or validated. Other photos can still be analyzed. |

A JPEG containing a clip does not by itself make Immich's video asset disposable.
Immich extracts the video and stores a `livePhotoVideoId` relationship for
playback. The audit protects that companion. If a photo lacks the relationship
but its existing standalone video matches, it proposes linking that resource.
It does not suggest rerunning metadata extraction, which can itself replace an
old companion in Immich.

## Reports and repeatability

Each run creates a new private directory containing:

- `report.json`: pair evidence, asset IDs, paths, hashes, reasons and deduplicated
  proposed actions. Includes the inventory SHA-256 and the next batch offset.
- `summary.txt`: counts, errors and pagination.
- `inventory.jsonl`: database snapshot, only when reading the database directly.

Reports contain private filenames and paths; keep them out of Git and shared
directories. Existing output directories are refused. Original media is opened
for reading only. Temporary extracted clips are removed after analysis. Source
file changes during the run invalidate the report before it is written.

Each proposal needs review against the current Immich state before anyone acts
on it. A report is a snapshot, not permission to execute a batch later. Link
ambiguities are checked across this batch and the whole inventory's name/CID
candidates. Content-only matches discovered in other batches may add further
ambiguities, so combine reports before acting on content-only link proposals.
Albums, favorites and descriptions on an extra video may also need preserving;
this audit does not transfer those properties.

Exit status is `0` for a completed audit, `1` when a report includes file errors,
and `2` for invalid configuration or a failed run. `review` is a normal outcome.
The default timeout is 60 seconds per subprocess and can be changed with
`--timeout`. No worker parallelism is used, to limit load on the photo server.

Examples to inspect the JSON report:

```sh
jq '.proposals' /tmp/motion-audit-batch-000/report.json
jq '.rows[] | select(.action == "review")' /tmp/motion-audit-batch-000/report.json
```

## Validation

```sh
uv run --no-project --python 3.11 -m unittest discover -s . -p 'test_*.py' -v
```

Tests cover owner isolation, protected companions, ambiguous links across
batches, filename-only false positives, path traversal, source changes and
generated motion JPEGs with real video payloads. They verify that source bytes
remain unchanged and report directories cannot be overwritten.

References:

- [Android Motion Photo format](https://developer.android.com/media/platform/motion-photo-format)
- [Immich v3.2.2 motion extraction and linking](https://github.com/immich-app/immich/blob/v3.2.2/server/src/services/metadata.service.ts)
- [ExifTool binary extraction](https://exiftool.org/exiftool_pod.html)
