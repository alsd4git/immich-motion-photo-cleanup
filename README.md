# Motion photo audit

Audit standalone MP4/MOV files and JPEG/HEIC/AVIF motion photos. The audit
commands are read-only. A separate, opt-in command can move proven redundant
videos to Immich's trash and restore them. It cannot permanently delete files,
empty the trash, link assets, rewrite metadata or refresh jobs. All database
access is read-only; changes use Immich's authenticated API.

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
Immich v3.2.2 and were checked against v3.3.5. A schema
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

Source files remain read-only. The audit runner never executes the proposals. Use a
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

## Move verified duplicates to Immich's trash

`trash.py` is separate from the read-only audit. It requires local read access
to the database and originals. Its API flow was checked against Immich v3.3.5.
Older or newer versions need schema and endpoint checks before use.

First create a fresh plan from a completed batch run:

```sh
python3 trash.py plan \
  --run /absolute/private/path/motion-audit-run \
  --immich-db-container immich_postgres \
  --path-map /data=/srv/immich \
  --preserve-sidecars \
  --output /absolute/private/path/trash-plan
```

The command writes `plan.json` in a new private directory and prints its SHA-256.
Review its `eligible`, `excluded` and `exclusion_reasons` entries. A plan is not
an execution instruction on its own.

Only a **full standalone-file containment proof** qualifies for execution.
Packet matches, filename matches, dates, content identifiers and link proposals
never qualify. Planning rehashes the whole standalone video and its exact range
in the current photo, including trailers. It also verifies the separate retained
companion inside that range. Execution repeats these checks for each batch.

Additional checks protect:

- Any registered companion, including references from trashed or locked photos.
- Missing, offline, locked, external-library or non-timeline candidate videos.
- Originals that share a path or inode with another inventoried original, or
  whose path another asset registers as a generated file.
- Videos with descriptions, ratings, locked metadata, edits, stacks,
  custom metadata or shared links.
- Favorites, albums, tags or memory memberships absent from the retaining photo. Associations already
  on that photo do not block trashing the extra video.

The tool excludes protected assets. It does not copy or merge their metadata.
Without `--preserve-sidecars`, it also excludes videos with sidecars. With that
option, it hashes the current video sidecars into the plan and checks whether
the photo has corresponding sidecars. `sidecar_comparisons` counts byte-identical
pairs, XML-equivalent pairs, differences and missing photo sidecars. XML comparison
normalizes formatting and attribute order; it is informational, not deletion proof.
The tool does not infer that a sidecar came from Google just from its filename.

Before every trash request, it copies all planned video sidecars byte for byte
into the private `sidecar-backups` directory beside the journal. It verifies
the copies and flushes them to disk before writing to Immich. Changed sidecars,
unsafe backup directories and corrupt existing backups stop or exclude the
operation. The plan maps each original sidecar path to its backup SHA-256 filename.
Keep these backups: a sidecar may contain information absent from the photo's
sidecar. They are not published or written back into the library.

Hashing proves byte preservation, not playback health. Avoid simultaneous library
changes, uploads, metadata jobs or administrative changes during execution.
Immich does not offer an atomic conditional trash operation; a small interval
exists between the final checks and the API write.

### API key setup

Sign in as the owner of the candidate videos. In Immich's account settings,
create an API key with these permissions:

| Permission | Purpose |
| --- | --- |
| `asset.read` | Check the current photo, standalone video and retained companion. |
| `asset.delete` | Move selected assets to trash and restore those same assets. |
| `user.read` | Verify that the key belongs to the plan's owner. |
| `userConfig.read` | Read the effective trash retention settings. |

The capitalization of `userConfig.read` matters. The key does not need admin,
upload, metadata-update or API-key-management permissions. Use a separate key
for each owner; an administrator's key is not a substitute for ownership.

Save the secret as the only text in a file owned by the user running the tool,
with permissions `600` or `400`, inside a private directory outside this
repository and the photo library. Do not put the secret in shell arguments,
reports, Git, screenshots or chat. The tool refuses symlinked key files and
files readable by other users. Revoke the key in Immich after the cleanup.

