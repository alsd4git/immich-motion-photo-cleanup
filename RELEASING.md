# Release procedure

The current project is an installable Python package with a console entry point.
Git-tag installation through uv is the distribution path. Build and validate
wheel and sdist artifacts before publishing a tag, but do not imply that a PyPI
package exists. There is no automatic package-registry publishing workflow.
The historical v0.1.0 source-only release remains unchanged.

## Approval and validation

- Obtain explicit maintainer approval for the version, annotated tag and public
  GitHub Release. Approval to implement a feature or merge a PR is not release
  authorization.
- Use the canonical repository,
  [alsd4git/immich-motion-photo-cleanup](https://github.com/alsd4git/immich-motion-photo-cleanup).
  Confirm `main` is current and the worktree is clean.
- Run the [project checks](AGENTS.md#project-checks). Check the first automatic
  review and all three GitHub Actions jobs on the exact final `main` commit.
  Do not request duplicate reviews or bypass failed checks to publish.
- Complete a post-change read-only library audit and plan/API preflight, as
  described in [issue #21](https://github.com/alsd4git/immich-motion-photo-cleanup/issues/21)
  and the [operations guide](docs/operations.md). Start with a bounded batch,
  resume the frozen inventory to completion, and compare results with the
  previous baseline. A zero-eligible plan is valid for an already cleaned library.
- Keep reports and verification evidence private. Publish only tool versions,
  aggregate counts and pass/fail outcomes, never media, filenames, paths,
  account identifiers, credentials, media hashes or execution journals.
- A real trash/restore operation is not a release prerequisite and needs
  separate authorization. Use synthetic tests for positive write/restore paths.
- Record the actual Immich and media-tool versions tested. Existing schema and
  API checks do not prove compatibility with every past or future Immich version.
  If runtime code changes after the library check, repeat the affected validation.

## Prepare the release commit

On a dedicated branch based on current `main`:

1. Match `pyproject.toml`'s version to the intended tag. Keep dependencies locked.
2. Keep the existing Keep a Changelog 1.1.0 and Semantic Versioning 2.0.0
   introduction. Move the released entries into a dated version section using
   the actual release date. Leave `[Unreleased]` for subsequent changes.
3. Use the canonical repository in comparison and release links. For the first
   version, `[Unreleased]` compares `v0.1.0...HEAD`, and `[0.1.0]` links to the
   release tagged `v0.1.0`. Do not invent a preceding tag.
4. Write concise public release notes with the supported execution method,
   external tool requirements, validation results and safety limitations.
   Trash retention is finite and byte preservation does not prove playback health.
5. Review the source archive contents for private data. Do not attach private
   reports, credentials, media or recovery bundles as release assets.
   Verify that SQL resources ship in wheel and sdist, that installed commands
   run outside the checkout, and that the pinned uvx invocation works. Keep
   development dependencies out of runtime metadata. A tag is not an alias for
   the latest release, and an existing tag must never be moved for packaging.
6. Integrate the release preparation PR through the normal first-review and
   squash-merge workflow. Verify the exact final `main` commit and its CI before
   tagging. Do not tag the unmerged release branch.

## Publish after approval

For the next approved release, replace `VERIFIED_MAIN_COMMIT` with the full
commit verified above. The notes file must contain only reviewed public text.
Check that neither the tag nor release already exists before creating either.

```sh
git tag -a v0.2.0 VERIFIED_MAIN_COMMIT -m "Release v0.2.0"
git push origin refs/tags/v0.2.0
gh release create v0.2.0 \
  --repo alsd4git/immich-motion-photo-cleanup \
  --verify-tag --title "v0.2.0" \
  --notes-file /absolute/private/path/release-notes.md
```

GitHub provides the source archives for the tag. This procedure does not publish
packages or introduce an automated publishing workflow.

After publication, verify the tag is annotated and resolves to the intended
`main` commit. Check the GitHub Release, notes, source archive contents,
changelog links and canonical URLs. If publication is interrupted, inspect the
existing tag and release before retrying. Never force-move a published tag.
