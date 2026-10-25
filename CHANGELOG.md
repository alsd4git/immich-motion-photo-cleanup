# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-25

### Added

- Installable package with one CLI for audit, plan, trash, restore and local requirement checks.
- Git-tag installation through uv/uvx without a manual checkout, with SQL resources bundled in wheel and sdist.
- Distribution tests exercising installed commands and private-output guards outside the source tree.
- Guidance on server-local access, future API-key-only evaluation and the known Immich album-cover fallback issue.

### Changed

- Move runtime modules to `src/immich_motion_photo_cleanup/` and offline tests to `tests/`.
- Use a shared, versioned fingerprint covering all packaged runtime modules and queries. Existing pre-package runs and journals require their exact frozen source for recovery; their headers must not be migrated by hand.

### Fixed

- Reject private outputs inside a checkout even when the CLI is installed elsewhere.

## [0.1.0] - 2026-10-24

### Added

- Read-only audits of embedded Motion Photos and standalone MP4/MOV candidates, with exact-byte and optional encoded-stream comparisons.
- Filesystem and Immich PostgreSQL inventory modes, owner scoping, and resumable batched reports.
- Conservative duplicate and companion-link proposals that require human review.
- Opt-in, ownership-scoped trash planning using full standalone-file byte-containment proofs, with revalidation before API writes.
- Journaled, recoverable trash actions, narrowly scoped restores, and private backup of sidecars when explicitly enabled.
- Synthetic-media tests and safety checks for ambiguous links, metadata, path aliases and interrupted writes.
- Human-readable plan and preview summaries, with opt-in JSON output for automation.
- Locked development tooling and CI checks for Ruff, syntax, and synthetic-media tests on Python 3.10 and 3.14.
- Repository contribution, privacy and integration guidance.

### Fixed

- Reject private report and temporary-output paths inside the source checkout or media libraries.
- Require completed audits with valid recorded configuration before creating trash plans.
- Carry the audited clip-size limit through schema-2 plans and execution while preserving historical schema-1 behavior.

[Unreleased]: https://github.com/alsd4git/immich-motion-photo-cleanup/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/alsd4git/immich-motion-photo-cleanup/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/alsd4git/immich-motion-photo-cleanup/releases/tag/v0.1.0