Use an HTTPS API URL ending in `/api`. For local use or an SSH tunnel,
`http://127.0.0.1:PORT/api` is also accepted. Other plain-HTTP hosts, URL
credentials, redirects and proxy environment variables are not used.

### Check and execute a plan

Without `--execute`, the following only checks authentication and retention and
prints the owner's candidate count and plan hash:

```sh
python3 trash.py trash \
  --plan /absolute/private/path/trash-plan/plan.json \
  --immich-db-container immich_postgres \
  --path-map /data=/srv/immich \
  --owner OWNER_UUID \
  --url https://photos.example.org/api \
  --api-key-file /absolute/private/path/immich-key \
  --journal /absolute/private/path/trash-plan/execution.jsonl
```

After review, repeat it with `--execute --plan-sha256 REVIEWED_SHA256`.
The default processes one batch of 25 videos. Inspect the timeline, trash and
journal before continuing. Repeat with the same journal to continue, or add
`--batch-size 100 --max-batches 0` to process all remaining candidates.
Batch sizes are limited to 1 through 100; `--max-batches 0` means all batches.

Every write uses `DELETE /assets` with **`force: false`**. There is no force-delete
option. The tool checks that trash is enabled with at least 7 days retention.
Restore remains available if the retention settings later become unsafe.
The tool does not change the server's settings. Immich will permanently delete items
after its configured retention period, and a user can manually empty the trash
earlier. Moving files to trash does not immediately reclaim their disk space.

The journal records a durable pending entry before each request, then confirms
the outcome against both the database and API. Resume skips confirmed actions.
It does not automatically retry write requests. After an interrupted request,
resume can confirm an already completed action; an uncertain outcome stops the
run for manual inspection. Do not edit the journal or run it concurrently.
Changes to the plan, API URL, owner or tool source invalidate journal reuse.

### Restore this tool's actions

Use the same arguments, plan hash and journal, changing `trash` to `restore`:

```sh
python3 trash.py restore \
  --plan /absolute/private/path/trash-plan/plan.json \
  --immich-db-container immich_postgres \
  --path-map /data=/srv/immich \
  --owner OWNER_UUID \
  --url https://photos.example.org/api \
  --api-key-file /absolute/private/path/immich-key \
  --journal /absolute/private/path/trash-plan/execution.jsonl \
  --execute --plan-sha256 REVIEWED_SHA256 --max-batches 0
```

Restore calls `POST /trash/restore/assets` only for IDs confirmed in this journal.
It never restores or empties the entire trash. Restore cannot recover files
already permanently deleted by Immich or another actor. Keep the plan and
journal privately for recovery, and retain sidecar backups for any metadata you
want to preserve after Immich's trash expires.

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
Trash tests also cover personal metadata, file aliases, API ownership checks,
retention guards, interrupted requests, scoped restore and credential handling.

## Related projects and upstream work

- [immich-go issue #1142](https://github.com/simulot/immich-go/issues/1142)
  tracks duplicate Google Takeout Motion Photo videos.
- [immich-go PR #1143](https://github.com/simulot/immich-go/pull/1143)
  proposes an import-time filename/size heuristic. This tool instead checks
  content in an already imported library. Its work does not replace import fixes.
- [immich-motion-deduplicator](https://github.com/itsWavs/immich-motion-deduplicator)
  addresses existing-library cleanup. Its filename-based candidate scan is not
  a full embedded-byte proof. Do not treat a shared stem as deletion evidence.
- [libmotionphoto](https://github.com/google/libmotionphoto) provides format
  parsing and validation. It is not an Immich cleanup tool.

This repository is an independent implementation, not an official Immich tool.
It does not publish reports, open upstream issues or send telemetry.

References:

- [Android Motion Photo format](https://developer.android.com/media/platform/motion-photo-format)
- [Immich v3.2.2 motion extraction and linking](https://github.com/immich-app/immich/blob/v3.2.2/server/src/services/metadata.service.ts)
- [Immich v3.3.5 trash semantics](https://github.com/immich-app/immich/blob/v3.3.5/server/src/services/asset.service.ts)
- [Immich v3.3.5 scoped restore](https://github.com/immich-app/immich/blob/v3.3.5/server/src/services/trash.service.ts)
- [ExifTool binary extraction](https://exiftool.org/exiftool_pod.html)
