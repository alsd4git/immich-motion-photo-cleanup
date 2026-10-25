# Contribution workflow

## Branches and pull requests

- `main` is the integration branch. Start from its latest remote state.
- Keep one work branch per issue or coherent task; do not bundle unrelated changes.
- Parallel implementation does not mean opening all PRs at once. Open PRs progressively, one at a time or in small independent groups, after rebasing onto current `main` and running the relevant checks. Keep work not yet ready for integration on its branch; do not spend repeated reviews on stale bases.
- When work branches depend on each other, document the stack and merge the base first. After a branch merges, rebase subsequent work on updated `main` and rerun relevant checks.
- Pushing and merging require maintainer authorization, which may cover an agreed workflow. Never directly push to `main` unless explicitly authorized; keep authorized direct pushes linear and fast-forward only.
- Rewriting a published work branch requires approval. Use `--force-with-lease`, never unrestricted force pushes. Never delete unmerged work without permission.
- PRs target `main`, link their issues, and explain why, what changed, what was tested and any remaining risk. Wait for the first automated review when configured and address its findings. Do not request another automated or manual review unless explicitly authorized.
- Squash-merge PRs. Verify integration before deleting merged branches. GitHub merge/protection settings are documented in [docs/repo-setup.md](docs/repo-setup.md).

## Implementation and validation

- Follow the existing Python standard-library implementation and avoid new runtime dependencies without clear justification.
- Preserve unrelated work. Never commit credentials, original photo libraries, inventories, hashes of private media, user asset identifiers, journals or unredacted reports.
- Run focused offline tests first, then the full test suite where appropriate. Do not claim validation without running the corresponding checks successfully.
- Use temporary directories, synthetic Motion Photo fixtures, mocked Immich APIs and mocked PostgreSQL responses. Do not require a real Immich installation for automated tests.
- Documentation-only changes normally require a diff and command/link check, not an entire media integration run.
- The project is POSIX-oriented (`fcntl` and Linux/Docker workflows); do not promise Windows portability without dedicated testing.

## Project checks

Python 3.10+ and ExifTool / FFmpeg tools (`exiftool`, `ffprobe` and `ffmpeg`) are required for the full synthetic-media integration tests:

```sh
uv sync --locked --dev
uv run --locked ruff check .
uv run --locked python -m compileall -q src tests
uv run --locked python -m unittest discover -s tests -p 'test_*.py' -v
```

The installable `src/immich_motion_photo_cleanup/` package has no third-party runtime dependencies. SQL resources must ship in both wheel and sdist. Test installed commands outside the checkout and preserve output guards for installed-package execution from a source checkout. Keep exact frozen pre-package revisions for old journals; never relax source-digest checks to resume them.

## Immich safety rules

- Audits and PostgreSQL queries remain read-only. Do not change or weaken evidence, companion-link, owner, retention, path/inode or metadata safety rules merely to increase the number of eligible duplicates.
- Only a full standalone-file containment proof can qualify for **executable** trashing. Filename, timestamps, content identifiers or media-packet equivalence alone are never permission to trash.
- Do not run `immich-motion-photo-cleanup trash --execute`, `immich-motion-photo-cleanup restore --execute`, database writes, permanent deletion, maintenance jobs or other operations against real user data for routine validation. Any real write requires explicit maintainer authorization for the exact target and action.
- Never add permanent deletion or empty-trash capabilities under the guise of testing or cleanup. Restore is intentionally scoped to this tool's recorded actions.
- Keep API keys out of argv, reports, logs and Git; use permission-restricted files. Do not weaken HTTPS/loopback restrictions.
- Keep sensitive output and temporary files outside the source checkout and actual media libraries, and do not publish real user file paths, photo contents or IDs as fixtures.
- Trash is not immediately equivalent to freed disk space; restore is only available while the server retains the asset. Maintain independent backups before performing real cleanup.
