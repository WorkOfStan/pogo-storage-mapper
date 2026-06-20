# Decisions

Accepted architectural, product, and process decisions for this repository.

## Offline Runtime

Normal scanner runtime stays offline-only. Network access must not be introduced
into frame scanning, evidence extraction, matching, or export.

Metadata refresh is a developer-only sync command. Normal runtime consumes the
packaged local catalog and must continue without network access.

## Evidence Before Export

The restart path proves frame extraction, classification, feature gates, and
manual audit artifacts before relying on Pokemon row export. `scan-frames`
remains the audit source of truth even after production export is available.

## Feature Gates Before Extraction

Extraction should consume class-specific feature gates rather than running every
parser on every frame. Detail, appraisal, and list-layout evidence remain
separate.

## Source-Local Matching First

Matching should start source-local and vicinity-based. Do not build global
cross-source guesses, and never merge ambiguous clusters.

## List Row Segmentation Deferred

List row segmentation is deferred because current list fragments do not provide
the core `hp + weight` identity key or important export fields. Existing list
classification and weak list evidence remain available for audit and future
fallback use, but they should not block matching or export restoration.

## Exhaustive Audit, Optimized Production

The `scan-frames` command is the exhaustive audit path and should continue
evaluating every frame. Production export may use a separate optimized path that
groups source-local Pokemon sequences, processes each sequence backward, skips
OCR for already accepted fields, and stops only when the export record is
complete and unambiguous. Production output should default to final export files
plus minimal manifest, timing, and warnings rather than full frame audit
artifacts.

## XLSX Writer Dependency

Production export uses `openpyxl` for XLSX generation instead of maintaining a
custom spreadsheet ZIP/XML writer. CSV remains the simple text export, while
XLSX uses the dedicated dependency for workbook compatibility and maintainable
code.

## Meta-Free Inventory Classification

Inventory classification stays offline and intentionally avoids current meta
rankings. `classify` ranks owned IV candidates within local evolution families
for duplicate reduction only; PvP and attacker flags do not claim that a species
is globally relevant.

Classifier runtime consumes packaged metadata only. PokeMiners Game Master
access remains limited to the developer-only `sync-metadata` refresh path, and
legacy/Elite move warnings stay in curated local data because Game Master data
is not a complete historical legacy-move source.
