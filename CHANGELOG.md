# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Read-only audits of embedded Motion Photos and standalone MP4/MOV candidates, with exact-byte and optional encoded-stream comparisons.
- Filesystem and Immich PostgreSQL inventory modes, owner scoping, and resumable batched reports.
- Conservative duplicate and companion-link proposals that require human review.
- Opt-in, ownership-scoped trash planning using full standalone-file byte-containment proofs, with revalidation before API writes.
- Journaled, recoverable trash actions, narrowly scoped restores, and private backup of sidecars when explicitly enabled.
- Synthetic-media tests and safety checks for ambiguous links, metadata, path aliases and interrupted writes.

No GitHub release or version tag has been published yet. When the first version is tagged, move the relevant entries into its versioned section, record its date, and add comparison links.
