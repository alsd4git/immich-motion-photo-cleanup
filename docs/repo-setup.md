# Repository workflow and GitHub setup

This file records verified settings and contribution policy for `alsd4git/immich-motion-photo-cleanup`. Editing it does not configure GitHub. Recheck the API before assuming the settings are unchanged. The day-to-day agent rules are in [AGENTS.md](../AGENTS.md).

## Repository identity

The canonical repository is [alsd4git/immich-motion-photo-cleanup](https://github.com/alsd4git/immich-motion-photo-cleanup). The rename preserved the repository identity, issues, PR history and default branch. GitHub redirects the former repository URL.

Existing clones can update only their own `origin` remote. Check its current URL with `git remote get-url origin` and keep the existing transport and authentication setup.

For an HTTPS clone, including one created with the README quickstart:

```sh
git remote set-url origin https://github.com/alsd4git/immich-motion-photo-cleanup.git
```

For an SSH clone with working GitHub SSH authentication:

```sh
git remote set-url origin git@github.com:alsd4git/immich-motion-photo-cleanup.git
```

Local checkout directory names need not change. Do not rename private report directories or frozen recovery copies as part of repository branding.

## Observed settings (2026-10-24)

- Integration branch: `main`.
- Squash merge: enabled. Merge commits and rebase merging: disabled.
- Automatic deletion of merged PR branches: enabled.
- Two active repository rulesets target the default branch. `main` has effective rules even though the legacy branch-protection endpoint has no separate protection object.
- `main-integrity` blocks deletion, force pushes and merge commits. It has no bypass actors, including the maintainer.
- `main-reviewed-integration` requires PRs, resolved review conversations and an up-to-date base with these passing checks from the GitHub Actions app:
  - `Ruff and syntax`
  - `Synthetic media (Python 3.10)`
  - `Synthetic media (Python 3.14)`
- No generic human approval count, code-owner approval or last-push approval is required. The workflow still waits for the first configured automatic review and addresses its findings.
- Only the repository owner's account, `alsd4git`, can bypass the integration ruleset for explicitly authorized maintenance. This does not bypass `main-integrity`.
- Work branches do not match either ruleset, so approved rebases and deletion of verified merged work remain possible.

## Merge policy

- Use **squash merging only**. Disable merge commits and rebase merging via repository settings.
- Enable automatic deletion of **merged** PR head branches after integration; never delete unmerged branches without authorization.
- Keep `main` linear, deny force-pushes and deletion. Do not apply force-push/deletion restrictions to disposable issue branches if maintainers need explicitly approved rebases.
- Keep a branch per issue; document intentionally stacked branches and merge the base first. After each merge, rebase the remaining branches onto the integration branch and rerun affected tests.
- Permit exceptional fast-forward direct pushes to `main` only when the maintainer explicitly authorizes them.

## Protection and maintainer maintenance

The owner's integration bypass permits an exceptional fast-forward direct push, but only with explicit maintainer authorization. Routine work still uses PRs and waits for checks and the first automatic review. Never use the bypass merely to merge a failed or unreviewed change.

The separate integrity ruleset has no bypass list. An integration bypass cannot authorize a force push, deletion or merge commit on `main`. Rule administration remains available to the repository owner; modifying rules is a separate authorized operation, not a validation technique.

To verify settings without mutating branches, inspect the repository's merge flags, list its rulesets, retrieve each ruleset's conditions and bypass actors, then request the effective rules for `main`. Check names are bound to the observed GitHub Actions app. No destructive branch push or deletion is needed to validate enforcement. See [GitHub's ruleset API](https://docs.github.com/en/rest/repos/rules) for those read-only endpoints.

## CI and privacy

- Required checks should come from `.github/workflows/python-tests.yml`, which is integrated into `main`. The project uses synthetic media and mocked API/database operations and should never require production credentials.
- Prefer PR-triggered validation with an optional manual dispatch, avoiding redundant matrix runs on every non-default branch push.
- Inventories, execution plans, asset paths, reports and journals contain private information and must remain outside the Git checkout. API keys must never be tracked or used as public CI artifacts; prefer storing them outside the checkout, or follow the [private workspace credential setup](operations.md#api-key-setup).

## Releases

- Follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html), using `vMAJOR.MINOR.PATCH` tags.
- Update `CHANGELOG.md` from `[Unreleased]` as part of a release. Only tag commits already integrated into `main`, after verification. Tagging, creating GitHub Releases and publishing packages require explicit authorization.
