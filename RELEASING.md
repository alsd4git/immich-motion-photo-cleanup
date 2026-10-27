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
3. Use the canonical repository in comparison and release links. Point
   `[Unreleased]` at `vMAJOR.MINOR.PATCH...HEAD` for the version being released,
   and link that version to the comparison from its preceding release tag.
   Preserve historical links, including the first `v0.1.0` release link, which
   has no preceding tag. Update stable installation examples to the new tag.
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

Set these environment variables for each approved release:

- `RELEASE_VERSION`: the approved SemVer tag in `vMAJOR.MINOR.PATCH` form.
- `RELEASE_COMMIT`: the full SHA of the final merged `main` commit whose CI
  and read-only library validation passed.
- `RELEASE_NOTES`: an absolute path outside the checkout to the reviewed public
  release notes.

Do not reuse a version that already exists. Fetch the current remote refs and
check both local and remote tags and GitHub Releases before publication. Treat
authentication or network errors as failures to verify absence. Confirm the
remote is the canonical repository, `HEAD` and `origin/main` equal the reviewed
commit, and the worktree is clean. The version in `pyproject.toml`, changelog
and installation examples must match `RELEASE_VERSION`.

The following block requires all three values instead of supplying a release
number that could accidentally be reused. Run it only after those checks and
explicit release approval. The subshell stops on failure without changing the
calling shell's options.

```sh
(
  set -eu
  : "${RELEASE_VERSION:?Set the approved vMAJOR.MINOR.PATCH tag}"
  : "${RELEASE_COMMIT:?Set the full verified main commit SHA}"
  : "${RELEASE_NOTES:?Set the absolute path to reviewed public release notes}"
  test -f "$RELEASE_NOTES"

  git tag -a "$RELEASE_VERSION" "$RELEASE_COMMIT" -m "Release $RELEASE_VERSION"
  git push origin "refs/tags/$RELEASE_VERSION"
  gh release create "$RELEASE_VERSION" \
    --repo alsd4git/immich-motion-photo-cleanup \
    --verify-tag --target "$RELEASE_COMMIT" --title "$RELEASE_VERSION" \
    --notes-file "$RELEASE_NOTES"
)
```

GitHub provides the source archives for the tag. This procedure does not publish
packages or introduce an automated publishing workflow.

After publication, verify the tag is annotated and resolves to the intended
`main` commit. Check the GitHub Release, notes, source archive contents,
changelog links and canonical URLs. If publication is interrupted, inspect the
existing tag and release before retrying. Never force-move a published tag.

Check installation from the published tag without a manual checkout:

```sh
uvx --from "git+https://github.com/alsd4git/immich-motion-photo-cleanup.git@${RELEASE_VERSION:?Set the published release tag}" \
  immich-motion-photo-cleanup --version
```

The reported version must match the tag without its leading `v`. Keep the exact
runtime revision used for each saved plan or execution journal, as described in
[recovery across versions](docs/operations.md#recovery-across-versions). A newer
release does not migrate those files or make their source digests compatible.
