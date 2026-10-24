# Repository workflow and GitHub setup

This file records desired settings for `alsd4git/motion-photo-audit`. **It does not configure GitHub** and must not be treated as proof that protections are active. The day-to-day agent rules are tracked in [issue #6](https://github.com/alsd4git/motion-photo-audit/issues/6).

## Observed settings (2026-10-24)

- Integration branch: `main`.
- Squash merge: enabled.
- Merge commits and rebase merging: **also enabled**, unlike the preferred configuration in comparable personal repositories.
- No repository rulesets were returned. The separate branch-protection endpoint reports that `main` is not protected, so required PR/check enforcement is not currently configured there.
- Automatic deletion of merged PR branches: disabled.

## Desired merge settings

- Use **squash merging only**. Disable merge commits and rebase merging via repository settings.
- Enable automatic deletion of **merged** PR head branches after integration; never delete unmerged branches without authorization.
- Keep `main` linear, deny force-pushes and deletion. Do not apply force-push/deletion restrictions to disposable issue branches if maintainers need explicitly approved rebases.
- Keep a branch per issue; document intentionally stacked branches and merge the base first. After each merge, rebase the remaining branches onto the integration branch and rerun affected tests.
- Permit exceptional fast-forward direct pushes to `main` only when the maintainer explicitly authorizes them.

## Protection when supported

- Require PRs, resolved review conversations, and **actually observed** passing PR quality checks for ordinary contributors.
- Do not require a generic human reviewer count for a single-maintainer repository. Follow the first configured automated review, without requesting duplicate reviews.
- When checks are stable, require the branch to be up to date before merging. Check the actual workflow job names before setting required-check rules: workflow titles are not always status-check identifiers.
- Administrators may be allowed to bypass PR requirements for explicitly approved maintenance, but linear-history/no-force-push/no-delete rules should still protect `main` independently.
- Only claim protections exist after verifying the applicable ruleset/branch-protection configuration and effective bypass list.

## CI and privacy

- Required checks should come from `.github/workflows/python-tests.yml` **once merged into `main`**. The project uses synthetic media and mocked API/database operations and should never require production credentials.
- Prefer PR-triggered validation with an optional manual dispatch, avoiding redundant matrix runs on every non-default branch push.
- Inventories, execution plans, asset paths, reports, journals and API keys contain private information. They must remain outside the Git checkout and should never be used as public CI artifacts.

## Releases

- Follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html), using `vMAJOR.MINOR.PATCH` tags.
- Update `CHANGELOG.md` from `[Unreleased]` as part of a release. Only tag commits already integrated into `main`, after verification. Tagging, creating GitHub Releases and publishing packages require explicit authorization.
