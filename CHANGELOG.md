# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-20

feat: release proof-of-concept for offline Pokemon GO inventory extraction

### Added

- Offline CLI commands for checking the environment, scanning frames, exporting
  Pokemon rows, validating exports, and classifying exported inventory files.
- Screenshot and MP4 input support for Pokemon GO storage recordings, with
  frame classification for inventory list, detail, appraisal, and
  non-extractable frames.
- Evidence-first scan artifacts, including frame JSONL, fragment JSONL,
  audit HTML, manifests, timing profiles, warnings, row diagnostics, and
  optional visible crop overlays.
- Production export to `pokemon.csv` and `pokemon.xlsx`, using source-local
  evidence from detail and appraisal screens.
- Offline species and move metadata catalog used to resolve visible Pokemon
  names, moves, evolutions, stats, and CP-related data without runtime network
  access.
- Export classification workflow that produces `KEEP`, `REVIEW`, and `LET-GO`
  recommendations plus CSV/XLSX workbooks with summary, PvP, attacker, and
  legacy-review sheets.

### Changed

- Detail/appraisal matching is centered on conservative source-local evidence,
  especially `HP + weight`, with CP treated as optional when OCR is noisy or
  conflicting.
- Export processing uses visual screening, backward sequence scanning,
  selective OCR, repair passes for near-miss sequences, and bounded temporary
  MP4 frame extraction when requested.
- OCR and visual evidence handling now rejects unstable transitions, incomplete
  move sections, dubious CP values, ambiguous IV evidence, and conflicting
  partial rows instead of exporting low-confidence data.
- Current usage, pipeline behavior, artifact contents, and document ownership
  are summarized in `README.md`; maintainer-level extraction and reconciliation
  details live in `docs/pipeline.md`.

### Fixed

- Improved recovery for common OCR and frame-state problems around CP, HP,
  weight, height, move sections, IV bars, appraisal stars, tag chips, Dynamax
  and Gigantamax evidence, and same-source duplicate or partial records.
- Stabilized scan/export behavior for committed regression fixtures covering
  list screens, detail screens, appraisal screens, scrolled physical stats, and
  bounded MP4 exports.
- Cleaned packaging, linting, typing, workbook generation, and CI/test
  compatibility issues needed for the prototype release.

### Known limitations

- This is an initial proof-of-concept release, not a polished daily inventory
  cleanup tool.
- Extraction is slow, especially when reading full details and moves from video.
- Recognition is calibrated around the currently documented Pixel 9 screenshot
  and MP4 dimensions; other devices or resolutions may need additional tuning.
- OCR remains imperfect. Rows are intentionally conservative and may be skipped
  or left partially blank when evidence is ambiguous.
- Move extraction requires the recording to scroll far enough past the move
  section for the tool to confirm the visible move block is complete.
- Classification does not use live Pokemon GO meta rankings and still requires
  manual review before transferring Pokemon.

[Unreleased]: https://github.com/WorkOfStan/pogo-storage-mapper/compare/v0.1.0...HEAD?w=1
[0.1.0]: https://github.com/WorkOfStan/pogo-storage-mapper/releases/tag/v0.1.0
