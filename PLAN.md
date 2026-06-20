# Roadmap

This document tracks planned work only. Current scanner behavior is documented in
[README.md](README.md) for users and [docs/pipeline.md](docs/pipeline.md) for
maintainers.

## Next Priorities

- How exactly and where in the code is the frame classification (list/detail/appraisal/non_extractable) done?

- Fix detail vs appraisal frame expected features:
  - Catch story can appear only on the appraisal frame, never on a detail frame!
  - Don't look for height on appraisal frame, it's always obscured.
  - Moves can only be on detail frame, never on the appraisal frame

- How and where in the code exactly is done identification of has\_\* fields?
  - by using the optional `save_as` argument in `_crop()` as in `moves_region = _crop(image, REGIONS["moves"], "moves")`, show where and how is the code looking for these features. When the feature is found, is it also stored along the feature, so that OCR doesn't have to find it again?

## Later Priorities

- Harden production export with committed media fixtures and real OCR examples.
- Expand diagnostics for rejected export sequences so manual review can quickly
  separate OCR misses from truly ambiguous Pokemon.
- Add deferred visual detectors only when they provide concrete export value.

## Production Export Hardening

- Add fixture-driven export acceptance tests for real detail/appraisal media,
  including accepted rows and rejected conflicts.
- Expand optional export OCR only when a concrete output field or diagnostic
  needs it.
- Improve `warnings.jsonl` with compact evidence summaries for missing core
  identity, conflicting strong evidence, and ambiguous same-key sequences.
- Keep list fragments out of identity matching unless a future export gap proves
  their weak CP/display-name evidence is needed.

## Export Performance

- Compare export timing against `scan-frames` on larger videos and identify the
  next bottleneck.
- Avoiding frame extraction or streaming videos backward can wait until timing
  data shows it is worthwhile.
- Keep normal runtime offline-only.

## Deferred Detectors

- Add detectors for gender, shiny, lucky, purified, favorite,
  costume/form visual evidence, mega/primal sections, and scroll position.
- Decide which deferred signals are extraction gates and which are audit-only
  evidence before they feed matching or export.
- Add classifier transfer-protection flags after the export can reliably provide
  them: shiny, lucky, shadow, legendary, mythical, costume, favorite, and tags
  containing `keep` or `never_transfer` should prevent automatic `LET-GO`.

## Deferred List Segmentation

- Keep existing `list` frame classification and weak list evidence for audit and
  future fallback use.
- Revisit list row segmentation only if matching/export work shows a concrete
  gap it can fill; it is not a blocker for unique Pokemon export.

## Test Expansion

- Add more source-local export fixtures for accepted merges, rejected ambiguous
  clusters, and conflict diagnostics.
- Add fixture coverage for optional columns such as height, Max Move, shadow,
  Dynamax, Gigantamax, and tag-chip evidence.
